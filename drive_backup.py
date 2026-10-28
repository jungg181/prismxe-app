"""Google Drive backup for ALL prismxe-app databases (app.prismxe.com).

The Render disk is ephemeral, so every deploy wipes the local JSON files.
This module backs up every module's database into the user's Google Drive
folder via the Apps Script web app (the only credentialed path to Drive).

Backup files in the folder:
  COA_Data.json       full COA database (restored on fresh deploy)
  COA_Timecards.csv   human-readable COA timecards
  CM_Data.json        full CM database
  Patrol_Data.json    patrol database
  PatrolCOA_Data.json patrol-COA database
  Gauge_Data.json     gauge database
  BibleUsers.json     bible user registrations/progress
  PortalConfig.json   portal folder/app config

Everything is best-effort and never raises: if the Apps Script hasn't been
updated with the saveFile/getFile actions yet, backups are skipped.
"""

import csv
import io
import json
import os
import threading
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SHEETS_URL = os.environ.get('CM_SHEETS_URL', '')
COA_DRIVE_FOLDER_ID = '1b5hO9wUMl1GO4UBhRd6YUdptGdSRC0r7'

# (local filename, drive filename) — every JSON DB that must survive redeploys
ALL_BACKUPS = [
    ('data_coa.json', 'COA_Data.json'),
    ('data_cm.json', 'CM_Data.json'),
    ('data_patrol.json', 'Patrol_Data.json'),
    ('data_patrol_coa.json', 'PatrolCOA_Data.json'),
    ('data_gauge.json', 'Gauge_Data.json'),
    ('data_gauge_photo_tags.json', 'GaugePhotoTags.json'),
    ('data/bible_users.json', 'BibleUsers.json'),
    ('portal_config.json', 'PortalConfig.json'),
]

# action -> human label, used by the /coa/backup status page
PROBE_ACTIONS = [
    ('saveData', 'CM spreadsheet sync'),
    ('saveCoaBackup', 'Drive folder backup (COA)'),
    ('getCoaBackup', 'Drive folder restore (COA)'),
    ('saveFile', 'Drive folder backup (all modules)'),
    ('getFile', 'Drive folder restore (all modules)'),
    ('sendEmail', 'Confirmation emails'),
    ('uploadPlan', 'Plan image upload'),
]

_last_result = {'ok': None, 'at': None, 'detail': ''}


def post(payload, timeout=25):
    """POST to the Apps Script. Returns the parsed JSON response or None."""
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


def probe():
    """Check which Apps Script actions the deployed script supports."""
    results = {}
    if not SHEETS_URL:
        return {a: False for a, _ in PROBE_ACTIONS}
    for action, _label in PROBE_ACTIONS:
        if action in ('sendEmail', 'getCoaBackup', 'saveCoaBackup',
                      'saveFile', 'getFile'):
            payload = {'action': action, 'probe': True,
                       'folder_id': COA_DRIVE_FOLDER_ID}
            if action in ('saveFile', 'getFile'):
                payload['filename'] = 'probe.json'
            res = post(payload, timeout=15)
        else:
            # saveData/uploadPlan have no probe; infer from a cheap call
            res = post({'action': action, 'probe': True}, timeout=15)
        results[action] = bool(res and (res.get('ok') or res.get('probe')))
    return results


def _read_db(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _coa_timecards_csv(coa_db):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['id', 'worker', 'email', 'phone', 'site', 'clock_in', 'clock_out',
                'hours', 'reported_hours', 'status'])
    sites = {s.get('id'): s.get('name', '') for s in coa_db.get('sites', [])}
    workers = {x.get('id'): x for x in coa_db.get('workers', [])}
    for t in sorted(coa_db.get('timecards', []), key=lambda x: x.get('clock_in') or ''):
        wr = workers.get(t.get('worker_id')) or {}
        w.writerow([t.get('id'), wr.get('name', ''), wr.get('email', ''),
                    wr.get('phone', ''), sites.get(t.get('site_id'), ''),
                    t.get('clock_in'), t.get('clock_out'), t.get('hours'),
                    t.get('reported_hours', ''), t.get('status')])
    return buf.getvalue()


