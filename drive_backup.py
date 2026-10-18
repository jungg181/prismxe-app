"""Shared Google Drive backup for the prismxe-app databases.

The Render disk is ephemeral, so every deploy wipes the local JSON files.
This module backs up BOTH databases (CM + COA) into the user's Google Drive
folder via the Apps Script web app (the only credentialed path to Drive).

Backup files in the folder:
  COA_Data.json      full COA database (restored on fresh deploy)
  CM_Data.json       full CM database (restored on fresh deploy)
  COA_Timecards.csv  human-readable COA timecards

Everything is best-effort and never raises: if the Apps Script hasn't been
updated with the saveCoaBackup/getCoaBackup actions yet, backups are skipped
and /coa/backup shows exactly what's missing.
"""

import csv
import io
import json
import os
import threading
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CM_DB_PATH = os.path.join(BASE_DIR, 'data_cm.json')
COA_DB_PATH = os.path.join(BASE_DIR, 'data_coa.json')

SHEETS_URL = os.environ.get('CM_SHEETS_URL', '')
COA_DRIVE_FOLDER_ID = '1b5hO9wUMl1GO4UBhRd6YUdptGdSRC0r7'

# action -> human label, used by the /coa/backup status page
PROBE_ACTIONS = [
    ('saveData', 'CM spreadsheet sync'),
    ('saveCoaBackup', 'Drive folder backup'),
    ('getCoaBackup', 'Drive folder restore'),
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
        if action == 'sendEmail':
            res = post({'action': action, 'probe': True}, timeout=15)
        elif action == 'getCoaBackup':
            res = post({'action': action, 'probe': True,
                        'folder_id': COA_DRIVE_FOLDER_ID}, timeout=15)
        elif action == 'saveCoaBackup':
            res = post({'action': action, 'probe': True,
                        'folder_id': COA_DRIVE_FOLDER_ID}, timeout=15)
        else:
            # saveData/getData/uploadPlan have no probe; infer from a cheap call
            res = post({'action': 'getData'}
                       if action == 'getData' else {'action': action, 'probe': True},
                       timeout=15)
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
    """Write both DBs to the Drive folder. Returns (ok, detail)."""
    global _last_result
    coa_db = _read_db(COA_DB_PATH)
    cm_db = _read_db(CM_DB_PATH)
    if coa_db is None and cm_db is None:
        _last_result = {'ok': False, 'at': _now(),
                        'detail': 'No local database files found.'}
        return False, _last_result['detail']
    res = post({'action': 'saveCoaBackup',
                'coa_json': json.dumps(coa_db or {}),
                'cm_json': json.dumps(cm_db or {}),
                'coa_csv': _coa_timecards_csv(coa_db or {}),
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
    """Fetch both DBs from the Drive folder. Returns {'coa':..., 'cm':...} or None."""
    res = post({'action': 'getCoaBackup', 'folder_id': COA_DRIVE_FOLDER_ID},
               timeout=timeout)
    if res and res.get('ok') and isinstance(res.get('data'), dict):
        return res['data']
    return None


def last_result():
    return dict(_last_result)


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
