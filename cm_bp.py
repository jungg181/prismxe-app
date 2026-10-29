"""Construction Manager - standalone site management web app (Flask)."""
import os
import io
import csv
import json
import copy
import uuid
import threading
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

bp = Blueprint("cm", __name__, url_prefix="/cm",
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
    return sum(1 for t in DB['timecards'] if t.get('status') == 'clocked_out')


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
    )

NAV = [
    ('dashboard', 'Dashboard', '/cm/'),
    ('sites', 'Sites', '/cm/sites'),
    ('blueprints', 'Map & Plans', '/cm/blueprints'),
    ('workers', 'Workers', '/cm/workers'),
    ('timecards', 'Timecards', '/cm/timecards'),
    ('incidents', 'Incidents', '/cm/incidents'),
    ('daily_logs', 'Daily Logs', '/cm/daily-logs'),
    ('labour', 'Labour', '/cm/labour'),
    ('materials', 'Materials', '/cm/materials'),
    ('qa', 'QA/QC', '/cm/qa'),
    ('data', 'Data', '/cm/data'),
]


@bp.context_processor
def inject_nav():
    return dict(nav=NAV, active_tab=(request.endpoint or '').split('.')[-1],
                attention=needs_attention_count(),
                open_defects=open_defects_count())


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
    return render_template('cm/sites.html', rows=rows, q=request.args.get('q', ''),
                           status=status, counts=counts, total=len(DB['sites']))


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
    return render_template('cm/site_form.html', site=s, statuses=SITE_STATUSES)


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


# ================= SITE MAP & PLANS =================

@bp.route('/blueprints')
def blueprints():
    site_id = request.args.get('site_id') or (DB['sites'][0]['id'] if DB['sites'] else '')
    site = find('sites', site_id)
    floors = sorted({p['floor'] for p in DB['plans'] if p['site_id'] == site_id} | {'1F', '2F'})
    floor = request.args.get('floor') or floors[0]
    plan = next((p for p in DB['plans'] if p['site_id'] == site_id and p['floor'] == floor), None)
    pins = [p for p in DB['pins'] if p['site_id'] == site_id and p.get('floor') == floor]
    counts = {
        'personnel': sum(1 for t in DB['timecards']
                         if t['site_id'] == site_id and t.get('status') == 'clocked_in'
                         and t.get('clock_in_lat') not in (None, '')),
        'defect': sum(1 for p in pins if p.get('kind') == 'defect'),
        'hazard': sum(1 for p in pins if p.get('kind') == 'hazard'),
    }
    return render_template('cm/blueprints.html', sites=DB['sites'], site=site,
                           floors=floors, floor=floor, plan=plan,
                           pins=pins, counts=counts)


@bp.route('/blueprints/upload', methods=['POST'])
def plan_upload():
    site_id = request.form.get('site_id')
    floor = request.form.get('floor', '1F').strip() or '1F'
    img = save_upload(request.files.get('plan'))
    if not img:
        flash('No file uploaded.')
        return redirect(url_for('cm.blueprints', site_id=site_id))
    old = next((p for p in DB['plans'] if p['site_id'] == site_id and p['floor'] == floor), None)
    if old:
        DB['plans'].remove(old)
    DB['plans'].append({'id': new_id(), 'site_id': site_id, 'floor': floor,
                        'image_path': img, 'bounds': None})
    save_db()
    flash(f'Plan uploaded for {floor}.')
    return redirect(url_for('cm.blueprints', site_id=site_id, floor=floor))


@bp.route('/api/plan-bounds', methods=['POST'])
def api_plan_bounds():
    data = request.get_json(force=True) or {}
    plan = next((p for p in DB['plans'] if p['id'] == data.get('plan_id')), None)
    if not plan:
        return {'ok': False, 'error': 'plan not found'}, 404
    b = data.get('bounds')
    if not b or len(b) != 2:
        return {'ok': False, 'error': 'invalid bounds'}, 400
    plan['bounds'] = [[float(b[0][0]), float(b[0][1])], [float(b[1][0]), float(b[1][1])]]
    save_db()
    return {'ok': True}


