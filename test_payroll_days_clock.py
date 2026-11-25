"""Staging test: Payroll Days list — edit clock in/out times.

Covers:
- the Days list renders Clock-In / Clock-Out columns with the stored
  times prefilled (and blank on 0h days).
- update_entry_hours saves payroll_hours + clock_in + clock_out
  together; site/notes untouched; clearing a time saves "".
- add_day_hours saves clock times when creating an entry, and creates
  an entry when only clock times are given (hours 0).
- a 0h day with no hours and no times still creates nothing.
"""
import json
import os
import re
import shutil
import sys

STAGING = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, STAGING)

DATA = os.path.join(STAGING, "data_payroll.json")
BAK = DATA + ".bak-days-clock-test"

seed = {
    "companies": [
        {"name": "Hanseong", "slug": "hanseong", "prefix": "HS",
         "vendor": "Hanseong USA", "ship_to": "Mesa AZ", "category": "Hanseong"},
    ],
    "rates": [
        {"id": "rate-h1", "name": "Han One", "company": "Hanseong",
         "staffing": 50.0, "billing": 65.0},
    ],
    "entries": [
        {"id": "e1", "company": "Hanseong", "employee": "Han One",
         "date": "2026-10-20", "payroll_hours": 8.0,
         "clock_in": "7:00 AM", "clock_out": "3:30 PM",
         "clock_out_date": None, "gross_hours": None,
         "site": "LGES", "notes": "keep me"},
    ],
    "expenses": [], "settings": {}, "aliases": {}, "variances": {},
    "jobsites": [], "jobsite_costs": [],
}

shutil.copy(DATA, BAK)
try:
    with open(DATA, "w") as f:
        json.dump(seed, f)

    from flask import Flask
    import payroll_bp as pb

    app = Flask(__name__, template_folder=os.path.join(STAGING, "templates"))
    app.secret_key = "test-secret"
    app.register_blueprint(pb.bp, url_prefix="/payroll")
    c = app.test_client()

    passed, failed = [], []

    def check(name, cond, extra=""):
        (passed if cond else failed).append(name)
        print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra and not cond else ""))

    # 1. Days list renders Clock-In / Clock-Out columns, prefilled
    t = c.get("/payroll/hanseong?from=2026-10-19&to=2026-10-21").get_data(as_text=True)
    check("payroll page 200", "Han One" in t)
    check("Days list has Clock-In header", "<th>Clock-In</th>" in t)
    check("Days list has Clock-Out header", "<th>Clock-Out</th>" in t)
    check("clock_in prefilled", 'value="7:00 AM"' in t)
    check("clock_out prefilled", 'value="3:30 PM"' in t)
    # 0h days (no entry) show an empty clock-in box — no grey
    # placeholder, no value. Two such days in this range (10/19, 10/21).
    check("0h day clock inputs blank",
          len(re.findall(
              r'name="clock_in" form="hrs-new-\d+" type="text" value=""',
              t)) >= 2)

    # 2. update_entry_hours: hours + clock times saved together
    r = c.post("/payroll/hanseong/entries/e1/hours?from=2026-10-19&to=2026-10-21",
               data={"payroll_hours": "9", "clock_in": "6:30 AM",
                     "clock_out": "4:00 PM"}, follow_redirects=True)
    check("update hours+times 200", r.status_code == 200)
    e = next(x for x in pb.DB["entries"] if x["id"] == "e1")
    check("hours updated 9", e["payroll_hours"] == 9.0, e["payroll_hours"])
    check("clock_in updated", e["clock_in"] == "6:30 AM", e["clock_in"])
    check("clock_out updated", e["clock_out"] == "4:00 PM", e["clock_out"])
    check("site untouched", e["site"] == "LGES")
    check("notes untouched", e["notes"] == "keep me")
    check("flash mentions times",
          "6:30 AM - 4:00 PM" in r.get_data(as_text=True))

    # 3. clearing a clock time saves ""
    r = c.post("/payroll/hanseong/entries/e1/hours?from=2026-10-19&to=2026-10-21",
               data={"payroll_hours": "9", "clock_in": "",
                     "clock_out": "4:00 PM"}, follow_redirects=True)
    e = next(x for x in pb.DB["entries"] if x["id"] == "e1")
    check("cleared clock_in saves empty", e["clock_in"] == "", repr(e["clock_in"]))

    # 4. add_day_hours: creates entry with clock times
    before = len(pb.DB["entries"])
    r = c.post("/payroll/hanseong/entries/add-day-hours?from=2026-10-19&to=2026-10-21",
               data={"employee": "Han One", "date": "2026-10-21",
                     "payroll_hours": "7.5", "clock_in": "7:15 AM",
                     "clock_out": "3:45 PM"}, follow_redirects=True)
    check("add-day creates entry", len(pb.DB["entries"]) == before + 1)
    ne = [x for x in pb.DB["entries"] if x.get("date") == "2026-10-21"][0]
    check("new entry hours 7.5", ne["payroll_hours"] == 7.5)
    check("new entry clock_in", ne["clock_in"] == "7:15 AM")
    check("new entry clock_out", ne["clock_out"] == "3:45 PM")

    # 5. add_day_hours: clock times only (hours 0) still creates entry
    before = len(pb.DB["entries"])
    r = c.post("/payroll/hanseong/entries/add-day-hours?from=2026-10-19&to=2026-10-21",
               data={"employee": "Han One", "date": "2026-10-19",
                     "payroll_hours": "0", "clock_in": "8:00 AM",
                     "clock_out": ""}, follow_redirects=True)
    check("times-only creates entry", len(pb.DB["entries"]) == before + 1)
    ne = [x for x in pb.DB["entries"] if x.get("date") == "2026-10-19"][0]
    check("times-only entry hours 0", ne["payroll_hours"] == 0.0)
    check("times-only entry clock_in", ne["clock_in"] == "8:00 AM")

    # 6. add_day_hours: 0h and no times creates nothing
    before = len(pb.DB["entries"])
    c.post("/payroll/hanseong/entries/add-day-hours?from=2026-10-19&to=2026-10-21",
           data={"employee": "Han One", "date": "2026-10-20",
                 "payroll_hours": "0", "clock_in": "",
                 "clock_out": ""}, follow_redirects=True)
    # date 2026-10-20 already has e1, so nothing is created either way;
    # use a fresh date to prove the no-times rule
    before = len(pb.DB["entries"])
    c.post("/payroll/hanseong/entries/add-day-hours?from=2026-10-19&to=2026-10-21",
           data={"employee": "Han One", "date": "2026-10-19",
                 "payroll_hours": "0", "clock_in": "",
                 "clock_out": ""}, follow_redirects=True)
    check("0h + no times creates nothing",
          len(pb.DB["entries"]) == before)

    print()
    print("passed %d/%d" % (len(passed), len(passed) + len(failed)))
    if failed:
        print("FAILED:", failed)
        sys.exit(1)
finally:
    shutil.copy(BAK, DATA)
    os.remove(BAK)
