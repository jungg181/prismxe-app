#!/usr/bin/env python3
"""Boiler gauge inspection server (v1).

- GET  /e/<equip_id>        : equipment inspection page (mobile)
- GET  /api/equipment/<id> : equipment info + gauge list JSON
- POST /api/read           : gauge photo upload → automatic needle reading {angle, value, unit}
- POST /api/log            : save measurement
- GET  /api/export         : download measurement log CSV
- GET  /health             : status check
"""
import csv
import io
import json
import math
import os
import sys
import threading
import urllib.request
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Blueprint, jsonify, redirect, render_template, request, send_file, session, url_for

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from gauge_reader import detect_dial, needle_angle, angle_to_value  # noqa: E402

import cv2
import numpy as np

bp = Blueprint("gauge", __name__, url_prefix="/Hanseong/gauge",
               template_folder="templates/gauge")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ChangeMe123")
SHEETS_URL = os.environ.get("SHEETS_URL", "")  # Google Apps Script web app URL
# Service-account Drive upload (server-side, no user OAuth needed)
DRIVE_SA_JSON = os.environ.get("GOOGLE_DRIVE_SA_JSON", "")  # service account JSON content (deprecated)
# OAuth refresh token approach (uploads as the user, has storage quota)
DRIVE_REFRESH_TOKEN = os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN", "")
DRIVE_CLIENT_ID = os.environ.get("GOOGLE_DRIVE_CLIENT_ID", "")
DRIVE_CLIENT_SECRET = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "")
DRIVE_FOLDER_ID = os.environ.get("GAUGE_DRIVE_FOLDER_ID", "")  # Drive folder for gauge photos


def drive_service():
    """Build a Drive API client. Prefers OAuth refresh token (user quota);
    falls back to service account JSON if configured."""
    try:
        from googleapiclient.discovery import build
        # OAuth refresh token (preferred — uploads as user with quota)
        if DRIVE_REFRESH_TOKEN and DRIVE_CLIENT_ID and DRIVE_CLIENT_SECRET:
            from google.oauth2.credentials import Credentials
            creds = Credentials(
                None,
                refresh_token=DRIVE_REFRESH_TOKEN,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=DRIVE_CLIENT_ID,
                client_secret=DRIVE_CLIENT_SECRET,
                scopes=["https://www.googleapis.com/auth/drive.file"])
            return build("drive", "v3", credentials=creds)
        # Service account fallback
        if DRIVE_SA_JSON:
            from google.oauth2 import service_account
            info = json.loads(DRIVE_SA_JSON)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/drive.file"])
            return build("drive", "v3", credentials=creds)
    except Exception:
        pass
    return None


@bp.route("/api/drive-upload", methods=["POST"])
def api_drive_upload():
    """Upload a gauge photo to Google Drive (as the user via OAuth).

    Expects multipart form: photo=<file>, filename=<name>.
    Optional: gauge_id, equip_id, unit, value — if value is present, the
    reading is also logged to the Sheet (same as /api/log).
    Auto-creates a YYYY-MM-DD subfolder inside DRIVE_FOLDER_ID.
    """
    svc = drive_service()
    if not svc:
        return jsonify({"ok": False, "error": "Drive not configured"}), 500
    if not DRIVE_FOLDER_ID:
        return jsonify({"ok": False, "error": "Drive folder not set"}), 500
    f = request.files.get("photo")
    if not f:
        return jsonify({"ok": False, "error": "no photo"}), 400
    filename = request.form.get("filename") or "photo.jpg"
    # Date subfolder: YYYY-MM-DD from filename or today
    import re
    m = re.match(r"(\d{4}-\d{2}-\d{2})", filename)
    date_str = m.group(1) if m else datetime.now().strftime("%Y-%m-%d")
    try:
        from googleapiclient.http import MediaIoBaseUpload
        # Find or create date subfolder
        q = (f"'{DRIVE_FOLDER_ID}' in parents and "
             f"name='{date_str}' and mimeType='application/vnd.google-apps.folder' "
             f"and trashed=false")
        res = svc.files().list(q=q, fields="files(id)", pageSize=1).execute()
        files = res.get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            meta = {"name": date_str, "mimeType": "application/vnd.google-apps.folder",
                    "parents": [DRIVE_FOLDER_ID]}
            folder_id = svc.files().create(body=meta, fields="id").execute()["id"]
        data = f.read()
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype="image/jpeg", resumable=False)
        meta = {"name": filename, "parents": [folder_id]}
        result = svc.files().create(body=meta, media_body=media, fields="id,name").execute()
        # If a reading value was sent, log it to the Sheet too
        value = (request.form.get("value") or "").strip()
        logged = False
        if value:
            try:
                log_reading(
                    equip_id=request.form.get("equip_id", ""),
                    gauge_id=request.form.get("gauge_id", ""),
                    value=value,
                    unit=request.form.get("unit", ""),
                    photo=filename,
                    slot=request.form.get("slot", ""),
                )
                logged = True
            except Exception:
                pass
        return jsonify({"ok": True, "id": result.get("id"), "name": result.get("name"),
                        "folder": date_str, "logged": logged})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