@bp.route('/api/pins')
def api_pins():
    site_id = request.args.get('site_id')
    floor = request.args.get('floor')
    pins = [p for p in DB['pins']
            if (not site_id or p['site_id'] == site_id)
            and (not floor or p.get('floor') == floor)]
    return jsonify(pins)


@bp.route('/api/pins', methods=['POST'])
def api_pin_add():
    d = request.get_json(force=True)
    pin = {'id': new_id(), 'site_id': d.get('site_id'), 'floor': d.get('floor', '1F'),
           'kind': d.get('kind', 'defect'), 'x': d.get('x', 0), 'y': d.get('y', 0),
           'lat': d.get('lat'), 'lng': d.get('lng'),
           'label': (d.get('label') or '').strip() or d.get('kind', 'pin').title()}
    DB['pins'].append(pin)
    save_db()
    return jsonify(pin)


@bp.route('/api/pins/<pid>', methods=['DELETE'])
def api_pin_delete(pid):
    DB['pins'] = [p for p in DB['pins'] if p.get('id') != pid]
    save_db()
    return jsonify({'ok': True})


@bp.route('/api/personnel')
def api_personnel():
    site_id = request.args.get('site_id')
    out = []
    for t in DB['timecards']:
        if t.get('status') != 'clocked_in':
            continue
        if site_id and t['site_id'] != site_id:
            continue
        if t.get('clock_in_lat') in (None, ''):
            continue
        out.append({'worker_id': t['worker_id'],
                    'worker': worker_name(t['worker_id']),
                    'lat': t['clock_in_lat'], 'lng': t['clock_in_lng'],
                    'since': fmt_dt(t.get('clock_in'))})
    return jsonify(out)


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


@bp.route('/api/dispatch', methods=['POST'])
def api_dispatch():
    d = request.get_json(force=True)
    rec = {'id': new_id(), 'worker_id': d.get('worker_id'),
           'message': (d.get('message') or '').strip(),
           'created_at': now_iso()}
    DB['dispatches'].append(rec)
    save_db()
    return jsonify(rec)


# ================= QR ATTENDANCE =================

@bp.route('/api/qr', methods=['POST'])
def api_qr_create():
    d = request.get_json(force=True)
    token = uuid.uuid4().hex
    rec = {'token': token, 'site_id': d.get('site_id'),
           'action': 'toggle',
           'created_at': now_iso(),
           'expires_at': (datetime.now() + timedelta(minutes=10)).isoformat(timespec='seconds'),
           'clock_url': BASE_URL.rstrip('/') + '/cm/clock/' + token}
    DB['qr_tokens'] = [t for t in DB['qr_tokens'] if t['token'] != token]
    DB['qr_tokens'].append(rec)
    save_db()
    return jsonify(rec)


@bp.route('/qr/<token>.png')
def qr_image(token):
    try:
        import qrcode
    except ImportError:
        abort(500, 'qrcode library not installed')
    rec = next((t for t in DB['qr_tokens'] if t['token'] == token), None)
    if not rec:
        abort(404)
    img = qrcode.make(rec['clock_url'])
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


def _valid_token(token):
    rec = next((t for t in DB['qr_tokens'] if t['token'] == token), None)
    if not rec:
        return None
    if parse_iso(rec['expires_at']) and parse_iso(rec['expires_at']) < datetime.now():
        return None
    return rec


@bp.route('/clock/<token>')
def clock_page(token):
    rec = _valid_token(token)
    if not rec:
        return render_template('cm/clock.html', error='This QR code has expired. Ask your supervisor for a new one.'), 410
    active_workers = [w for w in DB['workers'] if w.get('status') == 'active']
    return render_template('cm/clock.html', rec=rec, site=find('sites', rec['site_id']),
                           workers=active_workers, error=None)


