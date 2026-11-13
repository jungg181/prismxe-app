"""Staging test: Part 3 staffing cleanup (2026-11-13).

Verifies: 6 nav items removed + redirects, QR clock + dispatch API
removed, direct-clock endpoint added, attention badge = real problems
only, dashboard trimmed, COA QR link off CM sites, snapshots gone
from /data. Payroll calendar route untouched.
"""
import json
import os
import shutil
import sys

STAGING = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, STAGING)

DATA = os.path.join(STAGING, "data_cm.json")
BAK = DATA + ".bak-part3-test"

seed = {
    "sites": [{"id": "site-1", "name": "LGES", "lat": 33.28, "lng": -111.55,
               "address": "Queen Creek, AZ", "pm": "Jake",
               "start_date": "2024-11-01", "target_date": "2026-12-12",
               "status": "active"}],
    "workers": [
        {"id": "w1", "name": "Test One", "email": "test1@x.us",
         "phone": "123", "company": "KSR", "trade": "Electrician",
         "employment_type": "Contract", "hourly_rate": 30.0,
         "site_id": "site-1", "status": "active",
         "emergency_contact": "", "photo_url": "",
         "created_at": "2026-10-01T00:00:00"},
        {"id": "w2", "name": "Test Two", "email": "test2@x.us",
         "phone": "124", "company": "KSR", "trade": "Carpenter",
         "employment_type": "Contract", "hourly_rate": 30.0,
         "site_id": "site-1", "status": "active",
         "emergency_contact": "", "photo_url": "",
         "created_at": "2026-10-01T00:00:00"},
        {"id": "w3", "name": "Test Three", "email": "test3@x.us",
         "phone": "125", "company": "KSR", "trade": "Laborer",
         "employment_type": "Contract", "hourly_rate": 30.0,
         "site_id": "site-1", "status": "active",
         "emergency_contact": "", "photo_url": "",
         "created_at": "2026-10-01T00:00:00"},
    ],
    "timecards": [
        # w1: still open (1 open shift)
        {"id": "tc-open", "worker_id": "w1", "site_id": "site-1",
         "clock_in": "2026-11-13T07:00:00", "clock_in_lat": 33.28,
         "clock_in_lng": -111.55, "clock_out": None,
         "clock_out_lat": None, "clock_out_lng": None,
         "hours": 0.0, "status": "clocked_in", "notes": "",
         "offline": False},
        # w1: clocked in 7.8km away (1 GPS warning), clocked out long ago
        {"id": "tc-far", "worker_id": "w1", "site_id": "site-1",
         "clock_in": "2026-10-20T07:00:00", "clock_in_lat": 33.35,
         "clock_in_lng": -111.55, "clock_out": "2026-10-20T16:00:00",
         "clock_out_lat": 33.35, "clock_out_lng": -111.55,
         "hours": 8.0, "status": "clocked_out", "notes": "",
         "offline": False},
        # w1: normal clocked-out shift (must NOT count as a problem)
        {"id": "tc-ok", "worker_id": "w1", "site_id": "site-1",
         "clock_in": "2026-10-21T07:00:00", "clock_in_lat": 33.28,
         "clock_in_lng": -111.55, "clock_out": "2026-10-21T16:00:00",
         "clock_out_lat": 33.28, "clock_out_lng": -111.55,
         "hours": 8.0, "status": "clocked_out", "notes": "",
         "offline": False},
    ],
    "incidents": [], "daily_logs": [], "labour": [], "materials": [],
    "defects": [], "qa_checks": [], "pins": [], "plans": [], "qr_tokens": [],
    "snapshots": [], "trash": [], "dispatches": [], "patrol_punches": [],
    "live_locations": {}, "payroll_review_tokens": [],
    "payroll_corrections": [
        {"id": "c1", "worker_name": "Test One", "status": "correction",
         "applied_at": None, "start": "2026-10-01", "end": "2026-10-15",
         "days": []},
    ],
}

