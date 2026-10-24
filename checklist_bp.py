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
                "saved_at": e.get("saved_at", ""),
                "device": e.get("device", ""),
                "received_at": now,
                "written": False,
            }
            d["submissions"].append(rec)
            seen.add(e["id"])
            accepted.append(e["id"])
        if accepted:
            _save(d)
    return jsonify({"ok": True, "accepted": accepted, "duplicates": duplicates})


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
