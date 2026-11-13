"""Staging test: Payroll Part 1 (2026-11-13) — CM payroll calendar
retired into /payroll/.

Covers:
- GET /staffing/timecards/payroll 302-redirects to /payroll/ (not 404,
  not the old calendar); its email + export routes redirect too.
- Per-employee calendar on the payroll employee page: Monday-first
  grid, per-day hours from data_payroll.json entries, first clock-in
  – last clock-out, totals + days worked, open shifts marked.
  Isaac Oct 29 shows 8h (payroll source), never the 94.9h CM anomaly.
- "Email this payroll" form present; POST creates a review token
  (send itself needs the Apps Script URL, so it reports failure
  here and the token still exists).
- Payroll-native review/correction flow: worker corrects a day,
  correction is stored, admin page lists it, Apply updates the
  payroll entry (hours + clock-out) and marks it applied.
- The CM Adrian correction is untouched: still pending on the CM
  corrections page and its token review page still loads.
- /staffing/workers and /staffing/timecards still 200.
"""
import json
import os
import shutil
import sys
import urllib.parse

STAGING = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, STAGING)

DATA_P = os.path.join(STAGING, "data_payroll.json")
BAK_P = DATA_P + ".bak-cal-test"
DATA_C = os.path.join(STAGING, "data_cm.json")
BAK_C = DATA_C + ".bak-cal-test"

ADRIAN_TOKEN = "test-token-adrian-0001"


def entry(eid, employee, date, hours, cin, cout, site="LGES"):
    return {"id": eid, "company": "PrismXE", "employee": employee,
            "date": date, "payroll_hours": hours,
            "clock_in": cin, "clock_out": cout,
            "clock_out_date": None, "gross_hours": None,
            "site": site, "notes": "CM timecard", "jobsite_id": None}


seed_payroll = {
    "companies": [
        {"name": "PrismXE", "slug": "prismxe", "prefix": "PX",
         "vendor": "Prism", "ship_to": "", "category": ""},
    ],
    "rates": [],
    "entries": [
        entry("e-arg-1", "Argenis Cabana", "2026-09-21", 8.0,
              "7:00 AM", "4:00 PM"),
        entry("e-arg-2", "Argenis Cabana", "2026-09-22", 8.0,
              "7:00 AM", "4:00 PM"),
        entry("e-arg-3", "Argenis Cabana", "2026-09-23", 8.0,
              "7:00 AM", "4:00 PM"),
        # Open shift: clocked in, no clock-out yet.
        entry("e-arg-4", "Argenis Cabana", "2026-09-24", 0,
              "7:00 AM", ""),
        entry("e-isa-1", "Isaac Marino", "2026-10-29", 8,
              "6:41 AM", "3:41 PM", site="Office"),
        entry("e-adr-1", "Adrian zamora", "2026-10-01", 8.0,
              "7:00 AM", "4:00 PM"),
        entry("e-adr-2", "Adrian zamora", "2026-10-02", 8.1,
              "7:04 AM", "4:10 PM"),
        entry("e-adr-3", "Adrian zamora", "2026-10-03", 6.81,
              "7:02 AM", "2:51 PM"),
    ],
    "expenses": [], "settings": {}, "aliases": [], "variances": {},
    "jobsites": [], "jobsite_costs": [], "invoices": [],
    "payroll_review_tokens": [], "payroll_corrections": [],
}

seed_cm = {
    "sites": [{"id": "site-1", "name": "LGES", "lat": 33.28,
               "lng": -111.55, "address": "Queen Creek, AZ", "pm": "Jake",
               "start_date": "2024-11-01", "target_date": "2026-12-12",
               "status": "active"}],
    "workers": [
        {"id": "w-adrian", "name": "Adrian zamora",
         "email": "adrian@x.us", "phone": "123", "company": "KSR",
         "trade": "General Labor", "employment_type": "Contract",
         "hourly_rate": 30.0, "site_id": "site-1", "status": "active",
         "emergency_contact": "", "photo_url": "",
         "created_at": "2026-10-01T00:00:00"},
    ],
    "timecards": [
        {"id": "tc-adr", "worker_id": "w-adrian", "site_id": "site-1",
         "clock_in": "2026-10-03T07:02:00", "clock_in_lat": 33.28,
         "clock_in_lng": -111.55, "clock_out": "2026-10-03T14:51:00",
         "clock_out_lat": 33.28, "clock_out_lng": -111.55,
         "hours": 6.81, "status": "clocked_out", "notes": "",
         "offline": False},
    ],
    "incidents": [], "daily_logs": [], "labour": [], "materials": [],
    "defects": [], "qa_checks": [], "pins": [], "plans": [],
    "qr_tokens": [], "snapshots": [], "trash": [], "dispatches": [],
    "patrol_punches": [], "live_locations": {},
    "payroll_review_tokens": [
        {"id": "6593861f", "token": ADRIAN_TOKEN,
         "worker_id": "w-adrian", "start": "2026-10-01",
         "end": "2026-10-15", "created_at": "2026-10-29T13:00:00"},
    ],
    "payroll_corrections": [
        {"id": "7c49cecc6773", "token": ADRIAN_TOKEN,
         "worker_id": "w-adrian", "worker_name": "Adrian zamora",
         "start": "2026-10-01", "end": "2026-10-15",
         "status": "correction", "note": "", "applied_at": None,
         "submitted_at": "2026-10-29T13:51:20",
         "days": [{"date": "2026-10-03", "orig_in": "07:02",
                   "orig_out": "14:51", "orig_hours": "6.81",
                   "new_in": "07:02", "new_out": "18:51",
                   "new_hours": "10.82"}]},
    ],
}