def _perform_clock(site_id, action, worker_id, lat, lng, at_iso=None,
                   offline=False):
    """Shared clock-in/out engine used by both the /clock page and the
    On Site worker app API. at_iso overrides the timestamp (used for
    offline-synced punches); offline marks the record as synced offline.
    Returns (ok, info dict)."""
    w = find('workers', worker_id)
    if not w:
        return False, {'error': 'Worker not found.'}
    now = at_iso or now_iso()
    if action == 'clock_in':
        if open_timecard(worker_id):
            return False, {'error': f"{w['name']} is already clocked in.",
                           'already_in': True}
        t = {'id': new_id(), 'worker_id': worker_id, 'site_id': site_id,
             'clock_in': now, 'clock_in_lat': lat, 'clock_in_lng': lng,
             'clock_out': None, 'clock_out_lat': None, 'clock_out_lng': None,
             'hours': 0.0, 'status': 'clocked_in', 'notes': 'QR clock-in',
             'offline': offline}
        DB['timecards'].append(t)
        save_db()
        return True, {'action': 'clock_in', 'site_id': site_id,
                      'site_name': site_name(site_id), 'worker_name': w['name'],
                      'clock_in': now, 'clock_out': None, 'hours': 0.0}
    t = open_timecard(worker_id, site_id) or open_timecard(worker_id)
    if not t:
        return False, {'error': f"No open shift found for {w['name']}."}
    t['clock_out'] = now
    t['clock_out_lat'] = lat
    t['clock_out_lng'] = lng
    t['hours'] = compute_hours(t['clock_in'], now)
    t['status'] = 'clocked_out'
    if offline:
        t['offline'] = True
    save_db()
    return True, {'action': 'clock_out', 'site_id': t['site_id'],
                  'site_name': site_name(t['site_id']), 'worker_name': w['name'],
                  'clock_in': t['clock_in'], 'clock_out': now,
                  'hours': t['hours']}


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


@bp.route('/clock/<token>/confirm', methods=['POST'])
def clock_confirm(token):
    rec = _valid_token(token)
    if not rec:
        return render_template('cm/clock.html', error='This QR code has expired.'), 410
    vals = [v for v in request.form.getlist('worker_id') if v]
    worker_id = vals[0] if vals else None
    lat = request.form.get('lat') or None
    lng = request.form.get('lng') or None
    w = find('workers', worker_id)
    if not w:
        flash('Worker not found.')
        return redirect(url_for('cm.clock_page', token=token))
    # Toggle mode: clock out if worker has an open timecard, else clock in
    action = rec['action']
    if action == 'toggle':
        action = 'clock_out' if open_timecard(worker_id) else 'clock_in'
    ok, info = _perform_clock(rec['site_id'], action, worker_id, lat, lng)
    if ok:
        if action == 'clock_in':
            return render_template('cm/clock_done.html',
                                   message=f"{w['name']} clocked in at {site_name(rec['site_id'])}.",
                                   site=site_name(rec['site_id']))
        return render_template('cm/clock_done.html',
                               message=f"{w['name']} clocked out. Shift: {info['hours']}h (pending approval).",
                               site=site_name(rec['site_id']))
    return render_template('cm/clock_done.html', message=info['error'],
                           site=site_name(rec['site_id']))


# ================= ON SITE WORKER APP =================

@bp.route('/onsite')
def onsite():
    sites = [s for s in DB['sites'] if s.get('status') == 'active']
    return render_template('cm/onsite.html', trades=TRADES, sites=sites)


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


@bp.route('/api/qr-info/<token>')
def api_qr_info(token):
    rec = _valid_token(token)
    if not rec:
        return jsonify({'ok': False, 'error': 'This QR code has expired. Ask your supervisor for a new one.'}), 410
    return jsonify({'ok': True, 'site_id': rec['site_id'],
                    'site_name': site_name(rec['site_id']),
                    'action': rec['action']})


