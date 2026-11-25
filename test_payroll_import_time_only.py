#!/usr/bin/env python3
"""Imported clock-in/out times are times only ("7:00", "19:00"), never
full datetimes — the date lives in the entry's `date` (and
`clock_out_date` for overnight shifts). Otherwise the Days list
shows "2026-11-..." in the Clock-In/Clock-Out boxes. Run:
venv/bin/python test_payroll_import_time_only.py"""
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from payroll_import_app import _time_only, patrol_rows, timecard_rows

passed, failed = [], []

def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (
        (" | " + str(extra)) if extra and not cond else ""))

# ---- 1. _time_only ----
check("ISO datetime -> H:MM",
      _time_only("2026-11-19T06:53:32") == "6:53",
      _time_only("2026-11-19T06:53:32"))
check("ISO midnight hour -> 7:00",
      _time_only("2026-11-21T07:00:00") == "7:00")
check("ISO evening -> 18:56",
      _time_only("2026-11-19T18:56:10") == "18:56")
check("ISO with timezone -> local Phoenix time",
      _time_only("2026-11-19T06:53:32+00:00") == "23:53",
      _time_only("2026-11-19T06:53:32+00:00"))
check("plain 24h time passes through",
      _time_only("19:00") == "19:00")
check("plain AM/PM time passes through",
      _time_only("7:00 AM") == "7:00 AM")
check("empty -> empty", _time_only("") == "")
check("None -> empty", _time_only(None) == "")

# ---- 2. timecard_rows (CM/COA) stores time-only ----
CM_DATA = {
    "workers": [{"id": "w1", "name": "David"}],
    "sites": [{"id": "s1", "name": "LGES"}],
    "timecards": [
        {"id": "t1", "worker_id": "w1", "site_id": "s1",
         "clock_in": "2026-11-04T07:00:00",
         "clock_out": "2026-11-04T18:00:00", "hours": 10.0,
         "status": "approved", "notes": ""},
    ],
}
rows, _ = timecard_rows(CM_DATA, date(2026, 11, 3), date(2026, 11, 5))
check("timecard_rows has 1 row", len(rows) == 1, len(rows))
check("timecard clock_in is time-only",
      rows[0]["clock_in"] == "7:00", rows[0]["clock_in"])
check("timecard clock_out is time-only",
      rows[0]["clock_out"] == "18:00", rows[0]["clock_out"])
check("timecard date + clock_out_date still kept",
      rows[0]["date"] == "2026-11-04"
      and rows[0]["clock_out_date"] == "2026-11-04")

# ---- 3. patrol_rows stores time-only ----
PATROL_DATA = {
    "workers": [{"id": "p1", "email": "a@x.us", "name": "Argenis"}],
    "punches": [
        {"id": "q1", "email": "a@x.us", "name": "Argenis",
         "action": "in", "ts": "2026-11-21T07:00:00", "source": "manual"},
        {"id": "q2", "email": "a@x.us", "name": "Argenis",
         "action": "out", "ts": "2026-11-21T18:51:57",
         "source": "manual"},
    ],
}
rows, _ = patrol_rows(PATROL_DATA, date(2026, 11, 21),
                      date(2026, 11, 21))
check("patrol_rows has 1 row", len(rows) == 1, len(rows))
check("patrol clock_in is time-only",
      rows[0]["clock_in"] == "7:00", rows[0]["clock_in"])
check("patrol clock_out is time-only",
      rows[0]["clock_out"] == "18:51", rows[0]["clock_out"])
check("patrol date + clock_out_date still kept",
      rows[0]["date"] == "2026-11-21"
      and rows[0]["clock_out_date"] == "2026-11-21")

print(f"\n{len(passed)} passed, {len(failed)} failed")
if failed:
    raise SystemExit(1)
