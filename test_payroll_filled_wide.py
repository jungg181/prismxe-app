#!/usr/bin/env python3
"""Entered values in red, grey "e.g." placeholders gone — payroll-wide:
single Add, Bulk Add, Daily Add, Monthly Add, and the company-page
Days list. Run: venv/bin/python test_payroll_filled_wide.py"""
import json
import os
import re
import shutil
import sys

STAGING = "/home/hatch/workspace/prismxe-app-staging"
sys.path.insert(0, STAGING)

DATA_P = os.path.join(STAGING, "data_payroll.json")
BAK_P = os.path.join(STAGING, "data_payroll.json.bak-filled-wide")


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
              "7:00", "15:30", "LGES", ""),
        entry("e2", "Ana Test", "2026-11-03", 8.0,
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

    # ---- 1. Single Add form (edit mode) ----
    t = c.get(base + "&tab=single&edit=e1").get_data(as_text=True)
    check("single form renders", "Add Entry" in t or "Save Changes" in t)
    check("single form has no e.g. placeholders",
          "e.g. 7:00 AM" not in t and "e.g. 7" not in t
          and "e.g. LGES" not in t and "e.g. KB" not in t)
    check("single form prefilled clock_in is filled",
          re.search(r'id="clock_in"[^>]*class="filled"', t) is not None)
    check("single form prefilled hours is filled",
          re.search(r'id="payroll_hours"[^>]*class="filled"', t)
          is not None)
    check("single form red CSS covers form-grid",
          ".form-grid input.filled" in t)

    # ---- 2. Bulk Add: no e.g. placeholders ----
    t = c.get(base + "&tab=bulk").get_data(as_text=True)
    check("bulk form has no e.g. placeholders",
          "e.g. 7:00 AM" not in t and "e.g. 7" not in t
          and "e.g. LGES" not in t and "e.g. KB" not in t)

    # ---- 3. Daily Add: no e.g. placeholders, prefilled is filled ----
    t = c.get(base + "&tab=daily&daily_date=2026-11-02").get_data(as_text=True)
    check("daily form has no e.g. placeholders",
          "e.g. 7:00 AM" not in t and "e.g. 7" not in t
          and "e.g. LGES" not in t and "e.g. KB" not in t)
    check("daily prefilled clock_in is filled",
          re.search(r'name="rows\[\d+\]\[clock_in\]"[^>]*class="filled"',
                    t) is not None)
    check("daily prefilled hours is filled",
          re.search(r'name="rows\[\d+\]\[payroll_hours\]"[^>]*'
                    r'class="filled"', t) is not None)

    # ---- 4. Monthly Add: still no e.g. placeholders, filled ----
    t = c.get(base + "&tab=monthly&monthly_employee=Ana+Test"
              "&monthly_month=2026-11").get_data(as_text=True)
    mon = t.split('<h2 style="margin-top:0">Monthly Add</h2>', 1)[1].split(
        "Entries (", 1)[0]
    check("monthly form still has no e.g. placeholders",
          "placeholder=" not in mon)
    check("monthly prefilled hours is filled",
          re.search(r'name="days\[\d+\]\[payroll_hours\]"[^>]*'
                    r'class="filled"', mon) is not None)

    # ---- 5. JS toggles filled on every entry form ----
    check("input listener covers entry tables + single form",
          "closest('.entry-input-table')" in t
          and "closest('.form-grid')" in t)
    check("auto-calc marks hours filled on every form",
          "hours.classList.toggle('filled'" in t)

    # ---- 6. Company page Days list ----
    t = c.get("/payroll/testco?from=2026-11-01&to=2026-11-30"
              ).get_data(as_text=True)
    check("company page renders", "TestCo Payroll" in t)
    check("days list has no e.g. placeholders",
          "e.g. 7:00 AM" not in t and "e.g. 3:30 PM" not in t)
    check("days list filled clock_in is red",
          re.search(r'name="clock_in"[^>]*class="filled"', t) is not None)
    check("days list filled hours is red",
          re.search(r'name="payroll_hours"[^>]*class="filled"', t)
          is not None)
    check("days list red CSS rule present",
          'tr[id^="days-"] input.filled' in t
          and "#ff5c5c" in t)
    check("days list JS toggles filled on input",
          "classList.toggle('filled', input.value !== '')" in t)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)
finally:
    shutil.copy(BAK_P, DATA_P)
    os.remove(BAK_P)