@bp.route('/api/scan-clock', methods=['POST'])
def api_scan_clock():
    d = request.get_json(force=True)
    token = (d.get('token') or '').strip()
    rec = _valid_token(token)
    if not rec:
        return jsonify({'ok': False, 'error': 'This QR code has expired. Ask your supervisor for a new one.'}), 410
    worker_id = d.get('worker_id')
    ok, info = _perform_clock(rec['site_id'], rec['action'], worker_id,
                              d.get('lat'), d.get('lng'))
    info['ok'] = ok
    return jsonify(info), (200 if ok else 400)


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
    """Accept a clock action captured offline. The QR token is validated
    against the CLIENT timestamp: created_at <= client_at <= expires_at,
    i.e. the scan must have happened while the QR was valid. The timecard
    is stamped with the client time and flagged offline=True."""
    d = request.get_json(force=True)
    token = (d.get('token') or '').strip()
    rec = next((t for t in DB['qr_tokens'] if t['token'] == token), None)
    if not rec:
        return jsonify({'ok': False,
                        'error': 'QR code not recognized.'}), 410
    client_at = _client_at_local_iso(d.get('client_at'))
    if not client_at:
        return jsonify({'ok': False,
                        'error': 'Missing scan time.'}), 400
    created = parse_iso(rec.get('created_at'))
    expires = parse_iso(rec.get('expires_at'))
    if not created or not expires or not (created <= parse_iso(client_at) <= expires):
        return jsonify({'ok': False,
                        'error': 'QR expired before the scan.'}), 410
    worker_id = d.get('worker_id')
    if not find('workers', worker_id):
        return jsonify({'ok': False, 'error': 'Worker not found.'}), 400
    # Offline the worker picks the action manually (the QR can't be read
    # for its action), so it comes from the request, not the token.
    action = d.get('action')
    if action not in ('clock_in', 'clock_out'):
        return jsonify({'ok': False, 'error': 'Invalid action.'}), 400
    ok, info = _perform_clock(rec['site_id'], action, worker_id,
                              d.get('lat'), d.get('lng'),
                              at_iso=client_at, offline=True)
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
                    'clock_in': t.get('clock_in') if t else None,
                    'today_hours': round(today_hours, 2)})


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


@bp.route('/timecards/payroll')
def payroll():
    # Step 1: set a date range, step 2: pick a worker -> that range as a calendar.
    start_arg = request.args.get('start') or (today() - timedelta(days=13)).isoformat()
    end_arg = request.args.get('end') or today().isoformat()
    try:
        start = date.fromisoformat(start_arg)
    except ValueError:
        start = today() - timedelta(days=13)
    try:
        end = date.fromisoformat(end_arg)
    except ValueError:
        end = today()
    if start > end:
        start, end = end, start
    workers = [{'id': w['id'], 'name': worker_name(w['id'])}
               for w in sorted(DB['workers'], key=lambda w: worker_name(w['id']))]
    wid = request.args.get('worker') or ''
    if wid and not find('workers', wid):
        wid = ''
    cal_days = {}
    open_days = set()
    day_bounds = {}
    open_in_dt = {}
    if wid:
        for t in DB['timecards']:
            if t.get('worker_id') != wid:
                continue
            d = (t.get('clock_in') or '')[:10]
            if not (start.isoformat() <= d <= end.isoformat()):
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
    first_monday = start - timedelta(days=start.weekday())
    last_sunday = end + timedelta(days=6 - end.weekday())
    weeks = []
    cur = first_monday
    while cur <= last_sunday:
        weeks.append([(cur + timedelta(days=i)).isoformat() for i in range(7)])
        cur += timedelta(days=7)
    return render_template('cm/payroll.html', workers=workers, worker=wid,
                           start=start.isoformat(), end=end.isoformat(),
                           weeks=weeks, cal=cal, open_days=open_days,
                           day_io=day_io, open_in=open_in,
                           total_hours=total_hours, days_worked=days_worked,
                           monday=_week_monday_cm(start.isoformat()).isoformat(),
                           today_iso=today().isoformat())


def _fmt_num(v):
    s = f"{round(v, 2):.2f}".rstrip('0').rstrip('.')
    return s if s else '0'


