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
import re
import json
import base64
import threading
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

bp = Blueprint("checklist", __name__, url_prefix="/checklist")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "data_checklist.json")
DEFS_PATH = os.path.join(BASE_DIR, "checklist_defs.json")
PHOTOS_DIR = os.path.join(BASE_DIR, "checklist_photos")
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
            def _norm(k):
                return "|".join(p for p in k.split("|") if p)
            pending_photos = d.get("photo_index", {}).pop(rec["id"], [])
            base_key = "|".join(rec["id"].split("|")[:4])
            if base_key != rec["id"]:
                pending_photos += d.get("photo_index", {}).pop(base_key, [])
            for alt in (_norm(rec["id"]), _norm(base_key)):
                if alt and alt not in (rec["id"], base_key):
                    pending_photos += d.get("photo_index", {}).pop(alt, [])
            if pending_photos:
                rec["photos"] = pending_photos
            d["submissions"].append(rec)
            seen.add(e["id"])
            accepted.append(e["id"])
        if accepted:
            _save(d)
    if accepted and WRITE_SCRIPT_URL:
        threading.Thread(target=_flush_written, daemon=True).start()
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
    threading.Thread(target=_forward_photo_to_drive,
                     args=(stored, os.path.join(PHOTOS_DIR, stored), entry_id),
                     daemon=True).start()
    return jsonify({"ok": True, "name": stored})


PHOTO_SCRIPT_URL = os.environ.get("CHECKLIST_PHOTO_SCRIPT_URL", "")


