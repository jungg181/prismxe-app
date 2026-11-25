#!/usr/bin/env python3
"""Employee Work History: one worker's work across every company —
which day they worked where, and how many hours. Run:
venv/bin/python test_payroll_employee_history.py"""
import json
import os
import re
import shutil
import sys

STAGING = "/home/hatch/workspace/prismxe-app-staging"
sys.path.insert(0, STAGING)

DATA_P = os.path.join(STAGING, "data_payroll.json")
BAK_P = os.path.join(STAGING, "data_payroll.json.bak-emp-history")


def entry(eid, employee, company, date, hours, cin="", cout="",
          site="", notes=""):
    return {"id": eid, "company": company, "employee": employee,
            "date": date, "clock_in": cin, "clock_out": cout,
            "clock_out_date": None, "gross_hours": hours,
            "payroll_hours": hours, "site": site, "notes": notes,
            "meal_break": False}


seed = {
    "companies": [
        {"name": "M-Solution", "slug": "m-solution", "prefix": "MS",
         "vendor": "M", "ship_to": "", "category": ""},
        {"name": "COA - Carpenter", "slug": "coa-carpenter",
         "prefix": "COA", "vendor": "C", "ship_to": "", "category": ""},
        {"name": "PrismXE", "slug": "prismxe", "prefix": "PRISM",
         "vendor": "P", "ship_to": "", "category": ""},
    ],
    "rates": [
        {"name": "Isaac Marino", "company": "M-Solution",
         "staffing": 25, "billing": 39},
        {"name": "Isaac Marino", "company": "COA - Carpenter",
         "staffing": 32, "billing": 39},
        {"name": "Isaac Marino", "company": "PrismXE",
         "staffing": 25, "billing": 39},
    ],
    "entries": [
        # Isaac: M-Solution one day, Carpenter the next, PrismXE after.
        entry("h1", "Isaac Marino", "M-Solution", "2026-11-20", 8.0,
              "7:00", "15:00", "LGES", ""),
        entry("h2", "Isaac Marino", "COA - Carpenter", "2026-11-21", 9.5,
              "7:00", "16:30", "LGES", ""),
        entry("h3", "Isaac Marino", "COA - Carpenter", "2026-11-23", 8.0,
              "7:00", "15:00", "LGES", ""),
        entry("h4", "Isaac Marino", "PrismXE", "2026-11-24", 8.5,
              "7:00", "15:30", "LGES", ""),
        # Another worker, same companies — must NOT appear.
        entry("h5", "Carlos Brranco", "M-Solution", "2026-11-20", 8.0,
              "7:00", "15:00", "LGES", ""),
        # Outside the period — must NOT appear.
        entry("h6", "Isaac Marino", "M-Solution", "2026-10-01", 8.0,
              "7:00", "15:00", "LGES", ""),
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

    t = c.get("/payroll/employees/Isaac%20Marino/history"
              "?from=2026-11-19&to=2026-11-25").get_data(as_text=True)
    check("history page 200", "Work History" in t)
    check("shows the employee name", "Isaac Marino" in t)
    # The 4 in-period rows are present; the other worker + the
    # out-of-period row are not.
    check("shows M-Solution day", "2026-11-20" in t
          and "M-Solution" in t)
    check("shows Carpenter days", "2026-11-21" in t
          and "2026-11-23" in t and "COA - Carpenter" in t)
    check("shows PrismXE day", "2026-11-24" in t and "PrismXE" in t)
    check("other worker is not shown",
          "Carlos Brranco" not in t)
    check("out-of-period row is not shown",
          "2026-10-01" not in t)
    # Newest first: 11-24 appears before 11-23 before 11-21 before
    # 11-20, so their positions in the page increase in that order.
    idx = [t.index(d) for d in ("2026-11-24", "2026-11-23",
                                "2026-11-21", "2026-11-20")]
    check("newest first", idx == sorted(idx), idx)
    # Per-company breakdown: M-Solution 8h, Carpenter 17.5h,
    # PrismXE 8.5h; total 34h.
    check("per-company totals shown",
          "M-Solution" in t and "8h" in t
          and "COA - Carpenter" in t and "17.5h" in t
          and "PrismXE" in t and "8.5h" in t)
    check("grand total 34h", "34h" in t)
    # Weekday labels + clock times + edit links.
    check("weekday labels shown",
          re.search(r"<td>(Mon|Tue|Wed|Thu|Fri|Sat|Sun)</td>", t)
          is not None)
    check("clock times shown", "7:00" in t and "16:30" in t)
    check("edit link per row", "Edit" in t)
    # Period picker + back links.
    check("period picker present",
          'name="from"' in t and 'name="to"' in t)
    check("back to employee link",
          "Back to Isaac Marino" in t)
    # Unknown employee -> 404.
    r = c.get("/payroll/employees/Nobody%20Here/history")
    check("unknown employee 404", r.status_code == 404)
    # Employee detail page links to the history.
    t = c.get("/payroll/employees/Isaac%20Marino"
              ).get_data(as_text=True)
    check("employee detail links to Work History",
          "Work History" in t and "/history" in t)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)
finally:
    shutil.copy(BAK_P, DATA_P)
    os.remove(BAK_P)
