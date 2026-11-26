#!/usr/bin/env python3
"""Staging tests for the staffing clock-out upgrade (Part A + Part B).

Covers: onsite page renders with PWA tags + new modal, manifest + sw.js
served, clock-out stores meal break / signature / email opt-in, email
payload is formed correctly (captured, NOT actually sent), sync-clock
carries the new fields, backward compat (old clients without the new
fields), invalid signature is ignored, and the admin timecards page
shows the meal break + signature.

Run: ~/workspace/prismxe-app/venv/bin/python test_clockout_upgrade.py
"""
import base64
import io
import json
import os
import sys

sys.path.insert(0, "/tmp/cm_clockout_test")
os.chdir("/tmp/cm_clockout_test")

from PIL import Image, ImageDraw  # noqa: E402

import cm_bp  # noqa: E402
from flask import Flask  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + str(extra)) if extra and not cond else ""))


def make_sig_png():
    """A tiny fake signature PNG (a squiggle), as a data URL."""
    im = Image.new("RGB", (200, 60), "white")
    d = ImageDraw.Draw(im)
    d.line([(10, 40), (60, 15), (110, 45), (160, 20), (190, 35)],
           fill="black", width=3)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---- capture outgoing email instead of sending it ----
sent_emails = []
cm_bp.CM_SHEETS_URL = "https://example.invalid/hook"  # force the email path
cm_bp.sheets_post_async = lambda payload: sent_emails.append(payload)

app = Flask(__name__, template_folder="/tmp/cm_clockout_test/templates",
            static_folder="/tmp/cm_clockout_test/static")
app.register_blueprint(cm_bp.bp)
cm_bp.register_jinja(app)  # app.py does this at boot; provides worker_name etc.
client = app.test_client()

SIG = make_sig_png()

# ---------- Part B: PWA surfaces ----------
r = client.get("/staffing/onsite")
check("onsite 200", r.status_code == 200, r.status_code)
html = r.get_data(as_text=True)
for needle in ("manifest.json", "apple-touch-icon", "apple-mobile-web-app-capable",
               'id="sig-pad"', 'id="meal-seg"', 'id="co-step2"',
               'id="review-rows"', 'id="install-card"', 'id="ios-install-hint"',
               "beforeinstallprompt", "Email me a copy", "Meal break"):
    check("onsite contains " + needle, needle in html)

r = client.get("/static/cm/manifest.json")
check("manifest 200", r.status_code == 200, r.status_code)
man = json.loads(r.get_data(as_text=True))
check("manifest start_url", man.get("start_url") == "/staffing/onsite", man.get("start_url"))
check("manifest scope", man.get("scope") == "/staffing/", man.get("scope"))
check("manifest display standalone", man.get("display") == "standalone")
check("manifest has 192+512 icons",
      {i.get("sizes") for i in man.get("icons", [])} >= {"192x192", "512x512"})

r = client.get("/staffing/sw.js")
check("sw.js 200", r.status_code == 200, r.status_code)
check("sw cache v4", b"onsite-v4" in r.data)
check("sw caches manifest", b"manifest.json" in r.data)

for icon in ("icon-180.png", "icon-192.png", "icon-512.png"):
    r = client.get("/static/cm/" + icon)
    check(icon + " 200", r.status_code == 200, r.status_code)
    check(icon + " is png", r.data[:8] == b"\x89PNG\r\n\x1a\n")

# ---------- Part A: clock-out with the full new payload ----------
r = client.post("/staffing/api/direct-clock", json={
    "worker_id": "worker-test", "action": "clock_out",
    "lat": 33.4, "lng": -111.9, "reported_hours": 7.5,
    "meal_break": True, "signature": SIG, "email_opt_in": True})
d = r.get_json()
check("clock-out full payload 200/ok", r.status_code == 200 and d.get("ok"), d)
check("clock-out returns timecard_id", bool(d.get("timecard_id")), d)
check("clock-out returns meal_break", d.get("meal_break") is True, d)
check("clock-out returns signature path",
      (d.get("signature") or "").startswith("/static/cm/signatures/"), d)
tc = next(t for t in cm_bp.DB["timecards"] if t["id"] == d["timecard_id"])
check("timecard meal_break stored", tc.get("meal_break") is True)
check("timecard reported_hours stored", tc.get("reported_hours") == 7.5)
check("timecard email_opt_in stored", tc.get("email_opt_in") is True)
check("timecard status clocked_out", tc.get("status") == "clocked_out")
sig_path = "/tmp/cm_clockout_test" + tc["signature"]
check("signature file exists", os.path.exists(sig_path), sig_path)
check("signature file is a png",
      os.path.exists(sig_path) and open(sig_path, "rb").read(8) == b"\x89PNG\r\n\x1a\n")
r2 = client.get(tc["signature"])
check("signature served over HTTP", r2.status_code == 200, r2.status_code)

check("one email sent", len(sent_emails) == 1, len(sent_emails))
if sent_emails:
    em = sent_emails[0]
    check("email action sendEmail", em.get("action") == "sendEmail")
    check("email to info@ + worker",
          em.get("to") == "info@prismxe.com, worker@example.com", em.get("to"))
    check("email subject has worker+date",
          "Test Worker" in em.get("subject", "") and "2026-11-18" in em.get("subject", ""),
          em.get("subject"))
    body = em.get("body", "")
    for needle in ("Test Worker", "Test Site", "Meal break (1 hour): Yes",
                   "Hours reported by worker: 7.5h", "Signature: https://app.prismxe.com/static/cm/signatures/"):
        check("email body has " + needle[:28], needle in body, body[:200])
    check("email html has signature img",
          "<img" in em.get("html", "") and "static/cm/signatures/" in em.get("html", ""))