def _forward_photo_to_drive(stored, path, entry_id):
    """Copy a stored photo to Google Drive via the Apps Script sink so it
    survives Render disk wipes. No-op until CHECKLIST_PHOTO_SCRIPT_URL is set."""
    if not PHOTO_SCRIPT_URL:
        return
    try:
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        payload = {"action": "photo", "name": stored, "entry_id": entry_id, "data_b64": b64}
        req = urllib.request.Request(PHOTO_SCRIPT_URL, data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        if r.get("ok") and r.get("url"):
            with _lock:
                d = _load()
                d.setdefault("photo_drive", {})[stored] = r["url"]
                _save(d)
    except Exception as e:
        print("checklist photo drive forward failed: {}".format(e), flush=True)


WRITE_SCRIPT_URL = os.environ.get("CHECKLIST_WRITE_SCRIPT_URL", "")


def _flush_written():
    """Send unwritten submissions to the Apps Script Excel writer; entries it
    acknowledges are marked written. Failures leave flags untouched, so
    nothing is lost — the values stay queued on the server."""
    if not WRITE_SCRIPT_URL:
        return {"ok": False, "error": "writer not configured"}
    with _lock:
        d = _load()
        pending = [s for s in d["submissions"] if not s.get("written")]
    if not pending:
        return {"ok": True, "sent": 0, "marked": 0}
    entries = [{
        "id": s["id"], "system": s.get("system", ""), "equipment": s.get("equipment", ""),
        "date": s.get("date", ""), "slot": s.get("slot", ""),
        "target_file": s.get("target_file", ""), "values": s.get("values", {}),
    } for s in pending[:5]]
    payload = {"action": "write", "entries": entries}
    req = urllib.request.Request(WRITE_SCRIPT_URL, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            r = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": "writer request failed: {}".format(e)}
    ok_ids = {x["id"] for x in r.get("results", []) if x.get("ok")}
    if ok_ids:
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            d = _load()
            for s in d["submissions"]:
                if s["id"] in ok_ids and not s.get("written"):
                    s["written"] = True
                    s["written_at"] = now
            _save(d)
    return {"ok": True, "sent": len(entries), "marked": len(ok_ids),
            "results": r.get("results", [])}


@bp.post("/api/flush")
def flush():
    return jsonify(_flush_written())


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


# ---------------- monitor photo auto-read (vision) ----------------
VISION_MODEL = os.environ.get("CHECKLIST_VISION_MODEL", "claude-sonnet-5-5")

_SCREEN_HINTS = {
    "RO": ("The photo shows the SRP RO (reverse osmosis) HMI screen. On this screen some labels differ from the "
           "checklist tags: PI-01 is checklist PT-01, PI-02 is PT-02, FI-01 is FT-01, FI-02 is FIT-02, "
           "FI-03 is FIT-03, FI-04 is FIT-04 (Supply Flow). LT-01, AIT-01, AIT-02 and LT-02 use the same labels."),
    "NMP": ("The photo shows one page of the SRP NMP Recovery HMI (the Control page or the Transfer page). "
            "Screen tags are written WITHOUT dashes: TE005A means checklist tag TE-005A, LT001A means LT-001A, "
            "PUM006A means PUM-006A, and so on. Only one page is visible, so return only the tags you can actually see. "
            "For checklist item #51 the screen shows PUM-012 and PUM-013 separately: return the running (non-zero) "
            "one, or 0 if both are 0. For FT-002 return the instantaneous flow value, NOT the accumulated totalizer."),
    "HOB": ("The photo shows a hot oil heater burner HMI screen. The screen title names the heater: "
            "'#1 ANODE' = EAB01, '#2 ANODE' = EAB02, '#1 CATHODE' = ECB01, '#2 CATHODE' = ECB02, '#3 CATHODE' = ECB03. "
            "Also return \"screen_equipment\" with that equipment id. On this screen: FI-001 is the gas flow, "
            "the TIC-009 MV percent is checklist FCV-001 (linkage open rate), and the O2 percent value is O2I-001. "
            "FI-002 is shown in gal/h on this screen; SKIP FI-002 (do not return it). "
            "Return only items you can actually read."),
}


def _monitor_items(system_id):
    try:
        with open(DEFS_PATH) as f:
            defs = json.load(f)
    except Exception:
        return None
    for s in defs.get("systems", []):
        if s.get("id") == system_id:
            return [it for sec in s.get("sections", []) for it in sec.get("items", []) if it.get("monitor")]
    return None


@bp.post("/api/read-photo")
def read_photo():
    """Read monitor values from an HMI photo with the vision API.

    Multipart form: photo (jpeg), system (HOB|NMP|RO), equipment (optional).
    -> {ok, values: {"<sheet_row>": number, ...}, screen_equipment}
    The app fills the matching fields; the worker reviews before saving.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "vision not configured"}), 503
    system_id = (request.form.get("system") or "").strip().upper()
    f = request.files.get("photo")
    if f is None or not system_id:
        return jsonify({"ok": False, "error": "photo and system required"}), 400
    items = _monitor_items(system_id)
    if not items:
        return jsonify({"ok": False, "error": "unknown system"}), 400
    img = f.read()
    if not img or len(img) > 6_000_000:
        return jsonify({"ok": False, "error": "photo missing or too large"}), 400
    lines = "\n".join(
        '  row {}: #{} {} (tag {}, unit {})'.format(it["row"], it["no"], it["name"],
                                                   it.get("tag", ""), it.get("unit", ""))
        for it in items)
    prompt = (
        "You are reading a photo of an industrial monitor/HMI screen for a patrol checklist. "
        + _SCREEN_HINTS.get(system_id, "")
        + "\nRead these checklist items from the screen:\n" + lines
        + "\nReply with ONLY a JSON object: {\"values\": {\"<row>\": <number>, ...}} using the row numbers "
          "above as keys. Include only values you can actually read on the screen - never guess or invent "
          "a value. Use plain numbers (no units, no thousands separators)."
    )
    payload = {
        "model": VISION_MODEL,
        "max_tokens": 1500,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(img).decode()}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        return jsonify({"ok": False, "error": "vision request failed: {} | {}".format(
            e, detail)}), 502
    text = "".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return jsonify({"ok": False, "error": "could not parse vision reply"}), 502
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return jsonify({"ok": False, "error": "could not parse vision reply"}), 502
    allowed = {str(it["row"]) for it in items}
    values = {}
    for k, v in (parsed.get("values") or {}).items():
        if str(k) not in allowed:
            continue
        try:
            fv = float(str(v).replace(",", ""))
        except Exception:
            continue
        values[str(k)] = int(fv) if fv == int(fv) else fv
    return jsonify({"ok": True, "values": values,
                    "screen_equipment": parsed.get("screen_equipment")})
