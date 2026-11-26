#!/usr/bin/env python3
"""Staging tests for the Jobsite Edit feature (payroll).

Covers:
- Edit page GET renders 200 with prefilled values; the Parent
  selector excludes the jobsite itself.
- Edit Code + Name (+ Company/Site) saves and redirects to detail.
- Duplicate code refused (case-insensitive, other jobsites only).
- Move a location to a different parent.
- Promote a location to top-level (Parent blank).
- Demote a top-level jobsite to a location under another parent.
- Guards: a jobsite with locations cannot become a location itself;
  self-parent refused; parent-is-a-location refused.
- Tagged entries + purchase costs stay attached after an edit (the
  jobsite id never changes).
- List + detail pages show Edit buttons.

Run: ~/workspace/prismxe-app/venv/bin/python test_payroll_jobsite_edit.py
Uses staging data_payroll.json — the test seeds its own
company/rates/entries and restores the file in `finally`.
"""
import json
import os
import shutil
import sys

STAGING = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, STAGING)

DATA = os.path.join(STAGING, "data_payroll.json")
BACKUP = DATA + ".bak-jobsite-edit-test"

PASS = []
FAIL = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {extra}")
        print(f"FAIL: {name} {extra}")


shutil.copyfile(DATA, BACKUP)
try:
    import payroll_bp as pb
    from flask import Flask

    app = Flask(__name__, template_folder=os.path.join(STAGING, "templates"))
    app.secret_key = "test-secret"
    app.register_blueprint(pb.bp)
    client = app.test_client()

    pb.DB["companies"] = [{
        "name": "Test Co", "slug": "test-co", "prefix": "TST",
        "vendor": "Test Co", "ship_to": "", "category": "",
    }, {
        "name": "Other Co", "slug": "other-co", "prefix": "OTH",
        "vendor": "Other Co", "ship_to": "", "category": "",
    }]
    pb.DB["rates"] = [{"name": "Worker A", "company": "Test Co",
                       "staffing": 30, "billing": 50}]
    pb.DB["entries"] = [{
        "id": "e1", "company": "Test Co", "employee": "Worker A",
        "date": "2026-10-19", "payroll_hours": 8, "jobsite_id": None,
    }]
    pb.DB["jobsites"] = []
    pb.DB["jobsite_costs"] = []
    pb.DB["aliases"] = []
    pb.DB["variances"] = []
    pb.DB["expenses"] = []

    def mk(code, name="x", parent=None, company="Test Co"):
        r = client.post("/payroll/jobsites", data={
            "code": code, "name": name, "company": company,
            "parent_id": parent or "",
        }, follow_redirects=True)
        assert r.status_code == 200, r.status_code
        return next(j for j in pb.DB["jobsites"] if j["code"] == code)

    def edit(job_id, **kw):
        data = {"code": kw.get("code", ""), "name": kw.get("name", ""),
                "company": kw.get("company", "Test Co"),
                "site": kw.get("site", ""),
                "parent_id": kw.get("parent_id", "")}
        return client.post(f"/payroll/jobsites/{job_id}/edit",
                           data=data, follow_redirects=True)

    parent = mk("LGES", "LGES parent")
    loc = mk("BLDG-A", "Building A", parent=parent["id"])
    other = mk("SITE-B", "Site B")

    # --- edit page renders with prefilled values; self not a parent option
    r = client.get(f"/payroll/jobsites/{loc['id']}/edit")
    check("edit page 200", r.status_code == 200, str(r.status_code))
    check("edit page prefilled code", b'value="BLDG-A"' in r.data)
    check("edit page prefilled name", b'value="Building A"' in r.data)
    check("edit page parent preselected",
          f'value="{parent["id"]}" selected'.encode() in r.data)
    check("edit page excludes self as parent",
          f'value="{loc["id"]}"'.encode() not in r.data)
    check("edit page lists other parent", b"SITE-B" in r.data)
    r = client.get("/payroll/jobsites/unknown-id/edit",
                   follow_redirects=True)
    check("edit unknown redirects", r.status_code == 200)

    # --- edit Code + Name saves, redirects to detail, id unchanged
    old_id = loc["id"]
    r = edit(loc["id"], code="BLDG-A2", name="Building A Renamed",
             parent_id=parent["id"])
    check("edit code+name 200", r.status_code == 200, str(r.status_code))
    loc = pb.find_jobsite(old_id)
    check("edit kept id", loc is not None and loc["id"] == old_id)
    check("edit changed code", loc["code"] == "BLDG-A2")
    check("edit changed name", loc["name"] == "Building A Renamed")
    check("detail shows new code", b"BLDG-A2" in r.data)

    # --- duplicate code refused (case-insensitive, other jobsites only)
    r = edit(loc["id"], code="lges", name="dup", parent_id=parent["id"])
    check("duplicate code refused",
          pb.find_jobsite(old_id)["code"] == "BLDG-A2")
    check("duplicate flash", b"already exists" in r.data)
    # Same code on itself is fine.
    r = edit(loc["id"], code="BLDG-A2", name="Building A Renamed",
             parent_id=parent["id"])
    check("same code on itself ok",
          pb.find_jobsite(old_id)["code"] == "BLDG-A2")

    # --- tagged entries + costs stay attached after an edit
    pb.DB["entries"][0]["jobsite_id"] = old_id
    pb.DB["jobsite_costs"].append({
        "id": "jcost-1", "jobsite_id": old_id,
        "description": "Lumber", "amount": 50, "date": "2026-10-19"})
    r = edit(loc["id"], code="BLDG-A3", name="Building A v3",
             parent_id=parent["id"])
    check("edit with entries keeps entry tag",
          pb.DB["entries"][0]["jobsite_id"] == old_id)
    check("edit with costs keeps cost tag",
          pb.DB["jobsite_costs"][0]["jobsite_id"] == old_id)
    check("edit with entries changed code",
          pb.find_jobsite(old_id)["code"] == "BLDG-A3")

    # --- move location to a different parent
    r = edit(loc["id"], code="BLDG-A3", name="Building A v3",
             parent_id=other["id"])
    check("move location to other parent",
          pb.find_jobsite(old_id)["parent_id"] == other["id"])
    check("moved location detail shows new parent", b"SITE-B" in r.data)

    # --- promote location to top-level
    r = edit(loc["id"], code="BLDG-A3", name="Building A v3",
             parent_id="")
    check("promote to top-level",
          pb.find_jobsite(old_id)["parent_id"] is None)

    # --- demote a top-level jobsite to a location
    r = edit(other["id"], code="SITE-B", name="Site B",
             parent_id=parent["id"])
    check("demote top-level to location",
          pb.find_jobsite(other["id"])["parent_id"] == parent["id"])

    # --- guard: parent-with-children cannot become a location
    r = edit(parent["id"], code="LGES", name="LGES parent",
             parent_id=other["id"])
    check("parent-with-children cannot become location",
          pb.find_jobsite(parent["id"])["parent_id"] is None)
    check("parent-with-children flash",
          b"cannot sit under one of its own locations" in r.data)

    # --- guard: self-parent refused
    r = edit(parent["id"], code="LGES", name="LGES parent",
             parent_id=parent["id"])
    check("self-parent refused",
          pb.find_jobsite(parent["id"])["parent_id"] is None)
    check("self-parent flash", b"its own parent" in r.data)

    # --- guard: parent-is-a-location refused
    # other is now a location under parent; try to parent LGES under it.
    # First free LGES of children: move other back to top-level.
    r = edit(other["id"], code="SITE-B", name="Site B", parent_id="")
    check("move other back to top-level",
          pb.find_jobsite(other["id"])["parent_id"] is None)
    # Now make loc a location under parent again, then try to put
    # parent under loc (loc is a location -> refused).
    r = edit(loc["id"], code="BLDG-A3", name="Building A v3",
             parent_id=parent["id"])
    check("loc back under parent",
          pb.find_jobsite(old_id)["parent_id"] == parent["id"])
    r = edit(parent["id"], code="LGES", name="LGES parent",
             parent_id=old_id)
    check("parent-under-location refused",
          pb.find_jobsite(parent["id"])["parent_id"] is None)
    check("parent-under-location flash",
          b"cannot sit under one of its own locations" in r.data)

    # --- list + detail show Edit buttons
    r = client.get("/payroll/jobsites")
    check("list 200", r.status_code == 200, str(r.status_code))
    check("list has edit button",
          f'/payroll/jobsites/{parent["id"]}/edit'.encode() in r.data)
    check("list has edit button for location",
          f'/payroll/jobsites/{old_id}/edit'.encode() in r.data)
    r = client.get(f"/payroll/jobsites/{parent['id']}")
    check("detail 200", r.status_code == 200, str(r.status_code))
    check("detail has edit button",
          f'/payroll/jobsites/{parent["id"]}/edit'.encode() in r.data)
    check("detail has edit label", b"Edit Jobsite" in r.data)
finally:
    shutil.copyfile(BACKUP, DATA)
    os.remove(BACKUP)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(" -", f)
    sys.exit(1)
print("ALL PASS")
