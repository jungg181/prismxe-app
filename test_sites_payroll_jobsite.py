#!/usr/bin/env python3
"""Add a Payroll jobsite from the Sites page (/staffing/sites):
the page lists the payroll jobsites and has an Add form that posts
to /payroll/jobsites and returns to the Sites page. Run:
venv/bin/python test_sites_payroll_jobsite.py"""
import json
import os
import shutil
import sys

STAGING = "/home/hatch/workspace/prismxe-app-staging"
sys.path.insert(0, STAGING)

CM_P = os.path.join(STAGING, "data_cm.json")
PAY_P = os.path.join(STAGING, "data_payroll.json")
BAK_CM = CM_P + ".bak-sites-pj"
BAK_PAY = PAY_P + ".bak-sites-pj"

passed, failed = [], []

def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (
        (" | " + str(extra)) if extra and not cond else ""))

cm_seed = {
    "sites": [
        {"id": "s1", "name": "LGES", "status": "active", "address": "",
         "pm": "", "start_date": "", "target_date": ""},
    ],
    "workers": [], "timecards": [], "incidents": [], "defects": [],
    "materials": [],
}
pay_seed = {
    "companies": [
        {"name": "Hanseong", "slug": "hanseong", "prefix": "HS",
         "vendor": "", "ship_to": "", "category": ""},
        {"name": "M-Solution", "slug": "m-solution", "prefix": "MS",
         "vendor": "", "ship_to": "", "category": ""},
    ],
    "rates": [], "entries": [], "expenses": [], "settings": {},
    "aliases": [], "variances": {},
    "jobsites": [
        {"id": "job-aaa", "code": "LGES", "name": "COA - Carpenter",
         "company": "Hanseong", "site": "Test", "status": "active",
         "parent_id": None},
    ],
    "jobsite_costs": [], "invoices": [], "payroll_review_tokens": [],
    "payroll_corrections": [],
}

shutil.copy(CM_P, BAK_CM)
shutil.copy(PAY_P, BAK_PAY)
try:
    with open(CM_P, "w") as f:
        json.dump(cm_seed, f)
    with open(PAY_P, "w") as f:
        json.dump(pay_seed, f)

    import cm_bp
    import payroll_bp
    from flask import Flask

    cm_bp.DB = cm_bp.load_db()
    payroll_bp.DB = payroll_bp.load_db()

    app = Flask(__name__,
                template_folder=os.path.join(STAGING, "templates"),
                static_folder=os.path.join(STAGING, "static"))
    app.secret_key = "test-secret"
    app.register_blueprint(cm_bp.bp)
    app.register_blueprint(payroll_bp.bp, url_prefix="/payroll")
    cm_bp.register_jinja(app)
    c = app.test_client()

    # ---- 1. Sites page shows the Payroll Jobsites section ----
    t = c.get("/staffing/sites").get_data(as_text=True)
    check("sites page 200", "Sites" in t)
    check("payroll jobsites section present",
          "Payroll Jobsites" in t)
    check("existing payroll jobsite listed",
          "LGES" in t and "COA - Carpenter" in t
          and "Hanseong" in t)
    check("add form posts to /payroll/jobsites",
          'action="/payroll/jobsites"' in t)
    check("add form has code + name + company",
          'name="code"' in t and 'name="name"' in t
          and 'name="company"' in t)
    check("company dropdown lists payroll companies",
          "Hanseong" in t and "M-Solution" in t)
    check("add form returns to the Sites page",
          'name="next" value="/staffing/sites"' in t)
    check("link to full payroll jobsites page",
          'href="/payroll/jobsites"' in t)

    # ---- 2. Adding from the Sites page creates it + returns ----
    r = c.post("/payroll/jobsites", data={
        "code": "LGES-NEW", "name": "New Test Job",
        "company": "M-Solution", "site": "LGES",
        "parent_id": "", "next": "/staffing/sites"})
    check("add redirects back to Sites",
          r.status_code == 302
          and r.headers.get("Location", "").endswith("/staffing/sites"),
          r.status_code)
    t = c.get("/staffing/sites").get_data(as_text=True)
    check("new payroll jobsite listed on Sites page",
          "LGES-NEW" in t and "New Test Job" in t
          and "M-Solution" in t)

    # ---- 3. Without next (normal payroll-page add) -> jobsites ----
    r = c.post("/payroll/jobsites", data={
        "code": "LGES-2", "name": "Second Job",
        "company": "Hanseong", "site": "", "parent_id": ""})
    check("no next -> redirects to payroll jobsites",
          r.status_code == 302
          and r.headers.get("Location", "").endswith("/payroll/jobsites"),
          r.headers.get("Location"))

    # ---- 4. A foreign next is ignored (no open redirect) ----
    r = c.post("/payroll/jobsites", data={
        "code": "LGES-3", "name": "Third Job",
        "company": "Hanseong", "site": "", "parent_id": "",
        "next": "https://evil.example.com/"})
    check("foreign next ignored",
          r.status_code == 302
          and "evil.example.com" not in r.headers.get("Location", ""),
          r.headers.get("Location"))

    # ---- 5. The new jobsite shows on the clock-in picker ----
    t = c.get("/staffing/onsite").get_data(as_text=True)
    check("new jobsite on the onsite picker",
          "LGES-NEW" in t)

    # ---- 6. Duplicate code is refused ----
    r = c.post("/payroll/jobsites", data={
        "code": "LGES-NEW", "name": "Dup", "company": "Hanseong",
        "site": "", "parent_id": "", "next": "/staffing/sites"})
    check("duplicate code refused, still back to Sites",
          r.status_code == 302
          and r.headers.get("Location", "").endswith("/staffing/sites"))

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)
finally:
    shutil.copy(BAK_CM, CM_P)
    shutil.copy(BAK_PAY, PAY_P)
    os.remove(BAK_CM)
    os.remove(BAK_PAY)