# ---------- meal break No + no email ----------
# open a fresh shift first
cm_bp.DB["timecards"].append({
    "id": "tc-open2", "worker_id": "worker-test", "site_id": "site-test",
    "clock_in": "2026-11-18T07:00:00", "clock_in_lat": 33.4, "clock_in_lng": -111.9,
    "clock_out": None, "clock_out_lat": None, "clock_out_lng": None,
    "hours": 0.0, "status": "clocked_in", "notes": "", "offline": False})
r = client.post("/staffing/api/direct-clock", json={
    "worker_id": "worker-test", "action": "clock_out",
    "lat": 33.4, "lng": -111.9, "reported_hours": 8,
    "meal_break": False, "signature": SIG, "email_opt_in": False})
d = r.get_json()
check("clock-out meal No ok", r.status_code == 200 and d.get("ok"), d)
tc2 = next(t for t in cm_bp.DB["timecards"] if t["id"] == d["timecard_id"])
check("meal_break False stored", tc2.get("meal_break") is False)
check("no email when opted out", len(sent_emails) == 1, len(sent_emails))

# ---------- backward compat: old client sends no new fields ----------
cm_bp.DB["timecards"].append({
    "id": "tc-open3", "worker_id": "worker-test", "site_id": "site-test",
    "clock_in": "2026-11-18T07:00:00", "clock_in_lat": 33.4, "clock_in_lng": -111.9,
    "clock_out": None, "clock_out_lat": None, "clock_out_lng": None,
    "hours": 0.0, "status": "clocked_in", "notes": "", "offline": False})
r = client.post("/staffing/api/direct-clock", json={
    "worker_id": "worker-test", "action": "clock_out",
    "lat": 33.4, "lng": -111.9})
d = r.get_json()
check("legacy clock-out ok", r.status_code == 200 and d.get("ok"), d)
tc3 = next(t for t in cm_bp.DB["timecards"] if t["id"] == d["timecard_id"])
check("legacy: no meal_break key", "meal_break" not in tc3)
check("legacy: no signature key", "signature" not in tc3)
check("legacy: no email sent", len(sent_emails) == 1, len(sent_emails))

# ---------- invalid signature is ignored, clock-out still succeeds ----------
cm_bp.DB["timecards"].append({
    "id": "tc-open4", "worker_id": "worker-test", "site_id": "site-test",
    "clock_in": "2026-11-18T07:00:00", "clock_in_lat": 33.4, "clock_in_lng": -111.9,
    "clock_out": None, "clock_out_lat": None, "clock_out_lng": None,
    "hours": 0.0, "status": "clocked_in", "notes": "", "offline": False})
r = client.post("/staffing/api/direct-clock", json={
    "worker_id": "worker-test", "action": "clock_out",
    "lat": 33.4, "lng": -111.9, "signature": "data:image/png;base64,!!!not-base64!!!"})
d = r.get_json()
check("bad signature still clocks out", r.status_code == 200 and d.get("ok"), d)
tc4 = next(t for t in cm_bp.DB["timecards"] if t["id"] == d["timecard_id"])
check("bad signature not stored", "signature" not in tc4)

# ---------- sync-clock (offline queue) carries the new fields ----------
cm_bp.DB["timecards"].append({
    "id": "tc-open5", "worker_id": "worker-test", "site_id": "site-test",
    "clock_in": "2026-11-18T07:00:00", "clock_in_lat": 33.4, "clock_in_lng": -111.9,
    "clock_out": None, "clock_out_lat": None, "clock_out_lng": None,
    "hours": 0.0, "status": "clocked_in", "notes": "", "offline": False})
r = client.post("/staffing/api/sync-clock", json={
    "worker_id": "worker-test", "action": "clock_out",
    "lat": 33.4, "lng": -111.9, "client_at": "2026-11-18T19:00:00Z",
    "reported_hours": 9, "meal_break": True, "signature": SIG,
    "email_opt_in": True})
d = r.get_json()
check("sync-clock ok", r.status_code == 200 and d.get("ok"), d)
tc5 = next(t for t in cm_bp.DB["timecards"] if t["id"] == d["timecard_id"])
check("sync-clock meal_break stored", tc5.get("meal_break") is True)
check("sync-clock signature stored", bool(tc5.get("signature")))
check("sync-clock offline flag", tc5.get("offline") is True)
check("sync-clock email sent", len(sent_emails) == 2, len(sent_emails))

# ---------- clock-in still works ----------
r = client.post("/staffing/api/direct-clock", json={
    "worker_id": "worker-test", "site_id": "site-test", "action": "clock_in",
    "lat": 33.4, "lng": -111.9})
d = r.get_json()
check("clock-in ok", r.status_code == 200 and d.get("ok"), d)

# ---------- admin timecards page shows meal break + signature ----------
r = client.get("/staffing/timecards")
check("timecards page 200", r.status_code == 200, r.status_code)
th = r.get_data(as_text=True)
check("timecards shows Meal break", "Meal break:" in th)
check("timecards shows signature img", "static/cm/signatures/" in th)

print()
print("PASSED %d  FAILED %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
