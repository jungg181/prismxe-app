#!/usr/bin/env python3
"""Monthly Add entered values: prefilled inputs show in red, and the
grey "e.g." placeholders are gone, so entered days stand out from
empty rows at a glance. Run:
venv/bin/python test_payroll_monthly_filled.py"""
import json
import os
import re
import shutil
import sys

STAGING = "/home/hatch/workspace/prismxe-app-staging"
sys.path.insert(0, STAGING)

DATA_P = os.path.join(STAGING, "data_payroll.json")
BAK_P = os.path.join(STAGING, "data_payroll.json.bak-monthly-filled")


def entry(eid, employee, date, hours, cin="", cout="", site="",
          notes="", meal=False):
    return {"id": eid, "company": "TestCo", "employee": employee,
            "date": date, "clock_in": cin, "clock_out": cout,
            "clock_out_date": None, "gross_hours": hours,
            "payroll_hours": hours, "site": site, "notes": notes,
            "meal_break": meal}


seed = {
    "companies": [
        {"name": "TestCo", "slug": "testco", "prefix": "TC",
         "vendor": "Test", "ship_to": "", "category": ""},
    ],
    "rates": [
        {"name": "Ana Test", "company": "TestCo", "staffing": 20,
         "billing": 30},
    ],
    "entries": [
        entry("e1", "Ana Test", "2026-11-02", 9.5,
              "7:00 AM", "3:30 PM", "LGES", ""),
        entry("e2", "Ana Test", "2026-11-03", 0.0,
              "", "", "", ""),
    ],
    "expenses": [], "settings": {}, "aliases": [], "variances": {},
    "jobsites": [], "jobsite_costs": [], "invoices": [],
    "payroll_review_tokens": [], "payroll_corrections": [],
}

shutil.copy(DATA_P, BAK_P)
try:
    with open(DATA_P, "w") as f:
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
        print(("PASS " if cond else "FAIL ") + name + (
            (" | " + str(extra)) if extra and not cond else ""))

    base = "/payroll/testco/entries?from=2026-11-01&to=2026-11-30"
    t = c.get(base + "&tab=monthly&monthly_employee=Ana+Test"
              "&monthly_month=2026-11").get_data(as_text=True)
    mon = t.split('<h2 style="margin-top:0">Monthly Add</h2>', 1)[1].split(
        "Entries (", 1)[0]

    # ---- 1. No grey "e.g." placeholders in the Monthly Add rows ----
    check("no e.g. placeholders in monthly rows",
          "placeholder=" not in mon, "placeholder found")
    check("no e.g. 7:00 AM / e.g. 7 text in monthly rows",
          "e.g. 7:00 AM" not in mon and "e.g. 7" not in mon)

    # ---- 2. Prefilled inputs carry the filled class (red) ----
    # 11/02: clock_in 7:00 AM, clock_out 3:30 PM, hours 9.5, site LGES.
    check("prefilled clock_in is filled",
          re.search(r'name="days\[\d+\]\[clock_in\]"[^>]*class="filled"',
                    mon) is not None)
    check("prefilled clock_out is filled",
          re.search(r'name="days\[\d+\]\[clock_out\]"[^>]*class="filled"',
                    mon) is not None)
    check("prefilled payroll_hours is filled",
          re.search(r'name="days\[\d+\]\[payroll_hours\]"[^>]*'
                    r'class="filled"', mon) is not None)
    check("prefilled site is filled",
          re.search(r'name="days\[\d+\]\[site\]"[^>]*class="filled"',
                    mon) is not None)
    # 11/03 has 0.0h — a real entered day, still filled.
    check("0.0h entry still counts as filled",
          mon.count('class="filled"') >= 5, mon.count('class="filled"'))

    # ---- 3. Empty inputs have no filled class ----
    # 11/04 has no entry: its inputs must not be filled.
    m = re.search(r'value="2026-11-04".*?</tr>', mon, re.S)
    check("empty day row has no filled class",
          m is not None and 'class="filled"' not in m.group(0))

    # ---- 4. The red CSS rule exists ----
    check("filled inputs are red",
          ".entry-input-table input.filled" in t
          and "#ff5c5c" in t)

    # ---- 5. JS toggles filled as the user types / auto-calcs ----
    # (Now payroll-wide: the listener covers every entry form, not
    # just Monthly Add — see test_payroll_filled_wide.py.)
    check("input listener toggles filled on entry forms",
          "closest('.entry-input-table')" in t
          and "closest('.form-grid')" in t
          and "classList.toggle('filled'" in t)
    check("auto-calc marks hours filled on monthly form",
          "hours.classList.toggle('filled'" in t)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)
finally:
    shutil.copy(BAK_P, DATA_P)
    os.remove(BAK_P)
