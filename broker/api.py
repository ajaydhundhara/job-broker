"""FastAPI app and routes. Routes stay thin: validate, call service, map errors.

Run with:  uvicorn broker.api:app --reload
"""

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from broker import engine, service
from broker.config import Config
from broker.db import connect, init_db
from broker.models import (
    CostReport,
    Health,
    JobComplete,
    JobOut,
    JobSubmit,
    NodeOut,
    QueueEntry,
    SimAdvance,
    SimNow,
)
from broker.provider import FakeProvider
from retrieval import index as retrieval_index_module
from retrieval import search as retrieval_search
from retrieval.models import FailureRecord, FailureSearch, FailureSearchResponse

logger = logging.getLogger(__name__)


def create_app(
    db_path: str | Path | None = None,
    config: Config | None = None,
    provider: FakeProvider | None = None,
    retrieval_index=None,
) -> FastAPI:
    """Build an app bound to one database file.

    A factory (not just a module-level app) so tests can point each app at a
    temp file, and a restart test can build a second app on the same file.
    config defaults to Config.from_env(); provider to a FakeProvider seeded
    from it. Tests pass their own (dependency injection, SPEC #7 and #10).
    retrieval_index: a prebuilt retrieval.index.Index; by default it is
    built once at startup from FAILURE_LOGS_PATH (retrieval.index_from_env).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Resolve the path and create the schema at startup, not at import:
        # importing this module must not create ./broker.db as a side effect.
        app.state.db_path = db_path or os.environ.get("BROKER_DB", "./broker.db")
        app.state.config = config or Config.from_env()
        # The provider holds no state of its own: everything is in the DB.
        app.state.provider = provider or FakeProvider(app.state.config)
        conn = connect(app.state.db_path)
        try:
            init_db(conn)
            # Startup: fix any disagreement between our nodes and the provider's.
            engine.reconcile_now(conn, app.state.config, app.state.provider)
        finally:
            conn.close()
        # Built once, here: every search reuses the same matrices. None when
        # the default logs file is absent (then /failures answers 503).
        app.state.retrieval = retrieval_index or retrieval_index_module.index_from_env()
        yield

    app = FastAPI(title="Job Broker", lifespan=lifespan)

    def get_conn(request: Request) -> Iterator:
        # One connection per request, never shared between requests. WAL +
        # busy_timeout + BEGIN IMMEDIATE make concurrent connections safe.
        #
        # check_same_thread=False: FastAPI runs this dependency and the `def`
        # route in thread-pool threads that can differ (open here, use there,
        # close here). sqlite3's default check rejects that with an error and
        # the request fails with 500. Only this request ever touches this
        # connection, and one step at a time, so turning the check off is safe.
        conn = connect(request.app.state.db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    # Routes declare `conn: Conn` to get a per-request connection.
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    class SimContext:
        """The app's config and provider, injected into the routes that need them."""

        def __init__(self, request: Request):
            self.config: Config = request.app.state.config
            self.provider: FakeProvider = request.app.state.provider

        def run_pass(self, conn) -> None:
            # After the request's own transaction has committed: one engine
            # pass at the current time, so a job that fits starts now.
            #
            # A failure here must not become a 500. The request's change is
            # already committed, and a client told "500" would retry; without
            # an idempotency key that submits the job twice. The pass runs in
            # its own transaction, so a failure leaves nothing half-done, and
            # the next request or /sim/advance runs it again. Logged, so a
            # scheduler bug is still visible to the operator.
            try:
                engine.run_pass(conn, self.config, self.provider)
            except Exception:
                logger.exception("scheduling pass failed; deferred to the next pass")

    Sim = Annotated[SimContext, Depends(SimContext)]

    # Domain errors -> HTTP status codes, in one place.
    @app.exception_handler(service.NotFound)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(service.Conflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(service.InvalidInput)
    async def invalid_input(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's 422 body echoes the request input; for /failures that
        # input may be a log holding a secret. Keep only where and why.
        if not request.url.path.startswith("/failures"):
            return await request_validation_exception_handler(request, exc)
        detail = [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    def retrieval(request: Request):
        index = request.app.state.retrieval
        if index is None:
            raise HTTPException(503, "failure retrieval is not configured")
        return index

    @app.get("/health", response_model=Health)
    def health():
        return {"status": "ok"}

    @app.post("/jobs", response_model=JobOut, status_code=201)
    def submit_job(req: JobSubmit, response: Response, conn: Conn, sim: Sim):
        job, created = service.submit_job(conn, req)
        sim.run_pass(conn)
        if not created:
            response.status_code = 200  # repeated idempotent request: same job
        return service.get_job(conn, job["id"])  # as it is after the pass

    @app.get("/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: str, conn: Conn):
        return service.get_job(conn, job_id)

    @app.post("/jobs/{job_id}/cancel", response_model=JobOut)
    def cancel_job(job_id: str, conn: Conn, sim: Sim):
        job = service.cancel_job(conn, job_id)
        sim.run_pass(conn)  # freed GPUs may start a queued job
        return service.get_job(conn, job["id"])

    @app.post("/jobs/{job_id}/complete", response_model=JobOut)
    def complete_job(job_id: str, req: JobComplete, conn: Conn, sim: Sim):
        # Internal: used by tests and the simulator, not by teams.
        job = service.complete_job(conn, job_id, req.outcome)
        sim.run_pass(conn)
        return service.get_job(conn, job["id"])

    @app.get("/queue", response_model=list[QueueEntry])
    def list_queue(conn: Conn, sim: Sim):
        return service.list_queue(conn, sim.config.policy)

    @app.get("/sim/now", response_model=SimNow)
    def sim_now(conn: Conn):
        return service.sim_now(conn)

    @app.post("/sim/advance", response_model=SimNow)
    def sim_advance(req: SimAdvance, conn: Conn, sim: Sim):
        # Internal: only the simulator should move time.
        return service.sim_advance(conn, req, sim.config, sim.provider)

    @app.get("/nodes", response_model=list[NodeOut])
    def list_nodes(conn: Conn, include_terminated: bool = False):
        return service.list_nodes(conn, include_terminated)

    @app.get("/reports/cost", response_model=CostReport)
    def cost_report(conn: Conn, from_ts: int = 0, to_ts: int | None = None):
        # to_ts defaults to "now", which only the service can read.
        return service.cost_report(conn, from_ts, to_ts)

    @app.post("/failures/search", response_model=FailureSearchResponse)
    def search_failures(req: FailureSearch, request: Request):
        # Neither the text nor the results are logged: the text may hold a
        # secret until redact() has run on it.
        return retrieval_search.search(retrieval(request), req.text, req.k).response()

    @app.get("/failures/{failure_id}", response_model=FailureRecord)
    def get_failure(failure_id: str, request: Request):
        index = retrieval(request)
        if failure_id not in index.by_id:
            raise HTTPException(404, "unknown failure id")
        return retrieval_search.get_failure(index, failure_id)

    return app


app = create_app()
