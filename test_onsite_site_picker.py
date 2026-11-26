#!/usr/bin/env python3
"""On Site clock-in: the jobsite list is direct-click buttons fetched
live from /staffing/api/sites (a site added on /staffing/sites shows
up immediately, even when the phone serves the page from its offline
cache), and Clock In stays disabled until the worker taps a site.
Run: venv/bin/python test_onsite_site_picker.py"""
import copy
import json
import os
import sys

STAGING = "/home/hatch/workspace/prismxe-app-staging"
sys.path.insert(0, STAGING)

import cm_bp  # noqa: E402
from flask import Flask  # noqa: E402

passed, failed = [], []

def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (
        (" | " + str(extra)) if extra and not cond else ""))

app = Flask(__name__,
            template_folder=os.path.join(STAGING, "templates"),
            static_folder=os.path.join(STAGING, "static"))
app.register_blueprint(cm_bp.bp)
cm_bp.register_jinja(app)
c = app.test_client()

orig_sites = copy.deepcopy(cm_bp.DB.get("sites", []))
try:
    cm_bp.DB["sites"] = [
        {"id": "s-lges", "name": "LGES", "status": "active"},
        {"id": "s-dry", "name": "COA (Drywall)", "status": "active"},
        {"id": "s-old", "name": "Closed Site", "status": "on hold"},
    ]

    # ---- 1. The API returns active sites only ----
    r = c.get("/staffing/api/sites")
    check("api/sites 200", r.status_code == 200, r.status_code)
    d = json.loads(r.get_data(as_text=True))
    names = [s["name"] for s in d.get("sites", [])]
    check("api/sites lists active sites",
          names == ["LGES", "COA (Drywall)"], names)
    check("api/sites hides non-active sites",
          "Closed Site" not in names)

    # ---- 2. A site added on /staffing/sites appears immediately ----
    cm_bp.DB["sites"].append(
        {"id": "s-new", "name": "Brand New Site", "status": "active"})
    r = c.get("/staffing/api/sites")
    d = json.loads(r.get_data(as_text=True))
    names = [s["name"] for s in d.get("sites", [])]
    check("new site appears in api/sites with no restart",
          "Brand New Site" in names, names)

    # ---- 3. The onsite page: buttons, not a dropdown ----
    r = c.get("/staffing/onsite")
    t = r.get_data(as_text=True)
    check("onsite 200", r.status_code == 200, r.status_code)
    check("onsite has site buttons container",
          'id="dc-site-btns"' in t)
    check("onsite fetches the site list live",
          "fetch('/staffing/api/sites')" in t)
    check("onsite has an offline fallback list",
          "SITES_FALLBACK" in t)
    check("onsite has NO Jobsite dropdown anymore",
          'id="dc-site"' not in t
          and '<select id="dc-site"' not in t)
    check("onsite prompts to tap a site first",
          "Where are you working?" in t)
    check("Clock In button starts disabled",
          'id="dc-in-btn"' in t
          and 'onclick="directClockIn()" disabled' in t)
    check("clock-in reads the tapped site",
          "const siteId = curSite;" in t)
    check("site tap enables Clock In",
          "btn.disabled = !curSite" in t)

    # ---- 4. Service worker cache bumped so phones refetch ----
    r = c.get("/staffing/sw.js")
    check("sw.js 200", r.status_code == 200, r.status_code)
    check("sw cache is v4", b"onsite-v4" in r.data)
    check("sw does NOT cache the sites API",
          b"api/sites" not in r.data)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)
finally:
    cm_bp.DB["sites"] = orig_sites
