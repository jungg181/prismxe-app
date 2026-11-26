"""Construction Manager - standalone site management web app (Flask)."""
import os
import io
import csv
import base64
import json
import copy
import uuid
import threading
import secrets
import time
import urllib.request
from datetime import datetime, date, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for,
                   jsonify, send_file, abort, flash)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data_cm.json')
UPLOAD_DIR = os.path.join(BASE_DIR, 'static', 'cm', 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

bp = Blueprint("cm", __name__, url_prefix="/staffing",
               template_folder="templates/cm")
BASE_URL = os.environ.get("BASE_URL", "https://app.prismxe.com")

COLLECTIONS = ['sites', 'workers', 'timecards', 'incidents', 'daily_logs',
               'labour', 'materials', 'defects', 'qa_checks', 'pins',
               'plans', 'qr_tokens', 'snapshots', 'trash', 'dispatches',
               'patrol_punches']

# Shared key for the patrol-board relay API (read-only punch feed).
PATROL_API_KEY = os.environ.get('PATROL_API_KEY', 'ef2eaa7a03f346115f876731a1bd02ee')

# Google Sheets auto-sync — survives Render deploys (ephemeral disk).
# Set CM_SHEETS_URL env var to the Apps Script web app URL
# (see sheets_apps_script_cm.js for setup instructions).
CM_SHEETS_URL = os.environ.get("CM_SHEETS_URL", "")


def sheets_post(payload, timeout=12):
    """Best-effort POST to the Google Sheets webhook. Never raises."""
    if not CM_SHEETS_URL:
        return None
    try:
        req = urllib.request.Request(
            CM_SHEETS_URL, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"cm sheets_post failed: {e}", flush=True)
        return None


def sheets_get_data(timeout=15):
    """Fetch the persisted CM DB from Google Sheets. Returns dict or None."""
    if not CM_SHEETS_URL:
        return None
    try:
        with urllib.request.urlopen(CM_SHEETS_URL + "?action=getData",
                                    timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("data")
    except Exception as e:  # noqa: BLE001
        print(f"cm sheets_get_data failed: {e}", flush=True)
        return None


# --- Durable-restore state (added 2026-10-24) --------------------------------
# Render's disk is wiped on every deploy, so the Sheets copy is the real data.
# The old boot path tried ONE 15s fetch and silently seeded demo workers when
# it failed (and a later save could then push the demo data over the good
# Sheets copy). Now: retry, never seed demo data when a Sheets URL exists,
# block Sheets sync until a restore has succeeded, and keep retrying in the
# background.
RESTORE_OK = False
DB_DIRTY = False
_restore_lock = threading.Lock()


def restore_from_sheets(attempts=2, timeout=12):
    """Synchronous best-effort restore used at boot. Returns dict or None."""
    for i in range(attempts):
        data = sheets_get_data(timeout=timeout)
        if data and isinstance(data, dict):
            return data
        if i + 1 < attempts:
            time.sleep(2)
    return None


def _bg_restore():
    """Keep retrying the Sheets restore after a failed boot restore.

    Applies the restored data only if nothing was changed locally in the
    meantime, and re-enables Sheets sync once data is back."""
    global RESTORE_OK
    for _ in range(30):
        time.sleep(20)
        if RESTORE_OK:
            return
        data = sheets_get_data(timeout=20)
        if data and isinstance(data, dict):
            with _restore_lock:
                if DB_DIRTY:
                    print("cm: sheets data found but local changes exist; "
                          "keeping local DB, sync stays OFF", flush=True)
                    return
                for c in COLLECTIONS:
                    data.setdefault(c, [])
                DB.clear()
                DB.update(data)
                RESTORE_OK = True
                try:
                    with open(DB_PATH, 'w') as f:
                        json.dump(DB, f, indent=2)
                except Exception as e:  # noqa: BLE001
                    print(f"cm: bg restore file write failed: {e}", flush=True)
                print("cm: restored DB from Google Sheets (background)", flush=True)
            return
    print("cm: background restore gave up; Sheets sync stays OFF", flush=True)


def sheets_post_async(payload):
    threading.Thread(target=sheets_post, args=(payload,), daemon=True).start()


def sheets_sync_db():
    """Push the full CM DB to Google Sheets (async, best-effort)."""
    if not CM_SHEETS_URL:
        return
    try:
        # Copy to avoid mutation during async JSON encoding
        data = json.loads(json.dumps(DB))
        sheets_post_async({"action": "saveData", "data": data})
    except Exception as e:  # noqa: BLE001
        print(f"cm sheets_sync_db failed: {e}", flush=True)


def new_id():
    return uuid.uuid4().hex[:8]


def now_iso():
    return datetime.now().isoformat(timespec='seconds')


def today():
    return date.today()


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def seed(db):
    db['sites'] = [
        {'id': 'site-lges', 'name': 'LGES', 'photo_url': '',
         'lat': 33.28844, 'lng': -111.55389,
         'address': "33°17'18.4\"N 111°33'14.0\"W, Queen Creek, AZ",
         'pm': 'Jake', 'start_date': '2024-11-01',
         'target_date': '2026-12-12', 'status': 'active'},
        {'id': 'site-office', 'name': 'Office', 'photo_url': '',
         'lat': None, 'lng': None, 'address': '',
         'pm': '', 'start_date': '', 'target_date': '', 'status': 'active'},
    ]
    db['workers'] = [
        {'id': 'wkr-demo1', 'name': 'Alex Rivera', 'email': 'alex@example.com',
         'phone': '+14805550101', 'trade': 'Electrician',
         'employment_type': 'Direct Team', 'hourly_rate': 32.0,
         'site_id': 'site-lges', 'status': 'active',
         'emergency_contact': 'Maria Rivera', 'photo_url': '',
         'created_at': now_iso()},
        {'id': 'wkr-demo2', 'name': 'Sam Chen', 'email': 'sam@example.com',
         'phone': '+14805550102', 'trade': 'Carpenter',
         'employment_type': 'Contract', 'hourly_rate': 28.5,
         'site_id': 'site-lges', 'status': 'active',
         'emergency_contact': 'Li Chen', 'photo_url': '',
         'created_at': now_iso()},
        {'id': 'wkr-demo3', 'name': 'Jordan Lee', 'email': 'jordan@example.com',
         'phone': '+14805550103', 'trade': 'Laborer',
         'employment_type': 'Contract', 'hourly_rate': 24.0,
         'site_id': 'site-office', 'status': 'on_leave',
         'emergency_contact': 'Pat Lee', 'photo_url': '',
         'created_at': now_iso()},
    ]


def load_db():
    global RESTORE_OK
    if not os.path.exists(DB_PATH):
        # Fresh deploy (Render wiped the disk) — restore from Google Sheets.
        if CM_SHEETS_URL:
            restored = restore_from_sheets()
            if restored and isinstance(restored, dict):
                db = restored
                for c in COLLECTIONS:
                    db.setdefault(c, [])
                RESTORE_OK = True
                print("cm: restored DB from Google Sheets", flush=True)
            else:
                # NEVER seed demo data over a real deployment: boot empty,
                # keep Sheets sync OFF, and let the background thread retry.
                db = {c: [] for c in COLLECTIONS}
                RESTORE_OK = False
                print("cm: Sheets restore failed at boot; starting empty, "
                      "background restore running, sync OFF", flush=True)
                threading.Thread(target=_bg_restore, daemon=True).start()
        else:
            # No Sheets configured (fresh local install): demo seed is fine.
            db = {c: [] for c in COLLECTIONS}
            seed(db)
            RESTORE_OK = True
        with open(DB_PATH, 'w') as f:
            json.dump(db, f, indent=2)
        # Apply backfills below by falling through (don't return early)
    else:
        with open(DB_PATH) as f:
            db = json.load(f)
        for c in COLLECTIONS:
            db.setdefault(c, [])
    db.setdefault('live_locations', {})  # worker_id -> {lat, lng, at, accuracy}
    for w in db.get('workers', []):
        w.setdefault('self_registered', False)
    for t in db.get('qr_tokens', []):
        # old tokens predate created_at: backfill as expires_at minus the
        # 10-minute validity window so offline sync validation still works.
        if not t.get('created_at'):
            exp = parse_iso(t.get('expires_at'))
            t['created_at'] = (exp - timedelta(minutes=10)).isoformat(timespec='seconds') if exp else now_iso()
    for t in db.get('timecards', []):
        t.setdefault('offline', False)
    return db


def save_db():
    global DB_DIRTY
    DB_DIRTY = True
    with open(DB_PATH, 'w') as f:
        json.dump(DB, f, indent=2)
    # Auto-sync to Google Sheets (survives Render deploys) — but never push
    # an unrestored (empty/demo) DB over the good Sheets copy.
    if RESTORE_OK:
        sheets_sync_db()
    else:
        print("cm: save_db skipped Sheets sync (restore not OK)", flush=True)


DB = load_db()


# ---------- lookup / computed helpers ----------

def find(coll, _id):
    return next((r for r in DB[coll] if r.get('id') == _id), None)


def site_name(site_id):
    s = find('sites', site_id)
    return s['name'] if s else '—'


def worker_name(worker_id):
    w = find('workers', worker_id)
    return w['name'] if w else '—'


PAYROLL_PATH = os.path.join(BASE_DIR, 'data_payroll.json')


def _load_payroll_jobsites():
    """Payroll jobsites (top-level + locations) from data_payroll.json.

    Read fresh each call — the file is small and jobsites change when
    the user adds a location in /payroll/jobsites, so a cached copy
    would go stale. Missing/corrupt file -> []."""
    try:
        with open(PAYROLL_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [j for j in data.get("jobsites", []) if isinstance(j, dict)]


def _load_payroll_companies():
    """Payroll companies from data_payroll.json, fresh each call
    (same reason as _load_payroll_jobsites). Missing/corrupt -> []."""
    try:
        with open(PAYROLL_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [c for c in data.get("companies", []) if isinstance(c, dict)]


# The order the payroll companies show in the On Site clock-in
# Company picker (LGES > Company). The user set this order
# (2026-11-25) and hates a jumbled list — keep it exactly.
# Companies not in the order (KB Link, PrismXE) are not offered:
# the CM crew's own company (PrismXE) is the default when no
# company is picked.
CLOCKIN_COMPANY_ORDER = ["COA - Carpenter", "COA - Drywall",
                         "M-Solution", "Hanseong", "Myeongjin"]


def clockin_companies():
    """Active payroll companies for the On Site clock-in Company
    picker, in the user's set order. Returns [{name}]."""
    by_name = {}
    for c in _load_payroll_companies():
        name = (c.get("name") or "").strip()
        if name and c.get("active", True):
            by_name[name] = c
    return [{"name": n} for n in CLOCKIN_COMPANY_ORDER if n in by_name]


def _valid_clockin_company(name):
    """A clock-in Company pick, validated against the active
    payroll companies. Unknown/blank -> None. Never fails a punch
    on a bad pick."""
    n = (name or "").strip()
    if not n:
        return None
    for c in _load_payroll_companies():
        if (c.get("name") or "").strip() == n and c.get("active", True):
            return n
    return None


def _payroll_jobsites_for_site(site_name):
    """Payroll jobsites belonging to a CM site, for the Edit Site
    page: a jobsite belongs when its TOP-LEVEL ancestor's code is
    the site name (or starts with it + '-'), e.g. LGES-COA,
    LGES-HS, LGES-MJ, LGES-MS all sit under LGES. Returns
    (jobsites, companies, parents): the site's jobsites sorted by
    full path with a _depth key, the payroll companies, and the
    eligible parents (Jobsite IDs + company splits) for the Add
    form."""
    pj_jobs = _load_payroll_jobsites()
    pj_by_id = {j.get("id"): j for j in pj_jobs}

    def _pj_depth(j):
        depth, cur, seen = 0, j, set()
        while (cur.get("parent_id") or "") and cur.get("id") not in seen:
            seen.add(cur.get("id"))
            cur = pj_by_id.get(cur["parent_id"])
            if cur is None:
                break
            depth += 1
        return depth

    def _pj_top_code(j):
        cur, seen = j, set()
        while (cur.get("parent_id") or "") and cur.get("id") not in seen:
            seen.add(cur.get("id"))
            cur = pj_by_id.get(cur["parent_id"])
            if cur is None:
                break
        return (cur.get("code") or "").upper()

    def _pj_path(j):
        parts, cur, seen = [], j, set()
        while cur is not None and cur.get("id") not in seen:
            seen.add(cur.get("id"))
            parts.append((cur.get("code") or "").lower())
            pid = (cur.get("parent_id") or "").strip()
            cur = pj_by_id.get(pid) if pid else None
        return list(reversed(parts))

    for j in pj_jobs:
        j["_depth"] = _pj_depth(j)
    sname = (site_name or "").upper()
    mine = sorted(
        (j for j in pj_jobs
         if _pj_top_code(j) == sname
         or _pj_top_code(j).startswith(sname + "-")),
        key=_pj_path)
    parents = sorted((j for j in pj_jobs if _pj_depth(j) < 2),
                     key=lambda x: (_pj_depth(x),
                                    (x.get("code") or "").lower()))
    return mine, _load_payroll_companies(), parents


def payroll_jobsite_tree():
    """Top-level ACTIVE payroll jobsites with their company splits
    and Sub Sites, for the On Site clock-in picker: [{id, code,
    name, children: [{id, code, name, children: [{id, code,
    name}]}]}]. Three levels: a Jobsite ID (e.g. LGES) -> the
    companies working there -> their Sub Sites. Jobsites without a
    status are treated as active (older records predate the field)."""
    all_jobs = _load_payroll_jobsites()

    def _active(j):
        return (j.get("status") or "active") == "active"

    def _label(j):
        return {"id": j.get("id"), "code": j.get("code") or "",
                "name": j.get("name") or j.get("code") or ""}

    def _kids(parent_id):
        return sorted((c for c in all_jobs
                       if (c.get("parent_id") or "") == parent_id
                       and _active(c)),
                      key=lambda x: (x.get("code") or "").lower())
    top = sorted((j for j in all_jobs
                  if not (j.get("parent_id") or "") and _active(j)),
                 key=lambda x: (x.get("code") or "").lower())
    out = []
    for j in top:
        node = _label(j)
        node["children"] = []
        for c in _kids(j.get("id")):
            child = _label(c)
            child["children"] = [_label(g) for g in _kids(c.get("id"))]
            node["children"].append(child)
        out.append(node)
    return out


def _valid_payroll_jobsite(jobsite_id):
    """A clock-in jobsite pick, validated against the payroll jobsites.
    Unknown/blank ids -> None. Never fails a punch on a bad pick."""
    jid = (jobsite_id or "").strip()
    if not jid:
        return None
    for j in _load_payroll_jobsites():
        if (j.get("id") or "") == jid:
            return jid
    return None


def payroll_jobsite_label(jobsite_id):
    """Display label for a payroll jobsite id on a timecard: the
    full path, e.g. 'LGES / Hanseong / Building A' for a Sub Site,
    'LGES / Hanseong' for a company split, 'LGES' for a top-level
    Jobsite ID. Unknown/blank id -> ''."""
    jid = (jobsite_id or "").strip()
    if not jid:
        return ""
    jobs = _load_payroll_jobsites()
    by_id = {j.get("id"): j for j in jobs}
    job = by_id.get(jid)
    if not job:
        return ""
    parts, cur = [], job
    seen = set()
    while cur is not None and cur.get("id") not in seen:
        seen.add(cur.get("id"))
        parts.append(cur.get("code") or "")
        pid = (cur.get("parent_id") or "").strip()
        cur = by_id.get(pid) if pid else None
    return " / ".join(reversed([p for p in parts if p]))


def compute_hours(clock_in_iso, clock_out_iso):
    """Shift hours; 1h meal break auto-deducted for shifts >= 5h."""
    ci, co = parse_iso(clock_in_iso), parse_iso(clock_out_iso)
    if not ci or not co:
        return 0.0
    h = (co - ci).total_seconds() / 3600.0
    if h >= 5:
        h -= 1.0
    return round(max(h, 0.0), 2)


def days_remaining(target_date):
    try:
        d = date.fromisoformat(target_date) - today()
        return d.days
    except (ValueError, TypeError):
        return None


def is_low_stock(m):
    try:
        return float(m.get('quantity') or 0) <= float(m.get('reorder_threshold') or 0)
    except (ValueError, TypeError):
        return False


def open_timecard(worker_id, site_id=None):
    for t in DB['timecards']:
        if t['worker_id'] == worker_id and t['status'] == 'clocked_in':
            if site_id is None or t['site_id'] == site_id:
                return t
    return None


def trash_record(coll, record):
    DB['trash'].append({
        'type': coll,
        'record': record,
        'deleted_at': now_iso(),
        'deleted_by': 'Admin',
    })


def remove_from(coll, _id, to_trash=True):
    rec = find(coll, _id)
    if not rec:
        return False
    DB[coll] = [r for r in DB[coll] if r.get('id') != _id]
    if to_trash:
        trash_record(coll, rec)
    save_db()
    return True


def save_upload(f):
    if not f or not f.filename:
        return ''
    name = secure_filename(f.filename)
    fname = f"{uuid.uuid4().hex[:8]}_{name}"
    f.save(os.path.join(UPLOAD_DIR, fname))
    return '/static/cm/uploads/' + fname


def gmaps_link(lat, lng):
    if lat in (None, '') or lng in (None, ''):
        return None
    return f"https://www.google.com/maps?q={lat},{lng}"


def needs_attention_count():
    """Real problems only: still-open shifts, GPS distance warnings
    (clock-in >= 0.5 km from the jobsite), and unapplied payroll
    corrections. Clocked-out-but-unapproved timecards are NOT counted:
    payroll now runs in /payroll/, not via CM approve."""
    open_shifts = sum(1 for t in DB['timecards']
                      if t.get('status') == 'clocked_in')
    gps_warnings = sum(1 for t in DB['timecards']
                       if (clock_in_distance_km(t) or 0) >= 0.5)
    pending_corrections = sum(
        1 for c in DB.get('payroll_corrections', [])
        if c.get('status') == 'correction' and not c.get('applied_at'))
    return open_shifts + gps_warnings + pending_corrections


def open_defects_count():
    return sum(1 for d in DB['defects'] if d.get('status') == 'open')


def fmt_dt(iso):
    dt = parse_iso(iso)
    return dt.strftime('%m/%d %I:%M %p') if dt else '—'


def haversine_km(lat1, lng1, lat2, lng2):
    try:
        lat1, lng1, lat2, lng2 = map(float, (lat1, lng1, lat2, lng2))
    except (TypeError, ValueError):
        return None
    import math
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def clock_in_distance_km(t):
    s = find('sites', t.get('site_id'))
    if not s:
        return None
    return haversine_km(t.get('clock_in_lat'), t.get('clock_in_lng'),
                        s.get('lat'), s.get('lng'))


def register_jinja(app):
    app.jinja_env.globals.update(
        site_name=site_name, worker_name=worker_name, fmt_dt=fmt_dt,
        gmaps_link=gmaps_link, compute_hours=compute_hours,
        days_remaining=days_remaining, today=today,
        clock_in_distance_km=clock_in_distance_km,
        payroll_jobsite_label=payroll_jobsite_label,
    )

NAV = [
    ('dashboard', 'Dashboard', '/staffing/'),
    ('sites', 'Sites', '/staffing/sites'),
    ('workers', 'Workers', '/staffing/workers'),
    ('timecards', 'Timecards', '/staffing/timecards'),
    ('data', 'Data', '/staffing/data'),
]


@bp.context_processor
def inject_nav():
    return dict(nav=NAV, active_tab=(request.endpoint or '').split('.')[-1],
                attention=needs_attention_count())


# ================= DASHBOARD =================

@bp.route('/')
def dashboard():
    open_incidents = sum(1 for i in DB['incidents'] if i.get('status') == 'open')
    open_defects = open_defects_count()
    low_stock = sum(1 for m in DB['materials'] if is_low_stock(m))
    tdy = today().isoformat()
    on_site = {t['worker_id'] for t in DB['timecards']
               if (t.get('clock_in') or '').startswith(tdy)}
    sites = []
    for s in DB['sites']:
        sites.append({
            'site': s,
            'days': days_remaining(s.get('target_date') or ''),
            'incidents': sum(1 for i in DB['incidents'] if i['site_id'] == s['id'] and i.get('status') == 'open'),
            'defects': sum(1 for d in DB['defects'] if d['site_id'] == s['id'] and d.get('status') == 'open'),
        })
    recent_incidents = sorted(DB['incidents'], key=lambda i: i.get('created_at', ''), reverse=True)[:5]
    overdue = [d for d in DB['defects']
               if d.get('status') == 'open' and d.get('due_date') and d['due_date'] < tdy]
    return render_template('cm/dashboard.html',
                           open_incidents=open_incidents, open_defects=open_defects,
                           low_stock=low_stock, on_site=len(on_site),
                           sites=sites, recent_incidents=recent_incidents,
                           overdue=overdue)


# ================= SITES =================

SITE_STATUSES = ['active', 'on_hold', 'completed']


@bp.route('/sites')
def sites():
    q = (request.args.get('q') or '').lower()
    status = request.args.get('status') or 'all'
    rows = []
    for s in DB['sites']:
        if status != 'all' and s.get('status') != status:
            continue
        if q and q not in f"{s['name']} {s.get('address','')} {s.get('pm','')}".lower():
            continue
        tdy = today().isoformat()
        rows.append({
            'site': s,
            'days': days_remaining(s.get('target_date') or ''),
            'incidents': sum(1 for i in DB['incidents'] if i['site_id'] == s['id'] and i.get('status') == 'open'),
            'defects': sum(1 for d in DB['defects'] if d['site_id'] == s['id'] and d.get('status') == 'open'),
            'low_stock': sum(1 for m in DB['materials'] if m['site_id'] == s['id'] and is_low_stock(m)),
            'workers': sum(1 for t in DB['timecards']
                           if t['site_id'] == s['id'] and (t.get('clock_in') or '').startswith(tdy)),
        })
    counts = {st: sum(1 for s in DB['sites'] if s.get('status') == st) for st in SITE_STATUSES}
    # Payroll jobsites (the buttons workers tap on the On Site
    # clock-in page) + companies, for the "Payroll Jobsites" section
    # below the site cards — the user adds payroll jobsites here.
    # The Parent dropdown offers Jobsite IDs (depth 0) and company
    # splits (depth 1), so a company can be added under LGES and a
    # Sub Site under a company; a Sub Site (depth 2) can never be a
    # parent.
    pj_jobs = _load_payroll_jobsites()
    pj_by_id = {j.get("id"): j for j in pj_jobs}

    def _pj_depth(j):
        depth, cur, seen = 0, j, set()
        while (cur.get("parent_id") or "") and cur.get("id") not in seen:
            seen.add(cur.get("id"))
            cur = pj_by_id.get(cur["parent_id"])
            if cur is None:
                break
            depth += 1
        return depth
    pj_top = sorted((j for j in pj_jobs if not (j.get("parent_id") or "")),
                    key=lambda x: (x.get("code") or "").lower())
    pj_parents = sorted((j for j in pj_jobs if _pj_depth(j) < 2),
                        key=lambda x: (_pj_depth(x),
                                       (x.get("code") or "").lower()))
    # Depth on each row so the list below can indent company splits
    # and Sub Sites under their parent. Sorted by the full path
    # (LGES, then its companies, then their Sub Sites) so children
    # sit under their parent instead of scattered by code.
    for j in pj_jobs:
        j["_depth"] = _pj_depth(j)

    def _pj_path(j):
        parts, cur, seen = [], j, set()
        while cur is not None and cur.get("id") not in seen:
            seen.add(cur.get("id"))
            parts.append((cur.get("code") or "").lower())
            pid = (cur.get("parent_id") or "").strip()
            cur = pj_by_id.get(pid) if pid else None
        return list(reversed(parts))
    pj_list = sorted(pj_jobs, key=_pj_path)
    # Payroll jobsites belong to a CM site when their TOP-LEVEL
    # ancestor's code is the site name (or starts with it + "-"):
    # LGES-COA, LGES-HS, LGES-MJ, LGES-MS all sit under the LGES
    # site card. Each site card shows only its own jobsites.
    def _pj_top_code(j):
        cur, seen = j, set()
        while (cur.get("parent_id") or "") and cur.get("id") not in seen:
            seen.add(cur.get("id"))
            cur = pj_by_id.get(cur["parent_id"])
            if cur is None:
                break
        return (cur.get("code") or "").upper()
    for r in rows:
        sname = (r["site"].get("name") or "").upper()
        r["pj_jobs"] = [j for j in pj_list
                        if _pj_top_code(j) == sname
                        or _pj_top_code(j).startswith(sname + "-")]
    return render_template('cm/sites.html', rows=rows, q=request.args.get('q', ''),
                           status=status, counts=counts, total=len(DB['sites']),
                           pj_jobs=pj_list,
                           pj_top=pj_top,
                           pj_parents=pj_parents,
                           pj_companies=_load_payroll_companies())


@bp.route('/sites/new', methods=['GET', 'POST'])
def site_new():
    if request.method == 'POST':
        s = {
            'id': new_id(),
            'name': request.form.get('name', '').strip(),
            'photo_url': save_upload(request.files.get('photo')),
            'lat': _f(request.form.get('lat')), 'lng': _f(request.form.get('lng')),
            'address': request.form.get('address', '').strip(),
            'pm': request.form.get('pm', '').strip(),
            'start_date': request.form.get('start_date', ''),
            'target_date': request.form.get('target_date', ''),
            'status': request.form.get('status', 'active'),
        }
        DB['sites'].append(s)
        save_db()
        flash(f"Site '{s['name']}' added.")
        return redirect(url_for('cm.sites'))
    return render_template('cm/site_form.html', site=None, statuses=SITE_STATUSES)


@bp.route('/sites/<sid>/edit', methods=['GET', 'POST'])
def site_edit(sid):
    s = find('sites', sid)
    if not s:
        abort(404)
    if request.method == 'POST':
        s['name'] = request.form.get('name', '').strip()
        ph = save_upload(request.files.get('photo'))
        if ph:
            s['photo_url'] = ph
        s['lat'] = _f(request.form.get('lat'))
        s['lng'] = _f(request.form.get('lng'))
        s['address'] = request.form.get('address', '').strip()
        s['pm'] = request.form.get('pm', '').strip()
        s['start_date'] = request.form.get('start_date', '')
        s['target_date'] = request.form.get('target_date', '')
        s['status'] = request.form.get('status', 'active')
        save_db()
        flash(f"Site '{s['name']}' updated.")
        return redirect(url_for('cm.sites'))
    # The site's Payroll Jobsites live on this Edit page: the user
    # adds/edits them here instead of from the Sites list.
    pj_jobs, pj_companies, pj_parents = _payroll_jobsites_for_site(
        s.get("name") or "")
    return render_template('cm/site_form.html', site=s,
                           statuses=SITE_STATUSES,
                           pj_jobs=pj_jobs,
                           pj_companies=pj_companies,
                           pj_parents=pj_parents)


@bp.route('/sites/<sid>/delete', methods=['POST'])
def site_delete(sid):
    if remove_from('sites', sid):
        flash('Site deleted (moved to Recycle Bin).')
    return redirect(url_for('cm.sites'))


def _f(v):
    try:
        return float(v) if v not in (None, '') else None
    except (ValueError, TypeError):
        return None


# ================= REMOVED MODULES =================
# Incidents, Daily Logs, Labour, Materials, QA/QC and Map & Plans were
# removed (zero records in each collection). Old bookmarks redirect to
# the dashboard instead of landing on a 404. The QR clock is gone too:
# /staffing/clock/<token> points at the On Site worker app, the GPS
# clock-in workers actually use.

@bp.route('/incidents')
@bp.route('/daily-logs')
@bp.route('/labour')
@bp.route('/materials')
@bp.route('/qa')
@bp.route('/blueprints')
def _removed_module():
    return redirect(url_for('cm.dashboard'))


@bp.route('/clock/<token>')
def _removed_qr_clock(token):
    return redirect(url_for('cm.onsite'))


# ================= WORKERS =================

TRADES = ['Electrician', 'Carpenter', 'Plumber', 'Laborer', 'Welder',
          'Painter', 'Mason', 'Foreman', 'Operator', 'Other']
EMP_TYPES = ['Direct Team', 'Contract']
WORKER_STATUSES = ['active', 'on_leave', 'inactive']


@bp.route('/workers')
def workers():
    q = (request.args.get('q') or '').lower()
    fsite = request.args.get('site') or 'all'
    ftrade = request.args.get('trade') or 'all'
    fstatus = request.args.get('status') or 'all'
    rows = []
    for w in DB['workers']:
        if fsite != 'all' and w.get('site_id') != fsite:
            continue
        if ftrade != 'all' and w.get('trade') != ftrade:
            continue
        if fstatus != 'all' and w.get('status') != fstatus:
            continue
        if q and q not in f"{w['name']} {w.get('phone','')} {w.get('trade','')}".lower():
            continue
        w2 = dict(w)
        w2['clocked_in'] = open_timecard(w['id']) is not None
        rows.append(w2)
    total_hours = sum(t.get('hours') or compute_hours(t.get('clock_in'), t.get('clock_out'))
                      for t in DB['timecards'] if t.get('clock_out'))
    active_now = sum(1 for t in DB['timecards'] if t.get('status') == 'clocked_in')
    return render_template('cm/workers.html', rows=rows,
                           q=request.args.get('q', ''), fsite=fsite,
                           ftrade=ftrade, fstatus=fstatus,
                           sites=DB['sites'], trades=sorted({w.get('trade') for w in DB['workers'] if w.get('trade')} | set(TRADES)),
                           total=len(DB['workers']), active_now=active_now,
                           total_hours=round(total_hours, 1))


@bp.route('/workers/new', methods=['GET', 'POST'])
def worker_new():
    if request.method == 'POST':
        w = {
            'id': new_id(),
            'name': request.form.get('name', '').strip(),
            'email': request.form.get('email', '').strip(),
            'phone': request.form.get('phone', '').strip(),
            'trade': request.form.get('trade', '').strip() or 'Other',
            'employment_type': request.form.get('employment_type', 'Contract'),
            'hourly_rate': _f(request.form.get('hourly_rate')) or 0.0,
            'site_id': request.form.get('site_id') or None,
            'status': 'active',
            'emergency_contact': request.form.get('emergency_contact', '').strip(),
            'photo_url': save_upload(request.files.get('photo')),
            'created_at': now_iso(),
        }
        DB['workers'].append(w)
        save_db()
        flash(f"Worker '{w['name']}' registered.")
        return redirect(url_for('cm.workers'))
    return render_template('cm/worker_form.html', worker=None, sites=DB['sites'],
                           trades=TRADES, emp_types=EMP_TYPES, statuses=WORKER_STATUSES)


@bp.route('/workers/<wid>/edit', methods=['GET', 'POST'])
def worker_edit(wid):
    w = find('workers', wid)
    if not w:
        abort(404)
    if request.method == 'POST':
        w['name'] = request.form.get('name', '').strip()
        w['email'] = request.form.get('email', '').strip()
        w['phone'] = request.form.get('phone', '').strip()
        w['trade'] = request.form.get('trade', '').strip() or 'Other'
        w['employment_type'] = request.form.get('employment_type', 'Contract')
        w['hourly_rate'] = _f(request.form.get('hourly_rate')) or 0.0
        w['site_id'] = request.form.get('site_id') or None
        w['status'] = request.form.get('status', 'active')
        w['emergency_contact'] = request.form.get('emergency_contact', '').strip()
        ph = save_upload(request.files.get('photo'))
        if ph:
            w['photo_url'] = ph
        save_db()
        flash(f"Worker '{w['name']}' updated.")
        return redirect(url_for('cm.workers'))
    return render_template('cm/worker_form.html', worker=w, sites=DB['sites'],
                           trades=TRADES, emp_types=EMP_TYPES, statuses=WORKER_STATUSES)


@bp.route('/workers/<wid>/delete', methods=['POST'])
def worker_delete(wid):
    if remove_from('workers', wid):
        flash('Worker deleted (moved to Recycle Bin).')
    return redirect(url_for('cm.workers'))


@bp.route('/workers/export')
def workers_export():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['id', 'name', 'email', 'phone', 'trade', 'employment_type',
                'hourly_rate', 'site', 'status', 'emergency_contact', 'created_at'])
    for x in DB['workers']:
        w.writerow([x.get('id'), x.get('name'), x.get('email'), x.get('phone'),
                    x.get('trade'), x.get('employment_type'), x.get('hourly_rate'),
                    site_name(x.get('site_id')), x.get('status'),
                    x.get('emergency_contact'), x.get('created_at')])
    data = io.BytesIO(buf.getvalue().encode('utf-8'))
    return send_file(data, mimetype='text/csv', as_attachment=True,
                     download_name='workers.csv')


SIGNATURE_DIR = os.path.join(BASE_DIR, 'static', 'cm', 'signatures')


def _save_signature(timecard_id, data_url):
    """Save a data-URL PNG signature to disk. Returns the public URL
    path (e.g. /static/cm/signatures/<id>.png), or None on any failure
    — clock-out still succeeds without it."""
    if not data_url or not isinstance(data_url, str):
        return None
    if not data_url.startswith('data:image/png;base64,'):
        return None
    try:
        raw = base64.b64decode(data_url.split(',', 1)[1], validate=True)
    except Exception:
        return None
    if not raw or len(raw) > 300 * 1024:
        return None
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    try:
        os.makedirs(SIGNATURE_DIR, exist_ok=True)
        name = '%s.png' % timecard_id
        with open(os.path.join(SIGNATURE_DIR, name), 'wb') as f:
            f.write(raw)
        return '/static/cm/signatures/' + name
    except Exception:
        return None


def _meal_break(d):
    """Parse an optional meal-break flag (true/false) from a clock
    request body. Returns True/False, or None when not supplied."""
    v = d.get('meal_break')
    if v is None or v == '':
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v.lower() in ('true', 'yes', '1'):
            return True
        if v.lower() in ('false', 'no', '0'):
            return False
    return None


def _email_opt_in(d):
    """True when the worker asked for an emailed copy of the record."""
    v = d.get('email_opt_in')
    if isinstance(v, bool):
        return v
    return isinstance(v, str) and v.lower() in ('true', 'yes', '1')


def _send_clockout_email(worker, t, site_name):
    """Best-effort: email the signed clock-out record to info@prismxe.com
    and the worker. Sent in a background thread so clock-out never waits
    on email. The plain-text body is complete on its own; the HTML
    version adds the signature image."""
    if not CM_SHEETS_URL:
        return
    import html as _html
    email = (worker.get('email') or '').strip()
    to = 'info@prismxe.com'
    if email:
        to += ', ' + email
    date = (t.get('clock_in') or '')[:10]
    ci = fmt_dt(t.get('clock_in'))
    co = fmt_dt(t.get('clock_out'))
    sys_h = t.get('hours')
    rep_h = t.get('reported_hours')
    meal = t.get('meal_break')
    meal_txt = ('Yes (1 hour)' if meal is True else
                'No' if meal is False else 'Not recorded')
    subject = 'Timecard — %s — %s' % (worker.get('name'), date)
    lines = [
        'Worker: %s' % worker.get('name'),
        'Date: %s' % date,
        'Jobsite: %s' % site_name,
        'Clock in: %s' % ci,
        'Clock out: %s' % co,
        'Hours (system): %sh' % sys_h,
    ]
    if rep_h is not None:
        lines.append('Hours reported by worker: %sh' % rep_h)
    lines.append('Meal break (1 hour): %s' % meal_txt)
    lines.append('Signed by the worker at clock-out.')
    body = '\n'.join(lines)
    sig = t.get('signature')
    html_body = '<br>'.join(_html.escape(x) for x in lines)
    if sig:
        sig_url = BASE_URL.rstrip('/') + sig
        body += '\nSignature: ' + sig_url
        html_body += ('<br><br><img src="%s" alt="Worker signature" '
                      'style="max-width:320px;border:1px solid #ccc">'
                      % _html.escape(sig_url, quote=True))
    sheets_post_async({'action': 'sendEmail', 'to': to, 'subject': subject,
                       'body': body, 'html': html_body})


def _perform_clock(site_id, action, worker_id, lat, lng, at_iso=None,
                   offline=False, reported_hours=None, meal_break=None,
                   signature_data_url=None, email_opt_in=False,
                   jobsite_id=None, company=None):
    """Shared clock-in/out engine for the On Site worker app API.
    at_iso overrides the timestamp (used for offline-synced punches);
    offline marks the record as synced offline. jobsite_id is the
    payroll jobsite (or location) the worker picked at clock-in; it
    is validated against the payroll jobsites and stored on the
    timecard so payroll imports arrive already tagged. company is
    the payroll company the worker picked at clock-in (LGES >
    Company); it is validated against the active payroll companies
    and stored on the timecard so the hours go to that company's
    payroll. An unknown or missing pick stores None and never
    blocks the punch.
    Returns (ok, info dict)."""
    w = find('workers', worker_id)
    if not w:
        return False, {'error': 'Worker not found.'}
    now = at_iso or now_iso()
    if action == 'clock_in':
        if open_timecard(worker_id):
            return False, {'error': f"{w['name']} is already clocked in.",
                           'already_in': True}
        valid_jobsite = _valid_payroll_jobsite(jobsite_id)
        valid_company = _valid_clockin_company(company)
        t = {'id': new_id(), 'worker_id': worker_id, 'site_id': site_id,
             'clock_in': now, 'clock_in_lat': lat, 'clock_in_lng': lng,
             'clock_out': None, 'clock_out_lat': None, 'clock_out_lng': None,
             'hours': 0.0, 'status': 'clocked_in', 'notes': 'Onsite clock-in',
             'offline': offline, 'jobsite_id': valid_jobsite,
             'company': valid_company}
        DB['timecards'].append(t)
        save_db()
        return True, {'action': 'clock_in', 'site_id': site_id,
                      'site_name': site_name(site_id), 'worker_name': w['name'],
                      'clock_in': now, 'clock_out': None, 'hours': 0.0,
                      'jobsite_id': valid_jobsite,
                      'jobsite_label': payroll_jobsite_label(valid_jobsite),
                      'company': valid_company}
    t = open_timecard(worker_id, site_id) or open_timecard(worker_id)
    if not t:
        return False, {'error': f"No open shift found for {w['name']}."}
    t['clock_out'] = now
    t['clock_out_lat'] = lat
    t['clock_out_lng'] = lng
    t['hours'] = compute_hours(t['clock_in'], now)
    t['status'] = 'clocked_out'
    if reported_hours is not None:
        t['reported_hours'] = reported_hours
    if meal_break is not None:
        t['meal_break'] = meal_break
    if signature_data_url:
        sig = _save_signature(t['id'], signature_data_url)
        if sig:
            t['signature'] = sig
    if email_opt_in:
        t['email_opt_in'] = True
    if offline:
        t['offline'] = True
    save_db()
    info = {'action': 'clock_out', 'site_id': t['site_id'],
            'site_name': site_name(t['site_id']), 'worker_name': w['name'],
            'clock_in': t['clock_in'], 'clock_out': now,
            'hours': t['hours'], 'reported_hours': reported_hours,
            'timecard_id': t['id'], 'meal_break': t.get('meal_break'),
            'signature': t.get('signature')}
    if email_opt_in:
        _send_clockout_email(w, t, site_name(t['site_id']))
    return True, info


def _client_at_local_iso(s):
    """Normalize a client-supplied ISO timestamp (usually UTC 'Z') to a
    naive server-local ISO string so it compares/stores consistently with
    the rest of the DB. Returns None if unparseable."""
    dt = parse_iso(s)
    if not dt:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec='seconds')


def _reported_hours(d):
    """Parse an optional worker-reported hours value (0-24) from a
    clock request body. Returns (value, error_response)."""
    raw = d.get('reported_hours')
    if raw in (None, ''):
        return None, None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None, (jsonify({'ok': False,
                               'error': 'Invalid reported hours.'}), 400)
    if not 0 <= val <= 24:
        return None, (jsonify({'ok': False,
                               'error': 'Reported hours must be 0-24.'}), 400)
    return val, None


@bp.route('/api/direct-clock', methods=['POST'])
def api_direct_clock():
    """GPS self clock-in/out from /staffing/onsite (no QR needed).
    Body: {worker_id, site_id (clock-in only), action, lat, lng,
    jobsite_id (clock-in only, optional payroll jobsite/location),
    company (clock-in only, optional payroll company),
    reported_hours (clock-out only, optional),
    meal_break (clock-out only, optional bool),
    signature (clock-out only, optional data-URL PNG),
    email_opt_in (clock-out only, optional bool)}."""
    d = request.get_json(force=True) or {}
    action = d.get('action')
    if action not in ('clock_in', 'clock_out'):
        return jsonify({'ok': False, 'error': 'Invalid action.'}), 400
    if action == 'clock_in' and not d.get('site_id'):
        return jsonify({'ok': False, 'error': 'Choose a jobsite first.'}), 400
    reported, err = _reported_hours(d)
    if err:
        return err
    ok, info = _perform_clock(d.get('site_id'), action, d.get('worker_id'),
                              d.get('lat'), d.get('lng'),
                              reported_hours=reported,
                              meal_break=_meal_break(d),
                              signature_data_url=d.get('signature'),
                              email_opt_in=_email_opt_in(d),
                              jobsite_id=d.get('jobsite_id'),
                              company=d.get('company'))
    info['ok'] = ok
    return jsonify(info), (200 if ok else 400)


# ================= ON SITE WORKER APP =================

@bp.route('/onsite')
def onsite():
    sites = [s for s in DB['sites'] if s.get('status') == 'active']
    return render_template('cm/onsite.html', trades=TRADES, sites=sites,
                           payroll_jobsites=payroll_jobsite_tree(),
                           clockin_companies=clockin_companies())


@bp.route('/api/sites')
def api_sites():
    """Active CM sites, live. The On Site page fetches this on load
    (it is NOT cached by the service worker), so a site added on
    /staffing/sites shows up on the worker's clock-in list
    immediately — even when the phone serves the page from its
    offline cache. Public: the clock-in page is public too."""
    return jsonify({'sites': [
        {'id': s.get('id'), 'name': s.get('name')}
        for s in DB.get('sites', []) if s.get('status') == 'active']})


def _phone_match(a, b):
    """Lenient phone match: same digits, or one ends with the other
    (handles missing/extra country codes)."""
    if len(a) < 6 or len(b) < 6:
        return False
    return a == b or a.endswith(b) or b.endswith(a)


@bp.route('/api/register-worker', methods=['POST'])
def api_register_worker():
    """Self-registration from a worker's own phone. Matches on normalized
    phone (or email) so a reinstall doesn't create a duplicate."""
    d = request.get_json(force=True)
    name = (d.get('name') or '').strip()
    phone = (d.get('phone') or '').strip()
    if not name or not phone:
        return jsonify({'ok': False,
                        'error': 'Name and mobile phone are required.'}), 400
    norm = ''.join(c for c in phone if c.isdigit())
    email = (d.get('email') or '').strip().lower()
    if not email or '@' not in email or '.' not in email.rsplit('@', 1)[-1]:
        return jsonify({'ok': False,
                        'error': 'A valid email address is required.'}), 400
    w = None
    for cand in DB['workers']:
        wnorm = ''.join(c for c in (cand.get('phone') or '') if c.isdigit())
        if _phone_match(wnorm, norm):
            w = cand
            break
    if not w and email and '@' in email:
        for cand in DB['workers']:
            if (cand.get('email') or '').strip().lower() == email:
                w = cand
                break
    if w:
        # Returning worker self-registering: let them set their own name.
        updated = False
        if name and w.get('name') != name:
            w['name'] = name
            updated = True
        if not w.get('email'):
            w['email'] = email
            updated = True
        if updated:
            save_db()
        return jsonify({'ok': True, 'worker_id': w['id'],
                        'worker_name': w['name'], 'existing': True})
    trade = (d.get('trade') or '').strip() or 'Other'
    emp = (d.get('employment_type') or '').strip()
    if emp not in EMP_TYPES:
        emp = 'Contract'
    w = {
        'id': new_id(),
        'name': name,
        'email': email,
        'phone': phone,
        'trade': trade,
        'employment_type': emp,
        'hourly_rate': 0.0,
        'site_id': None,
        'status': 'active',
        'emergency_contact': '',
        'photo_url': '',
        'self_registered': True,
        'created_at': now_iso(),
    }
    DB['workers'].append(w)
    save_db()
    return jsonify({'ok': True, 'worker_id': w['id'],
                    'worker_name': w['name'], 'existing': False})


@bp.route('/api/worker-lookup')
def api_worker_lookup():
    """Find an active worker by registered mobile phone or email.
    Used at clock-in so workers don't have to scroll the name list."""
    q = (request.args.get('q') or '').strip()
    w = None
    if '@' in q:
        em = q.lower()
        for cand in DB['workers']:
            if (cand.get('email') or '').strip().lower() == em:
                w = cand
                break
    else:
        digits = ''.join(c for c in q if c.isdigit())
        for cand in DB['workers']:
            wnorm = ''.join(c for c in (cand.get('phone') or '')
                            if c.isdigit())
            if _phone_match(wnorm, digits):
                w = cand
                break
    if not w or w.get('status') != 'active':
        return jsonify({'ok': False,
                        'error': 'No worker found with that phone or email. '
                                 'Check the spelling or ask your supervisor '
                                 'to add you first.'}), 404
    return jsonify({'ok': True, 'worker': {'id': w['id'], 'name': w['name'],
                                           'trade': w.get('trade', '')}})


@bp.route('/api/location', methods=['POST'])
def api_location():
    d = request.get_json(force=True)
    worker_id = d.get('worker_id')
    if not find('workers', worker_id):
        return jsonify({'ok': False, 'error': 'Worker not found.'}), 404
    if not open_timecard(worker_id):
        return jsonify({'ok': False, 'error': 'Not clocked in.'})
    try:
        lat, lng = float(d.get('lat')), float(d.get('lng'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Invalid coordinates.'}), 400
    DB['live_locations'][worker_id] = {
        'lat': lat, 'lng': lng, 'at': now_iso(),
        'accuracy': d.get('accuracy'),
    }
    save_db()
    return jsonify({'ok': True})


@bp.route('/sw.js')
def service_worker():
    """Serve the On Site service worker from the origin root so its
    scope covers the whole app (not just /static/)."""
    return send_file(os.path.join(BASE_DIR, 'static', 'cm', 'sw.js'),
                     mimetype='application/javascript')


@bp.route('/api/sync-clock', methods=['POST'])
def api_sync_clock():
    """Accept a clock action captured offline by /staffing/onsite and
    sync it stamped with the client time. The offline queue only ever
    holds direct (GPS, no-QR) actions now; a token, if one is present
    in an old queued item, is ignored."""
    d = request.get_json(force=True) or {}
    client_at = _client_at_local_iso(d.get('client_at'))
    if not client_at:
        return jsonify({'ok': False,
                        'error': 'Missing scan time.'}), 400
    action = d.get('action')
    if action not in ('clock_in', 'clock_out'):
        return jsonify({'ok': False, 'error': 'Invalid action.'}), 400
    if action == 'clock_in' and not d.get('site_id'):
        return jsonify({'ok': False, 'error': 'Missing site.'}), 400
    reported, err = _reported_hours(d)
    if err:
        return err
    worker_id = d.get('worker_id')
    if not find('workers', worker_id):
        return jsonify({'ok': False, 'error': 'Worker not found.'}), 400
    ok, info = _perform_clock(d.get('site_id'), action, worker_id,
                              d.get('lat'), d.get('lng'),
                              at_iso=client_at, offline=True,
                              reported_hours=reported,
                              meal_break=_meal_break(d),
                              signature_data_url=d.get('signature'),
                              email_opt_in=_email_opt_in(d),
                              jobsite_id=d.get('jobsite_id'))
    info['ok'] = ok
    return jsonify(info), (200 if ok else 400)


@bp.route('/api/location-batch', methods=['POST'])
def api_location_batch():
    """Accept a batch of location points captured offline. Updates the
    worker's live location with the newest point."""
    d = request.get_json(force=True)
    worker_id = d.get('worker_id')
    if not find('workers', worker_id):
        return jsonify({'ok': False, 'error': 'Worker not found.'}), 404
    if not open_timecard(worker_id):
        return jsonify({'ok': False, 'error': 'Not clocked in.'})
    points = d.get('points') or []
    accepted = 0
    newest = None
    for p in points:
        try:
            lat, lng = float(p.get('lat')), float(p.get('lng'))
        except (TypeError, ValueError, AttributeError):
            continue
        at = _client_at_local_iso(p.get('at')) or now_iso()
        newest = {'lat': lat, 'lng': lng, 'at': at,
                  'accuracy': p.get('accuracy')}
        accepted += 1
    if newest:
        DB['live_locations'][worker_id] = newest
        save_db()
    return jsonify({'ok': True, 'accepted': accepted})


LIVE_STALE_MIN = 15


def _live_entry(worker_id):
    loc = DB.get('live_locations', {}).get(worker_id)
    if not loc:
        return None
    at = parse_iso(loc.get('at'))
    if not at:
        return None
    age_min = (datetime.now() - at).total_seconds() / 60.0
    if age_min > LIVE_STALE_MIN:
        return None
    return loc, age_min


@bp.route('/api/live-locations')
def api_live_locations():
    out = []
    for t in DB['timecards']:
        if t.get('status') != 'clocked_in':
            continue
        e = _live_entry(t['worker_id'])
        if not e:
            continue
        loc, age_min = e
        w = find('workers', t['worker_id']) or {}
        out.append({'worker_id': t['worker_id'],
                    'worker_name': w.get('name', '—'),
                    'trade': w.get('trade', ''),
                    'site_id': t['site_id'],
                    'site_name': site_name(t['site_id']),
                    'clock_in': t.get('clock_in'),
                    'lat': loc['lat'], 'lng': loc['lng'],
                    'at': loc['at'],
                    'age_min': round(age_min, 1)})
    return jsonify(out)


@bp.route('/api/worker-status/<worker_id>')
def api_worker_status(worker_id):
    w = find('workers', worker_id)
    if not w:
        abort(404)
    t = open_timecard(worker_id)
    tdy = today().isoformat()
    today_hours = sum(
        (x.get('hours') or compute_hours(x.get('clock_in'), x.get('clock_out')))
        for x in DB['timecards']
        if x['worker_id'] == worker_id and (x.get('clock_in') or '').startswith(tdy)
        and x.get('clock_out'))
    return jsonify({'worker_id': worker_id, 'worker_name': w['name'],
                    'clocked_in': t is not None,
                    'site_id': t['site_id'] if t else None,
                    'site_name': site_name(t['site_id']) if t else None,
                    'jobsite_id': t.get('jobsite_id') if t else None,
                    'jobsite_label': payroll_jobsite_label(
                        t.get('jobsite_id')) if t else '',
                    'clock_in': t.get('clock_in') if t else None,
                    'today_hours': round(today_hours, 2)})


@bp.route('/api/workers/<wid>')
def api_worker_detail(wid):
    # Worker detail for the Details button on /workers (was missing entirely:
    # the button fetched this endpoint and got 404, so nothing opened).
    w = find('workers', wid)
    if not w:
        abort(404)
    rows = []
    total_hours = 0.0
    for t in DB['timecards']:
        if t.get('worker_id') != wid:
            continue
        h = t.get('hours') or compute_hours(t.get('clock_in'), t.get('clock_out'))
        total_hours += h or 0
        rows.append({'date': (t.get('clock_in') or '')[:10],
                     'in': fmt_dt(t.get('clock_in')),
                     'out': fmt_dt(t.get('clock_out')) if t.get('clock_out') else '—',
                     'status': t.get('status'),
                     'hours': round(h, 2) if h is not None else 0})
    rows.sort(key=lambda r: r['date'], reverse=True)
    # Normalize: worker records may lack some fields the modal reads
    # (e.g. no worker has a 'company' key); never send undefined.
    worker = {'id': w.get('id'), 'name': w.get('name'),
              'trade': w.get('trade') or '',
              'company': w.get('company') or '',
              'employment_type': w.get('employment_type') or '',
              'site_id': w.get('site_id'),
              'email': w.get('email') or '',
              'phone': w.get('phone') or '',
              'emergency_contact': w.get('emergency_contact') or '',
              'status': w.get('status')}
    return jsonify({'worker': worker,
                    'timecards': rows,
                    'counts': {'timecards': len(rows),
                               'hours': round(total_hours, 1)}})


@bp.route('/api/my-timecards/<worker_id>')
def api_my_timecards(worker_id):
    try:
        days = max(1, min(90, int(request.args.get('days', 7))))
    except (TypeError, ValueError):
        days = 7
    cutoff = (today() - timedelta(days=days - 1)).isoformat()
    rows = []
    for t in DB['timecards']:
        if t['worker_id'] != worker_id:
            continue
        if (t.get('clock_in') or '')[:10] < cutoff:
            continue
        rows.append({'date': (t.get('clock_in') or '')[:10],
                     'site_name': site_name(t.get('site_id')),
                     'jobsite_label': payroll_jobsite_label(
                         t.get('jobsite_id')),
                     'clock_in': fmt_dt(t.get('clock_in')),
                     'clock_out': fmt_dt(t.get('clock_out')) if t.get('clock_out') else None,
                     'hours': t.get('hours') or compute_hours(t.get('clock_in'), t.get('clock_out')),
                     'status': t.get('status')})
    rows.sort(key=lambda r: r['date'], reverse=True)
    return jsonify(rows)


# ================= TIMECARDS =================

@bp.route('/timecards')
def timecards():
    fsite = request.args.get('site') or 'all'
    fstatus = request.args.get('status') or 'all'
    sort = request.args.get('sort') or 'newest'
    rows = []
    for t in DB['timecards']:
        if fsite != 'all' and t.get('site_id') != fsite:
            continue
        if fstatus != 'all' and t.get('status') != fstatus:
            continue
        rows.append(t)
    rows.sort(key=lambda t: t.get('clock_in') or '',
              reverse=(sort == 'newest'))
    total_hours = sum(t.get('hours') or compute_hours(t.get('clock_in'), t.get('clock_out'))
                      for t in rows if t.get('clock_out'))
    gps_n = sum(1 for t in rows if t.get('clock_in_lat') not in (None, ''))
    active_now = sum(1 for t in DB['timecards'] if t.get('status') == 'clocked_in')
    return render_template('cm/timecards.html', rows=rows, fsite=fsite,
                           fstatus=fstatus, sort=sort, sites=DB['sites'],
                           total_hours=round(total_hours, 1), total=len(rows),
                           gps_pct=round(100 * gps_n / len(rows)) if rows else 0,
                           active_now=active_now, attention=needs_attention_count())


@bp.route('/timecards/export')
def timecard_export():
    # CSV download of the timecards list, honouring the page's filters
    # plus an optional ?from=YYYY-MM-DD&to=YYYY-MM-DD clock-in range
    # (inclusive; overnight shifts belong to their clock-in day).
    # A reversed range is swapped; missing bounds are open-ended.
    fsite = request.args.get('site') or 'all'
    fstatus = request.args.get('status') or 'all'
    sort = request.args.get('sort') or 'newest'

    def _day(v):
        v = (v or '').strip()
        return v if len(v) == 10 and v[4] == '-' and v[7] == '-' else ''
    frm, to = _day(request.args.get('from')), _day(request.args.get('to'))
    if frm and to and frm > to:
        frm, to = to, frm
    rows = []
    for t in DB['timecards']:
        if fsite != 'all' and t.get('site_id') != fsite:
            continue
        if fstatus != 'all' and t.get('status') != fstatus:
            continue
        d = (t.get('clock_in') or '')[:10]
        if (frm or to) and not d:
            continue
        if frm and d < frm:
            continue
        if to and d > to:
            continue
        rows.append(t)
    rows.sort(key=lambda t: t.get('clock_in') or '',
              reverse=(sort == 'newest'))

    def _d(dt):
        return (f"{dt.day}-{dt.strftime('%b')}-{dt.strftime('%y')}"
                if dt else '')

    def _t(dt):
        return dt.strftime('%-I:%M %p') if dt else ''

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Employee', 'Site', 'Clock-In Date', 'Clock-In Time',
                'Clock-Out Date', 'Clock-Out Time', 'Hours', 'Status', 'Notes'])
    for t in rows:
        ci = parse_iso(t.get('clock_in'))
        co = parse_iso(t.get('clock_out'))
        if co:
            hours = t.get('hours')
            if hours is None:
                hours = compute_hours(t.get('clock_in'), t.get('clock_out'))
        else:
            hours = ''
        w.writerow([worker_name(t.get('worker_id')), site_name(t.get('site_id')),
                    _d(ci), _t(ci), _d(co), _t(co),
                    hours, (t.get('status') or '').replace('_', ' ').title(),
                    t.get('notes') or ''])
    data = io.BytesIO(buf.getvalue().encode('utf-8-sig'))
    if frm and to:
        name = f'cm-timecards-{frm}_to_{to}.csv'
    elif frm:
        name = f'cm-timecards-from-{frm}.csv'
    elif to:
        name = f'cm-timecards-to-{to}.csv'
    else:
        name = 'cm-timecards.csv'
    return send_file(data, mimetype='text/csv', as_attachment=True,
                     download_name=name)


@bp.route('/timecards/new', methods=['GET', 'POST'])
def timecard_new():
    if request.method == 'POST':
        t = _timecard_from_form(request.form)
        DB['timecards'].append(t)
        save_db()
        flash('Timecard logged.')
        return redirect(url_for('cm.timecards'))
    return render_template('cm/timecard_form.html', t=None, workers=DB['workers'], sites=DB['sites'])


def _timecard_from_form(form):
    ci_date, ci_time = form.get('date', ''), form.get('clock_in', '')
    co_time = form.get('clock_out', '')
    clock_in = f"{ci_date}T{ci_time}:00" if ci_date and ci_time else None
    clock_out = f"{ci_date}T{co_time}:00" if ci_date and co_time else None
    status = 'clocked_in'
    hours = 0.0
    if clock_out:
        hours = compute_hours(clock_in, clock_out)
        status = 'clocked_out'
    return {'id': new_id(), 'worker_id': form.get('worker_id'),
            'site_id': form.get('site_id'), 'clock_in': clock_in,
            'clock_in_lat': None, 'clock_in_lng': None,
            'clock_out': clock_out, 'clock_out_lat': None, 'clock_out_lng': None,
            'hours': hours, 'status': status,
            'notes': form.get('notes', '').strip()}


@bp.route('/timecards/bulk', methods=['GET', 'POST'])
def timecard_bulk():
    if request.method == 'POST':
        n = 0
        for i in range(8):
            if not request.form.get(f'rows[{i}][worker_id]'):
                continue
            mini = {'worker_id': request.form.get(f'rows[{i}][worker_id]'),
                    'site_id': request.form.get(f'rows[{i}][site_id]'),
                    'date': request.form.get(f'rows[{i}][date]'),
                    'clock_in': request.form.get(f'rows[{i}][clock_in]'),
                    'clock_out': request.form.get(f'rows[{i}][clock_out]'),
                    'notes': request.form.get(f'rows[{i}][notes]', '')}
            t = _timecard_from_form(mini)
            DB['timecards'].append(t)
            n += 1
        save_db()
        flash(f'{n} timecards added.')
        return redirect(url_for('cm.timecards'))
    return render_template('cm/timecard_bulk.html', workers=DB['workers'], sites=DB['sites'],
                           today=today().isoformat())


@bp.route('/timecards/<tid>/edit', methods=['GET', 'POST'])
def timecard_edit(tid):
    t = find('timecards', tid)
    if not t:
        abort(404)
    if request.method == 'POST':
        upd = _timecard_from_form(request.form)
        t['worker_id'] = upd['worker_id'] or t['worker_id']
        t['site_id'] = upd['site_id'] or t['site_id']
        if upd['clock_in']:
            t['clock_in'] = upd['clock_in']
        if upd['clock_out']:
            t['clock_out'] = upd['clock_out']
            t['hours'] = upd['hours']
            if t['status'] == 'clocked_in':
                t['status'] = 'clocked_out'
        t['notes'] = upd['notes']
        save_db()
        flash('Timecard updated.')
        return redirect(url_for('cm.timecards'))
    return render_template('cm/timecard_form.html', t=t, workers=DB['workers'], sites=DB['sites'])


@bp.route('/timecards/<tid>/approve', methods=['POST'])
def timecard_approve(tid):
    t = find('timecards', tid)
    if t and t.get('status') == 'clocked_out':
        t['status'] = 'approved'
        save_db()
        flash('Timecard approved.')
    return redirect(url_for('cm.timecards'))


@bp.route('/timecards/<tid>/delete', methods=['POST'])
def timecard_delete(tid):
    if remove_from('timecards', tid):
        flash('Timecard deleted (moved to Recycle Bin).')
    return redirect(url_for('cm.timecards'))


def _payroll_range_data(wid, start_iso, end_iso):
    """One worker's calendar data for a date range (screen + email share it)."""
    cal_days = {}
    open_days = set()
    day_bounds = {}
    open_in_dt = {}
    for t in DB['timecards']:
        if t.get('worker_id') != wid:
            continue
        d = (t.get('clock_in') or '')[:10]
        if not (start_iso <= d <= end_iso):
            continue
        if t.get('clock_out'):
            h = t.get('hours') or compute_hours(t.get('clock_in'), t.get('clock_out'))
            cal_days[d] = cal_days.get(d, 0.0) + h
            ci = parse_iso(t.get('clock_in'))
            co = parse_iso(t.get('clock_out'))
            if ci and co:
                b = day_bounds.setdefault(d, [ci, co])
                b[0] = min(b[0], ci)
                b[1] = max(b[1], co)
        else:
            open_days.add(d)
            ci = parse_iso(t.get('clock_in'))
            if ci and (d not in open_in_dt or ci < open_in_dt[d]):
                open_in_dt[d] = ci
    day_io = {d: f"{b[0].strftime('%-I:%M %p')} – {b[1].strftime('%-I:%M %p')}"
              for d, b in day_bounds.items()}
    open_in = {d: ci.strftime('%-I:%M %p') for d, ci in open_in_dt.items()}
    cal = {d: _fmt_num(h) for d, h in cal_days.items()}
    total_hours = _fmt_num(sum(cal_days.values()))
    days_worked = len([d for d in set(list(cal_days) + list(open_days))
                       if cal_days.get(d, 0) > 0 or d in open_days])
    in24 = {d: b[0].strftime('%H:%M') for d, b in day_bounds.items()}
    out24 = {d: b[1].strftime('%H:%M') for d, b in day_bounds.items()}
    for d, ci in open_in_dt.items():
        in24.setdefault(d, ci.strftime('%H:%M'))
    return {'cal': cal, 'day_io': day_io, 'open_in': open_in,
            'open_days': open_days, 'total_hours': total_hours,
            'days_worked': days_worked, 'in24': in24, 'out24': out24}


def _payroll_email_body(name, start, end, data):
    """Plain-text payroll summary for one worker + range (screen's numbers)."""
    def _md(iso):
        d = date.fromisoformat(iso)
        return f"{d.month}/{d.day}"

    period = (f"{start.strftime('%b')} {start.day}, {start.year} – "
              f"{end.strftime('%b')} {end.day}, {end.year}")
    lines = [f"Hi {name},", "",
             f"Here is your payroll summary for {period}:", ""]
    for iso in sorted(set(list(data['cal']) + list(data['open_days']))):
        if data['cal'].get(iso):
            if data['day_io'].get(iso):
                line = f"{_md(iso)}: {data['day_io'][iso]}, {data['cal'][iso]}h"
            else:
                line = f"{_md(iso)}: {data['cal'][iso]}h"
            if iso in data['open_days']:
                if data['open_in'].get(iso):
                    line += f" (On shift since {data['open_in'][iso]})"
                else:
                    line += " (On shift)"
        else:
            line = f"{_md(iso)}: {data['open_in'].get(iso, '')} – On shift"
        lines.append(line)
    lines += ["", f"Total hours: {data['total_hours']}h",
              f"Days worked: {data['days_worked']}"]
    return "\n".join(lines)


def _payroll_calendar_html(start, end, data):
    """Calendar-grid HTML table mirroring the screen (for the HTML email)."""
    day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    first_monday = start - timedelta(days=start.weekday())
    last_sunday = end + timedelta(days=6 - end.weekday())
    parts = ['<table cellpadding="6" cellspacing="0" '
             'style="border-collapse:collapse;font-family:sans-serif">']
    parts.append('<tr>' + ''.join(
        f'<th style="border:1px solid #ccc;padding:6px;background:#eee">{d}</th>'
        for d in day_names) + '</tr>')
    cur = first_monday
    while cur <= last_sunday:
        cells = []
        for i in range(7):
            d = cur + timedelta(days=i)
            iso = d.isoformat()
            if not (start.isoformat() <= iso <= end.isoformat()):
                cells.append('<td style="border:1px solid #ccc;padding:6px"></td>')
                continue
            inner = f'<b>{d.month}/{d.day}</b>'
            if data['day_io'].get(iso):
                inner += f'<br>{data["day_io"][iso]}'
            elif data['open_in'].get(iso):
                inner += f'<br>{data["open_in"][iso]} – On shift'
            if data['cal'].get(iso):
                inner += f'<br><b>{data["cal"][iso]}h</b>'
            if iso in data['open_days'] and data['cal'].get(iso):
                inner += '<br>On shift'
                if data['open_in'].get(iso):
                    inner += f' · {data["open_in"][iso]}'
            cells.append('<td style="border:1px solid #ccc;padding:6px;'
                         f'vertical-align:top">{inner}</td>')
        parts.append('<tr>' + ''.join(cells) + '</tr>')
        cur += timedelta(days=7)
    parts.append('</table>')
    parts.append(f'<p>Total hours: {data["total_hours"]}h<br>'
                 f'Days worked: {data["days_worked"]}</p>')
    return ''.join(parts)


@bp.route('/timecards/payroll')
def payroll():
    """Retired 2026-11-13 (Part 1): the CM payroll calendar is now
    /payroll/ — one payroll source (data_payroll.json). The
    per-employee calendar lives on each employee page there.
    Bookmarks and QRs land here and are forwarded, not 404'd."""
    flash('Payroll moved to /payroll/ — the per-employee calendar now '
          'lives on each employee page there.')
    return redirect("/payroll/")  # hardcoded: stable public URL, works even if the payroll blueprint is not mounted


@bp.route('/timecards/payroll/email', methods=['POST'])
def payroll_email():
    """Retired 2026-11-13 (Part 1): email-this-payroll now lives on
    the per-employee calendar in /payroll/. Nothing is sent here."""
    flash('Payroll moved to /payroll/ — email this payroll from the '
          'per-employee calendar there.')
    return redirect("/payroll/")  # hardcoded: stable public URL, works even if the payroll blueprint is not mounted


def _t12(hhmm):
    """'HH:MM' (24h) -> '7:00 AM'; '' -> '—'."""
    if not hhmm:
        return '—'
    try:
        return datetime.strptime(hhmm, '%H:%M').strftime('%-I:%M %p')
    except ValueError:
        return hhmm


@bp.route('/payroll/review/<token>', methods=['GET', 'POST'])
def payroll_review(token):
    """Public worker page: review the emailed payroll, confirm or correct it.

    Token link only, no login (same pattern as the COA confirmations).
    Corrections are stored as requests — original timecards never change
    here; the admin reviews and applies them from the corrections page.
    """
    tokens = DB.setdefault('payroll_review_tokens', [])
    tok = next((t for t in tokens if t.get('token') == token), None)
    if not tok or not find('workers', tok['worker_id']):
        return render_template('cm/payroll_review.html', tok=None), 404
    wid, start, end = tok['worker_id'], tok['start'], tok['end']
    data = _payroll_range_data(wid, start, end)
    days = []
    for iso in sorted(set(list(data['cal']) + list(data['open_days']))):
        days.append({'date': iso,
                     'md': f"{int(iso[5:7])}/{int(iso[8:10])}",
                     'io': data['day_io'].get(iso, ''),
                     'hours': data['cal'].get(iso, ''),
                     'in24': data['in24'].get(iso, ''),
                     'out24': data['out24'].get(iso, ''),
                     'open': iso in data['open_days']})
    msg = None
    if request.method == 'POST':
        action = request.form.get('action')
        note = (request.form.get('note') or '').strip()[:1000]
        if action == 'confirm':
            DB.setdefault('payroll_corrections', []).append({
                'id': uuid.uuid4().hex[:12], 'token': token,
                'worker_id': wid, 'worker_name': worker_name(wid),
                'start': start, 'end': end, 'status': 'confirmed',
                'note': note, 'days': [], 'submitted_at': now_iso(),
                'applied_at': None})
            save_db()
            msg = 'confirmed'
        elif action == 'correct':
            changed = []
            for d in days:
                iso = d['date']
                nin = (request.form.get(f'in_{iso}') or '').strip()
                nout = (request.form.get(f'out_{iso}') or '').strip()
                oin, oout = d['in24'], d['out24']
                if (not nin or nin == oin) and (not nout or nout == oout):
                    continue
                fin, fout = nin or oin, nout or oout
                if fin and fout:
                    nh = _fmt_num(compute_hours(f"{iso}T{fin}:00",
                                                f"{iso}T{fout}:00"))
                else:
                    nh = d['hours']
                changed.append({'date': iso, 'orig_in': oin,
                                'orig_out': oout, 'orig_hours': d['hours'],
                                'new_in': fin, 'new_out': fout,
                                'new_hours': nh})
            if not changed:
                msg = 'nochange'
            else:
                DB.setdefault('payroll_corrections', []).append({
                    'id': uuid.uuid4().hex[:12], 'token': token,
                    'worker_id': wid, 'worker_name': worker_name(wid),
                    'start': start, 'end': end, 'status': 'correction',
                    'note': note, 'days': changed,
                    'submitted_at': now_iso(), 'applied_at': None})
                save_db()
                diff_lines = []
                for cd in changed:
                    o_out = _t12(cd['orig_out']) if cd['orig_out'] else 'On shift'
                    diff_lines.append(
                        f"{cd['date']}: {_t12(cd['orig_in'])} – {o_out} "
                        f"({cd['orig_hours']}h) → {_t12(cd['new_in'])} – "
                        f"{_t12(cd['new_out'])} ({cd['new_hours']}h)")
                import html as _html
                body = ("\n".join([
                    f"Payroll correction from {worker_name(wid)}",
                    f"Period: {start} – {end}",
                    f"Note: {note or '(no note)'}", ""] + diff_lines + [
                    "",
                    "Review: " + BASE_URL.rstrip('/')
                    + '/staffing/timecards/payroll/corrections']))
                sheets_post({'action': 'sendEmail', 'to': 'info@prismxe.com',
                             'subject': f"Payroll correction: {worker_name(wid)} "
                                        f"{start} – {end}",
                             'body': body,
                             'html': _html.escape(body).replace('\n', '<br>')},
                            timeout=30)
                msg = 'corrected'
    return render_template('cm/payroll_review.html', tok=tok,
                           name=worker_name(wid), start=start, end=end,
                           days=days, data=data, msg=msg)


@bp.route('/timecards/payroll/corrections')
def payroll_corrections():
    recs = sorted(DB.setdefault('payroll_corrections', []),
                  key=lambda r: r.get('submitted_at') or '', reverse=True)
    return render_template('cm/payroll_corrections.html', recs=recs, t12=_t12)


@bp.route('/timecards/payroll/corrections/<cid>/apply', methods=['POST'])
def payroll_correction_apply(cid):
    """Apply one correction: update the day's first clock-in / last
    clock-out and recompute those records' hours. Original values stay
    in the correction record as the audit trail."""
    DB.setdefault('payroll_corrections', [])
    rec = find('payroll_corrections', cid)
    if not rec or rec.get('status') != 'correction':
        flash('Nothing to apply.')
        return redirect(url_for('cm.payroll_corrections'))
    wid = rec['worker_id']
    for cd in rec['days']:
        d = cd['date']
        rows = sorted([t for t in DB['timecards']
                       if t.get('worker_id') == wid
                       and (t.get('clock_in') or '')[:10] == d],
                      key=lambda t: t.get('clock_in') or '')
        if not rows:
            continue
        first, last = rows[0], rows[-1]
        touched = set()
        if cd['new_in'] and cd['new_in'] != cd['orig_in']:
            ci = parse_iso(first.get('clock_in'))
            if ci:
                first['clock_in'] = ci.replace(
                    hour=int(cd['new_in'][:2]), minute=int(cd['new_in'][3:5]),
                    second=0).isoformat(timespec='seconds')
                touched.add(first.get('id'))
        if cd['new_out'] and cd['new_out'] != cd['orig_out']:
            co = (parse_iso(last.get('clock_out'))
                  or parse_iso(last.get('clock_in')))
            if co:
                last['clock_out'] = co.replace(
                    hour=int(cd['new_out'][:2]), minute=int(cd['new_out'][3:5]),
                    second=0).isoformat(timespec='seconds')
                if last.get('status') == 'clocked_in':
                    last['status'] = 'clocked_out'
                touched.add(last.get('id'))
        for t in rows:
            if t.get('id') in touched and t.get('clock_out'):
                t['hours'] = compute_hours(t.get('clock_in'),
                                          t.get('clock_out'))
    rec['status'] = 'applied'
    rec['applied_at'] = now_iso()
    save_db()
    flash(f"Applied corrections from {rec.get('worker_name', '—')}.")
    return redirect(url_for('cm.payroll_corrections'))


def _fmt_num(v):
    s = f"{round(v, 2):.2f}".rstrip('0').rstrip('.')
    return s if s else '0'


@bp.route('/timecards/payroll/export')
def payroll_export():
    """Retired 2026-11-13 (Part 1): the CM payroll CSV came from
    data_cm.json timecards. Exports now live in /payroll/."""
    flash('Payroll moved to /payroll/ — exports now come from the '
          'payroll module.')
    return redirect("/payroll/")  # hardcoded: stable public URL, works even if the payroll blueprint is not mounted


def _week_monday_cm(arg):
    """Monday (date) of the week containing arg (YYYY-MM-DD) or today."""
    try:
        d0 = date.fromisoformat(arg) if arg else None
    except (ValueError, TypeError):
        d0 = None
    if d0 is None:
        d0 = today()
    return d0 - timedelta(days=d0.weekday())


def _range_payroll_data_cm(wid, start, end):
    """One worker's calendar data for a date range, matching the screen.

    day_hours: ISO date -> summed stored hours (the screen's numbers).
    open_days: ISO dates with an open shift (the screen's 'On shift').
    detail: per-pair rows with stored payroll hours.
    """
    day_hours = {}
    open_days = set()
    day_bounds = {}
    open_in_dt = {}
    detail = []
    for t in DB['timecards']:
        if t.get('worker_id') != wid:
            continue
        d = (t.get('clock_in') or '')[:10]
        if not (start <= d <= end):
            continue
        if not t.get('clock_out'):
            open_days.add(d)
            ci = parse_iso(t.get('clock_in'))
            if ci and (d not in open_in_dt or ci < open_in_dt[d]):
                open_in_dt[d] = ci
            continue
        pay_hours = t.get('hours')
        ci = parse_iso(t.get('clock_in'))
        co = parse_iso(t.get('clock_out'))
        if pay_hours is None:
            if not ci or not co:
                continue
            pay_hours = compute_hours(t.get('clock_in'), t.get('clock_out'))
        gross = (round((co - ci).total_seconds() / 3600.0, 2)
                 if ci and co else None)
        meal = round(gross - pay_hours, 2) if gross is not None else None
        day_hours[d] = day_hours.get(d, 0.0) + pay_hours
        if ci and co:
            b = day_bounds.setdefault(d, [ci, co])
            b[0] = min(b[0], ci)
            b[1] = max(b[1], co)
        status = t.get('status', '')
        approved = status == 'approved'
        detail.append({
            'employee': worker_name(wid), 'site': site_name(t.get('site_id')),
            'date': d, 'ci': ci, 'co': co, 'gross': gross, 'meal': meal,
            'pay_hours': pay_hours,
            'payroll_status': 'Approved' if approved else 'Needs Approval',
            'timecard_status': ('Approved' if approved
                                else 'Clocked Out' if status == 'clocked_out'
                                else 'Clocked In'),
            'notes': t.get('notes', '') or '',
        })
    detail.sort(key=lambda r: (r['date'],
                               r['ci'].isoformat() if r['ci'] else ''))
    total = round(sum(day_hours.values()), 2)
    days_worked = len([d for d in set(list(day_hours) + list(open_days))
                       if day_hours.get(d, 0) > 0 or d in open_days])
    return {'name': worker_name(wid), 'day_hours': day_hours,
            'open_days': open_days, 'detail': detail, 'total': total,
            'days_worked': days_worked,
            'day_io': {d: f"{b[0].strftime('%-I:%M %p')} – {b[1].strftime('%-I:%M %p')}"
                       for d, b in day_bounds.items()},
            'open_in': {d: ci.strftime('%-I:%M %p')
                        for d, ci in open_in_dt.items()}}


def _range_payroll_xlsx_cm(blocks, start, end):
    """Calendar-style payroll workbook: one block per worker, weeks as
    Monday-first rows, hours exactly as the calendar screen shows, plus
    a Detail sheet with the same per-pair rows."""
    from openpyxl import Workbook
    from openpyxl.styles import (Font, Alignment, Border, Side, PatternFill)
    from openpyxl.comments import Comment
    from openpyxl.utils import get_column_letter
    from datetime import time as _time
    day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    wb = Workbook()
    ws = wb.active
    ws.title = 'Payroll'
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    thin = Side(style='thin', color='808080')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill('solid', fgColor='D9D9D9')
    center = Alignment(horizontal='center', vertical='center',
                       wrap_text=True)
    for col in range(1, 8):
        ws.column_dimensions[get_column_letter(col)].width = 15
    title = (('Payroll — ' + blocks[0]['name']) if len(blocks) == 1
             else 'Payroll — All workers')
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=7)
    c = ws.cell(row=1, column=1, value=title)
    c.font = Font(bold=True, size=16)
    c.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 26
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=7)
    c = ws.cell(row=2, column=1, value=f'{start} – {end}')
    c.font = Font(bold=True, size=11)
    c.alignment = Alignment(horizontal='center', vertical='center')
    row = 4
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    first_monday = start_d - timedelta(days=start_d.weekday())
    last_sunday = end_d + timedelta(days=6 - end_d.weekday())
    for block in blocks:
        ws.merge_cells(start_row=row, start_column=1, end_row=row,
                       end_column=7)
        c = ws.cell(row=row, column=1, value=block['name'])
        c.font = Font(bold=True, size=12)
        row += 1
        cur = first_monday
        while cur <= last_sunday:
            week = [cur + timedelta(days=i) for i in range(7)]
            for i, d in enumerate(week):
                cc = ws.cell(row=row, column=i + 1,
                             value=f'{day_names[i]} {d.month}/{d.day}')
                cc.font = Font(bold=True, size=10)
                cc.fill = hdr_fill
                cc.alignment = center
                cc.border = border
            row += 1
            for i, d in enumerate(week):
                iso = d.isoformat()
                cc = ws.cell(row=row, column=i + 1)
                cc.alignment = center
                cc.border = border
                if start <= iso <= end:
                    if block['day_io'].get(iso):
                        cc.value = block['day_io'][iso]
                        cc.font = Font(size=10)
                    elif block['open_in'].get(iso):
                        cc.value = block['open_in'][iso] + ' – On shift'
                        cc.font = Font(size=10, italic=True)
            row += 1
            for i, d in enumerate(week):
                iso = d.isoformat()
                cc = ws.cell(row=row, column=i + 1)
                cc.alignment = center
                cc.border = border
                if start <= iso <= end:
                    hrs = block['day_hours'].get(iso)
                    if hrs:
                        cc.value = round(hrs, 2)
                        cc.number_format = '0.00'
                        if iso in block['open_days']:
                            note = 'On shift'
                            if block['open_in'].get(iso):
                                note += f" since {block['open_in'][iso]}"
                            cc.comment = Comment(note, 'Payroll')
                    elif iso in block['open_days']:
                        cc.value = 'On shift'
                        cc.font = Font(size=10, italic=True)
                # out-of-range days stay blank, like the screen
            row += 1
            cur += timedelta(days=7)
        ws.cell(row=row, column=1, value='Total hours').font = Font(bold=True)
        cc = ws.cell(row=row, column=2, value=block['total'])
        cc.number_format = '0.00'
        cc.font = Font(bold=True)
        cc.alignment = center
        ws.cell(row=row, column=3, value='Days worked').font = Font(bold=True)
        cc = ws.cell(row=row, column=4, value=block['days_worked'])
        cc.font = Font(bold=True)
        cc.alignment = center
        row += 2
    for label in ['Confirmed by', 'Manager', 'Approved']:
        ws.cell(row=row, column=1, value=label).font = Font(bold=True,
                                                            size=11)
        ws.merge_cells(start_row=row, start_column=2, end_row=row,
                       end_column=4)
        ws.cell(row=row, column=5, value='Sign').font = Font(bold=True,
                                                             size=11)
        ws.merge_cells(start_row=row, start_column=6, end_row=row,
                       end_column=7)
        for col in range(2, 5):
            ws.cell(row=row, column=col).border = Border(bottom=thin)
        for col in range(6, 8):
            ws.cell(row=row, column=col).border = Border(bottom=thin)
        row += 2
    ws.print_area = f'A1:G{row - 1}'
    det = wb.create_sheet('Detail')
    det.sheet_properties.pageSetUpPr.fitToPage = True
    det.page_setup.orientation = 'landscape'
    det.page_setup.paperSize = det.PAPERSIZE_LETTER
    det.page_setup.fitToWidth = 1
    det.page_setup.fitToHeight = 0
    widths = {'A': 22, 'B': 18, 'C': 12, 'D': 11, 'E': 11, 'F': 11,
              'G': 7, 'H': 12, 'I': 14, 'J': 14, 'K': 30}
    for col, w in widths.items():
        det.column_dimensions[col].width = w
    headers = ['Employee', 'Site', 'Date', 'Clock-In', 'Clock-Out',
               'Gross Hours', 'Meal', 'Payroll Hours', 'Payroll Status',
               'Timecard Status', 'Notes']
    for col, label in enumerate(headers, start=1):
        c = det.cell(row=1, column=col, value=label)
        c.font = Font(bold=True, size=10)
        c.fill = hdr_fill
        c.alignment = center
        c.border = border
    r = 2
    all_detail = []
    for block in blocks:
        all_detail.extend(block['detail'])
    all_detail.sort(key=lambda d: (d['employee'].lower(), d['date'],
                                   d['ci'].isoformat() if d['ci'] else ''))
    for d in all_detail:
        det.cell(row=r, column=1, value=d['employee'])
        det.cell(row=r, column=2, value=d['site'])
        det.cell(row=r, column=3, value=d['date'])
        if d['ci']:
            ci = det.cell(row=r, column=4,
                          value=_time(d['ci'].hour, d['ci'].minute))
            ci.number_format = 'h:mm'
        if d['co']:
            co = det.cell(row=r, column=5,
                          value=_time(d['co'].hour, d['co'].minute))
            co.number_format = 'h:mm'
        if d['gross'] is not None:
            cc = det.cell(row=r, column=6, value=d['gross'])
            cc.number_format = '0.00'
        if d['meal'] is not None:
            cc = det.cell(row=r, column=7, value=d['meal'])
            cc.number_format = '0.00'
        cc = det.cell(row=r, column=8, value=d['pay_hours'])
        cc.number_format = '0.00'
        det.cell(row=r, column=9, value=d['payroll_status'])
        det.cell(row=r, column=10, value=d['timecard_status'])
        det.cell(row=r, column=11, value=d['notes'])
        for col in range(1, 12):
            det.cell(row=r, column=col).border = border
            det.cell(row=r, column=col).alignment = center
        det.cell(row=r, column=1).alignment = Alignment(
            horizontal='left', vertical='center')
        det.cell(row=r, column=11).alignment = Alignment(
            horizontal='left', vertical='center')
        r += 1
    det.freeze_panes = 'A2'
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@bp.route('/timecards/payroll/export.xlsx')
def payroll_export_xlsx():
    """Retired 2026-11-13 (Part 1): the CM payroll XLSX came from
    data_cm.json timecards. Exports now live in /payroll/."""
    flash('Payroll moved to /payroll/ — exports now come from the '
          'payroll module.')
    return redirect("/payroll/")  # hardcoded: stable public URL, works even if the payroll blueprint is not mounted


