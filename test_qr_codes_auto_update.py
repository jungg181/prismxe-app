#!/usr/bin/env python3
"""QR codes page auto-updates: a new COA site or a new patrol company
shows up on /qr-codes with no edit to the page. Run:
venv/bin/python test_qr_codes_auto_update.py"""
import json
import os
import shutil
import sys

STAGING = "/home/hatch/workspace/prismxe-app-staging"
sys.path.insert(0, STAGING)

COA_P = os.path.join(STAGING, "data_coa.json")
BAK_P = COA_P + ".bak-qr-auto"
TEST_DB = os.path.join(STAGING, "data_patrol_testqr.json")

passed, failed = [], []

def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (
        (" | " + str(extra)) if extra and not cond else ""))

def seed_coa(sites):
    with open(COA_P, "w") as f:
        json.dump({"sites": sites, "workers": [], "timecards": []}, f)

shutil.copy(COA_P, BAK_P)
try:
    import app as appmod
    import patrol_bp as pb
    c = appmod.app.test_client()

    # ---- 1. Baseline: 1 COA site + 2 patrol companies = 5 cards ----
    seed_coa([{"id": "2fd3fd9b", "name": "LGES - Arizona",
               "status": "active"}])
    t = c.get("/qr-codes").get_data(as_text=True)
    check("baseline 5 QR cards", t.count('class="card"') == 5,
          t.count('class="card"'))
    check("baseline registry has 2 patrol instances",
          len(pb.PATROL_INSTANCES) == 2, len(pb.PATROL_INSTANCES))

    # ---- 2. A NEW COA site appears automatically ----
    seed_coa([
        {"id": "2fd3fd9b", "name": "LGES - Arizona", "status": "active"},
        {"id": "aabbccdd", "name": "New Test Site", "status": "active"},
    ])
    t = c.get("/qr-codes").get_data(as_text=True)
    check("new COA site adds a card (6 total)",
          t.count('class="card"') == 6, t.count('class="card"'))
    check("new COA site QR present",
          "/coa/qr/aabbccdd.png" in t
          and "https://app.prismxe.com/coa/clock?site=aabbccdd" in t
          and "New Test Site" in t)

    # ---- 3. A NEW patrol company appears automatically ----
    inst = pb.create_patrol_blueprint(
        company_id="testqr", url_prefix="/test/patrol",
        db_filename="data_patrol_testqr.json",
        sheets_env_var="TESTQR_SHEETS_URL",
        app_version_filename="app_version_testqr.json",
        blueprint_name="patrol_testqr",
        company_display_name="TestQR")
    t = c.get("/qr-codes").get_data(as_text=True)
    check("new patrol company adds a card (7 total)",
          t.count('class="card"') == 7, t.count('class="card"'))
    check("new patrol QR present",
          "/test/patrol/qr.png" in t
          and "https://app.prismxe.com/test/patrol/patrol-clock" in t
          and "TestQR Patrol" in t)
    check("existing patrol QRs still there",
          "/Hanseong/patrol/qr.png" in t
          and "/patrol-coa/qr.png" in t)
    # Clean up the test instance from the registry.
    pb.PATROL_INSTANCES.remove(inst)

    # ---- 4. Inactive COA site does NOT appear ----
    seed_coa([
        {"id": "2fd3fd9b", "name": "LGES - Arizona", "status": "active"},
        {"id": "deadbeef", "name": "Closed Site", "status": "inactive"},
    ])
    t = c.get("/qr-codes").get_data(as_text=True)
    check("inactive site adds no card (5 total)",
          t.count('class="card"') == 5, t.count('class="card"'))
    check("inactive site QR absent",
          "/coa/qr/deadbeef.png" not in t)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)
finally:
    shutil.copy(BAK_P, COA_P)
    os.remove(BAK_P)
    for p in (TEST_DB,
              os.path.join(STAGING, "app_version_testqr.json")):
        if os.path.exists(p):
            os.remove(p)
