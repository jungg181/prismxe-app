#!/usr/bin/env python3
"""Unsaved-changes guard (payroll-wide): editing a manual-save form
and then navigating to another page prompts "save before leaving?".
Auto-save forms (Days list) are excluded. Run:
venv/bin/python test_payroll_unsaved_guard.py"""
import json
import os
import shutil
import sys

STAGING = "/home/hatch/workspace/prismxe-app-staging"
sys.path.insert(0, STAGING)

DATA_P = os.path.join(STAGING, "data_payroll.json")
BAK_P = os.path.join(STAGING, "data_payroll.json.bak-unsaved-guard")

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
        {"id": "e1", "company": "TestCo", "employee": "Ana Test",
         "date": "2026-11-02", "payroll_hours": 9.5,
         "clock_in": "7:00", "clock_out": "15:30",
         "clock_out_date": None, "gross_hours": 9.5,
         "site": "LGES", "notes": ""},
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

    # ---- 1. The modal is on every payroll page (base template) ----
    t = c.get("/payroll/testco/entries?from=2026-11-01&to=2026-11-30"
              ).get_data(as_text=True)
    check("entries page renders", "TestCo" in t)
    check("unsaved modal present", 'id="unsaved-modal"' in t)
    check("modal asks to save before leaving",
          "Unsaved changes" in t and "Save them before leaving?" in t)
    check("modal has Save & Leave button",
          'id="unsaved-save"' in t and "Save &amp; Leave" in t)
    check("modal has Leave Without Saving button",
          'id="unsaved-discard"' in t
          and "Leave Without Saving" in t)
    check("modal has Cancel button",
          'id="unsaved-cancel"' in t and ">Cancel<" in t)

    # ---- 2. Guard JS: dirty tracking + link interception ----
    check("guard tracks edits (input/change)",
          "addEventListener('input'" in t
          and "addEventListener('change'" in t)
    check("guard only watches manual-save POST forms",
          "form.method.toLowerCase() === 'post'" in t
          and "data-autosave" in t)
    check("guard intercepts link clicks when dirty",
          "closest('a[href]')" in t and "showModal(a.href)" in t)
    check("guard clears dirty on submit",
          "addEventListener('submit'" in t)
    check("Save & Leave posts the dirty form then navigates",
          "fetch(form.action" in t and "new FormData(form)" in t
          and "go(url)" in t)
    check("Save & Leave validates required fields first",
          "form.reportValidity()" in t)
    check("guard has beforeunload fallback",
          "addEventListener('beforeunload'" in t)
    check("guard exposes a hook for auto-save forms",
          "__payrollFormSaved" in t)

    # ---- 3. Entry forms are guarded (no data-autosave) ----
    check("single add form is guarded",
          'id="daily-add-form"' in t or 'name="action" value="add"' in t)
    # The monthly/daily forms render only on their own tabs.
    t_mon = c.get("/payroll/testco/entries?from=2026-11-01&to=2026-11-30"
                  "&tab=monthly&monthly_employee=Ana+Test"
                  "&monthly_month=2026-11").get_data(as_text=True)
    check("monthly add form has no data-autosave",
          'id="monthly-add-form"' in t_mon
          and 'id="monthly-add-form" data-autosave' not in t_mon)
    t_day = c.get("/payroll/testco/entries?from=2026-11-01&to=2026-11-30"
                  "&tab=daily&daily_date=2026-11-02"
                  ).get_data(as_text=True)
    check("daily add form has no data-autosave",
          'id="daily-add-form"' in t_day
          and 'id="daily-add-form" data-autosave' not in t_day)

    # ---- 4. Days list auto-save forms are excluded ----
    t = c.get("/payroll/testco?from=2026-11-01&to=2026-11-30"
              ).get_data(as_text=True)
    check("company page renders", "TestCo Payroll" in t)
    check("days list forms carry data-autosave",
          'id="hrs-e1" data-autosave' in t)
    check("unsaved modal also on company page",
          'id="unsaved-modal"' in t)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)
finally:
    shutil.copy(BAK_P, DATA_P)
    os.remove(BAK_P)
