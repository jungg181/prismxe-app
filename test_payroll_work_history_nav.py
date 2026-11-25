#!/usr/bin/env python3
"""Work History moved front and centre: top-nav link, a picker page
(/payroll/work-history), and a History link on every company-page
employee row. The per-employee page still works. Run:
venv/bin/python test_payroll_work_history_nav.py"""
import json
import os
import shutil
import sys

STAGING = "/home/hatch/workspace/prismxe-app-staging"
sys.path.insert(0, STAGING)

DATA_P = os.path.join(STAGING, "data_payroll.json")
BAK_P = os.path.join(STAGING, "data_payroll.json.bak-wh-nav")


def entry(eid, employee, company, date, hours):
    return {"id": eid, "company": company, "employee": employee,
            "date": date, "clock_in": "", "clock_out": "",
            "clock_out_date": None, "gross_hours": hours,
            "payroll_hours": hours, "site": "", "notes": "",
            "meal_break": False}


seed = {
    "companies": [
        {"name": "M-Solution", "slug": "m-solution", "prefix": "MS",
         "vendor": "M", "ship_to": "", "category": ""},
        {"name": "COA - Carpenter", "slug": "coa-carpenter",
         "prefix": "COA", "vendor": "C", "ship_to": "", "category": ""},
    ],
    "rates": [
        {"name": "Isaac Marino", "company": "M-Solution",
         "staffing": 25, "billing": 39},
        {"name": "Isaac Marino", "company": "COA - Carpenter",
         "staffing": 32, "billing": 39},
    ],
    "entries": [
        entry("w1", "Isaac Marino", "M-Solution", "2026-11-20", 8.0),
        entry("w2", "Isaac Marino", "COA - Carpenter", "2026-11-21", 9.5),
        entry("w3", "Isaac Marino", "COA - Carpenter", "2026-11-23", 8.0),
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

    # ---- 1. Top nav has Work History on every payroll page ----
    t = c.get("/payroll/").get_data(as_text=True)
    check("payroll index renders", "Payroll" in t)
    check("top nav has Work History link",
          'href="/payroll/work-history"' in t
          and ">Work History<" in t)
    t = c.get("/payroll/m-solution?from=2026-11-01&to=2026-11-30"
              ).get_data(as_text=True)
    check("top nav on company page too",
          'href="/payroll/work-history"' in t)

    # ---- 2. Picker page (no employee yet) ----
    t = c.get("/payroll/work-history").get_data(as_text=True)
    check("work-history page 200", "Work History" in t)
    check("picker lists employees",
          '<select id="employee" name="employee"' in t
          and "Isaac Marino" in t)
    check("picker prompts when none chosen",
          "Pick an employee above" in t)
    check("picker has period inputs",
          'name="from"' in t and 'name="to"' in t)

    # ---- 3. Picker page with an employee shows their history ----
    t = c.get("/payroll/work-history?employee=Isaac%20Marino"
              "&from=2026-11-19&to=2026-11-25").get_data(as_text=True)
    check("picker history shows M-Solution day",
          "2026-11-20" in t and "M-Solution" in t)
    check("picker history shows Carpenter days",
          "2026-11-21" in t and "2026-11-23" in t
          and "COA - Carpenter" in t)
    check("picker history per-company totals",
          "8h" in t and "17.5h" in t)
    check("picker history grand total 25.5h", "25.5h" in t)
    check("picker history newest first",
          t.index("2026-11-23") < t.index("2026-11-21")
          < t.index("2026-11-20"))
    check("picker history links to employee details",
          "Isaac Marino details" in t)

    # ---- 4. Company page has a History link per employee ----
    t = c.get("/payroll/m-solution?from=2026-11-01&to=2026-11-30"
              ).get_data(as_text=True)
    check("company page renders", "M-Solution Payroll" in t)
    check("company employee row has History link",
          'href="/payroll/employees/Isaac%20Marino/history"' in t
          and ">History<" in t)

    # ---- 5. Per-employee history page still works ----
    t = c.get("/payroll/employees/Isaac%20Marino/history"
              "?from=2026-11-19&to=2026-11-25").get_data(as_text=True)
    check("per-employee history still 200",
          "Work History" in t and "2026-11-20" in t)
    r = c.get("/payroll/work-history?employee=Nobody%20Here")
    check("unknown employee on picker page shows prompt, 200",
          r.status_code == 200
          and b"Pick an employee above" in r.data)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)
finally:
    shutil.copy(BAK_P, DATA_P)
    os.remove(BAK_P)