shutil.copy(DATA, BAK)
passed, failed = [], []
try:
    with open(DATA, "w") as f:
        json.dump(seed, f)

    from flask import Flask
    import cm_bp as cm

    app = Flask(__name__, template_folder=os.path.join(STAGING, "templates"))
    app.secret_key = "test-secret"
    app.jinja_env.globals.update(
        worker_name=lambda wid: "Test One",
        site_name=lambda sid: "LGES",
        compute_hours=cm.compute_hours,
        fmt_dt=lambda iso: iso or "",
        clock_in_distance_km=cm.clock_in_distance_km,
        gmaps_link=lambda a, b: None,
    )
    app.register_blueprint(cm.bp, url_prefix="/staffing")
    c = app.test_client()

    def check(name, cond, extra=""):
        (passed if cond else failed).append(name)
        print(("PASS " if cond else "FAIL ") + name +
              ((" | " + str(extra)) if extra and not cond else ""))

    # ---- 1. Nav: 6 items gone, kept items present ----
    r = c.get("/staffing/")
    t = r.get_data(as_text=True)
    check("dashboard 200", r.status_code == 200, r.status_code)
    # Nav links (hrefs) to the removed modules must be gone. Note: the
    # Sites table still has an "Incidents" column header — that is kept
    # Sites content, so assert on hrefs, not on the bare word.
    for gone in ["/staffing/incidents", "/staffing/daily-logs",
                 "/staffing/labour", "/staffing/materials",
                 "/staffing/qa", "/staffing/blueprints"]:
        check(f"nav has no link {gone}", f'href="{gone}"' not in t)
    for gone in ["Daily Logs", "Labour", "Materials",
                 "QA/QC", "Map & Plans"]:
        check(f"nav has no {gone}", gone not in t)
    for kept in ["Dashboard", "Sites", "Workers", "Timecards", "Data"]:
        check(f"nav keeps {kept}", kept in t)

    # ---- 4. Dashboard trimmed ----
    for gone in ["Open incidents", "Open defects", "Low stock items",
                 "Recent incidents", "Overdue defects",
                 "No incidents reported", "No overdue defects"]:
        check(f"dashboard hides {gone!r}", gone not in t)
    check("dashboard keeps On site today", "On site today" in t)
    check("dashboard keeps Sites section", "LGES" in t)

    # ---- 3. Attention badge = real problems only: 1 open + 1 GPS + 1 correction = 3 ----
    r = c.get("/staffing/timecards")
    t = r.get_data(as_text=True)
    check("timecards 200", r.status_code == 200, r.status_code)
    check("badge says 3 need attention", "3 need attention" in t, t[:400])
    check("badge does not say 149", "149 need attention" not in t)

    # ---- 5. COA QR link off CM sites ----
    r = c.get("/staffing/sites")
    t = r.get_data(as_text=True)
    check("sites 200", r.status_code == 200, r.status_code)
    check("sites has no COA Clock QR", "COA Clock QR" not in t)
    check("sites has no /coa/qr link", "/coa/qr/" not in t)
    check("sites has no Site Map & Plans", "Site Map" not in t)

    # ---- 6. Data page: snapshots gone, trash + backup kept ----
    r = c.get("/staffing/data")
    t = r.get_data(as_text=True)
    check("data 200", r.status_code == 200, r.status_code)
    check("data has no Saved Snapshots", "Saved Snapshots" not in t)
    check("data has no snapshot form", "data_snapshot" not in t and
          "Save Data Snapshot" not in t)
    check("data keeps Download JSON Backup", "Download JSON Backup" in t)
    check("data keeps Recycle Bin", "Recycle Bin" in t)

    # ---- 1. Removed routes redirect to /staffing/ ----
    for path in ["/staffing/incidents", "/staffing/daily-logs",
                 "/staffing/labour", "/staffing/materials",
                 "/staffing/qa", "/staffing/blueprints"]:
        r = c.get(path, follow_redirects=False)
        check(f"{path} redirects to /staffing/",
              r.status_code in (301, 302) and
              r.headers.get("Location", "").endswith("/staffing/"),
              f"{r.status_code} {r.headers.get('Location')}")
    r = c.get("/staffing/clock/abc123", follow_redirects=False)
    check("/staffing/clock/<token> redirects to /staffing/onsite",
          r.status_code in (301, 302) and
          r.headers.get("Location", "").endswith("/staffing/onsite"),
          f"{r.status_code} {r.headers.get('Location')}")

    # ---- 2. Onsite kept, QR gone from it ----
    r = c.get("/staffing/onsite")
    t = r.get_data(as_text=True)
    check("onsite 200", r.status_code == 200, r.status_code)
    check("onsite has Clock In", "Clock In" in t)
    check("onsite has no Scan QR", "Scan QR" not in t)
    check("onsite has no html5-qrcode", "html5-qrcode" not in t)
    check("onsite has no api/qr", "api/qr" not in t)
    check("onsite has no scan-clock", "scan-clock" not in t)

    # ---- 2. Removed APIs are gone (404) ----
    r = c.post("/staffing/api/dispatch", json={"worker_id": "w1",
                                               "message": "hi"})
    check("POST api/dispatch 404", r.status_code == 404, r.status_code)
    r = c.post("/staffing/api/qr", json={"site_id": "site-1"})
    check("POST api/qr 404", r.status_code == 404, r.status_code)
    r = c.get("/staffing/api/qr-info/abc")
    check("GET api/qr-info 404", r.status_code == 404, r.status_code)
    r = c.post("/staffing/api/scan-clock", json={"token": "abc",
                                                 "worker_id": "w1"})
    check("POST api/scan-clock 404", r.status_code == 404, r.status_code)
    r = c.get("/staffing/qr/abc.png")
    check("GET qr png 404", r.status_code == 404, r.status_code)
    r = c.post("/staffing/data/snapshot", data={"name": "x"})
    check("POST data/snapshot 404", r.status_code == 404, r.status_code)

    # ---- 2. Direct clock (new): clock in + clock out with reported hours ----
    r = c.post("/staffing/api/direct-clock",
               json={"worker_id": "w2", "site_id": "site-1",
                     "action": "clock_in", "lat": 33.28, "lng": -111.55})
    d = r.get_json()
    check("direct clock-in 200 ok", r.status_code == 200 and d.get("ok"),
          f"{r.status_code} {d}")
    r = c.post("/staffing/api/direct-clock",
               json={"worker_id": "w2", "site_id": "site-1",
                     "action": "clock_in", "lat": 33.28, "lng": -111.55})
    check("double clock-in rejected", r.status_code == 400, r.status_code)
    r = c.post("/staffing/api/direct-clock",
               json={"worker_id": "w2", "action": "clock_out",
                     "lat": 33.28, "lng": -111.55, "reported_hours": 7.5})
    d = r.get_json()
    check("direct clock-out 200 ok", r.status_code == 200 and d.get("ok"),
          f"{r.status_code} {d}")
    check("clock-out returns reported_hours",
          d.get("reported_hours") == 7.5, d)
    live = json.load(open(DATA))
    tc = [x for x in live["timecards"]
          if x["worker_id"] == "w2" and x["status"] == "clocked_out"]
    check("clock-out stored reported_hours",
          tc and tc[0].get("reported_hours") == 7.5, tc)

    # ---- 2. Offline sync without a token now works ----
    r = c.post("/staffing/api/sync-clock",
               json={"worker_id": "w3", "site_id": "site-1",
                     "action": "clock_in", "lat": 33.28, "lng": -111.55,
                     "client_at": "2026-11-13T07:05:00"})
    d = r.get_json()
    check("sync-clock (no token) 200 ok",
          r.status_code == 200 and d.get("ok"), f"{r.status_code} {d}")

    # ---- Payroll calendar retired (Part 1, 2026-11-13) ----
    # /staffing/timecards/payroll now 302-redirects to /payroll/;
    # the per-employee calendar lives on the payroll employee page.
    r = c.get("/staffing/timecards/payroll")
    check("payroll calendar redirects to /payroll/",
          r.status_code == 302 and r.headers.get("Location", "") == "/payroll/",
          f"{r.status_code} {r.headers.get('Location')}")
    r = c.get("/staffing/timecards/payroll/export")
    check("payroll CSV export redirects",
          r.status_code == 302, r.status_code)
    r = c.get("/staffing/timecards/payroll/export.xlsx")
    check("payroll XLSX export redirects",
          r.status_code == 302, r.status_code)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)
    print("ALL PART3 CHECKS PASSED")
finally:
    shutil.copy(BAK, DATA)
    os.remove(BAK)
