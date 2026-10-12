"""Equipment Patrol Timetable - standalone Flask app (public, no login).

Dashboard for monitors: current patrols based on real time, ON PATROL NOW,
QR clock-in/out for workers' phones, full timetable, settings, timecards.
"""
import os
import io
import json
import uuid
import csv
import hashlib
import threading
import urllib.request
from functools import wraps
from datetime import datetime, date
from zoneinfo import ZoneInfo

from flask import (Blueprint, render_template, request, redirect, url_for,
                   jsonify, send_file, abort, flash, session, make_response)

def create_patrol_blueprint(company_id, url_prefix, db_filename,
                               sheets_env_var, app_version_filename,
                               blueprint_name, company_display_name):
    """Create an isolated Patrol app instance for one company.

    company_id: 'hanseong' | 'coa'  (used for session keys, log messages)
    url_prefix: '/patrol' | '/patrol-coa'
    db_filename: 'data_patrol.json' | 'data_patrol_coa.json'
    sheets_env_var: 'PATROL_SHEETS_URL' | 'COA_SHEETS_URL'
    app_version_filename: 'app_version.json' | 'app_version_coa.json'
    blueprint_name: 'patrol' | 'patrol_coa'
    company_display_name: 'Hanseong' | 'COA'
    Returns dict with bp, DB, login_required, company_id, etc.
    """
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(BASE_DIR, db_filename)
    # Factory local time (user's timezone). All "now" logic uses this.
    TZ = ZoneInfo("America/Phoenix")

    bp = Blueprint(blueprint_name, __name__, url_prefix=url_prefix,
                   template_folder="templates/patrol")
    BP = blueprint_name  # for url_for() endpoint prefixes
    ADMIN_SESSION_KEY = f'patrol_{company_id}_admin'
    BASE_URL = os.environ.get("BASE_URL", "https://app.prismxe.com")

    # Password gate for Timecard / Settings pages (SHA-256 of the admin
    # password; override with the ADMIN_PASSWORD_HASH env var to rotate it).
    ADMIN_PASSWORD_HASH = os.environ.get(
        'ADMIN_PASSWORD_HASH',
        '3a5d9797c6b9313b4e79eed8c8d369e94eabe3700cf864c625e510ebba02f5b7')

    # Google Sheets auto-backup (same pattern as the boiler gauge app).
    # Set <sheets_env_var> to the deployed Apps Script web app URL.
    # Every meaningful DB change is POSTed there asynchronously, and a fresh
    # server restores from it on startup — so a Render redeploy (ephemeral
    # disk) can no longer wipe workers/punches.
    SHEETS_URL = os.environ.get(sheets_env_var, '')


    def sheets_post(payload, timeout=12):
        """Best-effort POST to the Google Sheets webhook. Never raises."""
        if not SHEETS_URL:
            return None
        try:
            req = urllib.request.Request(
                SHEETS_URL, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"sheets_post failed: {e}", flush=True)
            return None


    def sheets_get_data(timeout=15):
        """Fetch the persisted patrol DB from Google Sheets. Returns dict or None."""
        if not SHEETS_URL:
            return None
        try:
            with urllib.request.urlopen(SHEETS_URL + "?action=getData",
                                        timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")).get("data")
        except Exception as e:  # noqa: BLE001
            print(f"sheets_get_data failed: {e}", flush=True)
            return None


    def sheets_post_async(payload):
        if SHEETS_URL:
            threading.Thread(target=sheets_post, args=(payload,), daemon=True).start()


    def sync_data_to_sheets():
        """Best-effort async backup of the full patrol DB to Google Sheets."""
        if SHEETS_URL:
            sheets_post_async({"action": "saveData", "data": DB})


    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            if not session.get(ADMIN_SESSION_KEY):
                return redirect(url_for(BP + '.login', next=request.path))
            return fn(*a, **k)
        return wrapper

    COLLECTIONS = ['slots', 'equipment', 'shifts', 'workers', 'punches']


    def new_id():
        return uuid.uuid4().hex[:8]


    def now_iso():
        return datetime.now(TZ).replace(tzinfo=None).isoformat(timespec='seconds')


    def local_now():
        return datetime.now(TZ).replace(tzinfo=None)


    def seed():
        slot_labels = (["7:00", "8:00", "9:00", "10:00", "11:00", "12:00"] +
                       ["13:00", "14:00", "15:00", "16:00", "17:00", "18:00",
                        "19:00", "20:00"] +
                       ["21:00", "22:00", "23:00", "0:00", "1:00", "2:00",
                        "3:00", "4:00"] +
                       ["5:00", "6:00"])
        slot_shifts = ["A"] * 6 + ["B"] * 8 + ["C"] * 8 + ["A"] * 2
        slots = [{"label": l, "shift": s} for l, s in zip(slot_labels, slot_shifts)]

        def eq(name, checks, active=True, note=""):
            return {"id": new_id(), "name": name, "note": note,
                    "active": active,
                    "checks": {str(k): v for k, v in checks.items()}}

        equipment = [
            eq("HOB", {0: 1, 2: 2, 4: 1, 6: 1, 8: 1, 10: 2, 12: 1, 14: 1,
                       16: 1, 18: 2, 20: 1, 22: 1}),
            eq("HOB LEAK CHECK", {0: 1, 4: 1, 8: 1, 12: 1, 16: 1, 20: 1}),
            eq("SOLVENT RECOVERY", {1: 2, 5: 1, 9: 2, 13: 1, 17: 2, 21: 1}),
            eq("SRP RO", {3: 1, 7: 2, 11: 1, 15: 2, 19: 1, 23: 2}),
            eq("SUPPLY & EXHAUST FAN", {2: 1, 10: 1, 18: 1}),
            eq("SOLVENT PURIFICATION", {1: 1, 5: 1, 9: 1, 13: 1, 17: 1, 21: 1},
               active=False, note="Scheduled for November"),
            eq("WASTE NMP RECOVERY", {1: 1, 5: 1, 9: 1, 13: 1, 17: 1, 21: 1},
               active=False, note="Scheduled for November"),
            eq("COOLING TOWER", {3: 1, 7: 1, 11: 1, 15: 1, 19: 1, 23: 1},
               active=False, note="Scheduled for November"),
        ]
        shifts = [
            {"id": "A", "name": "A", "color": "#2563eb",
             "intervals": [{"start": "07:00", "end": "12:00"},
                           {"start": "05:00", "end": "06:00"}]},
            {"id": "B", "name": "B", "color": "#059669",
             "intervals": [{"start": "13:00", "end": "20:00"}]},
            {"id": "C", "name": "C", "color": "#7c3aed",
             "intervals": [{"start": "21:00", "end": "04:00"}]},
        ]
        return {"slots": slots, "equipment": equipment, "shifts": shifts,
                "workers": [], "punches": []}


    def load_db():
        seeded = not os.path.exists(DB_PATH)
        if seeded:
            db = seed()
            with open(DB_PATH, 'w') as f:
                json.dump(db, f, indent=2)
        else:
            with open(DB_PATH) as f:
                db = json.load(f)
            for c in COLLECTIONS:
                db.setdefault(c, [])
            db.setdefault('config', {'zipcode': ''})
        if seeded:
            # Fresh server (e.g. Render redeploy wiped the disk): restore the
            # last backup from Google Sheets instead of starting empty.
            remote = sheets_get_data()
            if isinstance(remote, dict) and remote.get('workers') is not None:
                print(f"patrol[{company_id}]: restored DB from Google Sheets", flush=True)
                db = remote
                with open(DB_PATH, 'w') as f:
                    json.dump(db, f, indent=2)
        return db


    def save_db(sync=True):
        with open(DB_PATH, 'w') as f:
            json.dump(DB, f, indent=2)
        if sync:
            # sync=False for high-frequency/no-op saves (GPS pings, backup download)
            sync_data_to_sheets()


    DB = load_db()


    def find(coll, _id):
        return next((r for r in DB[coll] if r.get('id') == _id), None)


    def slot_hour(label):
        try:
            return int(label.split(':')[0])
        except (ValueError, IndexError):
            return None


    def current_slot_index():
        h = local_now().hour
        for i, s in enumerate(DB['slots']):
            if slot_hour(s['label']) == h:
                return i
        return 0


    def lighten(hexcolor, amt=0.82):
        """Mix a hex color toward white for light background tints."""
        try:
            h = (hexcolor or '').lstrip('#')
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except (ValueError, IndexError):
            return '#f1f5f9'
        r = int(r + (255 - r) * amt)
        g = int(g + (255 - g) * amt)
        b = int(b + (255 - b) * amt)
        return '#%02x%02x%02x' % (r, g, b)


    def shift_for_hour(h):
        # Derived from Time Slots (single source of truth): slots[0] = 7:00
        idx = (h - 7) % 24
        if 0 <= idx < len(DB['slots']):
            key = DB['slots'][idx].get('shift')
            for sh in DB['shifts']:
                if sh.get('id') == key or sh.get('name') == key:
                    return sh
        return None


    def patrols_at(slot_idx):
        out = []
        for e in DB['equipment']:
            if not e.get('active'):
                continue
            kind = e.get('checks', {}).get(str(slot_idx))
            if kind:
                out.append({"equipment": e, "kind": kind})
        return out


    def punch_status(email):
        email = (email or '').strip().lower()
        last = None
        for p in DB.get('punches', []):
            if (p.get('email') or '').strip().lower() == email:
                if last is None or (p.get('ts') or '') >= (last.get('ts') or ''):
                    last = p
        if last and last.get('action') == 'in':
            return True, last
        return False, last


    def on_patrol_now():
        seen = {}
        for p in DB.get('punches', []):
            em = (p.get('email') or '').strip().lower()
            if not em:
                continue
            if em not in seen or (p.get('ts') or '') >= (seen[em].get('ts') or ''):
                seen[em] = p
        out = []
        for em, p in seen.items():
            if p.get('action') == 'in':
                out.append(p)
        out.sort(key=lambda p: p.get('ts') or '')
        return out


    def paired_records():
        """Pair in/out punches per email for payroll, newest first."""
        by_email = {}
        for p in sorted(DB.get('punches', []), key=lambda x: x.get('ts') or ''):
            by_email.setdefault((p.get('email') or '').strip().lower(), []).append(p)
        rows = []
        for em, ps in by_email.items():
            i = 0
            while i < len(ps):
                if ps[i].get('action') == 'in':
                    cin = ps[i].get('ts')
                    cout = None
                    hours = None
                    if i + 1 < len(ps) and ps[i + 1].get('action') == 'out':
                        cout = ps[i + 1].get('ts')
                        try:
                            dt_in = datetime.fromisoformat(cin)
                            dt_out = datetime.fromisoformat(cout)
                            hours = round((dt_out - dt_in).total_seconds() / 3600, 2)
                        except (ValueError, TypeError):
                            pass
                        i += 2
                    else:
                        i += 1
                    rows.append({"email": ps[i - 1].get('email'),
                                 "name": ps[i - 1].get('name'),
                                 "clock_in": cin, "clock_out": cout,
                                 "hours": hours})
                else:
                    i += 1
        rows.sort(key=lambda r: r['clock_in'] or '', reverse=True)
        return rows


    # ================= PASSWORD GATE =================

    @bp.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            pw = request.form.get('password') or ''
            if hashlib.sha256(pw.encode()).hexdigest() == ADMIN_PASSWORD_HASH:
                session[ADMIN_SESSION_KEY] = True
                nxt = request.form.get('next') or request.args.get('next') or url_for(BP + '.dashboard')
                if not nxt.startswith('/'):
                    nxt = url_for(BP + '.dashboard')
                return redirect(nxt)
            flash('Wrong password.')
        return render_template('patrol/login.html', next=request.args.get('next', ''))


    @bp.route('/logout')
    def logout():
        session.pop(ADMIN_SESSION_KEY, None)
        return redirect(url_for(BP + '.dashboard'))


    # ================= DASHBOARD =================

    @bp.route('/')
    def dashboard():
        idx = current_slot_index()
        now = local_now()
        shift = shift_for_hour(now.hour)
        due = patrols_at(idx)
        # next slot with any patrol
        nxt, nxt_list = None, []
        for k in range(1, 25):
            j = (idx + k) % 24
            pl = patrols_at(j)
            if pl:
                nxt, nxt_list = j, pl
                break
        slot_counts = [len(patrols_at(i)) for i in range(24)]
        located = [{"name": w.get("name", ""),
                    "lat": w["last_lat"], "lng": w["last_lng"],
                    "ts": (w.get("last_loc_ts") or "")[:16].replace("T", " ")}
                   for w in DB.get('workers', [])
                   if w.get('last_lat') is not None and w.get('last_lng') is not None]
        shift_tints = {s['id']: lighten(s.get('color')) for s in DB['shifts']}
        return render_template('patrol/dashboard.html', now=now, slot_idx=idx,
                               shift=shift, due=due, nxt=nxt, nxt_list=nxt_list,
                               slots=DB['slots'], equipment=DB['equipment'],
                               shifts=DB['shifts'], counts=slot_counts,
                               on_patrol=on_patrol_now(), located=located,
                               shift_tints=shift_tints,
                               zipcode=DB.get('config', {}).get('zipcode', ''))


    # ================= QR CODE =================

    @bp.route('/qr.png')
    def qr_png():
        try:
            import qrcode
        except ImportError:
            abort(501)
        url = BASE_URL.rstrip('/') + url_prefix + '/patrol-clock'
        img = qrcode.make(url, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png')


    # ================= SETTINGS =================

    @bp.route('/settings')
    @login_required
    def settings():
        cfg = DB.get('config', {})
        # Effective app version/URL: app_version.json (auto-updated by builds) wins.
        eff_version = int(cfg.get('app_version', 1))
        eff_url = cfg.get('apk_url', '')
        auto_managed = False
        try:
            import os as _os, json as _json
            _p = _os.path.join(_os.path.dirname(__file__), app_version_filename)
            if _os.path.exists(_p):
                with open(_p) as _f:
                    _j = _json.load(_f)
                if _j.get('version'):
                    eff_version = max(eff_version, int(_j['version']))
                if _j.get('apk_url'):
                    eff_url = _j['apk_url']
                auto_managed = True
        except Exception:
            pass
        return render_template('patrol/settings.html', slots=DB['slots'],
                               equipment=DB['equipment'], shifts=DB['shifts'],
                               workers=DB['workers'],
                               zipcode=cfg.get('zipcode', ''),
                               app_version=eff_version,
                               apk_url=eff_url,
                               apk_cert_sha256=cfg.get('apk_cert_sha256', ''),
                               auto_managed=auto_managed)


    @bp.route('/settings/display/save', methods=['POST'])
    @login_required
    def settings_display_save():
        data = request.get_json(force=True)
        zipcode = (data.get('zipcode') or '').strip()
        DB.setdefault('config', {})['zipcode'] = zipcode
        try:
            DB['config']['app_version'] = max(1, int(data.get('app_version', 1)))
        except (TypeError, ValueError):
            pass
        DB['config']['apk_url'] = (data.get('apk_url') or '').strip()
        sha = (data.get('apk_cert_sha256') or '').strip().upper().replace(' ', '')
        if sha:
            DB['config']['apk_cert_sha256'] = sha
        save_db()
        return jsonify({'ok': True})


    @bp.route('/api/app-version')
    def api_app_version():
        cfg = DB.get('config', {})
        version = int(cfg.get('app_version', 1))
        apk_url = cfg.get('apk_url', '')
        # Auto-updated by build pipeline: app_version.json in repo root overrides DB.
        try:
            import os as _os, json as _json
            _p = _os.path.join(_os.path.dirname(__file__), app_version_filename)
            if _os.path.exists(_p):
                with open(_p) as _f:
                    _j = _json.load(_f)
                version = max(version, int(_j.get('version', 0)))
                if _j.get('apk_url'):
                    apk_url = _j['apk_url']
        except Exception:
            pass
        return jsonify({
            'version': version,
            'apk_url': apk_url,
        })


    @bp.route('/api/worker-status')
    def api_worker_status():
        email = (request.args.get('email') or '').strip().lower()
        if not email or '@' not in email:
            return jsonify({'ok': False, 'error': 'Valid email required.'}), 400
        clocked_in, last = punch_status(email)
        return jsonify({
            'ok': True,
            'clocked_in': clocked_in,
            'since': last.get('ts') if last else None,
        })


    @bp.route('/settings/equipment/save', methods=['POST'])
    @login_required
    def settings_equipment_save():
        data = request.get_json(force=True)
        for item in data.get('equipment', []):
            e = find('equipment', item.get('id'))
            if not e:
                continue
            e['name'] = item.get('name', e['name'])
            e['note'] = item.get('note', '')
            e['active'] = bool(item.get('active'))
        # reorder
        order = [i.get('id') for i in data.get('equipment', [])]
        DB['equipment'].sort(key=lambda e: order.index(e['id']) if e['id'] in order else 999)
        save_db()
        return jsonify({'ok': True})


    @bp.route('/settings/equipment/add', methods=['POST'])
    @login_required
    def settings_equipment_add():
        name = (request.form.get('name') or '').strip() or 'New Equipment'
        DB['equipment'].append({"id": new_id(), "name": name, "note": "",
                                "active": True, "checks": {}})
        save_db()
        return redirect(url_for(BP + '.settings'))


    @bp.route('/settings/equipment/<eid>/delete', methods=['POST'])
    @login_required
    def settings_equipment_delete(eid):
        DB['equipment'] = [e for e in DB['equipment'] if e['id'] != eid]
        save_db()
        return redirect(url_for(BP + '.settings'))


    @bp.route('/settings/check', methods=['POST'])
    @login_required
    def settings_check():
        d = request.get_json(force=True)
        e = find('equipment', d.get('equipment_id'))
        if not e:
            return jsonify({'ok': False}), 404
        slot = str(d.get('slot'))
        cur = e.get('checks', {}).get(slot, 0)
        nxt = {0: 1, 1: 2, 2: 0}[cur]
        if nxt:
            e.setdefault('checks', {})[slot] = nxt
        else:
            e.get('checks', {}).pop(slot, None)
        save_db()
        return jsonify({'ok': True, 'value': nxt})


    @bp.route('/settings/slots/save', methods=['POST'])
    @login_required
    def settings_slots_save():
        data = request.get_json(force=True)
        for i, s in enumerate(data.get('slots', [])):
            if i < len(DB['slots']):
                DB['slots'][i]['label'] = s.get('label', DB['slots'][i]['label'])
                DB['slots'][i]['shift'] = s.get('shift', DB['slots'][i]['shift'])
        save_db()
        return jsonify({'ok': True})


    @bp.route('/settings/shifts/save', methods=['POST'])
    @login_required
    def settings_shifts_save():
        data = request.get_json(force=True)
        DB['shifts'] = data.get('shifts', DB['shifts'])
        for sh in DB['shifts']:
            sh.setdefault('id', new_id())
        save_db()
        return jsonify({'ok': True})


    @bp.route('/settings/workers/add', methods=['POST'])
    @login_required
    def settings_worker_add():
        name = (request.form.get('name') or '').strip() or 'New Worker'
        email = (request.form.get('email') or '').strip()
        DB['workers'].append({"id": new_id(), "name": name, "email": email,
                              "shift_id": request.form.get('shift_id') or ''})
        save_db()
        return redirect(url_for(BP + '.settings'))


    @bp.route('/settings/workers/<wid>/delete', methods=['POST'])
    @login_required
    def settings_worker_delete(wid):
        DB['workers'] = [w for w in DB['workers'] if w['id'] != wid]
        save_db()
        return redirect(url_for(BP + '.settings'))


    @bp.route('/settings/workers/save', methods=['POST'])
    @login_required
    def settings_workers_save():
        data = request.get_json(force=True)
        for item in data.get('workers', []):
            w = find('workers', item.get('id'))
            if not w:
                continue
            w['name'] = item.get('name', w['name'])
            w['email'] = item.get('email', '')
            w['shift_id'] = item.get('shift_id', '')
        save_db()
        return jsonify({'ok': True})


    @bp.route('/settings/reset', methods=['POST'])
    @login_required
    def settings_reset():
        fresh = seed()
        # keep workers, punches, and shift assignment edits
        fresh['workers'] = DB.get('workers', [])
        fresh['punches'] = DB.get('punches', [])
        DB.clear()
        DB.update(fresh)
        save_db()
        return redirect(url_for(BP + '.settings'))


    @bp.route('/data/backup')
    def data_backup():
        save_db(sync=False)
        return send_file(DB_PATH, mimetype='application/json', as_attachment=True,
                         download_name='patrol-timetable-backup.json')


    # ================= TIMECARD =================

    @bp.route('/timecard')
    @login_required
    def timecard():
        workers = sorted(DB['workers'], key=lambda w: w.get('name', ''))
        status = []
        for w in workers:
            em = (w.get('email') or '').strip().lower()
            if em:
                cin, last = punch_status(em)
            else:
                cin, last = False, None
            status.append({"worker": w, "clocked_in": cin,
                           "since": last.get('ts') if last else None})
        return render_template('patrol/timecard.html', status=status,
                               records=paired_records())


    @bp.route('/timecard/clock', methods=['POST'])
    @login_required
    def timecard_clock():
        d = request.get_json(force=True)
        w = find('workers', d.get('worker_id'))
        if not w:
            return jsonify({'ok': False}), 404
        action = d.get('action')
        email = (w.get('email') or '').strip()
        name = w.get('name', '')
        if action == 'in':
            cin, _ = punch_status(email or name)
            if cin:
                return jsonify({'ok': False, 'error': 'Already clocked in.'}), 400
        DB['punches'].append({"id": new_id(), "email": email, "name": name,
                              "action": action, "ts": now_iso(),
                              "source": "manual"})
        save_db()
        sheets_post_async({"action": "punch", "punch": DB['punches'][-1]})
        return jsonify({'ok': True})


    @bp.route('/timecard/export')
    @login_required
    def timecard_export():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['date', 'name', 'email', 'clock_in', 'clock_out', 'hours'])
        for r in paired_records():
            w.writerow([(r['clock_in'] or '')[:10], r['name'], r['email'],
                        (r['clock_in'] or '')[11:16], (r['clock_out'] or '')[11:16],
                        r['hours'] or ''])
        data = io.BytesIO(buf.getvalue().encode('utf-8-sig'))
        return send_file(data, mimetype='text/csv', as_attachment=True,
                         download_name='patrol-timecards.csv')


    # ================= PATROL CLOCK (phone page) =================

    @bp.route('/sw.js')
    def service_worker():
        sw_path = os.path.join(BASE_DIR, 'static', 'patrol', 'sw.js')
        with open(sw_path, encoding='utf-8') as _f:
            sw_text = _f.read()
        if url_prefix != '/patrol':
            # Company-specific copy: re-point cached paths and keep the
            # CacheStorage name unique per company so the two boards
            # never overwrite each other's offline cache.
            sw_text = sw_text.replace('/patrol/', url_prefix + '/')
            sw_text = sw_text.replace("'/patrol'", "'%s'" % url_prefix)
            sw_text = sw_text.replace('patrol-board-v1',
                                      'patrol-board-v1-' + company_id)
        resp = make_response(sw_text)
        resp.mimetype = 'application/javascript'
        resp.headers['Cache-Control'] = 'no-cache'
        resp.headers['Service-Worker-Allowed'] = url_prefix + '/'
        return resp


    @bp.after_request
    def add_cors(resp):
        if request.path.startswith(url_prefix + '/api/'):
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp


    @bp.route('/api/patrol-schedule')
    def api_patrol_schedule():
        """Machine-readable patrol timetable for the worker On Site app.

        Always reflects the current Settings (reads the live in-memory DB).
        CORS open (*) so the worker app can fetch it cross-origin."""
        patrols = []
        for i, s in enumerate(DB['slots']):
            for p in patrols_at(i):
                patrols.append({
                    'equipment': p['equipment']['name'],
                    'time': s['label'],
                    'shift': s.get('shift', ''),
                    'photo': p['kind'] == 2,
                })
        return jsonify({'patrols': patrols,
                        'slots': [s['label'] for s in DB['slots']]})

    @bp.route('/patrol-clock')
    def patrol_clock():
        return render_template('patrol/patrol_clock.html')


    @bp.route('/api/patrol-status')
    def api_patrol_status():
        email = request.args.get('email', '')
        clocked_in, last = punch_status(email)
        return jsonify({'email': email, 'clocked_in': clocked_in,
                        'since': last.get('ts') if last else None})


    @bp.route('/api/patrol-mine')
    def api_patrol_mine():
        email = (request.args.get('email') or '').strip().lower()
        rows = [p for p in DB.get('punches', [])
                if (p.get('email') or '').strip().lower() == email]
        rows.sort(key=lambda p: p.get('ts') or '', reverse=True)
        return jsonify({'punches': rows[:30]})


    @bp.route('/api/patrol-punch', methods=['POST'])
    def api_patrol_punch():
        d = request.get_json(force=True)
        email = (d.get('email') or '').strip()
        name = (d.get('name') or '').strip()
        action = (d.get('action') or '').strip().lower()
        if '@' not in email or '.' not in email.split('@')[-1]:
            return jsonify({'ok': False, 'error': 'Enter a valid email address.'}), 400
        if not name:
            return jsonify({'ok': False, 'error': 'Enter your name.'}), 400
        if action not in ('in', 'out'):
            return jsonify({'ok': False, 'error': 'Invalid action.'}), 400
        clocked_in, _ = punch_status(email)
        if action == 'in' and clocked_in:
            return jsonify({'ok': False, 'error': 'Already clocked in.'}), 400
        if action == 'out' and not clocked_in:
            return jsonify({'ok': False, 'error': 'Not clocked in.'}), 400
        DB['punches'].append({"id": new_id(), "email": email, "name": name,
                              "action": action, "ts": now_iso(),
                              "source": "qr"})
        # auto-register worker by email
        worker = None
        for w in DB['workers']:
            if (w.get('email') or '').strip().lower() == email.lower():
                worker = w
                break
        if worker is None:
            worker = {"id": new_id(), "name": name, "email": email, "shift_id": ""}
            DB['workers'].append(worker)
        elif not worker.get('name'):
            worker['name'] = name
        # optional GPS from the phone — shown on the staff map
        try:
            lat = float(d.get('lat')) if d.get('lat') is not None else None
            lng = float(d.get('lng')) if d.get('lng') is not None else None
        except (TypeError, ValueError):
            lat = lng = None
        if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
            worker['last_lat'] = lat
            worker['last_lng'] = lng
            worker['last_loc_ts'] = now_iso()
        save_db()
        sheets_post_async({"action": "punch", "punch": DB['punches'][-1]})
        clocked_in, last = punch_status(email)
        return jsonify({'ok': True, 'clocked_in': clocked_in,
                        'since': last.get('ts') if last else None})


    @bp.route('/api/location', methods=['POST'])
    def api_location():
        d = request.get_json(force=True) or {}
        email = (d.get('email') or '').strip().lower()
        try:
            lat = float(d.get('lat'))
            lng = float(d.get('lng'))
        except (TypeError, ValueError):
            return jsonify({'ok': False}), 400
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return jsonify({'ok': False}), 400
        for w in DB['workers']:
            if (w.get('email') or '').strip().lower() == email:
                w['last_lat'] = lat
                w['last_lng'] = lng
                w['last_loc_ts'] = now_iso()
                save_db(sync=False)  # GPS pings every ~60s; not worth a Sheets sync
                return jsonify({'ok': True})
        return jsonify({'ok': False, 'error': 'Unknown worker'}), 404
    return {'bp': bp, 'DB': DB, 'login_required': login_required,
            'company_id': company_id, 'company_display_name': company_display_name}


# ================= COMPANY INSTANCES =================
# Hanseong — default, backward compatible (existing /patrol/ URLs keep working,
# existing data_patrol.json is preserved, `bp`/`DB`/`login_required` imports
# in app.py keep resolving to this instance).
_hanseong = create_patrol_blueprint(
    company_id='hanseong',
    url_prefix='/patrol',
    db_filename='data_patrol.json',
    sheets_env_var='PATROL_SHEETS_URL',
    app_version_filename='app_version.json',
    blueprint_name='patrol',
    company_display_name='Hanseong',
)
bp = _hanseong['bp']
DB = _hanseong['DB']
login_required = _hanseong['login_required']
patrol_bp = bp  # alias

# COA (Cleaners of America) — fully isolated instance.
_coa = create_patrol_blueprint(
    company_id='coa',
    url_prefix='/patrol-coa',
    db_filename='data_patrol_coa.json',
    sheets_env_var='COA_SHEETS_URL',
    app_version_filename='app_version_coa.json',
    blueprint_name='patrol_coa',
    company_display_name='COA',
)
patrol_coa_bp = _coa['bp']
patrol_coa_db = _coa['DB']
patrol_coa_login_required = _coa['login_required']