@bp.route('/timecards/payroll/export')
def payroll_export():
    start = request.args.get('start') or (today() - timedelta(days=13)).isoformat()
    end = request.args.get('end') or today().isoformat()
    wid = request.args.get('worker') or ''
    if wid and not find('workers', wid):
        wid = ''
    detail = []
    for t in DB['timecards']:
        if wid and t.get('worker_id') != wid:
            continue
        if not t.get('clock_out'):
            continue
        d = (t.get('clock_in') or '')[:10]
        if not (start <= d <= end):
            continue
        ci = parse_iso(t.get('clock_in'))
        co = parse_iso(t.get('clock_out'))
        # Payroll hours = stored hours, exactly as the calendar screen shows.
        pay_hours = t.get('hours')
        if pay_hours is None:
            if not ci or not co:
                continue
            pay_hours = compute_hours(t.get('clock_in'), t.get('clock_out'))
        gross = (round((co - ci).total_seconds() / 3600.0, 2)
                 if ci and co else None)
        meal = round(gross - pay_hours, 2) if gross is not None else None
        status = t.get('status', '')
        approved = status == 'approved'
        detail.append({
            'employee': worker_name(t.get('worker_id')),
            'site': site_name(t.get('site_id')),
            'ci': ci, 'co': co,
            'gross': gross, 'meal': meal, 'pay_hours': pay_hours,
            'payroll_status': 'Approved' if approved else 'Needs Approval',
            'timecard_status': ('Approved' if approved
                                else 'Clocked Out' if status == 'clocked_out'
                                else 'Clocked In'),
            'notes': t.get('notes', '') or '',
        })
    detail.sort(key=lambda r: (r['employee'].lower(),
                               r['ci'].isoformat() if r['ci'] else ''))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Employee', 'Site', 'Clock-In Date', 'Clock-In Time',
                'Clock-Out Date', 'Clock-Out Time', 'Gross Hours',
                'Meal Break Deduction', 'Payroll Hours', 'Payroll Status',
                'Timecard Status', 'Notes'])

    def _d(dt):
        return (f"{dt.day}-{dt.strftime('%b')}-{dt.strftime('%y')}"
                if dt else '')

    def _t(dt):
        return dt.strftime('%-I:%M %p') if dt else ''

    for r in detail:
        w.writerow([
            r['employee'], r['site'],
            _d(r['ci']), _t(r['ci']), _d(r['co']), _t(r['co']),
            _fmt_num(r['gross']) if r['gross'] is not None else '',
            _fmt_num(r['meal']) if r['meal'] is not None else '',
            _fmt_num(r['pay_hours']),
            r['payroll_status'], r['timecard_status'], r['notes'],
        ])
    data = io.BytesIO(buf.getvalue().encode('utf-8-sig'))
    return send_file(data, mimetype='text/csv', as_attachment=True,
                     download_name=f'payroll-report-{start}-to-{end}.csv')


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
    start_arg = request.args.get('start')
    end_arg = request.args.get('end')
    if not start_arg and not end_arg and request.args.get('monday'):
        monday = _week_monday_cm(request.args.get('monday'))
        start_arg = monday.isoformat()
        end_arg = (monday + timedelta(days=6)).isoformat()
    start_arg = start_arg or (today() - timedelta(days=13)).isoformat()
    end_arg = end_arg or today().isoformat()
    try:
        start_d = date.fromisoformat(start_arg)
    except ValueError:
        start_d = today() - timedelta(days=13)
    try:
        end_d = date.fromisoformat(end_arg)
    except ValueError:
        end_d = today()
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    start, end = start_d.isoformat(), end_d.isoformat()
    wid = request.args.get('worker') or ''
    if wid and not find('workers', wid):
        wid = ''
    if wid:
        blocks = [_range_payroll_data_cm(wid, start, end)]
        who = blocks[0]['name'].replace(' ', '_')
    else:
        blocks = [_range_payroll_data_cm(w['id'], start, end)
                  for w in sorted(DB['workers'],
                                  key=lambda w: worker_name(w['id']))]
        who = 'All'
    data = _range_payroll_xlsx_cm(blocks, start, end)
    fn = f'Payroll_{who}_{start}_to_{end}.xlsx'
    return send_file(
        io.BytesIO(data),
        mimetype='application/vnd.openxmlformats-officedocument.'
                 'spreadsheetml.sheet',
        as_attachment=True, download_name=fn)


# ================= INCIDENTS =================

