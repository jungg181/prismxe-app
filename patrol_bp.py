"""Equipment Patrol Timetable - standalone Flask app (public, no login).

Dashboard for monitors: current patrols based on real time, ON PATROL NOW,
QR clock-in/out for workers' phones, full timetable, settings, timecards.
"""
import os
import io
import base64
import json
import uuid
import csv
import hashlib
import hmac
import threading
import time
import urllib.request
from functools import wraps
from datetime import datetime, date
from zoneinfo import ZoneInfo

from flask import (Blueprint, render_template, request, redirect, url_for,
                   jsonify, send_file, abort, flash, session, make_response)

# Per-worker colors: workers are assigned palette entries by their
# position in the workers list (see _worker_color in the factory), so
# every worker gets a DISTINCT color. The hash pick below is only a
# fallback for identifiers that are not in the workers list.
WORKER_COLORS = [
    '#dc2626',  # strong red
    '#2563eb',  # strong blue
    '#16a34a',  # strong green
    '#ea580c',  # vivid orange
    '#7c3aed',  # vivid purple
    '#0d9488',  # teal
    '#92400e',  # brown
    '#db2777',  # pink
    '#4f46e5',  # indigo
    '#0891b2',  # cyan
    '#65a30d',  # lime green
    '#ca8a04',  # goldenrod
    '#9333ea',  # purple
    '#0f766e',  # dark teal
    '#b91c1c',  # dark red
    '#1d4ed8',  # royal blue
    '#15803d',  # forest green
    '#c2410c',  # burnt orange
    '#a21caf',  # fuchsia
    '#0369a1',  # sky blue
    '#3f6212',  # olive
    '#854d0e',  # dark gold
    '#be185d',  # raspberry
    '#334155',  # slate
]

def worker_color(key):
    """Return a stable hex color for a worker identifier (email or name)."""
    k = (key or '').strip().lower()
    if not k:
        return WORKER_COLORS[0]
    h = int(hashlib.md5(k.encode('utf-8')).hexdigest(), 16)
    return WORKER_COLORS[h % len(WORKER_COLORS)]

