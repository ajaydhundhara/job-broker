"""A runtime guard: any wall-clock read from broker/ code raises.

Why at runtime as well as the static scan (test_no_wall_clock.py): a scan
only sees the calls it knows how to spell. The guard sees what actually
runs during a request.

How:
  * time.time, time.monotonic, ... are replaced by wrappers that look at the
    CALLER's module. A caller in `broker.*` is recorded and gets an error;
    any other caller (FastAPI, anyio, httpx, sqlite3's Python side) gets the
    real clock, because servers legitimately use it for timeouts.
  * Every `datetime` / `date` class bound inside a loaded broker module is
    swapped for a subclass whose now() / utcnow() / today() raise.
    fromtimestamp(ts) still works: converting a stored simulated time is fine.

Limits (reported as NOT VERIFIED): wall-clock use inside third-party
libraries is allowed by design, and C code (e.g. SQLite's own 'now') can't
be intercepted here; the static scan covers SQL text instead.
"""

import datetime as _dt
import sys
import time
from contextlib import contextmanager

TIME_FUNCTIONS = ("time", "time_ns", "monotonic", "monotonic_ns",
                  "perf_counter", "perf_counter_ns", "localtime", "ctime")  # fmt: skip


class WallClockRead(AssertionError):
    """Application code read the real clock."""


def _is_app_frame(depth: int) -> bool:
    name = sys._getframe(depth).f_globals.get("__name__", "")
    return name == "broker" or name.startswith("broker.")


def _guarded(name, real, calls):
    def wrapper(*args, **kwargs):
        if _is_app_frame(2):
            calls.append(f"time.{name}")
            raise WallClockRead(f"broker code called time.{name}()")
        return real(*args, **kwargs)

    return wrapper


def _guarded_class(base, calls):
    def refuse(name):
        def method(cls, *args, **kwargs):
            calls.append(f"{base.__name__}.{name}")
            raise WallClockRead(f"broker code called {base.__name__}.{name}()")

        return classmethod(method)

    names = ("now", "utcnow", "today") if base is _dt.datetime else ("today",)
    return type(f"Guarded{base.__name__}", (base,), {n: refuse(n) for n in names})


@contextmanager
def no_wall_clock():
    """Yields the list of wall-clock reads attempted by broker code."""
    calls: list[str] = []
    saved = []
    for name in TIME_FUNCTIONS:
        real = getattr(time, name)
        saved.append((time, name, real))
        setattr(time, name, _guarded(name, real, calls))
    swaps = {_dt.datetime: _guarded_class(_dt.datetime, calls),
             _dt.date: _guarded_class(_dt.date, calls)}  # fmt: skip
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not (mod_name == "broker" or mod_name.startswith("broker.")):
            continue
        for attr, value in list(vars(mod).items()):
            if isinstance(value, type) and value in swaps:
                saved.append((mod, attr, value))
                setattr(mod, attr, swaps[value])
    try:
        yield calls
    finally:
        for owner, attr, value in reversed(saved):
            setattr(owner, attr, value)
