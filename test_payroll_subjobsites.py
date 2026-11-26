#!/usr/bin/env python3
"""Staging tests for payroll sub-jobsites (locations).

A jobsite has three levels: a Jobsite ID (e.g. LGES) -> a company
split under it (LGES -> Hanseong) -> a Sub Site under the company
(LGES -> Hanseong -> Building A). LGES stays one parent jobsite;
time entries tag to the deepest pick, the parent P&L rolls up its
own entries plus every company and Sub Site below it, and each
company and Sub Site keeps its own P&L. Location picking is
direct-click buttons, never a dropdown.

Covers:
- compute_jobsite_pnl with jobsite_ids: parent rollup = direct +
  locations, OT allocated to the later days of the week across the
  whole week, parts add up exactly to the company payroll.
- Routes: create a company split under a parent, create a Sub Site
  under a company, a 4th level under a Sub Site refused, duplicate
  code refused, list shows parent + indented company + Sub Site
  rows, parent detail shows the Locations breakdown, sub detail
  links back to its parent, delete guards (parent with locations,
  location with entries), entry Add/Edit tags to a location via the
  button field, invalid location falls back to the parent, bulk
  assign to a location, entries page has location buttons and no
  location dropdown, entries table shows "Parent / Location".

Run: ~/workspace/prismxe-app/venv/bin/python test_payroll_subjobsites.py
Uses staging data_payroll.json — the test seeds its own
company/rates/entries and restores the file in `finally`.
"""
import json
import os
import re
import shutil
import sys

STAGING = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, STAGING)

DATA = os.path.join(STAGING, "data_payroll.json")
BACKUP = DATA + ".bak-subjobsites-test"

from payroll_calc import compute_jobsite_pnl  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {extra}")
        print(f"FAIL: {name} {extra}")


# ------------------------------------------------------------------ pure fn
# Worker A, one Mon-Sat week, billing $50 / staffing $30.
# Mon 8h parent direct, Tue 8h loc A, Wed 8h loc B, Thu 8h loc A,
# Fri 8h parent direct, Sat 8h loc B. Week total 48h: first 40 reg
# (Mon-Fri), last 8 OT (Sat).
rates = [{"name": "Worker A", "company": "Test Co",
          "staffing": 30, "billing": 50}]
entries = []
for i, (d, jid) in enumerate([
        ("2026-10-19", "job-parent"),
        ("2026-10-20", "job-a"),
        ("2026-10-21", "job-b"),
        ("2026-10-22", "job-a"),
        ("2026-10-23", "job-parent"),
        ("2026-10-24", "job-b")]):
    entries.append({
        "id": f"e{i}", "company": "Test Co", "employee": "Worker A",
        "date": d, "payroll_hours": 8, "jobsite_id": jid,
    })

roll = compute_jobsite_pnl(entries, rates, "job-parent",
                           jobsite_ids={"job-parent", "job-a", "job-b"})
check("rollup reg 40h", roll["reg_hours"] == 40.0, str(roll))
check("rollup ot 8h (Sat)", roll["ot_hours"] == 8.0, str(roll))
check("rollup revenue 40*50 + 8*75 = 2600",
      roll["revenue"] == 2600.0, str(roll))
check("rollup labor 40*30 + 8*45 = 1560",
      roll["labor_cost"] == 1560.0, str(roll))
check("rollup lines carry jobsite_id",
      all("jobsite_id" in l for l in roll["lines"]), str(roll))

pa = compute_jobsite_pnl(entries, rates, "job-a")
check("loc A reg 16h (Tue+Thu)", pa["reg_hours"] == 16.0, str(pa))
check("loc A ot 0h", pa["ot_hours"] == 0.0, str(pa))
check("loc A revenue 800", pa["revenue"] == 800.0, str(pa))
check("loc A labor 480", pa["labor_cost"] == 480.0, str(pa))

pb = compute_jobsite_pnl(entries, rates, "job-b")
check("loc B reg 8h (Wed)", pb["reg_hours"] == 8.0, str(pb))
check("loc B ot 8h (Sat)", pb["ot_hours"] == 8.0, str(pb))
check("loc B revenue 8*50 + 8*75 = 1000",
      pb["revenue"] == 1000.0, str(pb))
