#!/usr/bin/env python3
"""Staging tests for clock-in Jobsite + Sub-jobsite pickers.

Covers: onsite page renders the Jobsite/Location button pickers (no
dropdown for the payroll jobsite), picking a jobsite shows its
locations, clock-in stores the jobsite_id on the timecard (location
wins over parent; invalid/blank pick stores None and never blocks
the punch), direct-clock + sync-clock carry it, worker-status and
my-timecards show the label, admin timecards shows it, and the
payroll import path (manual + auto-sync) carries the jobsite_id into
the entry so entries arrive already tagged.

Run: ~/workspace/prismxe-app/venv/bin/python test_clockin_jobsite.py
"""
import json
import os
import shutil
import sys

FIX = "/tmp/cm_jobsite_test"
STAGING = "/home/hatch/workspace/prismxe-app-staging"

# ---- fixture dir (fresh each run) ----
if os.path.exists(FIX):
    shutil.rmtree(FIX)
os.makedirs(os.path.join(FIX, "templates", "cm"))
os.makedirs(os.path.join(FIX, "static", "cm"))
shutil.copy(os.path.join(STAGING, "cm_bp.py"), os.path.join(FIX, "cm_bp.py"))
for f in os.listdir(os.path.join(STAGING, "templates", "cm")):
    src = os.path.join(STAGING, "templates", "cm", f)
    if os.path.isfile(src):
        shutil.copy(src, os.path.join(FIX, "templates", "cm", f))
for f in os.listdir(os.path.join(STAGING, "static", "cm")):
    src = os.path.join(STAGING, "static", "cm", f)
    if os.path.isfile(src):
        shutil.copy(src, os.path.join(FIX, "static", "cm", f))

SEED_CM = {
    "sites": [{"id": "site-lges", "name": "LGES", "status": "active"}],
    "workers": [{"id": "w-1", "name": "Test Worker", "email": "t@example.com",
                 "phone": "+14805550101", "trade": "Labor",
                 "employment_type": "Contract", "status": "active"}],
    "timecards": [],
}
SEED_PAYROLL = {
    "companies": [{"name": "PrismXE", "slug": "prismxe", "prefix": "PRISM"}],
    "rates": [{"name": "Test Worker", "company": "PrismXE",
               "staffing": 25, "billing": 35}],
    "aliases": [],
    "entries": [],
    "jobsites": [
        {"id": "job-parent", "code": "LGES", "name": "LGES",
         "company": "PrismXE", "site": "", "status": "active",
         "parent_id": None},
        {"id": "job-loc-a", "code": "BLDG-A", "name": "Building A",
         "company": "PrismXE", "site": "", "status": "active",
         "parent_id": "job-parent"},
        {"id": "job-loc-b", "code": "BLDG-B", "name": "Building B",
         "company": "PrismXE", "site": "", "status": "active",
         "parent_id": "job-parent"},
        {"id": "job-other", "code": "OTHER", "name": "Other Job",
         "company": "PrismXE", "site": "", "status": "active",
         "parent_id": None},
    ],
    "jobsite_costs": [],
}
with open(os.path.join(FIX, "data_cm.json"), "w") as f:
    json.dump(SEED_CM, f)
with open(os.path.join(FIX, "data_payroll.json"), "w") as f:
    json.dump(SEED_PAYROLL, f)

sys.path.insert(0, STAGING)
sys.path.insert(0, FIX)  # FIX first: cm_bp must load from the fixture dir
os.chdir(FIX)

import cm_bp  # noqa: E402
import payroll_import_app as pia  # noqa: E402
from flask import Flask  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name +
          ((" — " + str(extra)) if extra and not cond else ""))


app = Flask(__name__, template_folder=os.path.join(FIX, "templates"),
            static_folder=os.path.join(FIX, "static"))
app.register_blueprint(cm_bp.bp)
cm_bp.register_jinja(app)
client = app.test_client()


def clock_in(worker="w-1", site="site-lges", jobsite=None, via="direct"):
    body = {"worker_id": worker, "site_id": site, "action": "clock_in",
            "lat": 33.0, "lng": -111.0}
    if jobsite is not None:
        body["jobsite_id"] = jobsite
    if via == "direct":
        return client.post("/staffing/api/direct-clock", json=body)
    body["client_at"] = "2026-11-18T07:00:00"
    return client.post("/staffing/api/sync-clock", json=body)


def clock_out(worker="w-1"):
    return client.post("/staffing/api/direct-clock",
                       json={"worker_id": worker, "action": "clock_out",
                             "lat": 33.0, "lng": -111.0})


def open_tc():
    tcs = [t for t in cm_bp.DB["timecards"] if t.get("status") == "clocked_in"]
    return tcs[0] if tcs else None


# ---------- onsite renders the button pickers ----------
r = client.get("/staffing/onsite")
check("onsite 200", r.status_code == 200, r.status_code)
html = r.get_data(as_text=True)
check("onsite has pj-btns", 'id="pj-btns"' in html)
check("onsite has pj-loc-picker", 'id="pj-loc-picker"' in html)
check("onsite embeds LGES + locations",
      "job-parent" in html and "job-loc-a" in html and "BLDG-A" in html)
check("onsite has Parent only button", "Parent only" in html)
check("onsite has No jobsite button", "No jobsite" in html)
check("onsite has no payroll jobsite select",
      'id="pj-site"' not in html and 'name="payroll_jobsite"' not in html)
