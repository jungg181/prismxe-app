"""Prism Check — tablet checklist submissions inbox.

Receives patrol checklist entries from the Prism Check Android app
(com.prismxe.check), stores them durably in data_checklist.json.
The Excel writer (Drive workbook date-sheet filler) consumes the
pending queue; entries stay here until marked written.

Endpoints:
  POST /checklist/api/submit   {entries:[{id, system, equipment, date, slot,
                                values:{"<sheet_row>": "value", ...},
                                saved_at, device}]}
                               -> {ok, accepted:[ids], duplicates:[ids]}
  GET  /checklist/api/pending  submissions not yet written to Excel
  GET  /checklist/api/defs     checklist definitions JSON (form metadata)
  GET  /checklist/data/backup  full data backup (JSON download)
"""
import os
import json
import threading
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

bp = Blueprint("checklist", __name__, url_prefix="/checklist")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "data_checklist.json")
DEFS_PATH = os.path.join(BASE_DIR, "checklist_defs.json")
_lock = threading.Lock()


def _load():
    try:
        with open(DATA_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict) and "submissions" in d:
            return d
    except Exception:
        pass
    return {"submissions": []}


def _save(d):
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_PATH)


@bp.post("/api/submit")
def submit():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid json"}), 400
    entries = (payload or {}).get("entries")
    if not isinstance(entries, list) or not entries:
        return jsonify({"ok": False, "error": "entries required"}), 400
    now = datetime.now(timezone.utc).isoformat()
    accepted, duplicates = [], []
    with _lock:
        d = _load()
        seen = {s.get("id") for s in d["submissions"]}
        for e in entries:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            if e["id"] in seen:
                duplicates.append(e["id"])
                continue
            rec = {
                "id": e["id"],
                "system": e.get("system", ""),
                "equipment": e.get("equipment", ""),
                "date": e.get("date", ""),
                "slot": e.get("slot", ""),
                "values": e.get("values", {}),
                "target_file": e.get("target_file", ""),
                "saved_at": e.get("saved_at", ""),
                "device": e.get("device", ""),
                "received_at": now,
                "written": False,
            }
            pending_photos = d.get("photo_index", {}).pop(rec["id"], [])
            base_key = "|".join(rec["id"].split("|")[:4])
            if base_key != rec["id"]:
                pending_photos += d.get("photo_index", {}).pop(base_key, [])
            if pending_photos:
                rec["photos"] = pending_photos
            d["submissions"].append(rec)
            seen.add(e["id"])
            accepted.append(e["id"])
        if accepted:
            _save(d)
    return jsonify({"ok": True, "accepted": accepted, "duplicates": duplicates})


@bp.post("/api/photo")
def photo():
    """Store one monitor photo for an entry (multipart: entry_id + file)."""
    entry_id = request.form.get("entry_id", "")
    f = request.files.get("file")
    if not entry_id or f is None or not f.filename:
        return jsonify({"ok": False, "error": "entry_id and file required"}), 400
    import re as _re
    safe_id = _re.sub(r"[^A-Za-z0-9_.-]+", "_", entry_id)
    safe_name = _re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.basename(f.filename))
    stored = safe_id + "__" + safe_name
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    f.save(os.path.join(PHOTOS_DIR, stored))
    with _lock:
        d = _load()
        for srec in d["submissions"]:
            if srec.get("id") == entry_id:
                srec.setdefault("photos", [])
                if stored not in srec["photos"]:
                    srec["photos"].append(stored)
                break
        else:
            d.setdefault("photo_index", {}).setdefault(entry_id, [])
            if stored not in d["photo_index"][entry_id]:
                d["photo_index"][entry_id].append(stored)
        _save(d)
    return jsonify({"ok": True, "name": stored})


@bp.get("/api/photo/<path:name>")
def photo_get(name):
    from flask import send_from_directory
    return send_from_directory(PHOTOS_DIR, os.path.basename(name))


@bp.get("/api/day")
def day():
    """Merged values for one system/equipment/date, for the app viewer.
    Later submissions override earlier ones per (slot, row)."""
    system = request.args.get("system", "")
    equipment = request.args.get("equipment", "")
    date = request.args.get("date", "")
    if not system or not date:
        return jsonify({"ok": False, "error": "system and date required"}), 400
    with _lock:
        d = _load()
    rounds = {}
    day_photos = {}
    latest = ""
    for srec in sorted(d["submissions"], key=lambda x: x.get("received_at", "")):
        if srec.get("system") != system or srec.get("date") != date:
            continue
        if equipment and srec.get("equipment", "") != equipment:
            continue
        slot = srec.get("slot", "")
        if not slot:
            continue
        rounds.setdefault(slot, {}).update(srec.get("values", {}))
        latest = max(latest, srec.get("received_at", ""))
        for ph in srec.get("photos", []):
            day_photos.setdefault(slot, [])
            if ph not in day_photos[slot]:
                day_photos[slot].append(ph)
    return jsonify({"ok": True, "system": system, "equipment": equipment,
                    "date": date, "rounds": rounds, "photos": day_photos, "updated": latest})


@bp.get("/api/pending")
def pending():
    with _lock:
        d = _load()
    return jsonify({"ok": True, "pending": [s for s in d["submissions"] if not s.get("written")]})


@bp.get("/api/defs")
def defs():
    try:
        with open(DEFS_PATH) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"ok": False, "error": "defs not installed"}), 404


@bp.get("/data/backup")
def backup():
    with _lock:
        d = _load()
    return jsonify(d)