def backup_now(timeout=40):
    """Write the COA DB to the Drive folder. Returns (ok, detail)."""
    global _last_result
    coa_db = _read_db(COA_DB_PATH)
    if coa_db is None:
        _last_result = {'ok': False, 'at': _now(),
                        'detail': 'No local COA database file found.'}
        return False, _last_result['detail']
    res = post({'action': 'saveCoaBackup',
                'coa_json': json.dumps(coa_db),
                'coa_csv': _coa_timecards_csv(coa_db),
                'folder_id': COA_DRIVE_FOLDER_ID}, timeout=timeout)
    ok = bool(res and res.get('ok'))
    detail = '' if ok else (res.get('error') if res else
                           'No response — is the Apps Script updated?')
    _last_result = {'ok': ok, 'at': _now(), 'detail': detail}
    return ok, detail


def backup_async():
    """Fire-and-forget backup in a background thread."""
    if not SHEETS_URL:
        return
    threading.Thread(target=backup_now, daemon=True).start()


def restore(timeout=40):
    """Fetch the COA DB from the Drive folder. Returns {'coa':...} or None."""
    res = post({'action': 'getCoaBackup', 'folder_id': COA_DRIVE_FOLDER_ID},
               timeout=timeout)
    if res and res.get('ok') and isinstance(res.get('data'), dict):
        return res['data']
    return None


def backup_file(local_name, drive_name, timeout=40):
    """Back up one local JSON file to the Drive folder. Returns (ok, detail)."""
    path = os.path.join(BASE_DIR, local_name)
    try:
        with open(path) as f:
            content = f.read()
    except Exception as e:
        return False, f'Cannot read {local_name}: {e}'
    res = post({'action': 'saveFile', 'filename': drive_name,
                'content': content, 'mime': 'application/json',
                'folder_id': COA_DRIVE_FOLDER_ID}, timeout=timeout)
    ok = bool(res and res.get('ok'))
    detail = '' if ok else (res.get('error') if res else 'No response')
    return ok, detail


def restore_file(drive_name, local_name, timeout=40):
    """Restore one file from Drive if the local copy is missing/empty.
    Returns True if the local file now exists with content."""
    path = os.path.join(BASE_DIR, local_name)
    try:
        if os.path.getsize(path) > 10:
            return True  # local copy is fine
    except OSError:
        pass
    res = post({'action': 'getFile', 'filename': drive_name,
                'folder_id': COA_DRIVE_FOLDER_ID}, timeout=timeout)
    if res and res.get('ok') and res.get('content'):
        try:
            with open(path, 'w') as f:
                f.write(res['content'])
            return True
        except Exception:
            return False
    return False


def backup_all(timeout=60):
    """Back up every module's DB file to Drive. Returns {drive_name: (ok, detail)}."""
    global _last_result
    results = {}
    for local_name, drive_name in ALL_BACKUPS:
        ok, detail = backup_file(local_name, drive_name, timeout=timeout)
        results[drive_name] = (ok, detail)
    n_ok = sum(1 for ok, _ in results.values() if ok)
    _last_result = {'ok': n_ok == len(results), 'at': _now(),
                    'detail': f'{n_ok}/{len(results)} files backed up'}
    return results


def backup_all_async():
    """Fire-and-forget full backup in a background thread."""
    if not SHEETS_URL:
        return
    threading.Thread(target=backup_all, daemon=True).start()


def backup_one_async(local_name):
    """Fire-and-forget backup of a single DB file in a background thread."""
    if not SHEETS_URL:
        return
    drive_name = next((d for l, d in ALL_BACKUPS if l == local_name), None)
    if not drive_name:
        return
    threading.Thread(target=lambda: backup_file(local_name, drive_name),
                     daemon=True).start()


def restore_all(timeout=60):
    """Restore every missing DB file from Drive. Returns {local_name: bool}."""
    results = {}
    for local_name, drive_name in ALL_BACKUPS:
        results[local_name] = restore_file(drive_name, local_name,
                                           timeout=timeout)
    return results


def last_result():
    return dict(_last_result)


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