# ================= DATA & RESTORE =================

@bp.route('/data')
def data_page():
    trash = sorted(enumerate(DB['trash']),
                   key=lambda x: x[1].get('deleted_at', ''), reverse=True)
    return render_template('cm/data.html', trash=trash)


@bp.route('/data/backup')
def data_backup():
    save_db()
    return send_file(DB_PATH, mimetype='application/json', as_attachment=True,
                     download_name='site-manager-backup.json')


@bp.route('/data/trash/<int:idx>/restore', methods=['POST'])
def trash_restore(idx):
    try:
        item = DB['trash'][idx]
    except IndexError:
        abort(404)
    coll, rec = item['type'], item['record']
    if coll in COLLECTIONS and not find(coll, rec.get('id')):
        DB[coll].append(rec)
    DB['trash'].pop(idx)
    save_db()
    flash('Record restored.')
    return redirect(url_for('cm.data_page'))


@bp.route('/data/trash/<int:idx>/delete', methods=['POST'])
def trash_delete(idx):
    try:
        DB['trash'].pop(idx)
    except IndexError:
        abort(404)
    save_db()
    flash('Record permanently deleted.')
    return redirect(url_for('cm.data_page'))


@bp.route('/data/trash/empty', methods=['POST'])
def trash_empty():
    DB['trash'] = []
    save_db()
    flash('Recycle bin emptied.')
    return redirect(url_for('cm.data_page'))


# ================= ERROR / MAIN =================

@bp.errorhandler(404)
def not_found(e):
    return render_template('cm/error.html', code=404, message='Page not found.'), 404


@bp.errorhandler(410)
def gone(e):
    return render_template('cm/error.html', code=410, message='This link has expired.'), 410