@bp.route('/incidents', methods=['GET', 'POST'])
def incidents():
    if request.method == 'POST':
        inc = {'id': new_id(), 'site_id': request.form.get('site_id'),
               'severity': request.form.get('severity', 'minor'),
               'status': 'open',
               'description': request.form.get('description', '').strip(),
               'photo_url': save_upload(request.files.get('photo')),
               'lat': _f(request.form.get('lat')), 'lng': _f(request.form.get('lng')),
               'created_at': now_iso()}
        DB['incidents'].append(inc)
        save_db()
        flash('Incident reported.')
        return redirect(url_for('cm.incidents'))
    fsite = request.args.get('site') or 'all'
    rows = [i for i in DB['incidents'] if fsite == 'all' or i['site_id'] == fsite]
    rows.sort(key=lambda i: i.get('created_at', ''), reverse=True)
    return render_template('cm/incidents.html', rows=rows, sites=DB['sites'], fsite=fsite)


@bp.route('/incidents/<iid>/resolve', methods=['POST'])
def incident_resolve(iid):
    i = find('incidents', iid)
    if i:
        i['status'] = 'resolved'
        save_db()
        flash('Incident marked resolved.')
    return redirect(url_for('cm.incidents'))


@bp.route('/incidents/<iid>/delete', methods=['POST'])
def incident_delete(iid):
    if remove_from('incidents', iid):
        flash('Incident deleted (moved to Recycle Bin).')
    return redirect(url_for('cm.incidents'))


# ================= DAILY LOGS =================

@bp.route('/daily-logs', methods=['GET', 'POST'])
def daily_logs():
    if request.method == 'POST':
        DB['daily_logs'].append({
            'id': new_id(), 'site_id': request.form.get('site_id'),
            'log_date': request.form.get('log_date') or today().isoformat(),
            'weather': request.form.get('weather', '').strip(),
            'notes': request.form.get('notes', '').strip(),
            'delays': request.form.get('delays', '').strip(),
            'visitors': request.form.get('visitors', '').strip()})
        save_db()
        flash('Daily log saved.')
        return redirect(url_for('cm.daily_logs'))
    fsite = request.args.get('site') or 'all'
    rows = [l for l in DB['daily_logs'] if fsite == 'all' or l['site_id'] == fsite]
    rows.sort(key=lambda l: l.get('log_date', ''), reverse=True)
    return render_template('cm/daily_logs.html', rows=rows, sites=DB['sites'],
                           fsite=fsite, today=today().isoformat())


@bp.route('/daily-logs/<lid>/delete', methods=['POST'])
def daily_log_delete(lid):
    if remove_from('daily_logs', lid):
        flash('Daily log deleted (moved to Recycle Bin).')
    return redirect(url_for('cm.daily_logs'))


# ================= LABOUR =================

@bp.route('/labour', methods=['GET', 'POST'])
def labour():
    if request.method == 'POST':
        DB['labour'].append({
            'id': new_id(), 'site_id': request.form.get('site_id'),
            'date': request.form.get('date') or today().isoformat(),
            'trade': request.form.get('trade', '').strip() or 'Other',
            'workers_count': int(request.form.get('workers_count') or 0),
            'hours': _f(request.form.get('hours')) or 0.0})
        save_db()
        flash('Labour entry logged.')
        return redirect(url_for('cm.labour'))
    tdy = today().isoformat()
    td = [l for l in DB['labour'] if l.get('date') == tdy]
    stats = {'workers': sum(l.get('workers_count', 0) for l in td),
             'hours': round(sum(l.get('hours', 0) for l in td), 1),
             'trades': len({l.get('trade') for l in td})}
    by_date = {}
    for l in sorted(DB['labour'], key=lambda x: x.get('date', ''), reverse=True):
        by_date.setdefault(l.get('date'), []).append(l)
    return render_template('cm/labour.html', stats=stats, by_date=by_date,
                           sites=DB['sites'], trades=TRADES, today=tdy)


@bp.route('/labour/<lid>/delete', methods=['POST'])
def labour_delete(lid):
    if remove_from('labour', lid):
        flash('Labour entry deleted (moved to Recycle Bin).')
    return redirect(url_for('cm.labour'))


# ================= MATERIALS =================