def create_patrol_blueprint(company_id, url_prefix, db_filename,
                               sheets_env_var, app_version_filename,
                               blueprint_name, company_display_name):
    """Create an isolated Patrol app instance for one company.

    company_id: 'hanseong' | 'coa'  (used for session keys, log messages)
    url_prefix: '/Hanseong/patrol' | '/patrol-coa'
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

    # ---- Per-visit password gate for Timecard / Settings ----
    # When the PATROL_ADMIN_PASSWORD env var is set, every visit (GET) of
    # /timecard or /settings shows a password form — even with an existing
    # login session. POSTing the correct password unlocks just that page
    # view; a short-lived session flag authorizes the page's own action
    # POSTs (edits, saves). When the env var is unset, behavior is
    # unchanged (session login only).
    PAGE_PASSWORD = os.environ.get('PATROL_ADMIN_PASSWORD', '')
    VISIT_AUTH_KEY = f'patrol_{company_id}_visit_auth'
    VISIT_AUTH_TTL = 1800  # seconds an unlocked page view stays usable

    def _visit_authed(page):
        if not PAGE_PASSWORD:
            return bool(session.get(ADMIN_SESSION_KEY))
        auth = session.get(VISIT_AUTH_KEY) or {}
        ts = auth.get(page)
        return bool(ts) and (time.time() - ts < VISIT_AUTH_TTL)

    def _clear_visit_auth(page):
        auth = session.get(VISIT_AUTH_KEY) or {}
        auth.pop(page, None)
        session[VISIT_AUTH_KEY] = auth

    def _mark_visit_authed(page):
        auth = session.get(VISIT_AUTH_KEY) or {}
        auth[page] = time.time()
        session[VISIT_AUTH_KEY] = auth

    def visit_gate(page, page_title):
        """Wrap a page route: every GET starts locked behind a password
        form; a POST carrying the correct page_password unlocks one view."""
        def deco(fn):
            @wraps(fn)
            def wrapper(*a, **k):
                if not PAGE_PASSWORD:
                    return login_required(fn)(*a, **k)
                if not session.get(ADMIN_SESSION_KEY):
                    return redirect(url_for(BP + '.login',
                                            next=request.path))
                if (request.method == 'POST' and
                        request.form.get('page_password') is not None):
                    if hmac.compare_digest(
                            request.form.get('page_password') or '',
                            PAGE_PASSWORD):
                        _mark_visit_authed(page)
                        return fn(*a, **k)
                    flash('Wrong password.')
                else:
                    _clear_visit_auth(page)
                action = url_for(BP + '.' + page)
                if request.query_string:
                    action += '?' + request.query_string.decode(
                        'utf-8', 'replace')
                return render_template('patrol/page_password.html',
                                       page_title=page_title,
                                       action=action)
            return wrapper
        return deco

    def visit_action_required(page):
        """Guard a page's action routes (edits, saves, exports): they work
        only within an unlocked page view (or the plain login session when
        the per-visit gate is disabled)."""
        def deco(fn):
            @wraps(fn)
            def wrapper(*a, **k):
                if not _visit_authed(page):
                    if request.is_json:
                        return jsonify({'ok': False,
                                        'error': 'Password required.'}), 403
                    return redirect(url_for(BP + '.' + page))
                return fn(*a, **k)
            return wrapper
        return deco

    COLLECTIONS = ['slots', 'equipment', 'shifts', 'workers', 'punches',
                   'hour_batches']


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
        # The day runs 0:00 -> 23:00: rotate the slots (authored in
        # 7:00-first order above) so index 0 is the 0:00 slot, and
        # remap the equipment check indexes to match.
        _p = next(i for i, s in enumerate(slots) if s["label"] == "0:00")
        slots = slots[_p:] + slots[:_p]
        for _e in equipment:
            _e["checks"] = {str((int(k) - _p) % 24): v
                            for k, v in _e["checks"].items()}
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
        # Restore from Google Sheets when the local DB is empty but Sheets
        # has data. (The DB file is bundled in deploys, so "seeded" is not
        # a reliable signal for a wiped disk.)
        if not db.get('workers'):
            remote = sheets_get_data()
            if isinstance(remote, dict) and remote.get('workers'):
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

    def _worker_color(w):
        """Distinct color per worker: palette entry by the worker's
        position in the workers list (unique while <= palette size)."""
        if w is not None:
            ids = [x.get('id') for x in DB['workers']]
            if w.get('id') in ids:
                return WORKER_COLORS[ids.index(w['id']) % len(WORKER_COLORS)]
            return worker_color(w.get('email') or w.get('name', ''))
        return WORKER_COLORS[0]



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
        # Derived from Time Slots (single source of truth): find the
        # slot whose label hour matches.
        idx = next((i for i, s in enumerate(DB['slots'])
                    if slot_hour(s.get('label', '')) == h), None)
        if idx is not None and 0 <= idx < len(DB['slots']):
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
                p = dict(p)
                _w = _worker_by_email(em)
                p['color'] = _worker_color(_w) if _w else worker_color(em)
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
                    in_id = ps[i].get('id')
                    cin = ps[i].get('ts')
                    cout = None
                    hours = None
                    out_id = None
                    if i + 1 < len(ps) and ps[i + 1].get('action') == 'out':
                        cout = ps[i + 1].get('ts')
                        out_id = ps[i + 1].get('id')
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
                                 "hours": hours,
                                 "in_id": in_id, "out_id": out_id})
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
    @login_required
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
                    "color": _worker_color(w),
                    "ts": (w.get("last_loc_ts") or "")[:16].replace("T", " ")}
                   for w in DB.get('workers', [])
                   if w.get('last_lat') is not None and w.get('last_lng') is not None]
        shift_tints = {s['id']: lighten(s.get('color')) for s in DB['shifts']}
        # On-duty crew for the CURRENT time slot, from the 2-week plan
        # (Settings -> Time Slots & Shifts, names per hour). Supervisors
        # (12h) are not shown. Unplanned dates fall back to the standing
        # shift of the slot so the board never goes blank by surprise.
        _ensure_schedule_seed()
        _migrate_shift_ids()
        _midnight_rebase()
        _seed_slot_plan()
        crew = []
        crew_slot = DB['slots'][idx] if DB['slots'] else {}
        day_plan = DB.get('slot_plan', {}).get(now.date().isoformat())
        if day_plan is None:
            crew_ids = [w['id'] for w in DB['workers']
                        if (w.get('shift_id') or '') ==
                        (crew_slot.get('shift') or '')]
        else:
            crew_ids = day_plan.get(str(idx), [])
        for wid in crew_ids:
            w = find('workers', wid)
            if not w or (w.get('role') or '') == 'Supervisor':
                continue
            cin, _ = punch_status(w.get('email') or '')
            crew.append({"worker": w, "clocked_in": cin,
                         "role": w.get('role', ''),
                         "color": _worker_color(w)})
        crew.sort(key=lambda r: r['worker'].get('name', ''))
        have_roles = {r['role'] for r in crew}
        slot_missing = [r for r in ROLES
                        if r != 'Supervisor' and r not in have_roles]
        # Crew bars for the Full Timetable: every worker on today's
        # plan, with the set of slot indexes they work (colored band
        # across the hour columns). Same fallback as the crew card.
        bars_map = {}
        if day_plan is None:
            for i, s in enumerate(DB['slots']):
                for w in DB['workers']:
                    if (w.get('shift_id') or '') == (s.get('shift') or ''):
                        bars_map.setdefault(w['id'], set()).add(i)
        else:
            for idx_s, wids in day_plan.items():
                for wid in wids or []:
                    try:
                        bars_map.setdefault(wid, set()).add(int(idx_s))
                    except (TypeError, ValueError):
                        pass
        crew_bars = []
        for wid, hrs in bars_map.items():
            w = find('workers', wid)
            if not w or (w.get('role') or '') == 'Supervisor':
                continue
            crew_bars.append({"worker": w, "role": w.get('role', ''),
                              "color": _worker_color(w),
                              "hours": hrs})
        crew_bars.sort(key=lambda b: (min(b['hours']) if b['hours'] else 99,
                                      b['worker'].get('name', '')))
        return render_template('patrol/dashboard.html', now=now, slot_idx=idx,
                               shift=shift, due=due, nxt=nxt, nxt_list=nxt_list,
                               slots=DB['slots'], equipment=DB['equipment'],
                               shifts=DB['shifts'], counts=slot_counts,
                               on_patrol=on_patrol_now(), located=located,
                               shift_tints=shift_tints, crew=crew,
                               crew_slot_label=crew_slot.get('label', ''),
                               slot_missing=slot_missing, crew_bars=crew_bars,
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

    @bp.route('/settings', methods=['GET', 'POST'])
    @visit_gate('settings', 'Settings')
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
        _midnight_rebase()
        _seed_slot_plan()
        plan_monday = _sched_week(local_now().date())[0]
        return render_template('patrol/settings.html', slots=DB['slots'],
                               equipment=DB['equipment'], shifts=DB['shifts'],
                               workers=DB['workers'],
                               slot_plan=DB.get('slot_plan', {}),
                               plan_start=plan_monday.isoformat(),
                               today_str=local_now().date().isoformat(),
                               zipcode=cfg.get('zipcode', ''),
                               app_version=eff_version,
                               apk_url=eff_url,
                               apk_cert_sha256=cfg.get('apk_cert_sha256', ''),
                               auto_managed=auto_managed)


    @bp.route('/settings/display/save', methods=['POST'])
    @login_required
    @visit_action_required('settings')
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
    @visit_action_required('settings')
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
    @visit_action_required('settings')
    def settings_equipment_add():
        name = (request.form.get('name') or '').strip() or 'New Equipment'
        DB['equipment'].append({"id": new_id(), "name": name, "note": "",
                                "active": True, "checks": {}})
        save_db()
        return redirect(url_for(BP + '.settings'))


    @bp.route('/settings/equipment/<eid>/delete', methods=['POST'])
    @login_required
    @visit_action_required('settings')
    def settings_equipment_delete(eid):
        DB['equipment'] = [e for e in DB['equipment'] if e['id'] != eid]
        save_db()
        return redirect(url_for(BP + '.settings'))


    @bp.route('/settings/check', methods=['POST'])
    @login_required
    @visit_action_required('settings')
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
    @visit_action_required('settings')
    def settings_slots_save():
        data = request.get_json(force=True)
        valid_w = {w['id'] for w in DB['workers']}
        for i, s in enumerate(data.get('slots', [])):
            if i < len(DB['slots']):
                DB['slots'][i]['label'] = s.get('label', DB['slots'][i]['label'])
                DB['slots'][i]['shift'] = s.get('shift', DB['slots'][i]['shift'])
        plan = data.get('slot_plan')
        if isinstance(plan, dict):
            clean = {}
            for date_s, per_slot in plan.items():
                if not isinstance(per_slot, dict):
                    continue
                day = {}
                for idx_s, wids in per_slot.items():
                    try:
                        idx = int(idx_s)
                    except (TypeError, ValueError):
                        continue
                    seen = []
                    for wid in wids or []:
                        if wid in valid_w and wid not in seen:
                            seen.append(wid)
                    if seen:
                        day[str(idx)] = seen
                clean[date_s] = day
            DB['slot_plan'] = clean
        save_db()
        return jsonify({'ok': True})


    @bp.route('/settings/shifts/save', methods=['POST'])
    @login_required
    @visit_action_required('settings')
    def settings_shifts_save():
        data = request.get_json(force=True)
        DB['shifts'] = data.get('shifts', DB['shifts'])
        for sh in DB['shifts']:
            sh.setdefault('id', new_id())
        save_db()
        return jsonify({'ok': True})


    @bp.route('/settings/workers/add', methods=['POST'])
    @login_required
    @visit_action_required('settings')
    def settings_worker_add():
        name = (request.form.get('name') or '').strip() or 'New Worker'
        email = (request.form.get('email') or '').strip()
        DB['workers'].append({"id": new_id(), "name": name, "email": email,
                              "shift_id": request.form.get('shift_id') or ''})
        save_db()
        return redirect(url_for(BP + '.settings'))


    @bp.route('/settings/workers/<wid>/delete', methods=['POST'])
    @login_required
    @visit_action_required('settings')
    def settings_worker_delete(wid):
        DB['workers'] = [w for w in DB['workers'] if w['id'] != wid]
        save_db()
        return redirect(url_for(BP + '.settings'))


    @bp.route('/settings/workers/save', methods=['POST'])
    @login_required
    @visit_action_required('settings')
    def settings_workers_save():
        data = request.get_json(force=True)
        for item in data.get('workers', []):
            w = find('workers', item.get('id'))
            if not w:
                continue
            w['name'] = item.get('name', w['name'])
            w['email'] = item.get('email', '')
            w['shift_id'] = item.get('shift_id', '')
            if 'role' in item:
                w['role'] = item.get('role', '')
            if 'phone' in item:
                w['phone'] = item.get('phone', '')
        save_db()
        return jsonify({'ok': True})


    @bp.route('/settings/reset', methods=['POST'])
    @login_required
    @visit_action_required('settings')
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

    def render_batch_page(sel_id=None):
        batches = sorted(DB.get('hour_batches', []),
                         key=lambda b: b.get('created_at', ''), reverse=True)
        for b in batches:
            b['counts'] = batch_counts(b)
        sel = None
        if sel_id:
            sel = next((b for b in batches if b['id'] == sel_id), None)
        if sel is None and batches:
            sel = batches[0]
        return render_template(
            'patrol/timecard.html', view='batch', batches=batches,
            sel_batch=sel,
            confirm_base=(request.host_url.rstrip('/') + url_prefix +
                          '/confirm/'))

    @bp.route('/timecard/batch', methods=['GET', 'POST'])
    @visit_gate('timecard_batch', 'Batch')
    def timecard_batch():
        return render_batch_page(request.args.get('batch'))

    @bp.route('/timecard/batch/<batch_id>')
    @visit_action_required('timecard_batch')
    def timecard_batch_detail(batch_id):
        return render_batch_page(batch_id)

    @bp.route('/timecard', methods=['GET', 'POST'])
    @visit_gate('timecard', 'Timecard')
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
        return render_template('patrol/timecard.html', view='records',
                               status=status, records=paired_records())

    @bp.route('/timecard/batch/resend', methods=['POST'])
    @visit_action_required('timecard_batch')
    def batch_resend():
        d = request.get_json(force=True) or {}
        batch = next((b for b in DB.get('hour_batches', [])
                      if b['id'] == d.get('batch_id')), None)
        if not batch:
            return jsonify({'ok': False, 'error': 'Batch not found.'}), 404
        entry = next((e for e in batch['entries']
                      if (e.get('email') or '').strip().lower() ==
                      (d.get('email') or '').strip().lower()), None)
        if not entry:
            return jsonify({'ok': False, 'error': 'Entry not found.'}), 404
        subject, body = hours_email(
            entry['name'], batch['start'], batch['end'], entry['shifts'],
            entry['total'], confirm_url=confirm_url_for(entry['token']))
        try:
            pdf_bytes = hours_pdf(entry['name'], entry['email'],
                                  batch['start'], batch['end'],
                                  entry['shifts'], entry['total'])
            safe = ''.join(ch if ch.isalnum() else '_'
                           for ch in entry['name']).strip('_') or 'worker'
            ok, err = mail_send(
                entry['email'], subject, body,
                attach_name=f"hours_{safe}_{batch['start']}_to_"
                            f"{batch['end']}.pdf",
                attach_bytes=pdf_bytes)
        except Exception as e:  # noqa: BLE001
            ok, err = mail_send(entry['email'], subject, body)
            if ok:
                err = f'sent without PDF ({e})'
        if ok:
            entry['sent_at'] = datetime.now().isoformat(timespec='seconds')
            entry['resends'] = entry.get('resends', 0) + 1
            save_db()
        return jsonify({'ok': ok, 'error': err})


    @bp.route('/timecard/clock', methods=['POST'])
    @login_required
    @visit_action_required('timecard')
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


    @bp.route('/timecard/punch/edit', methods=['POST'])
    @login_required
    @visit_action_required('timecard')
    def timecard_punch_edit():
        """Manually fix a paired timecard record (in/out timestamps)."""
        d = request.get_json(force=True)
        in_p = find('punches', d.get('in_id'))
        if not in_p or in_p.get('action') != 'in':
            return jsonify({'ok': False, 'error': 'Record not found.'}), 404
        out_p = find('punches', d.get('out_id')) if d.get('out_id') else None
        if out_p and out_p.get('action') != 'out':
            out_p = None

        def parse_ts(v):
            if not v:
                return None
            try:
                return datetime.fromisoformat(v).replace(
                    tzinfo=None).isoformat(timespec='seconds')
            except (ValueError, TypeError):
                return None

        new_in = parse_ts(d.get('clock_in'))
        if not new_in:
            return jsonify({'ok': False, 'error': 'Invalid clock-in time.'}), 400
        new_out = parse_ts(d.get('clock_out'))
        if new_out and new_out <= new_in:
            return jsonify({'ok': False,
                            'error': 'Clock-out must be after clock-in.'}), 400

        in_p['ts'] = new_in
        in_p['source'] = 'manual-edit'
        if out_p:
            if new_out:
                out_p['ts'] = new_out
                out_p['source'] = 'manual-edit'
            else:
                # out time cleared -> make it an open record
                DB['punches'].remove(out_p)
        elif new_out:
            # no out punch yet -> create one
            DB['punches'].append({"id": new_id(),
                                  "email": in_p.get('email'),
                                  "name": in_p.get('name'),
                                  "action": 'out', "ts": new_out,
                                  "source": 'manual-edit'})
        save_db()  # full DB sync to Sheets (Durability layer)
        return jsonify({'ok': True})


    @bp.route('/timecard/punch/delete', methods=['POST'])
    @login_required
    @visit_action_required('timecard')
    def timecard_punch_delete():
        """Delete a paired timecard record (its in/out punches)."""
        d = request.get_json(force=True)
        removed = 0
        for pid in (d.get('in_id'), d.get('out_id')):
            if not pid:
                continue
            p = find('punches', pid)
            if p:
                DB['punches'].remove(p)
                removed += 1
        if not removed:
            return jsonify({'ok': False, 'error': 'Record not found.'}), 404
        save_db()  # full DB sync to Sheets (durability layer)
        return jsonify({'ok': True, 'removed': removed})


    @bp.route('/timecard/punch/add', methods=['POST'])
    @login_required
    @visit_action_required('timecard')
    def timecard_punch_add():
        """Manually add a timecard record for a chosen date (admin correction)."""
        d = request.get_json(force=True)
        w = find('workers', d.get('worker_id'))
        if not w:
            return jsonify({'ok': False, 'error': 'Worker not found.'}), 404
        email = (w.get('email') or '').strip()
        name = w.get('name', '')

        def parse_day_time(day, t):
            if not day or not t:
                return None
            try:
                return datetime.fromisoformat(f"{day}T{t}").replace(
                    tzinfo=None).isoformat(timespec='seconds')
            except (ValueError, TypeError):
                return None

        in_ts = parse_day_time(d.get('date'), d.get('clock_in'))
        if not in_ts:
            return jsonify({'ok': False, 'error': 'Invalid date or clock-in time.'}), 400
        out_ts = parse_day_time(d.get('date'), d.get('clock_out')) if d.get('clock_out') else None
        if d.get('clock_out') and not out_ts:
            return jsonify({'ok': False, 'error': 'Invalid clock-out time.'}), 400
        if out_ts and out_ts <= in_ts:
            return jsonify({'ok': False, 'error': 'Clock-out must be after clock-in.'}), 400
        DB['punches'].append({"id": new_id(), "email": email, "name": name,
                              "action": 'in', "ts": in_ts,
                              "source": "manual-add"})
        if out_ts:
            DB['punches'].append({"id": new_id(), "email": email, "name": name,
                                  "action": 'out', "ts": out_ts,
                                  "source": "manual-add"})
        save_db()  # full DB sync to Sheets (durability layer)
        return jsonify({'ok': True})


    @bp.route('/timecard/export')
    @login_required
    @visit_action_required('timecard')
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


    # ================= EMAIL HOURS =================

    def confirm_url_for(token):
        return (request.host_url.rstrip('/') + url_prefix +
                '/confirm/' + token)

    def find_token_entry(token):
        """(batch, entry) for a worker confirmation token, or (None, None)."""
        for b in DB.get('hour_batches', []):
            for e in b.get('entries', []):
                if e.get('token') == token:
                    return b, e
        return None, None

    def batch_counts(batch):
        c = {'sent': 0, 'confirmed': 0, 'problem': 0}
        for e in batch.get('entries', []):
            if e.get('status') in c:
                c[e['status']] += 1
        c['total'] = len(batch.get('entries', []))
        return c
    # 형아 (2026-10-26): pick a date range, see each worker's hours,
    # check only the workers who should get the email, and each checked
    # worker is emailed their own shifts + total. Sending goes through
    # a small Apps Script relay deployed under info@prismxe.com
    # (PATROL_MAIL_SCRIPT_URL env var) - the same webhook pattern as
    # the Sheets backup. Until the relay is linked, the page still
    # computes and previews everything.
    MAIL_SCRIPT_URL = os.environ.get('PATROL_MAIL_SCRIPT_URL', '')

    # Google Sheets ledger for hour batches (Apps Script webhook,
    # PATROL_HOURS_SHEET_URL env var; Hanseong Patrol only — 형아,
    # 2026-10-26: Sheets is the preservation/retrieval master).
    HOURS_SHEET_URL = os.environ.get('PATROL_HOURS_SHEET_URL', '')

    def sheet_hours_post(payload):
        """Best-effort POST to the Patrol Hours Sheets webhook.
        Returns True on {'ok': true}; never raises."""
        if not HOURS_SHEET_URL or company_id != 'hanseong':
            return False
        try:
            req = urllib.request.Request(
                HOURS_SHEET_URL,
                data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return bool(json.loads(resp.read().decode()).get('ok'))
        except Exception as e:  # noqa: BLE001 - ledger must not break email
            print(f'patrol[{company_id}]: hours sheet post failed: {e}',
                  flush=True)
            return False

    def sheet_log_batch(batch):
        return sheet_hours_post({
            'action': 'log_batch',
            'batch': {'id': batch['id'], 'start': batch['start'],
                      'end': batch['end'],
                      'created_at': batch['created_at']},
            'entries': [{'name': e['name'], 'email': e['email'],
                         'total': e['total'], 'status': e['status'],
                         'shifts': e['shifts']} for e in batch['entries']]})

    def sheet_set_status(batch, entry):
        return sheet_hours_post({
            'action': 'set_status', 'batch_id': batch['id'],
            'email': entry['email'], 'status': entry['status'],
            'confirmed_at': entry.get('confirmed_at') or '',
            'problem_note': entry.get('problem_note') or ''})

    def hours_summary(start_s, end_s):
        """Every worker with their paired shifts inside [start_s, end_s]."""
        by_email = {}
        for r in paired_records():
            cin = r.get('clock_in') or ''
            if cin and start_s <= cin[:10] <= end_s:
                key = (r.get('email') or '').strip().lower()
                by_email.setdefault(key, []).append(r)
        out = []
        for w in sorted(DB['workers'], key=lambda x: x.get('name', '')):
            em = (w.get('email') or '').strip().lower()
            shifts = by_email.get(em, []) if em else []
            total = round(sum(s['hours'] for s in shifts
                              if s.get('hours') is not None), 2)
            out.append({'name': w.get('name', ''),
                        'email': w.get('email', ''),
                        'shifts': shifts, 'total': total})
        return out

    # ================= WEEKLY PAYROLL =================

    def _week_monday(arg):
        """Monday (date) of the week containing arg (YYYY-MM-DD) or today."""
        from datetime import timedelta as _td
        try:
            d0 = datetime.fromisoformat(arg).date() if arg else None
        except (ValueError, TypeError):
            d0 = None
        if d0 is None:
            d0 = datetime.now(TZ).date()
        return d0 - _td(days=d0.weekday())

    def _payroll_week(monday):
        """Attendance-sheet style week grid built from paired punches.

        Returns one row per worker who has shifts that week:
        {name, email, days: [ {in, out, hours} | None x7 ], total, reg, ot}.
        Times are minute-truncated 'HH:MM'; hours come from those minutes
        (overnight shifts wrap past midnight), matching the paper sheet.
        """
        from datetime import timedelta as _td
        days = [monday + _td(days=i) for i in range(7)]
        rows = []
        for entry in hours_summary(days[0].isoformat(),
                                   days[-1].isoformat()):
            cells = {}
            exact = {}
            for s in entry['shifts']:
                cin = s.get('clock_in') or ''
                if not cin:
                    continue
                dkey = cin[:10]
                in_hm = cin[11:16]
                out_hm = (s['clock_out'][11:16]
                          if s.get('clock_out') else '')
                hrs = None
                if out_hm:
                    im = int(in_hm[:2]) * 60 + int(in_hm[3:5])
                    om = int(out_hm[:2]) * 60 + int(out_hm[3:5])
                    if om < im:
                        om += 24 * 60
                    hrs = (om - im) / 60
                if dkey in cells and cells[dkey]['hours'] is not None \
                        and hrs is not None:
                    prev = cells[dkey]
                    exact[dkey] += hrs
                    prev['hours'] = round(exact[dkey], 2)
                    prev['out'] = out_hm
                else:
                    exact[dkey] = hrs or 0
                    cells[dkey] = {'in': in_hm, 'out': out_hm,
                                   'hours': (round(hrs, 2)
                                             if hrs is not None else None)}
            if not cells:
                continue
            day_list = [cells.get(d.isoformat()) for d in days]
            total = round(sum(exact.values()), 2)
            rows.append({'name': entry['name'], 'email': entry['email'],
                         'days': day_list, 'total': total,
                         'reg': min(total, 40),
                         'ot': round(max(total - 40, 0), 2)})
        return rows

    def _payroll_xlsx_bytes(monday, rows):
        """The week grid as an .xlsx in the paper Attendance Sheet layout.

        In/Out are real time values; Hours/Total/Reg/OT are formulas so
        edited times recalculate. The Sign column is left blank for the
        worker's signature.
        """
        from datetime import timedelta as _td, time as _time
        from openpyxl import Workbook
        from openpyxl.styles import (Font, Alignment, Border, Side,
                                     PatternFill)
        from openpyxl.utils import get_column_letter
        days = [monday + _td(days=i) for i in range(7)]
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                     'Friday', 'Saturday', 'Sunday']
        wb = Workbook()
        ws = wb.active
        ws.title = 'Weekly Payroll'
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 1
        ws.print_options.horizontalCentered = True
        thin = Side(style='thin', color='808080')
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        hdr_fill = PatternFill('solid', fgColor='D9D9D9')
        center = Alignment(horizontal='center', vertical='center',
                           wrap_text=True)
        ncols = 14
        for col, w in {'A': 5, 'B': 22, 'C': 11, 'K': 9, 'L': 9,
                       'M': 9, 'N': 26}.items():
            ws.column_dimensions[col].width = w
        for i in range(7):
            ws.column_dimensions[get_column_letter(4 + i)].width = 10
        ws.merge_cells(start_row=1, start_column=1, end_row=1,
                       end_column=ncols)
        c = ws.cell(row=1, column=1,
                    value=f'Weekly Payroll — {company_display_name} Patrol')
        c.font = Font(bold=True, size=16)
        c.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[1].height = 26
        ws.merge_cells(start_row=2, start_column=1, end_row=2,
                       end_column=ncols)
        c = ws.cell(row=2, column=1,
                    value=f"Week of Monday, {days[0].strftime('%B %d, %Y')}"
                          f" – Sunday, {days[-1].strftime('%B %d, %Y')}")
        c.font = Font(bold=True, size=11)
        c.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[2].height = 18
        hr = 3
        for col, label in [(1, 'No'), (2, 'Name'), (3, 'Day / Date')]:
            ws.merge_cells(start_row=hr, start_column=col,
                           end_row=hr + 1, end_column=col)
            c = ws.cell(row=hr, column=col, value=label)
            c.font = Font(bold=True, size=10)
            c.fill = hdr_fill
            c.alignment = center
        for i, day in enumerate(days):
            col = 4 + i
            for r, val in [(hr, day_names[i]),
                           (hr + 1, day.strftime('%Y/%m/%d'))]:
                c = ws.cell(row=r, column=col, value=val)
                c.font = Font(bold=True, size=10)
                c.fill = hdr_fill
                c.alignment = center
        for col, label in [(11, 'Total Hours'), (12, 'Regular (≤40)'),
                           (13, 'Overtime (>40)'), (14, 'Sign')]:
            ws.merge_cells(start_row=hr, start_column=col,
                           end_row=hr + 1, end_column=col)
            c = ws.cell(row=hr, column=col, value=label)
            c.font = Font(bold=True, size=10)
            c.fill = hdr_fill
            c.alignment = center
        for r in (hr, hr + 1):
            for col in range(1, ncols + 1):
                ws.cell(row=r, column=col).border = border
        row = hr + 2
        first = row
        for idx, entry in enumerate(rows, start=1):
            base = row
            for col in (1, 2, 11, 12, 13, 14):
                ws.merge_cells(start_row=base, start_column=col,
                               end_row=base + 2, end_column=col)
            c = ws.cell(row=base, column=1, value=idx)
            c.alignment = center
            c = ws.cell(row=base, column=2, value=entry['name'])
            c.alignment = Alignment(horizontal='left', vertical='center')
            for j, label in enumerate(['Check In', 'Check Out', 'Hours']):
                c = ws.cell(row=base + j, column=3, value=label)
                c.alignment = center
                c.font = Font(size=9, italic=(j == 2))
            for i in range(7):
                col = 4 + i
                L = get_column_letter(col)
                cell = entry['days'][i]
                ci = ws.cell(row=base, column=col)
                co = ws.cell(row=base + 1, column=col)
                if cell:
                    if cell['in']:
                        ci.value = _time(int(cell['in'][:2]),
                                         int(cell['in'][3:5]))
                        ci.number_format = 'h:mm'
                    if cell['out']:
                        co.value = _time(int(cell['out'][:2]),
                                         int(cell['out'][3:5]))
                        co.number_format = 'h:mm'
                ci.alignment = center
                co.alignment = center
                h = ws.cell(row=base + 2, column=col)
                h.value = (f'=IF(OR({L}{base}="",{L}{base + 1}=""),"",'
                           f'({L}{base + 1}-{L}{base}'
                           f'+IF({L}{base + 1}<{L}{base},1,0))*24)')
                h.number_format = '0.00'
                h.alignment = center
            tot = ws.cell(row=base, column=11,
                          value=f'=SUM(D{base + 2}:J{base + 2})')
            tot.number_format = '0.00'
            tot.alignment = center
            tot.font = Font(bold=True)
            reg = ws.cell(row=base, column=12, value=f'=MIN(K{base},40)')
            reg.number_format = '0.00'
            reg.alignment = center
            ot = ws.cell(row=base, column=13,
                         value=f'=MAX(K{base}-40,0)')
            ot.number_format = '0.00'
            ot.alignment = center
            for r in range(base, base + 3):
                for col in range(1, ncols + 1):
                    ws.cell(row=r, column=col).border = border
            row += 3
        ws.cell(row=row, column=2, value='TOTAL').font = Font(bold=True)
        bases = [first + 3 * i for i in range(len(rows))]
        for col, letter in [(11, 'K'), (12, 'L'), (13, 'M')]:
            c = ws.cell(row=row, column=col,
                        value='=' + '+'.join(f'{letter}{b}' for b in bases))
            c.font = Font(bold=True)
            c.alignment = center
            c.number_format = '0.00'
        for col in range(1, ncols + 1):
            ws.cell(row=row, column=col).border = border
        row += 2
        for label in ['Confirmed by', 'Manager', 'Approved']:
            ws.cell(row=row, column=2, value=label).font = Font(bold=True,
                                                                size=11)
            ws.merge_cells(start_row=row, start_column=3, end_row=row,
                           end_column=6)
            ws.cell(row=row, column=8, value='Sign').font = Font(bold=True,
                                                                 size=11)
            ws.merge_cells(start_row=row, start_column=9, end_row=row,
                           end_column=12)
            for col in range(3, 7):
                ws.cell(row=row, column=col).border = Border(bottom=thin)
            for col in range(9, 13):
                ws.cell(row=row, column=col).border = Border(bottom=thin)
            row += 2
        ws.print_area = f'A1:N{row - 1}'
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    @bp.route('/payroll', methods=['GET', 'POST'])
    @visit_gate('payroll', 'Weekly Payroll')
    def payroll():
        from datetime import timedelta as _td
        monday = _week_monday(request.args.get('start'))
        days = [monday + _td(days=i) for i in range(7)]
        rows = _payroll_week(monday)
        return render_template(
            'patrol/payroll.html', view='payroll', monday=monday,
            days=days, rows=rows,
            prev=(monday - _td(days=7)).isoformat(),
            nxt=(monday + _td(days=7)).isoformat(),
            grand=round(sum(r['total'] for r in rows), 2))

    @bp.route('/payroll.xlsx')
    @visit_action_required('payroll')
    def payroll_xlsx():
        from datetime import timedelta as _td
        monday = _week_monday(request.args.get('start'))
        data = _payroll_xlsx_bytes(monday, _payroll_week(monday))
        fn = (f'Weekly_Payroll_{monday.isoformat()}_'
              f'{(monday + _td(days=6)).isoformat()}.xlsx')
        return send_file(
            io.BytesIO(data),
            mimetype='application/vnd.openxmlformats-officedocument.'
                     'spreadsheetml.sheet',
            as_attachment=True, download_name=fn)

    def hours_email(name, start_s, end_s, shifts, total, confirm_url=None):
        def fday(ts):
            return datetime.fromisoformat(ts).strftime('%a, %b %d')
        def ftm(ts):
            return datetime.fromisoformat(ts).strftime('%I:%M %p').lstrip('0')
        lines = [f"Hello {name},", "",
                 f"Here are your recorded work hours from {start_s} to {end_s}:",
                 ""]
        for s in shifts:
            out_t = ftm(s['clock_out']) if s.get('clock_out') else 'still clocked in'
            hrs = f"{s['hours']:.2f} hrs" if s.get('hours') is not None else '-'
            lines.append(f"{fday(s['clock_in'])}: {ftm(s['clock_in'])} - {out_t}  ({hrs})")
        lines += ["", f"Total: {total:.2f} hours", "",
                  "A PDF copy of this statement is attached.", "",
                  "Please check your hours carefully."]
        if confirm_url:
            lines += ["",
                      "To CONFIRM your hours are correct, open your "
                      "confirmation page and tap Confirm:",
                      confirm_url, "",
                      "If anything is wrong, you can report the problem "
                      "on that page."]
        else:
            lines += ["",
                      "If your hours are CORRECT, please REPLY to this "
                      "email with the word: Confirmed", "",
                      "If anything is WRONG, please REPLY to this email "
                      "and tell us what needs to be fixed."]
        if company_id == 'hanseong':
            lines += ["", "PrismXE", "Hanseong Arizona"]
        else:
            lines += ["", f"- {company_display_name} Patrol"]
        return (f"Your work hours: {start_s} to {end_s}", "\n".join(lines))

    def hours_pdf(name, email, start_s, end_s, shifts, total):
        """A clean one-page PDF statement of the same hours (형아,
        2026-10-26: send it 'organized like a PDF file')."""
        from fpdf import FPDF

        def fday(ts):
            return datetime.fromisoformat(ts).strftime('%a, %b %d')
        def fdate(ts):
            return datetime.fromisoformat(ts).strftime('%Y-%m-%d')
        def ftm(ts):
            return datetime.fromisoformat(ts).strftime('%I:%M %p').lstrip('0')

        pdf = FPDF(format='A4', unit='mm')
        pdf.set_auto_page_break(True, margin=15)
        font_dir = os.path.dirname(os.path.abspath(__file__))
        pdf.add_font("nanum", "", os.path.join(font_dir, "NanumGothic.ttf"))
        bold_path = os.path.join(font_dir, "NanumGothicBold.ttf")
        pdf.add_font("nanum", "B",
                     bold_path if os.path.exists(bold_path)
                     else os.path.join(font_dir, "NanumGothic.ttf"))
        pdf.add_page()
        pdf.set_font("nanum", "B", 17)
        if company_id == 'hanseong':
            pdf.cell(0, 9, "PrismXE", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("nanum", "", 12)
            pdf.cell(0, 7, "Hanseong Arizona", new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.cell(0, 9, f"{company_display_name} Patrol", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("nanum", "", 12)
        pdf.cell(0, 7, "Work Hours Statement", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
        pdf.set_font("nanum", "", 11)
        pdf.cell(0, 6, f"Worker: {name} ({email})", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 6, f"Period: {start_s} to {end_s}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        widths = [30, 26, 42, 42, 30]
        pdf.set_font("nanum", "B", 11)
        for wdt, txt in zip(widths, ["Date", "Day", "Clock In", "Clock Out", "Hours"]):
            pdf.cell(wdt, 8, txt, border=1, align="C")
        pdf.ln()
        pdf.set_font("nanum", "", 11)
        for s in shifts:
            out_t = ftm(s['clock_out']) if s.get('clock_out') else 'still in'
            hrs = f"{s['hours']:.2f}" if s.get('hours') is not None else '-'
            row = [fdate(s['clock_in']), fday(s['clock_in']).split(',')[0],
                   ftm(s['clock_in']), out_t, hrs]
            for wdt, txt in zip(widths, row):
                pdf.cell(wdt, 7, txt, border=1,
                         align="R" if txt == hrs else "L")
            pdf.ln()
        pdf.set_font("nanum", "B", 11)
        pdf.cell(sum(widths) - 30, 8, "TOTAL", border=1, align="R")
        pdf.cell(30, 8, f"{total:.2f}", border=1, align="R")
        pdf.ln(12)
        pdf.set_font("nanum", "", 10)
        pdf.cell(0, 6, "Confirm your hours using the link in the email.",
                 new_x="LMARGIN", new_y="NEXT")
        return bytes(pdf.output())

    def mail_send(to, subject, body, attach_name=None, attach_bytes=None):
        if not MAIL_SCRIPT_URL:
            return False, 'mail relay is not connected yet'
        try:
            payload = {"action": "send", "to": to,
                       "subject": subject, "body": body}
            if attach_bytes:
                payload["attach_name"] = attach_name or "statement.pdf"
                payload["attach_b64"] = base64.b64encode(attach_bytes).decode()
            req = urllib.request.Request(
                MAIL_SCRIPT_URL,
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                out = json.loads(resp.read().decode('utf-8'))
            return bool(out.get('ok')), (out.get('error') or '')
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    @bp.route('/email-hours', methods=['GET', 'POST'])
    @visit_gate('email_hours', 'Email hours')
    def email_hours():
        today = datetime.now(TZ).date()
        return render_template('patrol/email_hours.html',
                               start=(today - timedelta(days=13)).isoformat(),
                               end=today.isoformat(),
                               relay=bool(MAIL_SCRIPT_URL))

    @bp.route('/email-hours/compute', methods=['POST'])
    @visit_action_required('email_hours')
    def email_hours_compute():
        start_s = request.form.get('start') or ''
        end_s = request.form.get('end') or ''
        if not start_s or not end_s or start_s > end_s:
            flash('Pick a valid date range.')
            return render_template('patrol/email_hours.html', start=start_s,
                                   end=end_s, relay=bool(MAIL_SCRIPT_URL))
        workers = hours_summary(start_s, end_s)
        previews = {}
        for w in workers:
            if w['email'] and w['shifts']:
                previews[w['email'].strip().lower()] = hours_email(
                    w['name'], start_s, end_s, w['shifts'], w['total'])
        return render_template('patrol/email_hours.html', start=start_s,
                               end=end_s, workers=workers, previews=previews,
                               relay=bool(MAIL_SCRIPT_URL))

    @bp.route('/email-hours/send', methods=['POST'])
    @visit_action_required('email_hours')
    def email_hours_send():
        start_s = request.form.get('start') or ''
        end_s = request.form.get('end') or ''
        chosen = {e.strip().lower() for e in request.form.getlist('send_to')}
        results = []
        batch = {'id': uuid.uuid4().hex[:12], 'start': start_s,
                 'end': end_s,
                 'created_at': datetime.now().isoformat(timespec='seconds'),
                 'entries': []}
        for w in hours_summary(start_s, end_s):
            em = (w['email'] or '').strip().lower()
            if em not in chosen or not w['shifts']:
                continue
            token = uuid.uuid4().hex
            subject, body = hours_email(w['name'], start_s, end_s,
                                        w['shifts'], w['total'],
                                        confirm_url=confirm_url_for(token))
            try:
                pdf_bytes = hours_pdf(w['name'], w['email'], start_s, end_s,
                                      w['shifts'], w['total'])
                safe = ''.join(ch if ch.isalnum() else '_'
                               for ch in w['name']).strip('_') or 'worker'
                ok, err = mail_send(
                    em, subject, body,
                    attach_name=f"hours_{safe}_{start_s}_to_{end_s}.pdf",
                    attach_bytes=pdf_bytes)
            except Exception as e:  # noqa: BLE001 - never lose the email over a PDF
                ok, err = mail_send(em, subject, body)
                if ok:
                    err = f'sent without PDF ({e})'
            results.append({'name': w['name'], 'email': w['email'],
                            'ok': ok, 'error': err})
            if ok:
                batch['entries'].append({
                    'name': w['name'], 'email': w['email'], 'token': token,
                    'shifts': w['shifts'], 'total': w['total'],
                    'sent_at': datetime.now().isoformat(timespec='seconds'),
                    'status': 'sent', 'confirmed_at': None,
                    'problem_note': '', 'resends': 0})
        batch_id = None
        if batch['entries']:
            DB.setdefault('hour_batches', []).append(batch)
            save_db()
            batch_id = batch['id']
            sheet_log_batch(batch)
        return render_template('patrol/email_hours.html', start=start_s,
                               end=end_s, results=results,
                               relay=bool(MAIL_SCRIPT_URL),
                               batch_id=batch_id)

    # ---- Worker hour confirmation (public, token link from the email) ----

    @bp.route('/confirm/<token>', methods=['GET', 'POST'])
    def confirm_hours(token):
        batch, entry = find_token_entry(token)
        if not entry:
            return render_template('patrol/confirm.html', not_found=True), 404
        if request.method == 'POST':
            action = request.form.get('action') or ''
            if action == 'confirm':
                entry['status'] = 'confirmed'
                entry['confirmed_at'] = datetime.now().isoformat(
                    timespec='seconds')
                save_db()
                sheet_set_status(batch, entry)
            elif action == 'problem':
                entry['status'] = 'problem'
                entry['problem_note'] = (
                    request.form.get('note') or '').strip()[:2000]
                save_db()
                sheet_set_status(batch, entry)
        brand = (['PrismXE', 'Hanseong Arizona'] if company_id == 'hanseong'
                 else [f'{company_display_name} Patrol'])
        return render_template('patrol/confirm.html', batch=batch,
                               entry=entry, brand=brand)


    # ================= STAFF SCHEDULE =================
    from datetime import timedelta

    DB.setdefault('schedule', {})
    DB.setdefault('schedule_requests', [])

    ROLES = ['Supervisor', 'Foreman', 'Helper']

    def _sched_week(anchor):
        monday = anchor - timedelta(days=anchor.weekday())
        return [monday + timedelta(days=i) for i in range(7)]

    def _worker_by_email(email):
        email = (email or '').strip().lower()
        if not email:
            return None
        for w in DB['workers']:
            if (w.get('email') or '').strip().lower() == email:
                return w
        return None

    def _ensure_schedule_seed():
        """One-time sample starter: hand out Supervisor/Foreman/Helper
        roles to workers who have none and fill a sample per-day
        schedule. Runs once (flag stored in DB)."""
        if DB.get('_schedule_seeded'):
            return
        DB['_schedule_seeded'] = True
        workers = [w for w in DB['workers'] if (w.get('email') or '').strip()]
        for i, w in enumerate(sorted(workers,
                                     key=lambda x: x.get('name', ''))):
            if not w.get('role'):
                w['role'] = ROLES[i % len(ROLES)]
        shift_ids = [s['id'] for s in DB['shifts']]
        if workers and shift_ids:
            by_role = {r: [w for w in workers if w.get('role') == r]
                       for r in ROLES}
            today = local_now().date()
            for wk in range(2):
                for d in _sched_week(today + timedelta(days=7 * wk)):
                    if d.weekday() >= 5:
                        continue
                    day = DB['schedule'].setdefault(d.isoformat(), {})
                    wd = d.weekday()
                    for si, sid in enumerate(shift_ids):
                        for r in ROLES:
                            pool = by_role.get(r) or []
                            if pool:
                                w = pool[(wd + si) % len(pool)]
                                day.setdefault(w['id'], sid)
            first = workers[0]
            nxt_mon = _sched_week(today + timedelta(days=7))[0]
            DB['schedule_requests'].append({
                "id": new_id(), "worker_id": first['id'],
                "name": first.get('name', ''), "email": first.get('email', ''),
                "date": nxt_mon.isoformat(),
                "from_shift": DB['schedule'].get(
                    nxt_mon.isoformat(), {}).get(first['id'], ''),
                "to_shift": "OFF",
                "note": "Sample request - approve or reject it to try the flow.",
                "status": "pending", "created": now_iso(), "decided_at": ""})
        save_db()

    def _migrate_shift_ids():
        """One-time: give each worker a standing shift_id (the shift set
        next to their name) derived from the sample per-day schedule -
        their most frequent assignment. Workers are managed by standing
        shift from then on."""
        if DB.get('_shift_migrated'):
            return
        DB['_shift_migrated'] = True
        changed = False
        for w in DB['workers']:
            if w.get('shift_id'):
                continue
            counts = {}
            for day in DB.get('schedule', {}).values():
                v = day.get(w['id'])
                if v and v != 'OFF':
                    counts[v] = counts.get(v, 0) + 1
            if counts:
                w['shift_id'] = max(counts, key=counts.get)
                changed = True
        if changed:
            save_db()

    def _midnight_rebase():
        """One-time: rotate the 24 slots so the day starts at 0:00 and
        ends at 23:00 (was 7:00 -> 6:00). Equipment check indexes are
        remapped, and the 2-week plan is rebuilt so each hour lands on
        its real calendar date (post-midnight hours move back a day)."""
        if DB.get('_midnight_rebase'):
            return
        DB['_midnight_rebase'] = True
        slots = DB['slots']
        p = next((i for i, s in enumerate(slots)
                  if slot_hour(s.get('label', '')) == 0), None)
        if p is None or p == 0:
            save_db()
            return
        n = len(slots)
        DB['slots'] = slots[p:] + slots[:p]

        def new_idx(old):
            return (old - p) % n
        for e in DB['equipment']:
            ch = e.get('checks') or {}
            e['checks'] = {str(new_idx(int(k))): v
                           for k, v in ch.items() if str(k).lstrip('-').isdigit()}
        old_plan = DB.get('slot_plan') or {}
        if old_plan:
            from datetime import date as _d
            new_plan = {}
            dates = sorted(old_plan)
            cur = _d.fromisoformat(dates[0]) - timedelta(days=1)
            last = _d.fromisoformat(dates[-1]) + timedelta(days=1)
            while cur <= last:
                ds = cur.isoformat()
                prev = (cur - timedelta(days=1)).isoformat()
                day = {}
                for oi_s, wids in (old_plan.get(ds) or {}).items():
                    oi = int(oi_s)
                    if oi < p:
                        day[str(new_idx(oi))] = wids
                for oi_s, wids in (old_plan.get(prev) or {}).items():
                    oi = int(oi_s)
                    if oi >= p:
                        day[str(new_idx(oi))] = wids
                if ds in old_plan or day:
                    new_plan[ds] = day
                cur += timedelta(days=1)
            DB['slot_plan'] = new_plan
        save_db()

    def _seed_slot_plan():
        """One-time: build the first 2-week plan (from this week's
        Monday) out of the standing shift assignments - each hour gets
        the workers of the shift that hour belongs to. After that the
        plan is edited directly in Settings -> Time Slots & Shifts."""
        if DB.get('_slot_plan_seeded'):
            return
        DB['_slot_plan_seeded'] = True
        plan = DB.setdefault('slot_plan', {})
        monday = _sched_week(local_now().date())[0]
        for off in range(14):
            d = (monday + timedelta(days=off)).isoformat()
            if d in plan:
                continue
            day = {}
            for i, s in enumerate(DB['slots']):
                wids = [w['id'] for w in DB['workers']
                        if (w.get('shift_id') or '') == (s.get('shift') or '')]
                if wids:
                    day[str(i)] = wids
            plan[d] = day
        save_db()

    def _ranges_for(day_plan, wid):
        """Merge a worker's slot indexes in one day into hour ranges,
        e.g. [('7:00', '12:00'), ('13:00', '20:00')]."""
        slots = DB['slots']
        idxs = sorted(int(k) for k, v in (day_plan or {}).items()
                      if wid in (v or []))
        if not idxs:
            return []
        runs = [[idxs[0]]]
        for i in idxs[1:]:
            if i == runs[-1][-1] + 1:
                runs[-1].append(i)
            else:
                runs.append([i])
        out = []
        for run in runs:
            start = slots[run[0]].get('label', '')
            end = slots[(run[-1] + 1) % len(slots)].get('label', '')
            out.append((start, end))
        return out

    def _two_weeks(monday):
        """14 day summaries from the given Monday:
        [{date, label, today, planned, entries: [{worker, ranges}]}]"""
        plan = DB.get('slot_plan', {})
        days = []
        for off in range(14):
            d = monday + timedelta(days=off)
            day_plan = plan.get(d.isoformat())
            entries = []
            if day_plan:
                wids = []
                for v in day_plan.values():
                    for wid in v or []:
                        if wid not in wids:
                            wids.append(wid)
                for wid in wids:
                    w = find('workers', wid)
                    if w is not None:
                        entries.append({"worker": w,
                                        "ranges": _ranges_for(day_plan, wid)})
                entries.sort(key=lambda e: e['worker'].get('name', ''))
            days.append({"date": d.isoformat(),
                         "label": d.strftime('%a %m/%d'),
                         "today": d == local_now().date(),
                         "planned": day_plan is not None,
                         "entries": entries})
        return days

    @bp.route('/schedule', methods=['GET', 'POST'])
    @visit_gate('schedule_page', 'Schedule')
    def schedule_page():
        _ensure_schedule_seed()
        _migrate_shift_ids()
        _midnight_rebase()
        _seed_slot_plan()
        monday = _sched_week(local_now().date())[0]
        pending = sorted(
            [r for r in DB['schedule_requests'] if r.get('status') == 'pending'],
            key=lambda r: r.get('created', ''))
        history = sorted(
            [r for r in DB['schedule_requests'] if r.get('status') != 'pending'],
            key=lambda r: r.get('decided_at', ''), reverse=True)
        return render_template('patrol/schedule.html',
                               days=_two_weeks(monday),
                               pending=pending, history=history[:10])

    @bp.route('/schedule/requests/<rid>/decide', methods=['POST'])
    @visit_action_required('schedule_page')
    def schedule_decide(rid):
        d = request.get_json(force=True)
        req = next((r for r in DB['schedule_requests'] if r['id'] == rid), None)
        if not req or req.get('status') != 'pending':
            return jsonify({'ok': False}), 404
        decision = d.get('decision')
        if decision not in ('approved', 'rejected'):
            return jsonify({'ok': False}), 400
        req['status'] = decision
        req['decided_at'] = now_iso()
        if decision == 'approved':
            valid = {s['id'] for s in DB['shifts']} | {'OFF'}
            if req.get('to_shift') in valid:
                w = find('workers', req['worker_id'])
                if w is not None:
                    w['shift_id'] = req['to_shift']
        save_db()
        return jsonify({'ok': True})

    @bp.route('/my-schedule')
    @login_required
    def my_schedule():
        _ensure_schedule_seed()
        _migrate_shift_ids()
        _midnight_rebase()
        _seed_slot_plan()
        email = request.args.get('email', '')
        worker = _worker_by_email(email)
        reqs = []
        my_days = []
        if worker:
            reqs = sorted(
                [r for r in DB['schedule_requests']
                 if r.get('worker_id') == worker['id']],
                key=lambda r: r.get('created', ''), reverse=True)
            monday = _sched_week(local_now().date())[0]
            plan = DB.get('slot_plan', {})
            for off in range(14):
                d = monday + timedelta(days=off)
                ranges = _ranges_for(plan.get(d.isoformat()),
                                     worker['id'])
                if ranges:
                    my_days.append({"label": d.strftime('%a %m/%d'),
                                    "today": d == local_now().date(),
                                    "ranges": ranges})
        return render_template('patrol/my_schedule.html', worker=worker,
                               email=email, my_days=my_days,
                               my_requests=reqs[:8])

    @bp.route('/my-schedule/request', methods=['POST'])
    def my_schedule_request():
        email = request.form.get('email', '')
        worker = _worker_by_email(email)
        if not worker:
            flash('Email not found. Use the email you clock in with.')
            return redirect(url_for(BP + '.my_schedule', email=email))
        note = (request.form.get('note') or '').strip()
        if not note:
            flash('Write what change you need.')
            return redirect(url_for(BP + '.my_schedule', email=email))
        DB['schedule_requests'] = [
            r for r in DB['schedule_requests']
            if not (r.get('worker_id') == worker['id']
                    and r.get('status') == 'pending')]
        DB['schedule_requests'].append({
            "id": new_id(), "worker_id": worker['id'],
            "name": worker.get('name', ''), "email": worker.get('email', ''),
            "date": local_now().date().isoformat(),
            "from_shift": "", "to_shift": "", "note": note,
            "status": "pending", "created": now_iso(), "decided_at": ""})
        save_db()
        flash('Request sent. A manager will review it.')
        return redirect(url_for(BP + '.my_schedule', email=email))

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
    url_prefix='/Hanseong/patrol',
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
