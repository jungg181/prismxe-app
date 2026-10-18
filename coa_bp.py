"""
COA — Cleaners of America: fully separate time-clock module.

This is NOT part of /cm/. COA is a different company, so it has its own
blueprint (/coa), its own database file (data_coa.json), and its own
templates (templates/coa/). Data persists in two ways:

1. Google Drive folder (user-designated): every save backs up the full DB
   as COA_Data.json plus a human-readable COA_Timecards.csv, via the
   existing Apps Script web app (saveCoaBackup action).
2. On a fresh deploy (Render wipes the disk), the DB is restored from
   that Drive folder via the getCoaBackup action.
"""
import os
import io
import csv
import json
import uuid
import secrets
import threading
import urllib.request
import drive_backup
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for,
                   jsonify, abort, send_file)

bp = Blueprint('coa', __name__, url_prefix='/coa', template_folder='templates')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data_coa.json')
BASE_URL = os.environ.get('BASE_URL', 'https://app.prismxe.com')
# Same Apps Script web app as CM — used only as a transport to the
# COA Drive folder. COA data never touches the CM spreadsheet.
SHEETS_URL = os.environ.get('CM_SHEETS_URL', '')
COA_DRIVE_FOLDER_ID = '1b5hO9wUMl1GO4UBhRd6YUdptGdSRC0r7'

COLLECTIONS = ['sites', 'workers', 'timecards', 'confirmations']


# ---------- small helpers ----------

def new_id():
    return uuid.uuid4().hex[:8]


def now_iso():
    return datetime.now().isoformat(timespec='seconds')


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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


def clock_out_distance_km(t):
    s = find('sites', t.get('site_id'))
    if not s:
        return None
    return haversine_km(t.get('clock_out_lat'), t.get('clock_out_lng'),
                        s.get('lat'), s.get('lng'))


def fmt_coords(lat, lng):
    """Short 'lat, lng' string for at-a-glance display, or None."""
    try:
        return f"{float(lat):.4f}, {float(lng):.4f}"
    except (TypeError, ValueError):
        return None


def open_timecard(worker_id, site_id=None):
    for t in DB['timecards']:
        if t['worker_id'] == worker_id and t['status'] == 'clocked_in':
            if site_id is None or t['site_id'] == site_id:
                return t
    return None


def gmaps_link(lat, lng):
    if lat in (None, '') or lng in (None, ''):
        return None
    return f"https://www.google.com/maps?q={lat},{lng}"


# ---------- persistence: Drive folder backup + restore ----------