check("loc B labor 8*30 + 8*45 = 600",
      pb["labor_cost"] == 600.0, str(pb))

pd = compute_jobsite_pnl(entries, rates, "job-parent")
check("parent direct reg 16h (Mon+Fri)", pd["reg_hours"] == 16.0, str(pd))
check("parent direct ot 0h", pd["ot_hours"] == 0.0, str(pd))
check("parent direct revenue 800", pd["revenue"] == 800.0, str(pd))
check("parts add up to rollup revenue",
      pa["revenue"] + pb["revenue"] + pd["revenue"] == 2600.0)
check("parts add up to rollup labor",
      pa["labor_cost"] + pb["labor_cost"] + pd["labor_cost"] == 1560.0)
check("parts add up to rollup hours",
      pa["total_hours"] + pb["total_hours"] + pd["total_hours"] == 48.0)

# -------------------------------------------------------------------- routes
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
    }]
    pb.DB["rates"] = list(rates)
    pb.DB["entries"] = [dict(e) for e in entries]
    pb.DB["jobsites"] = []
    pb.DB["jobsite_costs"] = []
    pb.DB["aliases"] = []
    pb.DB["variances"] = []
    pb.DB["expenses"] = []

    # Create the parent jobsite.
    r = client.post("/payroll/jobsites", data={
        "code": "LGES", "name": "LGES big site",
        "company": "Test Co", "site": "LGES",
    }, follow_redirects=True)
    check("create parent 200", r.status_code == 200, str(r.status_code))
    check("parent stored top-level",
          len(pb.DB["jobsites"]) == 1
          and pb.DB["jobsites"][0].get("parent_id") is None)
    parent = pb.DB["jobsites"][0]

    # Create a location under it.
    r = client.post("/payroll/jobsites", data={
        "code": "LGES-A", "name": "Building A",
        "company": "Test Co", "site": "LGES",
        "parent_id": parent["id"],
    }, follow_redirects=True)
    check("create location 200", r.status_code == 200, str(r.status_code))
    check("location stored with parent_id",
          len(pb.DB["jobsites"]) == 2
          and pb.DB["jobsites"][1].get("parent_id") == parent["id"])
    loc_a = pb.DB["jobsites"][1]

    # A Sub Site under a company split is allowed (3 levels);
    # a 4th level under a Sub Site is refused.
    r = client.post("/payroll/jobsites", data={
        "code": "LGES-A-1", "name": "Sub of A",
        "company": "Test Co", "parent_id": loc_a["id"],
    }, follow_redirects=True)
    check("sub site under company allowed", len(pb.DB["jobsites"]) == 3)
    sub_a1 = pb.DB["jobsites"][2]
    r = client.post("/payroll/jobsites", data={
        "code": "LGES-A-1-X", "name": "Too deep",
        "company": "Test Co", "parent_id": sub_a1["id"],
    }, follow_redirects=True)
    check("4th level under sub site refused",
          len(pb.DB["jobsites"]) == 3)
    check("4th level flash", b"Sub Site" in r.data)

    # Duplicate code refused (global, incl. locations).
    r = client.post("/payroll/jobsites", data={
        "code": "lges-a", "name": "dup", "company": "Test Co",
    }, follow_redirects=True)
    check("duplicate location code refused", len(pb.DB["jobsites"]) == 3)

    # Unknown parent id is treated as top-level (no crash).
    r = client.post("/payroll/jobsites", data={
        "code": "OTHER", "name": "Other", "company": "Test Co",
        "parent_id": "no-such-id",
    }, follow_redirects=True)
    check("unknown parent id -> top-level",
          len(pb.DB["jobsites"]) == 4
          and pb.DB["jobsites"][3].get("parent_id") is None)
    other = pb.DB["jobsites"][3]

    # Retag the seeded entries to the real ids, then check the list.
    idmap = {"job-parent": parent["id"], "job-a": loc_a["id"]}
    for e in pb.DB["entries"]:
        if e.get("jobsite_id") in idmap:
            e["jobsite_id"] = idmap[e["jobsite_id"]]
    # The Wed + Sat entries were on job-b (no such location yet);
    # tag them to the parent for the rollup check.
    for e in pb.DB["entries"]:
        if e.get("jobsite_id") == "job-b":
            e["jobsite_id"] = parent["id"]
    r = client.get("/payroll/jobsites")
    check("list 200", r.status_code == 200, str(r.status_code))
    check("list shows parent", b"LGES" in r.data)
    check("list shows location row", b"LGES-A" in r.data)
    check("list marks location rows", b"Location" in r.data)
    check("list shows location count for parent",
          b"1 location(s)" in r.data)
    check("list shows rollup revenue 2600", b"2600.00" in r.data)
    check("list parent row has no Remove button",
          r.data.count(b">Remove</button>") == 2,
          str(r.data.count(b">Remove</button>")))
    check("list location with entries shows In use, not Remove",
          b"In use" in r.data)

    # Parent detail: Locations breakdown + rollup.
    r = client.get(f"/payroll/jobsites/{parent['id']}")
    check("parent detail 200", r.status_code == 200, str(r.status_code))
    check("parent detail has Locations card", b"Locations (1)" in r.data)
    check("parent detail breakdown shows LGES-A", b"LGES-A" in r.data)
    check("parent detail rollup revenue 2600", b"2600.00" in r.data)
    check("parent detail rollup labor 1560", b"1560.00" in r.data)
    check("parent detail entries show location column",
          b"Location" in r.data)

    # Location detail: own P&L only + link back to the parent.
    r = client.get(f"/payroll/jobsites/{loc_a['id']}")
    check("location detail 200", r.status_code == 200, str(r.status_code))
    check("location detail links parent", b"Location of" in r.data)
    check("location detail own revenue 800", b"800.00" in r.data)
    check("location detail has Locations card for its sub site",
          b"Locations (1)" in r.data)
    check("location detail breakdown shows LGES-A-1", b"LGES-A-1" in r.data)

    # Entries page: location picker is buttons, not a dropdown.
    r = client.get("/payroll/test-co/entries?from=2026-10-19&to=2026-10-24")
    check("entries page 200", r.status_code == 200, str(r.status_code))
    check("entries page has location buttons",
          b"loc-btn" in r.data)
    check("entries page has location picker",
          b"Location \xe2\x80\x94 tap one" in r.data)
    check("location field is a hidden input, not a select",
          b'type="hidden" id="sub_jobsite_id" name="sub_jobsite_id"'
          in r.data)
    check("no select named sub_jobsite_id",
          b'<select id="sub_jobsite_id"' not in r.data
          and b'<select name="sub_jobsite_id"' not in r.data)
    sel = re.search(rb'<select id="jobsite_id"[^>]*>(.*?)</select>',
                    r.data, re.S)
    check("jobsite select found", sel is not None)
    if sel:
        check("jobsite select lists parent",
              parent["id"].encode() in sel.group(1))
        check("jobsite select does NOT list the location",
              loc_a["id"].encode() not in sel.group(1))
    check("entries table shows Parent / Location",
          b"LGES / LGES-A" in r.data)
    check("entries page has bulk assign with location picker",
          b"assign-location-btns" in r.data)

    # Add an entry picking the parent + tapping a location button.
    r = client.post("/payroll/test-co/entries?from=2026-10-19&to=2026-10-24",
                    data={"action": "add", "employee": "Worker A",
                          "date": "2026-10-19", "payroll_hours": "8",
                          "clock_in": "", "clock_out": "", "site": "",
                          "notes": "new",
                          "jobsite_id": parent["id"],
                          "sub_jobsite_id": loc_a["id"]},
                    follow_redirects=True)
    check("add entry with location 200",
          r.status_code == 200, str(r.status_code))
    new_e = next((e for e in pb.DB["entries"]
                  if e.get("notes") == "new"), None)
    check("add entry stored with location id",
          new_e is not None and new_e.get("jobsite_id") == loc_a["id"],
          str(new_e))

    # An invalid location falls back to the parent.
    r = client.post("/payroll/test-co/entries?from=2026-10-19&to=2026-10-24",
                    data={"action": "add", "employee": "Worker A",
                          "date": "2026-10-20", "payroll_hours": "8",
                          "clock_in": "", "clock_out": "", "site": "",
                          "notes": "badloc",
                          "jobsite_id": parent["id"],
                          "sub_jobsite_id": "no-such-location"},
                    follow_redirects=True)
    bad_e = next((e for e in pb.DB["entries"]
                  if e.get("notes") == "badloc"), None)
    check("invalid location falls back to parent",
          bad_e is not None and bad_e.get("jobsite_id") == parent["id"],
          str(bad_e))

    # A location from another parent is ignored too.
    r = client.post("/payroll/jobsites", data={
        "code": "OTHER-X", "name": "Other loc",
        "company": "Test Co", "parent_id": other["id"],
    }, follow_redirects=True)
    loc_x = pb.DB["jobsites"][-1]
    r = client.post("/payroll/test-co/entries?from=2026-10-19&to=2026-10-24",
                    data={"action": "add", "employee": "Worker A",
                          "date": "2026-10-21", "payroll_hours": "8",
                          "clock_in": "", "clock_out": "", "site": "",
                          "notes": "wrongparent",
                          "jobsite_id": parent["id"],
                          "sub_jobsite_id": loc_x["id"]},
                    follow_redirects=True)
    wp_e = next((e for e in pb.DB["entries"]
                 if e.get("notes") == "wrongparent"), None)
    check("location of another parent ignored",
          wp_e is not None and wp_e.get("jobsite_id") == parent["id"],
          str(wp_e))

    # Edit a location-tagged entry: dropdown shows the parent, the
    # location button comes pre-selected (hidden input carries it).
    r = client.get("/payroll/test-co/entries?from=2026-10-19&to=2026-10-24"
                   f"&edit={new_e['id']}")
    check("edit page 200", r.status_code == 200, str(r.status_code))
    check("edit dropdown selects parent",
          (f'value="{parent["id"]}" selected'.encode() in r.data))
    check("edit hidden input carries location",
          (f'id="sub_jobsite_id" name="sub_jobsite_id" '
           f'value="{loc_a["id"]}"'.encode() in r.data))

    # Bulk assign to a location: untagged entries get the location id.
    pb.DB["entries"].append({
        "id": "e-untagged", "company": "Test Co", "employee": "Worker A",
        "date": "2026-10-20", "payroll_hours": 4, "jobsite_id": None,
        "clock_in": "", "clock_out": "", "clock_out_date": None,
        "gross_hours": None, "site": "", "notes": "",
    })
    r = client.post("/payroll/test-co/entries?from=2026-10-19&to=2026-10-24",
                    data={"action": "assign_jobsite",
                          "jobsite_id": parent["id"],
                          "sub_jobsite_id": loc_a["id"]},
                    follow_redirects=True)
    check("bulk assign to location 200",
          r.status_code == 200, str(r.status_code))
    ut = next(e for e in pb.DB["entries"] if e["id"] == "e-untagged")
    check("bulk assign tagged to location",
          ut.get("jobsite_id") == loc_a["id"], str(ut))

    # Delete guards: parent with locations refused.
    r = client.post(f"/payroll/jobsites/{parent['id']}/delete",
                    follow_redirects=True)
    check("delete parent with locations refused",
          len(pb.DB["jobsites"]) == 5)
    check("delete parent flash mentions locations",
          b"location(s)" in r.data)

    # Location with entries refused.
    r = client.post(f"/payroll/jobsites/{loc_a['id']}/delete",
                    follow_redirects=True)
    check("delete location with entries refused",
          len(pb.DB["jobsites"]) == 5)

    # Clean up: remove the test entries, then the sub site, then the
    # location, then the parent.
    pb.DB["entries"] = [e for e in pb.DB["entries"]
                        if e.get("jobsite_id") not in (loc_a["id"],
                                                       parent["id"])]
    r = client.post(f"/payroll/jobsites/{sub_a1['id']}/delete",
                    follow_redirects=True)
    check("delete empty sub site works",
          len(pb.DB["jobsites"]) == 4)
    r = client.post(f"/payroll/jobsites/{loc_a['id']}/delete",
                    follow_redirects=True)
    check("delete empty location works",
          len(pb.DB["jobsites"]) == 3)
    r = client.post(f"/payroll/jobsites/{parent['id']}/delete",
                    follow_redirects=True)
    check("delete parent after locations removed",
          len(pb.DB["jobsites"]) == 2)
finally:
    shutil.copyfile(BACKUP, DATA)
    os.remove(BACKUP)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(" -", f)
    sys.exit(1)
print("ALL PASS")