def log_reading(equip_id="", gauge_id="", value="", unit="", photo="", state="", worker="", manual=True, slot=""):
    """Shared reading logger used by /api/log and /api/drive-upload."""
    sys, dev = find_equipment(equip_id)
    gauge = next((g for g in (dev["gauges"] if dev else [])
                  if g["id"] == gauge_id), None)
    row = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "system": sys["name"] if sys else "",
        "equip_id": equip_id,
        "equip_name": dev["name"] if dev else "",
        "gauge_id": gauge_id,
        "gauge_name": gauge["name"] if gauge else "",
        "state": state,
        "value": value,
        "unit": unit,
        "angle_deg": "",
        "manual": "Y" if manual else "",
        "worker": worker,
        "photo": photo,
        "slot": slot,
    }
    new_file = not LOG_CSV.exists()
    with open(LOG_CSV, "a", newline="", encoding="utf-8-sig") as fo:
        w = csv.DictWriter(fo, fieldnames=LOG_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    # persist to Google Sheets (best-effort, background)
    sheets_post_async({"action": "reading", "reading": row})
    return row


@bp.route("/api/drive-photos", methods=["GET"])
def api_drive_photos():
    """List photos in the Drive folder and its date subfolders (for agent review).
    Returns id, name, createdTime, and folder path."""
    ok, result = _list_drive_photos()
    if not ok:
        return jsonify({"ok": False, "error": result}), 500
    return jsonify({"ok": True, "files": result})


@bp.route("/api/drive-photo/<fid>", methods=["GET"])
def api_drive_photo(fid):
    """Download a photo from Drive by file ID (for agent review).

    Falls back to the service account when the user OAuth token is revoked.
    """
    svc = drive_service()
    if not svc:
        return jsonify({"ok": False, "error": "Drive not configured"}), 500
    try:
        return _serve_drive_photo(svc, fid)
    except Exception as e:
        if "invalid_grant" in str(e):
            sa = drive_service_sa()
            if sa:
                try:
                    return _serve_drive_photo(sa, fid)
                except Exception as e2:
                    return jsonify({"ok": False, "error": str(e2)[:200]}), 500
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


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


def sheets_get_config(timeout=15):
    """Fetch the persisted settings from Google Sheets. Returns dict or None."""
    if not SHEETS_URL:
        return None
    try:
        with urllib.request.urlopen(SHEETS_URL + "?action=getConfig",
                                    timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("config")
    except Exception as e:  # noqa: BLE001
        print(f"sheets_get_config failed: {e}", flush=True)
        return None


def sheets_post_async(payload):
    threading.Thread(target=sheets_post, args=(payload,), daemon=True).start()

_gauge_file = BASE / "data_gauge.json"
if _gauge_file.exists():
    _gauge_data = json.loads(_gauge_file.read_text())
else:
    # Fresh deploy (file is gitignored / ephemeral): restore config from
    # Google Sheets, else start with empty equipment/calibration.
    _restored = sheets_get_config()
    _gauge_data = _restored if isinstance(_restored, dict) and "equipment" in _restored \
        else {"equipment": [], "calibration": {}}
    if _restored:
        print("gauge: restored config from Google Sheets", flush=True)
EQUIPMENT = _gauge_data["equipment"]
CALIB = _gauge_data["calibration"]
LOG_CSV = BASE / "data" / "gauge_readings.csv"
UPLOAD_DIR = BASE / "uploads" / "gauge"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
LOG_FIELDS = ["time", "system", "equip_id", "equip_name", "gauge_id",
              "gauge_name", "state", "value", "unit", "angle_deg",
              "manual", "worker", "photo", "slot"]

# Patrol round times per system (from the Daily Check List rev3 sheets).
# The worker picks the round first; every reading in that round is logged
# with slot = "YYYY-MM-DD HH:MM" (the round's date + start time).
PATROL_SLOTS = {
    "HOB": ["07:00", "09:00", "11:00", "13:00", "15:00", "17:00",
            "19:00", "21:00", "23:00", "01:00", "03:00", "05:00"],
    "SRP": ["08:00", "12:00", "16:00", "20:00", "00:00", "04:00"],
    "RO": ["10:00", "14:00", "18:00", "22:00", "02:00", "06:00"],
    "FAN": ["09:00", "17:00", "01:00"],
}


def find_equipment(equip_id):
    for sys in EQUIPMENT["systems"]:
        for grp in ("boilers", "pumps"):
            for dev in sys[grp]:
                if dev["id"] == equip_id:
                    return sys, dev
    return None, None


def read_gauge_bytes(data: bytes, gauge_id: str):
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not read the photo")
    # 큰 사진은 축소 (처리 속도 향상 — 판독 정확도에 영향 없음)
    h0, w0 = img.shape[:2]
    if max(h0, w0) > 1280:
        s = 1280 / max(h0, w0)
        img = cv2.resize(img, (int(w0 * s), int(h0 * s)),
                         interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cal = CALIB.get(gauge_id, {})
    if {"cx", "cy", "radius"} <= set(cal):
        h, w = gray.shape
        cx, cy = cal["cx"] * w, cal["cy"] * h
        radius = cal["radius"] * min(h, w)
    else:
        cx, cy, radius = detect_dial(gray)
    angle = needle_angle(gray, cx, cy, radius)
    out = {"angle_deg": round(angle, 2)}
    if "angle_min" in cal and "angle_max" in cal:
        out["value"] = round(angle_to_value(angle, cal), 2)
        out["unit"] = cal.get("unit", "")
    else:
        out["value"] = None
        out["unit"] = ""
        out["needs_calibration"] = True
    return out


@bp.get("/api/qr")
def api_qr():
    """Return a QR code PNG for the given text (used for gauge labels)."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H
    text = request.args.get("text", "")
    if not text or len(text) > 500:
        return "bad request", 400
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H,
                       box_size=12, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@bp.get("/health")
def health():
    return {"ok": True}


@bp.get("/api/checklist")
def api_checklist():
    """Simple equipment+gauge list for the native Gauge Check app."""
    systems = []
    for s in EQUIPMENT.get("systems", []):
        items = []
        for dev in s.get("boilers", []) + s.get("pumps", []):
            items.append({
                "id": dev["id"], "name": dev["name"],
                "kind": "pump" if dev in s.get("pumps", []) else "boiler",
                "gauges": [{"id": g["id"], "name": g["name"],
                            "unit": g.get("unit", "")} for g in dev.get("gauges", [])],
            })
        systems.append({"id": s.get("id", ""), "name": s.get("name", ""), "equipment": items})
    return jsonify({"systems": systems})


@bp.get("/")
def index():
    items = []
    boilers = []
    pumps = []
    for sys in EQUIPMENT["systems"]:
        for grp in ("boilers", "pumps"):
            for dev in sys[grp]:
                items.append({"system": sys["name"], "id": dev["id"],
                              "name": dev["name"],
                              "kind": "Burner" if grp == "boilers" else "Pump"})
                if grp == "boilers":
                    boilers.append({"system": sys["name"], "id": dev["id"],
                                    "name": dev["name"]})
                else:
                    pumps.append({"system": sys["name"], "id": dev["id"],
                                  "name": dev["name"],
                                  "boilers": dev.get("boilers", [])})
    return render_template("index.html", items=items, boilers=boilers,
                           pumps=pumps, patrol_slots=PATROL_SLOTS)


@bp.get("/api/slots")
def api_slots():
    return jsonify({"slots": PATROL_SLOTS})


def _equip_payload(sys, dev, kind):
    gauges = []
    for g in dev.get("gauges", []):
        cal = CALIB.get(g["id"], {})
        gauges.append({
            "id": g["id"], "name": g["name"],
            "item": g.get("item", ""),
            "unit": g.get("unit", "") or cal.get("unit", ""),
            "calibrated": "angle_min" in cal,
        })
    return {"system": sys["name"], "category": sys["name"], "id": dev["id"], "name": dev["name"],
            "kind": kind, "gauges": gauges}


@bp.get("/inspect")
def inspect_page():
    return render_template("inspect.html")


@bp.get("/upload")
def upload_page():
    """Standalone photo upload page — no need to go through Inspection."""
    return render_template("upload.html")


@bp.get("/api/inspect")
def api_inspect():
    ids = [b.strip() for b in request.args.get("boilers", "").split(",")
           if b.strip()]
    pump_id_set = {p["id"] for s in EQUIPMENT["systems"] for p in s["pumps"]}
    items, seen = [], set()
    for bid in ids:  # selected boilers first
        sys, dev = find_equipment(bid)
        if dev and bid not in seen and bid not in pump_id_set:
            items.append(_equip_payload(sys, dev, "Burner"))
            seen.add(bid)
    if "pumps" in request.args:
        # explicit pump selection from the home screen
        pump_ids = [p.strip() for p in request.args.get("pumps", "").split(",")
                    if p.strip()]
        for pid in pump_ids:
            if pid in pump_id_set and pid not in seen:
                sys, dev = find_equipment(pid)
                items.append(_equip_payload(sys, dev, "Pump"))
                seen.add(pid)
    else:
        # auto: pumps connected to the selected boilers (deduplicated)
        for bid in ids:
            for sys in EQUIPMENT["systems"]:
                for p in sys["pumps"]:
                    if bid in p.get("boilers", []) and p["id"] not in seen:
                        items.append(_equip_payload(sys, p, "Pump"))
                        seen.add(p["id"])
    return jsonify({"boilers": ids, "items": items})


@bp.get("/e/<equip_id>")
def equipment_page(equip_id):
    sys, dev = find_equipment(equip_id)
    if dev is None:
        return f"Equipment {equip_id} not found", 404
    return render_template("equip.html", sys=sys, dev=dev, equip_id=equip_id)


@bp.get("/api/equipment/<equip_id>")
def api_equipment(equip_id):
    sys, dev = find_equipment(equip_id)
    if dev is None:
        return jsonify({"error": "not found"}), 404
    gauges = []
    for g in dev["gauges"]:
        cal = CALIB.get(g["id"], {})
        gauges.append({
            "id": g["id"], "name": g["name"],
            "item": g.get("item", ""), "unit": g.get("unit", "") or cal.get("unit", ""),
            "check_running": g.get("check_running", ""),
            "check_stopped": g.get("check_stopped", ""),
            "calibrated": "angle_min" in cal,
        })
    return jsonify({"system": sys["name"], "id": dev["id"], "name": dev["name"],
                    "gauges": gauges})


@bp.post("/api/read")
def api_read():
    gauge_id = request.form.get("gauge_id", "")
    photo = request.files.get("photo")
    if not photo:
        return jsonify({"error": "No photo uploaded"}), 400
    data = photo.read()
    # 원본 저장 (이력용)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{ts}_{gauge_id}.jpg"
    (UPLOAD_DIR / fname).write_bytes(data)
    try:
        result = read_gauge_bytes(data, gauge_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    result["photo"] = fname
    return jsonify(result)


@bp.post("/api/log")
def api_log():
    body = request.get_json(force=True)
    log_reading(
        equip_id=body.get("equip_id", ""),
        gauge_id=body.get("gauge_id", ""),
        value=body.get("value", ""),
        unit=body.get("unit", ""),
        photo=body.get("photo", ""),
        state=body.get("state", ""),
        worker=body.get("worker", ""),
        manual=bool(body.get("manual")),
        slot=body.get("slot", ""),
    )
    return jsonify({"ok": True})


@bp.get("/api/export")
def api_export():
    if not LOG_CSV.exists():
        return "No records yet", 404
    return send_file(LOG_CSV, as_attachment=True,
                     download_name="gauge_readings.csv")


# ---------------- Photo tags ----------------
# Tags pinned on photos: {photo_key: [{id, x, y, name}]}. x/y are fractions
# of the original image (0..1), so pins stay put at any zoom level.
# photo_key is "drive:<drive-file-id>" or "local:<filename>".
# Kept in its own file: admin_save / the Sheets startup restore rewrite
# data_gauge.json with only {equipment, calibration}, which would drop
# any extra keys stored there.
PHOTO_TAGS_FILE = BASE / "data_gauge_photo_tags.json"
try:
    PHOTO_TAGS = json.loads(PHOTO_TAGS_FILE.read_text(encoding="utf-8"))
    if not isinstance(PHOTO_TAGS, dict):
        PHOTO_TAGS = {}
except Exception:
    PHOTO_TAGS = {}


def _save_photo_tags():
    _atomic_write(PHOTO_TAGS_FILE, json.dumps(PHOTO_TAGS, indent=2, ensure_ascii=False))


# Tag editing on/off switch (admin-controlled). Kept in its own file for the
# same reason as PHOTO_TAGS: admin_save rewrites data_gauge.json with only
# {equipment, calibration}, which would drop extra keys stored there.
TAG_SETTINGS_FILE = BASE / "data_gauge_settings.json"
try:
    TAG_SETTINGS = json.loads(TAG_SETTINGS_FILE.read_text(encoding="utf-8"))
    if not isinstance(TAG_SETTINGS, dict):
        TAG_SETTINGS = {}
except Exception:
    TAG_SETTINGS = {}


def tag_editing_enabled():
    """True when photo tag editing is allowed (default ON = current behavior)."""
    return TAG_SETTINGS.get("tag_editing", True)


def _save_tag_settings():
    _atomic_write(TAG_SETTINGS_FILE, json.dumps(TAG_SETTINGS, indent=2, ensure_ascii=False))


def _valid_photo_key(key):
    if not isinstance(key, str):
        return False
    if key.startswith("drive:"):
        fid = key[6:]
        return bool(fid) and all(c.isalnum() or c in "-_" for c in fid)
    if key.startswith("local:"):
        fn = key[6:]
        return bool(fn) and "/" not in fn and "\\" not in fn and fn == Path(fn).name \
            and fn.rsplit(".", 1)[-1].lower() in ("jpg", "jpeg", "png", "webp")
    return False


def _photo_img_url(key):
    if key.startswith("drive:"):
        return f"/Hanseong/gauge/api/drive-photo/{key[6:]}"
    return f"/Hanseong/gauge/api/tag-photo/{key[6:]}"


def drive_service_sa():
    """Read-only Drive client via the service account.

    Used as a fallback for READS when the user's OAuth refresh token is
    revoked/expired (invalid_grant): listing and serving photos still work,
    while uploads keep using the primary (user) credentials untouched.
    Returns None when no service-account JSON is configured.
    """
    try:
        from googleapiclient.discovery import build
        if DRIVE_SA_JSON:
            from google.oauth2 import service_account
            info = json.loads(DRIVE_SA_JSON)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
            return build("drive", "v3", credentials=creds)
    except Exception:
        pass
    return None


def _list_drive_files(svc):
    """Recursive Drive photo listing for one service client (raises on error).

    A failed SUBFOLDER is skipped (best-effort); a failed top folder raises
    so the caller can fall back to another credential.
    """
    files = []

    def list_children(parent_id, prefix="", top=False):
        page_token = None
        while True:
            try:
                res = svc.files().list(
                    q=f"'{parent_id}' in parents and trashed=false",
                    fields="files(id,name,createdTime,mimeType),nextPageToken",
                    orderBy="createdTime", pageSize=100,
                    pageToken=page_token).execute()
            except Exception:
                if top:
                    raise
                return  # best-effort: one unreadable subfolder must not kill the list
            for f in res.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    list_children(f["id"], prefix + f["name"] + "/")
                else:
                    files.append({
                        "id": f["id"], "name": f["name"],
                        "createdTime": f.get("createdTime"),
                        "path": prefix + f["name"],
                    })
            page_token = res.get("nextPageToken")
            if not page_token:
                break

    list_children(DRIVE_FOLDER_ID, top=True)
    files.sort(key=lambda f: f.get("createdTime") or "")
    return files


def _serve_drive_photo(svc, fid):
    """Download one Drive photo via one service client (raises on error)."""
    from googleapiclient.http import MediaIoBaseDownload
    req = svc.files().get_media(fileId=fid)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return send_file(buf, mimetype="image/jpeg")


def _list_drive_photos():
    """Shared Drive photo listing. Returns (ok, files_or_error).

    Falls back to the service account when the user OAuth token is revoked.
    """
    svc = drive_service()
    if not svc or not DRIVE_FOLDER_ID:
        return False, "Drive not configured"
    try:
        return True, _list_drive_files(svc)
    except Exception as e:
        if "invalid_grant" in str(e):
            sa = drive_service_sa()
            if sa:
                try:
                    return True, _list_drive_files(sa)
                except Exception as e2:
                    return False, str(e2)[:200]
        return False, str(e)[:200]


@bp.get("/photos")
def photos_page():
    """Photo list for tagging: local uploads + Drive photos (Drive loads via JS)."""
    local = []
    try:
        for p in sorted(UPLOAD_DIR.iterdir(), key=lambda p: p.name):
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                local.append(p.name)
    except Exception:
        local = []
    return render_template("photos.html", local_photos=local, tag_editing=tag_editing_enabled())


@bp.post("/api/photo-upload")
def api_photo_upload():
    """Upload a photo for tagging (stored locally, no Drive needed)."""
    f = request.files.get("photo")
    if not f:
        return jsonify({"ok": False, "error": "no photo"}), 400
    ext = (f.filename or "").rsplit(".", 1)[-1].lower() if "." in (f.filename or "") else "jpg"
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = None
    for n in range(1000):
        cand = f"tagged_{ts}_{n}.{ext}"
        if not (UPLOAD_DIR / cand).exists():
            fname = cand
            break
    if fname is None:
        return jsonify({"ok": False, "error": "too many uploads"}), 500
    (UPLOAD_DIR / fname).write_bytes(f.read())
    return jsonify({"ok": True, "key": f"local:{fname}", "name": fname})


@bp.get("/api/tag-photo/<fname>")
def api_tag_photo(fname):
    if "/" in fname or "\\" in fname or fname != Path(fname).name:
        return "bad request", 400
    p = UPLOAD_DIR / fname
    if not p.exists() or p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        return "not found", 404
    mt = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
          "png": "image/png", "webp": "image/webp"}[p.suffix.lower()[1:]]
    return send_file(p, mimetype=mt)


@bp.get("/photo/<path:key>")
def photo_view_page(key):
    if not _valid_photo_key(key):
        return "bad photo", 400
    return render_template("photo_tags.html", mode="view", photo_key=key,
                           img_url=_photo_img_url(key),
                           tags=PHOTO_TAGS.get(key, []), label=key[6:],
                           tag_editing=tag_editing_enabled())


@bp.get("/photo/<path:key>/edit")
def photo_edit_page(key):
    if not _valid_photo_key(key):
        return "bad photo", 400
    if not tag_editing_enabled():
        return redirect(url_for(".photo_view_page", key=key))
    return render_template("photo_tags.html", mode="edit", photo_key=key,
                           img_url=_photo_img_url(key),
                           tags=PHOTO_TAGS.get(key, []), label=key[6:],
                           tag_editing=True)


@bp.get("/api/photo-tags/<path:key>")
def api_photo_tags_get(key):
    if not _valid_photo_key(key):
        return jsonify({"error": "bad photo"}), 400
    return jsonify({"ok": True, "tags": PHOTO_TAGS.get(key, [])})


@bp.post("/api/photo-tags/<path:key>")
def api_photo_tags_save(key):
    if not _valid_photo_key(key):
        return jsonify({"error": "bad photo"}), 400
    if not tag_editing_enabled():
        return jsonify({"error": "tag editing is off"}), 403
    body = request.get_json(force=True, silent=True) or {}
    raw = body.get("tags")
    if not isinstance(raw, list) or len(raw) > 200:
        return jsonify({"error": "tags must be a list of at most 200"}), 400
    clean = []
    for t in raw:
        if not isinstance(t, dict):
            return jsonify({"error": "bad tag"}), 400
        try:
            x, y = float(t["x"]), float(t["y"])
        except Exception:
            return jsonify({"error": "bad coordinates"}), 400
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return jsonify({"error": "coordinates out of range"}), 400
        name = str(t.get("name", "")).strip()
        if not name or len(name) > 100:
            return jsonify({"error": "tag name required (max 100 chars)"}), 400
        tid = str(t.get("id", ""))[:64]
        clean.append({"id": tid or uuid.uuid4().hex[:12],
                      "x": round(x, 4), "y": round(y, 4), "name": name})
    PHOTO_TAGS[key] = clean
    try:
        _save_photo_tags()
    except Exception as e:
        return jsonify({"error": f"save failed: {e}"}), 500
    return jsonify({"ok": True, "count": len(clean)})


# ---------------- Admin (settings) ----------------

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("gauge_admin"):
            return jsonify({"error": "login required"}), 401
        return fn(*args, **kwargs)
    return wrapper


@bp.get("/admin")
def admin_page():
    return render_template("admin.html")


@bp.post("/api/save-calibration")
def save_single_calibration():
    """Save calibration for a single gauge (no admin required - used by quick setup)"""
    global CALIB
    body = request.get_json(force=True, silent=True) or {}
    gid = body.get("gauge_id")
    if not gid:
        return jsonify({"error": "gauge_id required"}), 400
    CALIB[gid] = {
        "angle_min": body.get("angle_min", 225),
        "value_min": body.get("value_min", 0),
        "angle_max": body.get("angle_max", 135),
        "value_max": body.get("value_max", 100),
        "unit": body.get("unit", ""),
    }
    # Persist to disk
    data_path = BASE / "data_gauge.json"
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
        data["calibration"] = CALIB
        _atomic_write(data_path, json.dumps(data, indent=2, ensure_ascii=False))
    except Exception:
        pass
    # Persist to Sheets (best-effort)
    sheets_post_async({"action": "saveConfig",
                       "config": {"equipment": EQUIPMENT, "calibration": CALIB}})
    return jsonify({"ok": True})


@bp.post("/api/admin/login")
def admin_login():
    body = request.get_json(force=True, silent=True) or {}
    if body.get("password") == ADMIN_PASSWORD:
        session["gauge_admin"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Wrong password"}), 403


@bp.post("/api/admin/logout")
def admin_logout():
    session.pop("gauge_admin", None)
    return jsonify({"ok": True})


@bp.get("/api/tag-setting")
@admin_required
def api_tag_setting_get():
    return jsonify({"ok": True, "tag_editing": tag_editing_enabled()})


@bp.post("/api/tag-setting")
@admin_required
def api_tag_setting_save():
    body = request.get_json(force=True, silent=True) or {}
    val = body.get("tag_editing")
    if not isinstance(val, bool):
        return jsonify({"error": "tag_editing must be true or false"}), 400
    TAG_SETTINGS["tag_editing"] = val
    try:
        _save_tag_settings()
    except Exception as e:
        return jsonify({"error": f"save failed: {e}"}), 500
    return jsonify({"ok": True, "tag_editing": val})


@bp.get("/api/admin/config")
@admin_required
def admin_config():
    return jsonify({"equipment": EQUIPMENT, "calibration": CALIB})


def _validate_config(eq, cal):
    if not isinstance(eq, dict) or not isinstance(eq.get("systems"), list):
        return "bad equipment structure"
    if not isinstance(cal, dict):
        return "bad calibration structure"
    seen = set()
    for sys in eq["systems"]:
        for grp in ("boilers", "pumps"):
            for dev in sys.get(grp, []):
                if not dev.get("id") or not dev.get("name"):
                    return "equipment needs id and name"
                for g in dev.get("gauges", []):
                    if not g.get("id"):
                        return "gauge needs id"
                    if g["id"] in seen:
                        return f'duplicate gauge id: {g["id"]}'
                    seen.add(g["id"])
    return None


def _atomic_write(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@bp.post("/api/admin/save")
@admin_required
def admin_save():
    global EQUIPMENT, CALIB
    body = request.get_json(force=True, silent=True) or {}
    eq, cal = body.get("equipment"), body.get("calibration")
    err = _validate_config(eq, cal)
    if err:
        return jsonify({"error": err}), 400
    data_path = BASE / "data_gauge.json"
    if data_path.exists():
        _atomic_write(data_path.with_suffix(".json.bak"),
                      data_path.read_text(encoding="utf-8"))
    _atomic_write(data_path, json.dumps({"equipment": eq, "calibration": cal},
                                        indent=2, ensure_ascii=False))
    EQUIPMENT, CALIB = eq, cal
    # persist settings to Google Sheets (best-effort, background)
    sheets_post_async({"action": "saveConfig",
                       "config": {"equipment": eq, "calibration": cal}})
    return jsonify({"ok": True})


@bp.get("/api/admin/backup")
@admin_required
def admin_backup():
    blob = json.dumps({"equipment": EQUIPMENT, "calibration": CALIB},
                      indent=2, ensure_ascii=False).encode("utf-8")
    return send_file(io.BytesIO(blob), as_attachment=True,
                     download_name="gauge_config_backup.json",
                     mimetype="application/json")


# Restore settings from Google Sheets on startup (survives server wipes).
# Local disk files remain the fallback when Sheets is unreachable/empty.
if SHEETS_URL:
    _remote = sheets_get_config()
    if isinstance(_remote, dict) and isinstance(_remote.get("equipment"), dict):
        EQUIPMENT = _remote["equipment"]
        CALIB = _remote.get("calibration", {}) or {}
        try:
            _atomic_write(BASE / "data_gauge.json",
                          json.dumps({"equipment": EQUIPMENT,
                                      "calibration": CALIB},
                                     indent=2, ensure_ascii=False))
            print("config restored from Google Sheets", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"config cache write failed: {e}", flush=True)