def _drive_post(payload, timeout=25):
    if not SHEETS_URL:
        return None
    try:
        req = urllib.request.Request(
            SHEETS_URL, data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return None


def load_db():
    if not os.path.exists(DB_PATH):
        # Fresh deploy — restore from the Drive folder backup.
        restored = drive_backup.restore()
        coa_data = restored.get('coa') if isinstance(restored, dict) else None
        if isinstance(coa_data, dict):
            db = coa_data
            for c in COLLECTIONS:
                db.setdefault(c, [])
            print('coa: restored DB from Drive folder', flush=True)
        else:
            db = {c: [] for c in COLLECTIONS}
        with open(DB_PATH, 'w') as f:
            json.dump(db, f, indent=2)
    else:
        with open(DB_PATH) as f:
            db = json.load(f)
        for c in COLLECTIONS:
            db.setdefault(c, [])
    return db


def save_db():
    with open(DB_PATH, 'w') as f:
        json.dump(DB, f, indent=2)
    drive_backup.backup_async()
    # Piggyback on the CM Sheets sync so COA data survives Render deploys
    # even before the Apps Script update (the old script blind-stores the
    # saveData blob, wrapped as {"cm":..., "coa":...}).
    try:
        from cm_bp import sheets_sync_db
        sheets_sync_db()
    except Exception as e:  # noqa: BLE001
        print(f"coa: sheets sync failed: {e}", flush=True)


DB = load_db()


# ---------- clock engine ----------

def _perform_clock(site_id, action, worker_id, lat, lng, at_iso=None,
                   offline=False, reported_hours=None):
    w = find('workers', worker_id)
    if not w:
        return False, {'error': 'Worker not found.'}
    now = at_iso or now_iso()
    try:
        rh = float(reported_hours) if reported_hours not in (None, '') else None
        if rh is not None and (rh < 0 or rh > 24):
            rh = None
    except (TypeError, ValueError):
        rh = None
    if action == 'clock_in':
        if open_timecard(worker_id):
            return False, {'error': f"{w['name']} is already clocked in.",
                           'already_in': True}
        t = {'id': new_id(), 'worker_id': worker_id, 'site_id': site_id,
             'clock_in': now, 'clock_in_lat': lat, 'clock_in_lng': lng,
             'clock_out': None, 'clock_out_lat': None, 'clock_out_lng': None,
             'hours': 0.0, 'status': 'clocked_in', 'notes': 'COA clock-in',
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
    if rh is not None:
        t['reported_hours'] = round(rh, 2)
    if offline:
        t['offline'] = True
    save_db()
    return True, {'action': 'clock_out', 'site_id': t['site_id'],
                  'site_name': site_name(t['site_id']), 'worker_name': w['name'],
                  'clock_in': t['clock_in'], 'clock_out': now,
                  'hours': t['hours'],
                  'reported_hours': t.get('reported_hours')}


def _find_or_register_worker(name, email, phone, site_id):
    """Email is the identity key; falls back to phone, then name."""
    email = (email or '').strip().lower()
    phone = (phone or '').strip()
    name = (name or '').strip()
    if '@' not in email or '.' not in email.split('@')[-1]:
        return None, 'Enter a valid email address.'
    if not name:
        return None, 'Enter your name.'
    w = next((x for x in DB['workers']
              if (x.get('email') or '').strip().lower() == email), None)
    if not w and phone:
        w = next((x for x in DB['workers']
                  if (x.get('phone') or '').strip() == phone), None)
    if not w:
        w = next((x for x in DB['workers']
                  if x.get('name', '').strip().lower() == name.lower()), None)
    if not w:
        w = {'id': new_id(), 'name': name, 'email': email, 'phone': phone,
             'site_id': site_id or '', 'status': 'active',
             'created_at': now_iso()}
        DB['workers'].append(w)
    else:
        if not w.get('email'):
            w['email'] = email
        if phone and not w.get('phone'):
            w['phone'] = phone
    return w, None


# ================= WORKER APP =================

@bp.route('/clock')
def clock_page():
    sites = [s for s in DB['sites'] if s.get('status') == 'active']
    # ?site=<site_id> — opened from a jobsite QR code: the site is fixed.
    fixed_site = find('sites', request.args.get('site') or '')
    if fixed_site and fixed_site.get('status') != 'active':
        fixed_site = None
    return render_template('coa/clock.html', sites=sites,
                           fixed_site=fixed_site,
                           workers=sorted(DB['workers'], key=lambda w: w['name'].lower()))


@bp.route('/sw.js')
def service_worker():
    """Serve the COA service worker from the /coa scope so installed
    home-screen app works offline."""
    return send_file(os.path.join(BASE_DIR, 'static', 'coa', 'sw.js'),
                     mimetype='application/javascript')


@bp.route('/qr/<site_id>.png')
def qr_image(site_id):
    """Printable QR code for a jobsite. Workers scan it to open the COA
    clock with that site pre-selected — the same code registers them and
    punches their timecards."""
    try:
        import qrcode
    except ImportError:
        abort(500, 'qrcode library not installed')
    s = find('sites', site_id)
    if not s:
        abort(404)
    url = BASE_URL.rstrip('/') + '/coa/clock?site=' + site_id
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


@bp.route('/api/workers')
def api_workers():
    return jsonify([{'id': w['id'], 'name': w['name'],
                     'email': w.get('email', ''), 'phone': w.get('phone', '')}
                    for w in sorted(DB['workers'], key=lambda w: w['name'].lower())])


@bp.route('/api/clock', methods=['POST'])
def api_clock():
    """Clock in/out. Registers the worker by email on first use (main
    flow), records the punch with GPS, and backs everything up to Drive."""
    d = request.get_json(force=True)
    w = find('workers', d.get('worker_id')) if d.get('worker_id') else None
    if not w:
        w, err = _find_or_register_worker(d.get('name'), d.get('email'),
                                          d.get('phone'), d.get('site_id'))
        if not w:
            return jsonify({'ok': False, 'error': err}), 400
    worker_id = w['id']
    action = d.get('action')
    if action not in ('clock_in', 'clock_out'):
        action = 'clock_out' if open_timecard(worker_id) else 'clock_in'
    site_id = d.get('site_id') or w.get('site_id') or ''
    if action == 'clock_in':
        if not find('sites', site_id):
            return jsonify({'ok': False, 'error': 'Choose a jobsite first.'}), 400
        w['site_id'] = site_id
    ok, info = _perform_clock(site_id, action, worker_id,
                              d.get('lat'), d.get('lng'),
                              reported_hours=d.get('reported_hours'))
    info['ok'] = ok
    info['worker_id'] = worker_id
    info['worker_name'] = w['name']
    return jsonify(info), (200 if ok else 400)


@bp.route('/api/worker-status/<worker_id>')
def api_worker_status(worker_id):
    w = find('workers', worker_id)
    if not w:
        abort(404)
    t = open_timecard(worker_id)
    return jsonify({'worker_id': worker_id, 'worker_name': w['name'],
                    'clocked_in': t is not None,
                    'site_id': t['site_id'] if t else None,
                    'site_name': site_name(t['site_id']) if t else None,
                    'clock_in': t.get('clock_in') if t else None})


@bp.route('/api/my-timecards/<worker_id>')
def api_my_timecards(worker_id):
    try:
        days = max(1, min(90, int(request.args.get('days', 7))))
    except (TypeError, ValueError):
        days = 7
    cutoff = (datetime.now() - timedelta(days=days - 1)).date().isoformat()
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


@bp.route('/api/sync-clock', methods=['POST'])
def api_sync_clock():
    """Accept a clock action captured offline (site_id based). Stamped with
    the client time and flagged offline=True."""
    d = request.get_json(force=True)
    site_id = d.get('site_id')
    if not site_id or not find('sites', site_id):
        return jsonify({'ok': False, 'error': 'Unknown jobsite.'}), 400
    client_at = parse_iso(d.get('client_at'))
    client_at = client_at.isoformat(timespec='seconds') if client_at else now_iso()
    worker_id = d.get('worker_id')
    w = find('workers', worker_id) if worker_id else None
    if not w:
        w, err = _find_or_register_worker(d.get('name'), d.get('email'),
                                          d.get('phone'), site_id)
        if not w:
            return jsonify({'ok': False, 'error': err}), 400
        worker_id = w['id']
    action = d.get('action')
    if action not in ('clock_in', 'clock_out'):
        return jsonify({'ok': False, 'error': 'Invalid action.'}), 400
    ok, info = _perform_clock(site_id, action, worker_id,
                              d.get('lat'), d.get('lng'),
                              at_iso=client_at, offline=True,
                              reported_hours=d.get('reported_hours'))
    info['ok'] = ok
    info['worker_id'] = worker_id
    info['worker_name'] = w['name']
    return jsonify(info), (200 if ok else 400)


# ================= ADMIN =================

@bp.route('/')
def dashboard():
    today = datetime.now().date().isoformat()
    on_shift = [t for t in DB['timecards'] if t.get('status') == 'clocked_in']
    today_hours = sum(t.get('hours') or compute_hours(t.get('clock_in'), t.get('clock_out'))
                      for t in DB['timecards']
                      if (t.get('clock_in') or '').startswith(today) and t.get('clock_out'))
    shift_rows = [{'worker': worker_name(t['worker_id']),
                   'site': site_name(t['site_id']),
                   'clock_in': fmt_dt(t.get('clock_in'))} for t in on_shift]
    return render_template('coa/dashboard.html',
                           n_workers=len(DB['workers']),
                           n_sites=len([s for s in DB['sites'] if s.get('status') == 'active']),
                           shift_rows=shift_rows,
                           today_hours=round(today_hours, 1),
                           total_timecards=len(DB['timecards']))


@bp.route('/sites')
def sites():
    return render_template('coa/sites.html',
                           sites=sorted(DB['sites'], key=lambda s: s['name'].lower()))


@bp.route('/sites/new', methods=['GET', 'POST'])
def site_new():
    if request.method == 'POST':
        s = {'id': new_id(),
             'name': request.form.get('name', '').strip(),
             'address': request.form.get('address', '').strip(),
             'lat': _f(request.form.get('lat')),
             'lng': _f(request.form.get('lng')),
             'status': 'active',
             'created_at': now_iso()}
        if not s['name']:
            return render_template('coa/site_form.html', site=None, error='Name is required.')
        DB['sites'].append(s)
        save_db()
        return redirect(url_for('coa.sites'))
    return render_template('coa/site_form.html', site=None, error=None)


@bp.route('/sites/<sid>/edit', methods=['GET', 'POST'])
def site_edit(sid):
    s = find('sites', sid)
    if not s:
        abort(404)
    if request.method == 'POST':
        s['name'] = request.form.get('name', '').strip()
        s['address'] = request.form.get('address', '').strip()
        s['lat'] = _f(request.form.get('lat'))
        s['lng'] = _f(request.form.get('lng'))
        s['status'] = request.form.get('status', 'active')
        save_db()
        return redirect(url_for('coa.sites'))
    return render_template('coa/site_form.html', site=s, error=None)


@bp.route('/sites/<sid>/delete', methods=['POST'])
def site_delete(sid):
    s = find('sites', sid)
    if s:
        DB['sites'].remove(s)
        save_db()
    return redirect(url_for('coa.sites'))


@bp.route('/workers')
def workers():
    q = (request.args.get('q') or '').lower()
    rows = [w for w in DB['workers']
            if not q or q in f"{w['name']} {w.get('email','')} {w.get('phone','')}".lower()]
    rows.sort(key=lambda w: w['name'].lower())
    return render_template('coa/workers.html', rows=rows, q=request.args.get('q', ''))


@bp.route('/timecards')
def timecards():
    fsite = request.args.get('site') or 'all'
    fstatus = request.args.get('status') or 'all'
    sort = request.args.get('sort') or 'newest'
    rows = [t for t in DB['timecards']
            if (fsite == 'all' or t.get('site_id') == fsite)
            and (fstatus == 'all' or t.get('status') == fstatus)]
    rows.sort(key=lambda t: t.get('clock_in') or '', reverse=(sort == 'newest'))
    gps_n = sum(1 for t in rows if t.get('clock_in_lat') not in (None, ''))
    return render_template('coa/timecards.html', rows=rows,
                           fsite=fsite, fstatus=fstatus, sort=sort,
                           sites=DB['sites'],
                           total=len(rows),
                           gps_pct=round(100 * gps_n / len(rows)) if rows else 0,
                           active_now=sum(1 for t in DB['timecards'] if t.get('status') == 'clocked_in'),
                           fmt_dt=fmt_dt, gmaps_link=gmaps_link,
                           compute_hours=compute_hours,
                           clock_in_distance_km=clock_in_distance_km,
                           clock_out_distance_km=clock_out_distance_km,
                           fmt_coords=fmt_coords,
                           worker_name=worker_name)


@bp.route('/timecards/<tid>/approve', methods=['POST'])
def timecard_approve(tid):
    t = find('timecards', tid)
    if t and t.get('status') == 'clocked_out':
        t['status'] = 'approved'
        save_db()
    return redirect(url_for('coa.timecards'))


@bp.route('/timecards/<tid>/edit', methods=['GET', 'POST'])
def timecard_edit(tid):
    """Manually fix clock in/out times (e.g. worker forgot to clock out)."""
    t = find('timecards', tid)
    if not t:
        abort(404)
    if request.method == 'POST':
        date = request.form.get('date', '').strip()
        ci = request.form.get('clock_in', '').strip()
        co = request.form.get('clock_out', '').strip()
        if date and ci:
            t['clock_in'] = f"{date}T{ci}:00"
        if date and co:
            t['clock_out'] = f"{date}T{co}:00"
            if t.get('status') == 'clocked_in':
                t['status'] = 'clocked_out'
        if t.get('clock_in') and t.get('clock_out'):
            t['hours'] = compute_hours(t['clock_in'], t['clock_out'])
        t['worker_id'] = request.form.get('worker_id') or t.get('worker_id')
        t['site_id'] = request.form.get('site_id') or t.get('site_id')
        rh = request.form.get('reported_hours', '').strip()
        try:
            t['reported_hours'] = float(rh) if rh else None
        except ValueError:
            pass
        save_db()
        return redirect(url_for('coa.timecards'))
    return render_template('coa/timecard_form.html', t=t,
                           workers=DB['workers'], sites=DB['sites'])


@bp.route('/timecards/<tid>/delete', methods=['POST'])
def timecard_delete(tid):
    t = find('timecards', tid)
    if t:
        DB['timecards'].remove(t)
        save_db()
    return redirect(url_for('coa.timecards'))


@bp.route('/api/backup-now', methods=['POST'])
def api_backup_now():
    """Manual trigger: back up the COA DB to the Drive folder right now."""
    drive_backup.backup_async()
    return jsonify({'ok': True})


# ================= PAY-PERIOD CONFIRMATIONS =================
# A few days before payday the admin picks the worked date range and sends
# each worker a confirmation email (everyone registers with an email).
# The worker opens their personal link, reviews their hours and taps
# "No problem" or "There's a problem" — the admin sees the results here.

def _email_available():
    """True when the deployed Apps Script supports sendEmail."""
    res = _drive_post({'action': 'sendEmail', 'probe': True}, timeout=15)
    return bool(res and res.get('ok'))


def _send_email(to, subject, body, html):
    res = _drive_post({'action': 'sendEmail', 'to': to, 'subject': subject,
                       'body': body, 'html': html}, timeout=30)
    return bool(res and res.get('ok'))


def _site_name(site_id):
    s = find('sites', site_id)
    return s['name'] if s else '—'


def _period_rows(worker_id, start, end):
    """Completed timecards for a worker with clock-in date in [start, end]."""
    rows = [t for t in DB['timecards']
            if t.get('worker_id') == worker_id
            and t.get('status') in ('clocked_out', 'approved')
            and start <= (t.get('clock_in') or '')[:10] <= end]
    rows.sort(key=lambda t: t.get('clock_in') or '')
    return rows


def _period_hours(rows):
    total = 0.0
    for t in rows:
        h = t.get('hours')
        if h is None:
            h = compute_hours(t.get('clock_in'), t.get('clock_out'))
        try:
            total += float(h)
        except (TypeError, ValueError):
            pass
    return round(total, 2)


def _confirm_url(rec):
    return BASE_URL.rstrip('/') + '/coa/confirm/' + rec['token']


def _confirmation_email(rec):
    link = _confirm_url(rec)
    shift_rows = ''.join(
        '<tr><td style="padding:6px 10px;border-bottom:1px solid #eee">%s</td>'
        '<td style="padding:6px 10px;border-bottom:1px solid #eee">%s</td>'
        '<td style="padding:6px 10px;border-bottom:1px solid #eee">%s – %s</td>'
        '<td style="padding:6px 10px;border-bottom:1px solid #eee">%s h</td></tr>'
        % ((t.get('clock_in') or '')[:10], _site_name(t.get('site_id')),
           fmt_dt(t.get('clock_in')), fmt_dt(t.get('clock_out')),
           t.get('hours') if t.get('hours') is not None
           else compute_hours(t.get('clock_in'), t.get('clock_out')))
        for t in rec['details'])
    subject = f"Please confirm your hours — {rec['period_start']} to {rec['period_end']}"
    body = (f"Hi {rec['worker_name']},\n\n"
            f"Please review your hours for {rec['period_start']} to {rec['period_end']}.\n"
            f"Total: {rec['hours']} hours.\n\n"
            f"Open this link to confirm: {link}\n\n"
            f"Tap 'No problem' if the hours are correct, or 'There's a problem' "
            f"and tell us what's wrong.\n\n— Cleaners of America")
    html = (f"<p>Hi {rec['worker_name']},</p>"
            f"<p>Please review your hours for <b>{rec['period_start']}</b> to "
            f"<b>{rec['period_end']}</b>.</p>"
            f"<table style='border-collapse:collapse'>"
            f"<tr><th style='text-align:left;padding:6px 10px'>Date</th>"
            f"<th style='text-align:left;padding:6px 10px'>Jobsite</th>"
            f"<th style='text-align:left;padding:6px 10px'>Shift</th>"
            f"<th style='text-align:left;padding:6px 10px'>Hours</th></tr>"
            f"{shift_rows}</table>"
            f"<p><b>Total: {rec['hours']} hours</b></p>"
            f"<p><a href='{link}' style='display:inline-block;background:#0d4f3c;"
            f"color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;"
            f"font-weight:700'>Review &amp; Confirm My Hours</a></p>"
            f"<p>If the button doesn't work, open this link:<br>{link}</p>"
            f"<p>— Cleaners of America</p>")
    return subject, body, html


def _build_confirmation(worker, start, end):
    rows = _period_rows(worker['id'], start, end)
    if not rows:
        return None
    details = [{
        'date': (t.get('clock_in') or '')[:10],
        'site': _site_name(t.get('site_id')),
        'clock_in': t.get('clock_in'), 'clock_out': t.get('clock_out'),
        'hours': t.get('hours') if t.get('hours') is not None
                 else compute_hours(t.get('clock_in'), t.get('clock_out')),
        'reported_hours': t.get('reported_hours'),
        'status': t.get('status'),
    } for t in rows]
    return {
        'id': uuid.uuid4().hex[:12],
        'worker_id': worker['id'],
        'worker_name': worker.get('name', '—'),
        'email': worker.get('email', ''),
        'period_start': start, 'period_end': end,
        'hours': _period_hours(rows),
        'details': details,
        'token': secrets.token_urlsafe(32),
        'status': 'pending',  # pending | confirmed | disputed
        'note': '',
        'sent_at': None, 'responded_at': None,
        'created_at': now_iso(),
    }


@bp.route('/confirmations')
def confirmations():
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    preview = []
    if start and end and start <= end:
        for w in DB['workers']:
            rec = _build_confirmation(w, start, end)
            if rec:
                preview.append(rec)
        preview.sort(key=lambda r: r['worker_name'].lower())
    email_ok = _email_available()
    recs = sorted(DB['confirmations'], key=lambda r: r.get('created_at') or '',
                  reverse=True)
    return render_template('coa/confirmations.html', start=start, end=end,
                           preview=preview, recs=recs, email_ok=email_ok,
                           worker_name=worker_name)


@bp.route('/confirmations/send', methods=['POST'])
def confirmations_send():
    start = request.form.get('start', '').strip()
    end = request.form.get('end', '').strip()
    if not (start and end and start <= end):
        return redirect(url_for('coa.confirmations'))
    sent, skipped, failed = 0, 0, 0
    email_ok = _email_available()
    for w in DB['workers']:
        # don't duplicate a pending confirmation for the same worker+period
        dup = any(r['worker_id'] == w['id'] and r['period_start'] == start
                  and r['period_end'] == end and r['status'] == 'pending'
                  for r in DB['confirmations'])
        if dup:
            skipped += 1
            continue
        rec = _build_confirmation(w, start, end)
        if not rec:
            continue
        if email_ok and rec['email']:
            subject, body, html = _confirmation_email(rec)
            if _send_email(rec['email'], subject, body, html):
                rec['sent_at'] = now_iso()
                sent += 1
            else:
                failed += 1
        else:
            # no email delivery (no address or old Apps Script) — the record
            # is still created so the admin can copy the link manually.
            failed += 1
        DB['confirmations'].append(rec)
    save_db()
    return redirect(url_for('coa.confirmations', start=start, end=end,
                            sent=sent, failed=failed, skipped=skipped,
                            _anchor='results'))


@bp.route('/confirmations/<cid>/resend', methods=['POST'])
def confirmation_resend(cid):
    rec = find('confirmations', cid)
    if rec and rec.get('email') and _email_available():
        subject, body, html = _confirmation_email(rec)
        if _send_email(rec['email'], subject, body, html):
            rec['sent_at'] = now_iso()
            rec['status'] = 'pending'
            rec['responded_at'] = None
            save_db()
    return redirect(url_for('coa.confirmations', _anchor='results'))


@bp.route('/confirm/<token>', methods=['GET', 'POST'])
def confirm_hours(token):
    """Public worker page: review hours and tap No problem / There's a problem."""
    rec = next((r for r in DB['confirmations'] if r.get('token') == token), None)
    if not rec:
        return render_template('coa/confirm.html', rec=None), 404
    msg = None
    if request.method == 'POST' and rec['status'] == 'pending':
        choice = request.form.get('choice')
        if choice == 'ok':
            rec['status'] = 'confirmed'
            rec['responded_at'] = now_iso()
            msg = 'ok'
        elif choice == 'problem':
            rec['status'] = 'disputed'
            rec['note'] = request.form.get('note', '').strip()[:1000]
            rec['responded_at'] = now_iso()
            msg = 'problem'
            # notify the admin (script owner) by email — best effort
            _drive_post({'action': 'notifyDispute',
                         'worker': f"{rec['worker_name']} <{rec['email']}>",
                         'period': f"{rec['period_start']} to {rec['period_end']}",
                         'note': rec['note'] or '(no note)'}, timeout=20)
        save_db()
    return render_template('coa/confirm.html', rec=rec, msg=msg,
                           site_name=_site_name)


# ================= EMPLOYEE CONTACTS =================
# Phone directory: COA cleaners + CM crew, tap to call / text.

def _contact_list():
    contacts = []
    for w in DB['workers']:
        phone = (w.get('phone') or '').strip()
        if not phone:
            continue
        name = w.get('name') or '—'
        sub = w.get('email') or 'COA'
        contacts.append({
            'name': name, 'phone': phone,
            'phone_display': phone, 'sub': sub, 'team': 'coa',
            'search': f"{name} {phone} {sub}".lower(),
        })
    try:
        from cm_bp import DB as CM_DB
        for w in CM_DB.get('workers', []):
            phone = (w.get('phone') or '').strip()
            if not phone:
                continue
            name = w.get('name') or '—'
            parts = [p for p in (w.get('company'), w.get('trade')) if p]
            sub = ' · '.join(parts) if parts else 'CM Crew'
            contacts.append({
                'name': name, 'phone': phone,
                'phone_display': phone, 'sub': sub, 'team': 'cm',
                'search': f"{name} {phone} {sub}".lower(),
            })
    except Exception:  # noqa: BLE001
        pass
    contacts.sort(key=lambda c: c['name'].lower())
    return contacts


@bp.route('/contacts')
def contacts():
    return render_template('coa/contacts.html', contacts=_contact_list())


# ================= BACKUP STATUS =================

@bp.route('/backup', methods=['GET'])
def backup_page():
    probes_raw = drive_backup.probe()
    probes = [(a, label, probes_raw.get(a, False))
              for a, label in drive_backup.PROBE_ACTIONS]
    drive_ok = bool(probes_raw.get('saveCoaBackup') and probes_raw.get('getCoaBackup'))
    coa_counts = {c: len(DB.get(c, [])) for c in ('workers', 'timecards', 'sites')}
    script_code = ''
    if not drive_ok:
        try:
            with open(os.path.join(BASE_DIR, 'sheets_apps_script_cm.js')) as f:
                script_code = f.read()
        except Exception:
            script_code = ''
    return render_template('coa/backup.html', probes=probes, drive_ok=drive_ok,
                           coa_counts=coa_counts, last=drive_backup.last_result(),
                           script_code=script_code,
                           backup_msg=request.args.get('msg'),
                           backup_ok=request.args.get('ok') == '1')


@bp.route('/backup/now', methods=['POST'])
def backup_now():
    ok, detail = drive_backup.backup_now()
    return redirect(url_for('coa.backup_page', ok='1' if ok else '0',
                            msg='' if ok else detail[:200]))