shutil.copy(DATA_P, BAK_P)
shutil.copy(DATA_C, BAK_C)
try:
    with open(DATA_P, "w") as f:
        json.dump(seed_payroll, f)
    with open(DATA_C, "w") as f:
        json.dump(seed_cm, f)

    from flask import Flask
    import cm_bp as cm
    import payroll_bp as pb

    app = Flask(__name__, template_folder=os.path.join(STAGING, "templates"))
    app.secret_key = "test-secret"
    app.jinja_env.globals.update(
        worker_name=lambda wid: "Adrian zamora",
        site_name=lambda sid: "LGES",
        compute_hours=cm.compute_hours,
        fmt_dt=lambda iso: iso or "",
        clock_in_distance_km=lambda t: None,
        gmaps_link=lambda a, b: None,
    )
    app.register_blueprint(cm.bp, url_prefix="/staffing")
    app.register_blueprint(pb.bp, url_prefix="/payroll")
    c = app.test_client()

    passed, failed = [], []

    def check(name, cond, extra=""):
        (passed if cond else failed).append(name)
        print(("PASS " if cond else "FAIL ") + name + (
            (" | " + str(extra)) if extra and not cond else ""))

    def emp_url(name, company="prismxe", **qs):
        q = urllib.parse.urlencode({"name": name, **qs})
        return f"/payroll/{company}/employee?{q}"

    # ---- 1. Retired CM payroll surface redirects ----
    r = c.get("/staffing/timecards/payroll")
    check("CM payroll calendar 302 to /payroll/",
          r.status_code == 302
          and r.headers.get("Location", "") == "/payroll/",
          f"{r.status_code} {r.headers.get('Location')}")
    r = c.post("/staffing/timecards/payroll/email", data={})
    check("CM payroll email POST redirects",
          r.status_code == 302, r.status_code)
    r = c.get("/staffing/timecards/payroll/export")
    check("CM payroll CSV export redirects",
          r.status_code == 302, r.status_code)
    r = c.get("/staffing/timecards/payroll/export.xlsx")
    check("CM payroll XLSX export redirects",
          r.status_code == 302, r.status_code)

    # ---- 2. Per-employee calendar on the payroll employee page ----
    t = c.get(emp_url("Argenis Cabana")).get_data(as_text=True)
    check("employee page 200", "Argenis Cabana" in t)
    check("calendar heading present", "Payroll calendar" in t)
    check("calendar is Monday-first",
          all(d in t for d in ("Mon", "Tue", "Wed", "Thu", "Fri",
                               "Sat", "Sun")))
    check("calendar shows per-day in-out",
          "7:00 AM" in t and "4:00 PM" in t)
    check("calendar shows per-day hours", "8h" in t)
    check("calendar total 24h", "24h" in t)
    check("open shift marked On shift", "On shift" in t)
    check("Email this payroll present", "Email this payroll" in t)
    check("email form posts to calendar email",
          "/payroll/prismxe/employee/calendar/email" in t)
    check("corrections link present", "/payroll/corrections" in t)

    # ---- 3. Payroll source, not the CM anomaly ----
    t = c.get(emp_url("Isaac Marino", **{"from": "2026-10-29",
                                         "to": "2026-10-29"})
              ).get_data(as_text=True)
    check("Isaac Oct 29 shows 8h (payroll source)",
          "8h" in t and "6:41 AM" in t)
    check("Isaac Oct 29 never shows 94.9h", "94.9" not in t)

    # ---- 4. Email creates a review token (send needs Apps Script) ----
    r = c.post("/payroll/prismxe/employee/calendar/email", data={
        "name": "Adrian zamora", "start": "2026-10-01",
        "end": "2026-10-15", "to": "adrian@x.us",
        "subject": "Payroll summary Oct 1 – Oct 15, 2026",
        "body": "Hi Adrian,"}, follow_redirects=False)
    check("calendar email POST redirects to employee page",
          r.status_code == 302
          and "employee" in r.headers.get("Location", ""), r.status_code)
    toks = pb.DB.get("payroll_review_tokens", [])
    check("review token created",
          len(toks) == 1 and toks[0].get("employee") == "Adrian zamora",
          toks)
    token = toks[0]["token"] if toks else ""
    t = c.get(f"/payroll/review/{token}").get_data(as_text=True)
    check("payroll review page 200", "Adrian zamora" in t)
    check("review page shows Oct 3 original times",
          "7:02 AM" in t and "2:51 PM" in t)
    check("review page shows Oct 3 6.81h", "6.81h" in t)

    # ---- 5. Worker correction -> stored, listed, applied ----
    r = c.post(f"/payroll/review/{token}", data={
        "action": "correct",
        "in_2026-10-01": "07:00", "out_2026-10-01": "16:00",
        "in_2026-10-02": "07:04", "out_2026-10-02": "16:10",
        "in_2026-10-03": "07:02", "out_2026-10-03": "18:51",
        "note": "stayed late"}, follow_redirects=True)
    check("correction submit 200", r.status_code == 200, r.status_code)
    recs = pb.DB.get("payroll_corrections", [])
    check("payroll correction stored",
          len(recs) == 1 and recs[0]["status"] == "correction", recs)
    check("correction has the Oct 3 change",
          recs and recs[0]["days"]
          and recs[0]["days"][0]["new_hours"] == "10.82", recs)
    t = c.get("/payroll/corrections").get_data(as_text=True)
    check("payroll corrections page 200",
          "Adrian zamora" in t and "10.82" in t)
    cid = recs[0]["id"] if recs else ""
    r = c.post(f"/payroll/corrections/{cid}/apply", follow_redirects=True)
    check("apply 200", r.status_code == 200, r.status_code)
    e3 = next(e for e in pb.DB["entries"] if e["id"] == "e-adr-3")
    check("apply updates payroll hours to 10.82",
          e3["payroll_hours"] == 10.82, e3)
    check("apply updates clock-out to 6:51 PM",
          e3["clock_out"] == "6:51 PM", e3)
    check("correction marked applied",
          recs[0]["status"] == "applied"
          and recs[0].get("applied_at"), recs[0])
    t = c.get(emp_url("Adrian zamora", **{"from": "2026-10-03",
                                          "to": "2026-10-03"})
              ).get_data(as_text=True)
    check("calendar reflects applied correction",
          "10.82h" in t, t[:200])

    # ---- 6. CM Adrian correction untouched ----
    t = c.get("/staffing/timecards/payroll/corrections"
              ).get_data(as_text=True)
    check("CM corrections page still 200",
          "Adrian zamora" in t)
    check("CM Adrian correction still pending",
          "10.82" in t and "correction" in t)
    check("CM Adrian correction NOT applied in CM DB",
          cm.DB["payroll_corrections"][0]["status"] == "correction"
          and cm.DB["payroll_corrections"][0]["applied_at"] is None,
          cm.DB["payroll_corrections"])
    t = c.get(f"/staffing/payroll/review/{ADRIAN_TOKEN}"
              ).get_data(as_text=True)
    check("CM Adrian token review page still 200",
          "Adrian zamora" in t and "6.81h" in t)

    # ---- 7. CM workers/timecards + payroll index still work ----
    check("workers page 200",
          c.get("/staffing/workers").status_code == 200)
    r = c.get("/staffing/timecards")
    check("timecards page 200", r.status_code == 200, r.status_code)
    check("timecards Payroll report button present",
          "Payroll report" in r.get_data(as_text=True))
    check("payroll index 200", c.get("/payroll/").status_code == 200)
    check("payroll company page 200",
          c.get("/payroll/prismxe").status_code == 200)
    check("payroll corrections nav present",
          "/payroll/corrections" in c.get("/payroll/").get_data(as_text=True))

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)
    print("ALL PAYROLL CALENDAR CHECKS PASSED")
finally:
    shutil.copy(BAK_P, DATA_P)
    os.remove(BAK_P)
    shutil.copy(BAK_C, DATA_C)
    os.remove(BAK_C)
