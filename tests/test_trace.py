"""Trace loading and data quality (broker/trace.py).

Every test writes its own tiny CSV; the real trace is data and is only read.
"""

import pytest

from broker import trace

HEADER = "submitted_at,team,gpus,estimated_minutes,actual_minutes,priority,outcome\n"
OK = "vision,1,30,10,normal,succeeded"  # every field after the timestamp


def load(tmp_path, *rows, **kwargs):
    path = tmp_path / "trace.csv"
    path.write_text(HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return trace.load_trace(path, **kwargs)


# --- timestamps --------------------------------------------------------------------


def test_an_offset_gives_the_same_epoch_as_utc():
    assert trace.parse_ts("2025-03-03T06:29:00+01:00") == trace.parse_ts(
        "2025-03-03T05:29:00Z"
    )
    assert trace.parse_ts("2025-03-03T05:29:00Z") == 1740979740


def test_a_timestamp_without_an_offset_is_rejected_with_a_reason(tmp_path):
    rows, report = load(tmp_path, f"2025-03-03T05:29:00,{OK}")
    assert rows == []
    [rejected] = report.rejected
    assert rejected.line == 2  # line 1 is the header
    assert "offset" in rejected.reason


def test_non_utc_offsets_are_converted_and_counted(tmp_path):
    rows, report = load(
        tmp_path,
        f"2025-03-03T06:29:00+01:00,{OK}",
        f"2025-03-03T01:00:00-07:00,{OK}",
        f"2025-03-03T09:00:00+00:00,{OK}",  # an explicit +00:00 is UTC
        f"2025-03-03T10:00:00Z,{OK}",
    )
    assert report.non_utc_offsets == 2
    assert [r.utc_ts for r in rows][:2] == [
        trace.parse_ts("2025-03-03T05:29:00Z"),
        trace.parse_ts("2025-03-03T08:00:00Z"),
    ]


# --- validation: rejected, never repaired ------------------------------------------


def test_gpus_3_and_16_are_rejected_and_the_others_accepted(tmp_path):
    rows, report = load(
        tmp_path,
        "2025-03-03T01:00:00Z,vision,1,30,10,normal,succeeded",
        "2025-03-03T01:01:00Z,vision,3,30,10,normal,succeeded",
        "2025-03-03T01:02:00Z,vision,8,30,10,normal,succeeded",
        "2025-03-03T01:03:00Z,vision,16,30,10,normal,succeeded",
    )
    assert [r.line for r in rows] == [2, 4]
    assert [(r.line, r.value) for r in report.rejected] == [(3, "3"), (5, "16")]
    assert all("gpus" in r.reason for r in report.rejected)
    assert (report.total_rows, report.accepted) == (4, 2)
    assert report.rejected_by_reason == {report.rejected[0].reason: [3, 5]}


@pytest.mark.parametrize(
    "fields, word",
    [
        (" ,1,30,10,normal,succeeded", "team"),
        ("vision,two,30,10,normal,succeeded", "gpus"),
        ("vision,1,0,10,normal,succeeded", "estimated_minutes"),
        ("vision,1,30,-1,normal,succeeded", "actual_minutes"),
        ("vision,1,30,10,urgent,succeeded", "priority"),
        ("vision,1,30,10,normal,timeout", "outcome"),
        ("vision,1,20000,10,normal,succeeded", "estimated_minutes"),  # API max 10080
        ("vision,1,30,10,normal", "missing"),
    ],
)
def test_each_invalid_field_is_rejected_with_its_name(tmp_path, fields, word):
    rows, report = load(tmp_path, f"2025-03-03T01:00:00Z,{fields}")
    assert rows == []
    [rejected] = report.rejected
    assert word in rejected.reason


def test_actual_minutes_zero_is_accepted(tmp_path):
    rows, _ = load(tmp_path, "2025-03-03T01:00:00Z,vision,1,30,0,normal,cancelled")
    assert rows[0].actual_minutes == 0


def test_a_wrong_header_is_an_error_not_a_rejected_row(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("when,who\n2025-03-03T01:00:00Z,vision\n", encoding="utf-8")
    with pytest.raises(ValueError, match="columns"):
        trace.load_trace(path)


# --- duplicates: kept in the rows, collapsed later by the API's idempotency -------


def test_identical_rows_share_a_key_and_are_both_kept(tmp_path):
    row = f"2025-03-03T01:00:00Z,{OK}"
    rows, report = load(tmp_path, row, row)
    assert len(rows) == 2  # not dropped here
    assert rows[0].idempotency_key == rows[1].idempotency_key
    assert report.duplicate_rows == 1
    assert report.duplicate_lines == [3]


def test_keep_duplicates_makes_the_keys_differ(tmp_path):
    row = f"2025-03-03T01:00:00Z,{OK}"
    rows, report = load(tmp_path, row, row, keep_duplicates=True)
    assert rows[0].idempotency_key != rows[1].idempotency_key
    assert report.duplicate_rows == 1  # still reported either way


def test_a_row_identical_only_after_utc_conversion_is_a_duplicate(tmp_path):
    rows, report = load(
        tmp_path, f"2025-03-03T05:29:00Z,{OK}", f"2025-03-03T06:29:00+01:00,{OK}"
    )
    assert rows[0].idempotency_key == rows[1].idempotency_key
    assert (report.duplicate_rows, report.duplicates_only_after_utc) == (1, 1)


def test_rows_differing_in_any_field_are_not_duplicates(tmp_path):
    rows, report = load(
        tmp_path,
        "2025-03-03T01:00:00Z,vision,1,30,10,normal,succeeded",
        "2025-03-03T01:00:00Z,vision,1,30,11,normal,succeeded",  # actual differs
        "2025-03-03T01:00:00Z,vision,1,30,10,normal,failed",  # outcome differs
    )
    assert len({r.idempotency_key for r in rows}) == 3
    assert report.duplicate_rows == 0


# --- order ---------------------------------------------------------------------------


def test_out_of_order_input_comes_out_sorted_with_the_count(tmp_path):
    rows, report = load(
        tmp_path,
        f"2025-03-03T02:00:00Z,{OK}",  # line 2
        f"2025-03-03T01:00:00Z,{OK}",  # line 3: earlier than line 2
        f"2025-03-03T03:00:00Z,{OK}",  # line 4
        f"2025-03-03T02:30:00Z,{OK}",  # line 5: earlier than line 4
    )
    assert [r.line for r in rows] == [3, 2, 5, 4]
    assert report.out_of_order == 2


def test_equal_timestamps_keep_file_order(tmp_path):
    rows, report = load(
        tmp_path,
        "2025-03-03T01:00:00Z,b,1,30,10,normal,succeeded",
        "2025-03-03T01:00:00Z,a,1,30,10,normal,succeeded",
    )
    assert [r.team for r in rows] == ["b", "a"]
    assert report.out_of_order == 0


# --- the rest of the report ---------------------------------------------------------


def test_team_burst_and_overrun_counts(tmp_path):
    rows, report = load(
        tmp_path,
        "2025-03-05T04:59:00Z,vision,1,30,10,normal,succeeded",  # before the burst
        "2025-03-05T05:00:00Z,vision,1,30,121,normal,succeeded",  # in; 121 > 4 x 30
        "2025-03-05T07:59:00Z,nlp,1,30,120,normal,succeeded",  # in; 120 = 4 x 30
        "2025-03-05T08:00:00Z,nlp,1,30,10,normal,succeeded",  # end is excluded
    )
    assert report.rows_per_team == {"nlp": 2, "vision": 2}
    assert report.burst_rows_per_team == {"nlp": 1, "vision": 1}
    assert report.overrun_lines == [3]
    assert report.time_range == (rows[0].utc_ts, rows[-1].utc_ts)


def test_the_burst_window_is_configurable(tmp_path):
    window = (
        trace.parse_ts("2025-03-05T00:00:00Z"),
        trace.parse_ts("2025-03-06T00:00:00Z"),
    )
    _, report = load(tmp_path, f"2025-03-05T04:59:00Z,{OK}", burst_window=window)
    assert report.burst_rows_per_team == {"vision": 1}


def test_the_markdown_report_lists_rejected_lines(tmp_path):
    _, report = load(
        tmp_path,
        "2025-03-03T01:00:00Z,vision,3,30,10,normal,succeeded",
        f"2025-03-03T01:01:00Z,{OK}",
    )
    md = report.to_markdown()
    assert md.startswith("# Data quality")
    assert "| 2 |" in md  # the rejected line number
    assert "accepted" in report.summary()