@bp.route('/materials')
def materials():
    fsite = request.args.get('site') or 'all'
    rows = [{'m': m, 'low': is_low_stock(m)} for m in DB['materials']
            if fsite == 'all' or m['site_id'] == fsite]
    return render_template('cm/materials.html', rows=rows, sites=DB['sites'], fsite=fsite)


@bp.route('/materials/new', methods=['GET', 'POST'])
def material_new():
    if request.method == 'POST':
        m = {'id': new_id(), 'site_id': request.form.get('site_id'),
             'name': request.form.get('name', '').strip(),
             'quantity': _f(request.form.get('quantity')) or 0,
             'unit': request.form.get('unit', '').strip() or 'pcs',
             'reorder_threshold': _f(request.form.get('reorder_threshold')) or 0,
             'photo_url': save_upload(request.files.get('photo'))}
        DB['materials'].append(m)
        save_db()
        flash(f"Material '{m['name']}' added.")
        return redirect(url_for('cm.materials'))
    return render_template('cm/material_form.html', m=None, sites=DB['sites'])


@bp.route('/materials/<mid>/edit', methods=['GET', 'POST'])
def material_edit(mid):
    m = find('materials', mid)
    if not m:
        abort(404)
    if request.method == 'POST':
        m['site_id'] = request.form.get('site_id')
        m['name'] = request.form.get('name', '').strip()
        m['quantity'] = _f(request.form.get('quantity')) or 0
        m['unit'] = request.form.get('unit', '').strip() or 'pcs'
        m['reorder_threshold'] = _f(request.form.get('reorder_threshold')) or 0
        ph = save_upload(request.files.get('photo'))
        if ph:
            m['photo_url'] = ph
        save_db()
        flash('Material updated.')
        return redirect(url_for('cm.materials'))
    return render_template('cm/material_form.html', m=m, sites=DB['sites'])


@bp.route('/materials/<mid>/delete', methods=['POST'])
def material_delete(mid):
    if remove_from('materials', mid):
        flash('Material deleted (moved to Recycle Bin).')
    return redirect(url_for('cm.materials'))


# ================= QA / QC =================

@bp.route('/qa')
def qa():
    tab = request.args.get('tab') or 'defects'
    fsite = request.args.get('site') or 'all'
    defects = [d for d in DB['defects'] if fsite == 'all' or d['site_id'] == fsite]
    defects.sort(key=lambda d: d.get('created_at', ''), reverse=True)
    checks = [c for c in DB['qa_checks'] if fsite == 'all' or c['site_id'] == fsite]
    checks.sort(key=lambda c: c.get('check_date', ''), reverse=True)
    return render_template('cm/qa.html', tab=tab, defects=defects, checks=checks,
                           sites=DB['sites'], fsite=fsite,
                           open_count=sum(1 for d in defects if d.get('status') == 'open'))


@bp.route('/qa/defects/new', methods=['GET', 'POST'])
def defect_new():
    if request.method == 'POST':
        d = {'id': new_id(), 'site_id': request.form.get('site_id'),
             'category': request.form.get('category', '').strip() or 'General',
             'severity': request.form.get('severity', 'minor'),
             'status': 'open',
             'location': request.form.get('location', '').strip(),
             'description': request.form.get('description', '').strip(),
             'photo_url': save_upload(request.files.get('photo')),
             'assigned_to': request.form.get('assigned_to', '').strip(),
             'due_date': request.form.get('due_date', ''),
             'created_at': now_iso()}
        DB['defects'].append(d)
        save_db()
        flash('Defect logged.')
        return redirect(url_for('cm.qa'))
    return render_template('cm/defect_form.html', d=None, sites=DB['sites'],
                           workers=DB['workers'])


@bp.route('/qa/defects/<did>/status', methods=['POST'])
def defect_status(did):
    d = find('defects', did)
    if d:
        d['status'] = request.form.get('status', 'open')
        save_db()
        flash('Defect status updated.')
    return redirect(url_for('cm.qa'))


