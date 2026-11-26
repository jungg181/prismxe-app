#!/usr/bin/env python3
"""3-level payroll jobsites: LGES (Jobsite ID, Parent) -> company
split (LGES -> Hanseong) -> Sub Site (LGES -> Hanseong -> Building
A). Run: venv/bin/python test_jobsites_3_levels.py"""
import json
import os
import shutil
import sys

STAGING = "/home/hatch/workspace/prismxe-app-staging"
sys.path.insert(0, STAGING)

CM_P = os.path.join(STAGING, "data_cm.json")
PAY_P = os.path.join(STAGING, "data_payroll.json")
BAK_CM = CM_P + ".bak-jobs3"
BAK_PAY = PAY_P + ".bak-jobs3"

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
        {"name": "COA - Carpenter", "slug": "coa-carpenter",
         "prefix": "COA", "vendor": "", "ship_to": "", "category": ""},
    ],
    "rates": [
        {"name": "Worker A", "company": "Hanseong", "staffing": 25,
         "billing": 35, "billing_night": None, "employee_id": "",
         "shift": "7AM-7PM", "position": "Helper"},
    ],
    "entries": [], "expenses": [], "settings": {},
    "aliases": [], "variances": {},
    "jobsites": [
        {"id": "job-lges", "code": "LGES", "name": "LGES",
         "company": "Hanseong", "site": "LGES", "status": "active",
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

    # ---- 1. Add a company split under LGES ----
    r = c.post("/payroll/jobsites", data={
        "code": "LGES-HS", "name": "Hanseong", "company": "Hanseong",
        "site": "LGES", "parent_id": "job-lges"})
    check("company split under LGES added",
          r.status_code == 302, r.status_code)
    hs = payroll_bp.find_jobsite("job-lges")
    hs_child = [j for j in payroll_bp.DB["jobsites"]
                if j.get("code") == "LGES-HS"][0]
    check("company split depth is 1",
          payroll_bp._jobsite_depth(hs_child) == 1)
    check("LGES depth is 0",
          payroll_bp._jobsite_depth(hs) == 0)

    # ---- 2. Add a Sub Site under the company split ----
    r = c.post("/payroll/jobsites", data={
        "code": "LGES-HS-A1", "name": "Building A",
        "company": "Hanseong", "site": "LGES",
        "parent_id": hs_child["id"]})
    check("sub site under company added",
          r.status_code == 302, r.status_code)
    sub = [j for j in payroll_bp.DB["jobsites"]
           if j.get("code") == "LGES-HS-A1"][0]
    check("sub site depth is 2",
          payroll_bp._jobsite_depth(sub) == 2)

    # ---- 3. A Sub Site cannot be a parent (4th level refused) ----
    r = c.post("/payroll/jobsites", data={
        "code": "LGES-HS-A1-X", "name": "Too Deep",
        "company": "Hanseong", "site": "LGES",
        "parent_id": sub["id"]})
    check("4th level refused",
          r.status_code == 302
          and not any(j.get("code") == "LGES-HS-A1-X"
                      for j in payroll_bp.DB["jobsites"]),
          r.status_code)

    # ---- 4. Descendants + P&L rollup ----
    desc = payroll_bp._jobsite_descendants(hs)
    check("LGES descendants = company + sub site",
          {d["code"] for d in desc} == {"LGES-HS", "LGES-HS-A1"})
    # One entry tagged to the Sub Site, one to the company, one to LGES.
    for code, hrs in (("LGES-HS-A1", 10.0), ("LGES-HS", 8.0),
                      ("LGES", 5.0)):
        jid = [j for j in payroll_bp.DB["jobsites"]
               if j.get("code") == code][0]["id"]
        payroll_bp.DB["entries"].append({
            "id": "entry-" + code, "company": "Hanseong",
            "employee": "Worker A", "date": "2026-11-23",
            "clock_in": "7:00", "clock_out": "17:00",
            "clock_out_date": None, "gross_hours": hrs,
            "payroll_hours": hrs, "site": "LGES", "notes": "",
            "jobsite_id": jid})
    s_lges = payroll_bp._jobsite_summary(hs)
    s_hs = payroll_bp._jobsite_summary(hs_child)
    s_sub = payroll_bp._jobsite_summary(sub)
    check("LGES rolls up all 3 levels (23h)",
          s_lges["pnl"]["total_hours"] == 23.0,
          s_lges["pnl"]["total_hours"])
    check("company rolls up itself + sub site (18h)",
          s_hs["pnl"]["total_hours"] == 18.0,
          s_hs["pnl"]["total_hours"])
    check("sub site only its own (10h)",
          s_sub["pnl"]["total_hours"] == 10.0,
          s_sub["pnl"]["total_hours"])
    check("LGES revenue = 23h x $35 billing",
          s_lges["pnl"]["revenue"] == 805.0,
          s_lges["pnl"]["revenue"])

    # ---- 5. Resolve pick: deepest valid wins ----
    picked = payroll_bp._resolve_jobsite_pick(hs, hs_child, sub)
    check("resolve returns the sub site",
          picked is not None and picked["code"] == "LGES-HS-A1")
    picked = payroll_bp._resolve_jobsite_pick(hs, hs_child, None)
    check("resolve without sub site returns the company",
          picked is not None and picked["code"] == "LGES-HS")
    picked = payroll_bp._resolve_jobsite_pick(hs, None, sub)
    check("resolve with mismatched chain returns LGES",
          picked is not None and picked["code"] == "LGES")

    # ---- 6. Jobsites page shows all 3 levels ----
    t = c.get("/payroll/jobsites").get_data(as_text=True)
    check("jobsites page lists LGES, company, sub site",
          "LGES" in t and "LGES-HS" in t and "LGES-HS-A1" in t)
    check("sub site badge shown", "Sub Site" in t)
    check("add form parent offers LGES and the company",
          "LGES-HS" in t and "LGES" in t)

    # ---- 7. Detail page shows the sub site under the company ----
    t = c.get("/payroll/jobsites/job-lges").get_data(as_text=True)
    check("LGES detail shows company + sub site",
          "LGES-HS" in t and "LGES-HS-A1" in t)

    # ---- 8. Edit guards ----
    # Moving the company (has a sub site) under a Sub Site -> refused.
    r = c.post(f"/payroll/jobsites/{hs_child['id']}/edit", data={
        "code": "LGES-HS", "name": "Hanseong", "company": "Hanseong",
        "site": "LGES", "parent_id": sub["id"]})
    check("company with a sub site cannot move under a sub site",
          payroll_bp.find_jobsite(hs_child["id"])["parent_id"]
          == "job-lges")
    # Moving LGES under its own company -> refused (cycle).
    r = c.post("/payroll/jobsites/job-lges/edit", data={
        "code": "LGES", "name": "LGES", "company": "Hanseong",
        "site": "LGES", "parent_id": hs_child["id"]})
    check("LGES cannot move under its own child",
          payroll_bp.find_jobsite("job-lges")["parent_id"] is None)
    # Moving the Sub Site under LGES directly -> allowed.
    r = c.post(f"/payroll/jobsites/{sub['id']}/edit", data={
        "code": "LGES-HS-A1", "name": "Building A",
        "company": "Hanseong", "site": "LGES",
        "parent_id": "job-lges"})
    check("sub site can move up to LGES",
          payroll_bp.find_jobsite(sub["id"])["parent_id"] == "job-lges")
    # Move it back under the company for the delete checks.
    c.post(f"/payroll/jobsites/{sub['id']}/edit", data={
        "code": "LGES-HS-A1", "name": "Building A",
        "company": "Hanseong", "site": "LGES",
        "parent_id": hs_child["id"]})

    # ---- 9. Delete guards ----
    r = c.post("/payroll/jobsites/job-lges/delete")
    check("LGES with descendants cannot be deleted",
          payroll_bp.find_jobsite("job-lges") is not None)
    r = c.post(f"/payroll/jobsites/{hs_child['id']}/delete")
    check("company with a sub site cannot be deleted",
          payroll_bp.find_jobsite(hs_child["id"]) is not None)
    # A leaf sub site with entries cannot be deleted either.
    r = c.post(f"/payroll/jobsites/{sub['id']}/delete")
    check("sub site with entries cannot be deleted",
          payroll_bp.find_jobsite(sub["id"]) is not None)

    # ---- 10. Sites page: parent dropdown + tree list ----
    t = c.get("/staffing/sites").get_data(as_text=True)
    check("sites page lists the 3-level tree",
          "LGES" in t and "LGES-HS" in t and "LGES-HS-A1" in t)
    check("sites page parent dropdown offers the company",
          'name="parent_id"' in t and "LGES-HS" in t)
    check("sites page shows Sub Site badge", "Sub Site" in t)

    # ---- 11. Onsite picker: 3 levels in the tree ----
    tree = cm_bp.payroll_jobsite_tree()
    check("tree has LGES with company + sub site",
          len(tree) == 1 and tree[0]["code"] == "LGES"
          and len(tree[0]["children"]) == 1
          and tree[0]["children"][0]["code"] == "LGES-HS"
          and len(tree[0]["children"][0]["children"]) == 1
          and tree[0]["children"][0]["children"][0]["code"]
          == "LGES-HS-A1")
    check("label walks the full path",
          cm_bp.payroll_jobsite_label(sub["id"])
          == "LGES / LGES-HS / LGES-HS-A1")
    t = c.get("/staffing/onsite").get_data(as_text=True)
    check("onsite page has the sub site picker",
          "pj-sub-picker" in t and "pj-sub-btns" in t)
    check("onsite page renders the 3-level tree",
          "LGES-HS-A1" in t)

    # ---- 12. Entry form: tagging an entry to the Sub Site ----
    r = c.post("/payroll/hanseong/entries?from=2026-11-23&to=2026-11-29",
               data={"action": "single", "employee": "Worker A",
                     "date": "2026-11-24", "payroll_hours": "8",
                     "clock_in": "7:00", "clock_out": "15:00",
                     "site": "LGES", "notes": "",
                     "jobsite_id": "job-lges",
                     "sub_jobsite_id": hs_child["id"],
                     "sub2_jobsite_id": sub["id"]})
    tagged = [e for e in payroll_bp.DB["entries"]
              if e.get("date") == "2026-11-24"
              and e.get("employee") == "Worker A"]
    check("entry tagged to the sub site",
          len(tagged) == 1
          and tagged[0].get("jobsite_id") == sub["id"],
          tagged)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)
finally:
    shutil.copy(BAK_CM, CM_P)
    shutil.copy(BAK_PAY, PAY_P)
    os.remove(BAK_CM)
    os.remove(BAK_PAY)