# The CM site picker is direct-click buttons now (onsite site picker,
# 2026-11-25), not a dropdown — the button container carries the list.
check("onsite has CM site buttons", 'id="dc-site-btns"' in html)

# ---------- clock-in stores the picked location ----------
r = clock_in(jobsite="job-loc-a")
check("clock-in with location 200", r.status_code == 200, r.status_code)
tc = open_tc()
check("timecard stores location id",
      tc and tc.get("jobsite_id") == "job-loc-a", tc)
r = client.get("/staffing/api/worker-status/w-1")
d = r.get_json()
check("worker-status jobsite label",
      d.get("jobsite_label") == "LGES / BLDG-A", d)
r = client.get("/staffing/api/my-timecards/w-1")
rows = r.get_json()
check("my-timecards jobsite label",
      rows and rows[0].get("jobsite_label") == "LGES / BLDG-A", rows)
r = client.get("/staffing/timecards")
check("admin timecards shows jobsite label",
      "LGES / BLDG-A" in r.get_data(as_text=True))
clock_out()
tc = [t for t in cm_bp.DB["timecards"] if t.get("jobsite_id") == "job-loc-a"]
check("jobsite survives clock-out", len(tc) == 1, len(tc))

# ---------- clock-in to the parent only ----------
r = clock_in(jobsite="job-parent")
check("clock-in with parent 200", r.status_code == 200, r.status_code)
tc = open_tc()
check("timecard stores parent id",
      tc and tc.get("jobsite_id") == "job-parent", tc)
r = client.get("/staffing/api/worker-status/w-1")
check("worker-status parent label",
      r.get_json().get("jobsite_label") == "LGES")
clock_out()

# ---------- no pick -> untagged, punch still works ----------
r = clock_in()
check("clock-in with no pick 200", r.status_code == 200, r.status_code)
tc = open_tc()
check("timecard jobsite None when no pick",
      tc and tc.get("jobsite_id") is None, tc)
clock_out()

# ---------- invalid pick -> None, punch still works ----------
r = clock_in(jobsite="job-nope")
check("clock-in with invalid pick 200", r.status_code == 200, r.status_code)
tc = open_tc()
check("invalid pick stores None",
      tc and tc.get("jobsite_id") is None, tc)
clock_out()

# ---------- offline sync-clock carries the pick ----------
r = clock_in(jobsite="job-loc-b", via="sync")
check("sync-clock with location 200", r.status_code == 200, r.status_code)
tc = open_tc()
check("sync-clock stores location id",
      tc and tc.get("jobsite_id") == "job-loc-b", tc)
clock_out()

# ---------- payroll import path carries jobsite_id ----------
cm_data = {"workers": [{"id": "w-1", "name": "Test Worker"}],
           "sites": [{"id": "site-lges", "name": "LGES"}],
           "timecards": [
               {"id": "t1", "worker_id": "w-1", "site_id": "site-lges",
                "clock_in": "2026-11-17T07:00:00",
                "clock_out": "2026-11-17T19:00:00",
                "jobsite_id": "job-loc-a", "notes": ""},
               {"id": "t2", "worker_id": "w-1", "site_id": "site-lges",
                "clock_in": "2026-11-16T07:00:00",
                "clock_out": "2026-11-16T19:00:00",
                "notes": ""},
           ]}
from datetime import date  # noqa: E402
rows, skipped = pia.timecard_rows(
    cm_data, date(2026, 11, 16), date(2026, 11, 17))
by_date = {row["date"]: row for row in rows}
check("timecard_rows carries location id",
      by_date.get("2026-11-17", {}).get("jobsite_id") == "job-loc-a",
      by_date)
check("timecard_rows without pick is None",
      by_date.get("2026-11-16", {}).get("jobsite_id") is None, by_date)
preview_rows = rows
for row in preview_rows:
    row["matched"] = "Test Worker"
    row["staffing"] = 25
    row["billing"] = 35
to_add, n_dupes, unmatched = pia.import_rows(
    preview_rows, [], "PrismXE", [], source_label="CM",
    use_canonical=True)
by_date = {e["date"]: e for e in to_add}
check("import carries location id into entry",
      by_date.get("2026-11-17", {}).get("jobsite_id") == "job-loc-a",
      by_date)
check("import without pick is None",
      by_date.get("2026-11-16", {}).get("jobsite_id") is None, by_date)
check("import adds both rows", len(to_add) == 2, len(to_add))

# patrol rows have no jobsite (unchanged behaviour)
prows, _ = pia.patrol_rows(
    {"workers": [], "punches": [
        {"email": "a@x.com", "name": "A", "action": "in",
         "ts": "2026-11-17T07:00:00"},
        {"email": "a@x.com", "name": "A", "action": "out",
         "ts": "2026-11-17T19:00:00"}]},
    date(2026, 11, 17), date(2026, 11, 17))
check("patrol row has no jobsite_id key",
      prows and "jobsite_id" not in prows[0], prows)
to_add2, _, _ = pia.import_rows(
    [{"employee": "A", "date": "2026-11-17", "hours": 12.0,
      "clock_in": "2026-11-17T07:00:00", "clock_out": "2026-11-17T19:00:00",
      "clock_out_date": "2026-11-17", "site": "", "notes": "",
      "matched": "A"}],
    [], "PrismXE", [])
check("patrol import entry jobsite None",
      to_add2 and to_add2[0].get("jobsite_id") is None, to_add2)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