@bp.route('/qa/defects/<did>/edit', methods=['GET', 'POST'])
def defect_edit(did):
    d = find('defects', did)
    if not d:
        abort(404)
    if request.method == 'POST':
        d['site_id'] = request.form.get('site_id')
        d['category'] = request.form.get('category', '').strip()
        d['severity'] = request.form.get('severity', 'minor')
        d['location'] = request.form.get('location', '').strip()
        d['description'] = request.form.get('description', '').strip()
        ph = save_upload(request.files.get('photo'))
        if ph:
            d['photo_url'] = ph
        d['assigned_to'] = request.form.get('assigned_to', '').strip()
        d['due_date'] = request.form.get('due_date', '')
        save_db()
        flash('Defect updated.')
        return redirect(url_for('cm.qa'))
    return render_template('cm/defect_form.html', d=d, sites=DB['sites'], workers=DB['workers'])


@bp.route('/qa/defects/<did>/delete', methods=['POST'])
def defect_delete(did):
    if remove_from('defects', did):
        flash('Defect deleted (moved to Recycle Bin).')
    return redirect(url_for('cm.qa'))


@bp.route('/qa/checks/new', methods=['GET', 'POST'])
def check_new():
    if request.method == 'POST':
        c = {'id': new_id(), 'site_id': request.form.get('site_id'),
             'name': request.form.get('name', '').strip(),
             'location': request.form.get('location', '').strip(),
             'result': request.form.get('result', 'pass'),
             'status': 'awaiting_signoff',
             'inspector': request.form.get('inspector', '').strip(),
             'notes': request.form.get('notes', '').strip(),
             'check_date': request.form.get('check_date') or today().isoformat()}
        DB['qa_checks'].append(c)
        save_db()
        flash('QA check logged.')
        return redirect(url_for('cm.qa', tab='checks'))
    return render_template('cm/check_form.html', sites=DB['sites'])


@bp.route('/qa/checks/<cid>/signoff', methods=['POST'])
def check_signoff(cid):
    c = find('qa_checks', cid)
    if c:
        c['status'] = 'signed_off'
        save_db()
        flash('Check signed off.')
    return redirect(url_for('cm.qa', tab='checks'))


@bp.route('/qa/checks/<cid>/delete', methods=['POST'])
def check_delete(cid):
    if remove_from('qa_checks', cid):
        flash('QA check deleted (moved to Recycle Bin).')
    return redirect(url_for('cm.qa', tab='checks'))


# ================= DATA & RESTORE =================

@bp.route('/data')
def data_page():
    trash = sorted(enumerate(DB['trash']),
                   key=lambda x: x[1].get('deleted_at', ''), reverse=True)
    snaps = sorted(DB['snapshots'], key=lambda s: s.get('created_at', ''), reverse=True)
    return render_template('cm/data.html', trash=trash, snapshots=snaps)


@bp.route('/data/backup')
def data_backup():
    save_db()
    return send_file(DB_PATH, mimetype='application/json', as_attachment=True,
                     download_name='site-manager-backup.json')


@bp.route('/data/snapshot', methods=['POST'])
def data_snapshot():
    name = (request.form.get('name') or '').strip() or f"Snapshot {now_iso()}"
    snap_data = {k: copy.deepcopy(v) for k, v in DB.items() if k != 'snapshots'}
    DB['snapshots'].append({'id': new_id(), 'name': name,
                            'created_at': now_iso(), 'data': snap_data})
    save_db()
    flash(f"Snapshot '{name}' saved.")
    return redirect(url_for('cm.data_page'))


@bp.route('/data/snapshot/<sid>/restore', methods=['POST'])
def snapshot_restore(sid):
    s = find('snapshots', sid)
    if not s:
        abort(404)
    snap = s['data']
    for coll in COLLECTIONS:
        if coll == 'snapshots':
            continue
        if coll in snap:
            DB[coll] = copy.deepcopy(snap[coll])
    save_db()
    flash(f"Restored snapshot '{s['name']}'.")
    return redirect(url_for('cm.data_page'))


@bp.route('/data/snapshot/<sid>/delete', methods=['POST'])
def snapshot_delete(sid):
    DB['snapshots'] = [s for s in DB['snapshots'] if s.get('id') != sid]
    save_db()
    flash('Snapshot deleted.')
    return redirect(url_for('cm.data_page'))


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
