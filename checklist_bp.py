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
import hashlib
import threading
import urllib.request
from datetime import datetime, timezone

from flask import (Blueprint, request, jsonify, send_file, render_template,
                    redirect, url_for, flash)

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
    new_recs = []
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
                "note": e.get("note", ""),
                "target_file": e.get("target_file", ""),
                "saved_at": e.get("saved_at", ""),
                "device": e.get("device", ""),
                "received_at": now,
                "written": False,
                # the app sends "mnt" with an MNT mark; keep it so the
                # web page and the app agree the round is maintenance
                "mnt": bool(e.get("mnt", False)),
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
            if e.get("edited_by"):
                # an edit of an already-submitted record: file who
                # edited it, when, and exactly what changed (row ->
                # [old, new]) in the audit log (형아, 2026-10-26)
                rec["edited_by"] = e.get("edited_by", "")
                d.setdefault("audit_log", []).append({
                    "ts": now, "id": e["id"],
                    "system": rec["system"], "equipment": rec["equipment"],
                    "date": rec["date"], "slot": rec["slot"],
                    "edited_by": e.get("edited_by", ""),
                    "device": rec["device"],
                    "changes": e.get("changes", {}),
                })
            d["submissions"].append(rec)
            seen.add(e["id"])
            accepted.append(e["id"])
            new_recs.append((rec, e.get("changes")))
        if accepted:
            _save(d)
    for rec, changes in new_recs:
        _issue_pdf(rec, changes)
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


# ---------------- PDF issuance + integrity (형아, 2026-10-26) --------
# The moment a submission is accepted, the server renders a PDF of
# exactly what was submitted, stores it, and records the PDF's SHA-256
# with the submission record — later, re-hashing the stored PDF and
# comparing proves the document was never altered. PDFs are forwarded
# to Google Drive (the checklist folder Ji designated for photos) so
# they survive Render disk wipes; POST /api/pdf-sweep retries both
# generation and forwarding for anything that missed.

PDFS_DIR = os.path.join(BASE_DIR, "checklist_pdfs")
PDF_FOLDER_ID = os.environ.get("CHECKLIST_PDF_FOLDER_ID",
                               "1PMlJ48gZKd0FzrWwgj0hne3tD73zOZrj")
_FONT_DIR = BASE_DIR  # NanumGothic TTFs live at the repo root
_defs_cache = None


def _defs():
    global _defs_cache
    if _defs_cache is None:
        try:
            with open(DEFS_PATH, encoding="utf-8") as f:
                _defs_cache = json.load(f)
        except Exception:
            _defs_cache = {"systems": []}
    return _defs_cache


def _pdf_filename(rec):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", rec.get("id", "entry"))
    return ("PDF__" + safe)[:140] + ".pdf"


def _make_pdf(rec, changes=None):
    """Render one submission as a PDF (bytes) with fpdf2 and the
    bundled NanumGothic subset (Hangul + Latin)."""
    from fpdf import FPDF

    class Doc(FPDF):
        def footer(self):
            self.set_y(-14)
            self.set_font("nanum", "", 8)
            self.set_text_color(130, 130, 130)
            self.cell(0, 8, "Entry {} · page {}".format(
                rec.get("id", ""), self.page_no()), align="C")

    systems = {s.get("id"): s for s in _defs().get("systems", [])}
    system = systems.get(rec.get("system", ""), {})
    sys_name = system.get("name") or rec.get("system", "")
    values = rec.get("values", {}) or {}

    pdf = Doc(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_font("nanum", "", os.path.join(_FONT_DIR, "NanumGothic.ttf"))
    _bold = os.path.join(_FONT_DIR, "NanumGothicBold.ttf")
    if not os.path.exists(_bold):
        _bold = os.path.join(_FONT_DIR, "NanumGothic.ttf")
    pdf.add_font("nanum", "B", _bold)
    pdf.add_page()
    pdf.set_font("nanum", "B", 16)
    pdf.cell(0, 9, "Hanseong Arizona — Daily Check List",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("nanum", "B", 13)
    pdf.cell(0, 8, "{} · {}".format(sys_name, rec.get("slot", "")),
             new_x="LMARGIN", new_y="NEXT")
    meta = [
        ("Date", rec.get("date", "")),
        ("Equipment", rec.get("equipment", "") or "-"),
        ("Round", rec.get("slot", "")),
        ("Submitted", (rec.get("received_at", "") or "")[:19].replace("T", " ") + " UTC"),
        ("Device", rec.get("device", "")),
        ("Entry ID", rec.get("id", "")),
    ]
    for k, v in meta:
        pdf.set_font("nanum", "B", 10)
        pdf.cell(26, 6, k)
        pdf.set_font("nanum", "", 10)
        pdf.cell(0, 6, str(v), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    widths = (12, 86, 30, 22, 40)  # No | Item | Value | Unit | Range

    def fit(text, w):
        text = str(text)
        while text and pdf.get_string_width(text) > w - 2:
            text = text[:-1]
        return text

    pdf.set_font("nanum", "B", 9.5)
    pdf.set_fill_color(55, 71, 79)
    pdf.set_text_color(255, 255, 255)
    for w, t in zip(widths, ("No.", "Item", "Value", "Unit", "Range")):
        pdf.cell(w, 7, " " + t, border=1, fill=True)
    pdf.ln()
    pdf.set_text_color(0, 0, 0)
    for sec in system.get("sections", []):
        pdf.set_font("nanum", "B", 10)
        pdf.set_fill_color(233, 226, 248)
        pdf.cell(sum(widths), 7, " " + (sec.get("name", "") or ""),
                 border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        for it in sec.get("items", []):
            val = values.get(str(it.get("row")), "")
            name = it.get("name", "")
            if it.get("tag"):
                name = "{} ({})".format(name, it["tag"])
            pdf.set_font("nanum", "", 9.5)
            cells = (
                (widths[0], str(it.get("no", "")), "C"),
                (widths[1], " " + fit(name, widths[1]), "L"),
                (widths[2], fit(str(val) if val != "" else "—", widths[2]), "C"),
                (widths[3], fit(it.get("unit", ""), widths[3]), "C"),
                (widths[4], fit(it.get("range", ""), widths[4]), "C"),
            )
            for w, t, al in cells:
                pdf.cell(w, 6, t, border=1, align=al)
            pdf.ln()
    if rec.get("note"):
        pdf.ln(3)
        pdf.set_font("nanum", "B", 10.5)
        pdf.cell(0, 7, "Note", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("nanum", "", 10)
        pdf.multi_cell(0, 6, str(rec["note"]), border=1)
    if rec.get("edited_by"):
        pdf.ln(3)
        pdf.set_font("nanum", "B", 10.5)
        pdf.cell(0, 7, "Edit record", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("nanum", "", 10)
        pdf.cell(0, 6, "Edited by: {}".format(rec["edited_by"]),
                 new_x="LMARGIN", new_y="NEXT")
        for row, pair in (changes or {}).items():
            try:
                old, new = pair[0], pair[1]
            except Exception:
                old, new = "", pair
            pdf.cell(0, 6, "  Row {}: {} -> {}".format(row, old, new),
                     new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("nanum", "", 8.5)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(0, 5, "This PDF was generated by the server at submission "
                         "time. Its SHA-256 hash is stored with the submission "
                         "record; recomputing the hash of this file and "
                         "comparing it with the stored value proves the "
                         "document is unaltered.")
    return bytes(pdf.output())


def _issue_pdf(rec, changes=None):
    """Render + store + hash one submission's PDF, record the fields on
    the submission, and start its Drive forward. Never raises."""
    try:
        data = _make_pdf(rec, changes)
    except Exception as e:
        import traceback as _tb
        print("checklist pdf failed: {}".format(e), flush=True)
        with _lock:
            d = _load()
            d["pdf_last_error"] = _tb.format_exc()[-600:]
            for s in d["submissions"]:
                if s.get("id") == rec["id"]:
                    s["pdf_error"] = str(e)[:200]
                    break
            _save(d)
        return False
    os.makedirs(PDFS_DIR, exist_ok=True)
    fname = _pdf_filename(rec)
    with open(os.path.join(PDFS_DIR, fname), "wb") as fh:
        fh.write(data)
    digest = hashlib.sha256(data).hexdigest()
    with _lock:
        d = _load()
        for s in d["submissions"]:
            if s.get("id") == rec["id"]:
                s["pdf_file"] = fname
                s["pdf_sha256"] = digest
                break
        _save(d)
    threading.Thread(target=_forward_pdf_to_drive,
                     args=(rec["id"], fname, rec.get("date", "")),
                     daemon=True).start()
    return True


def _drive_api_upload_pdf(path, fname, date_str):
    """Upload one PDF via the Drive API using the gauge OAuth
    credentials (same Render service env). Returns the Drive URL or
    None when not configured / failed."""
    token = os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN", "")
    cid = os.environ.get("GOOGLE_DRIVE_CLIENT_ID", "")
    secret = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "")
    if not (token and cid and secret):
        return None
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    import io as _io
    creds = Credentials(None, refresh_token=token,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=cid, client_secret=secret,
                        scopes=["https://www.googleapis.com/auth/drive.file"])
    svc = build("drive", "v3", credentials=creds)
    day = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    q = ("'{}' in parents and name='{}' and "
         "mimeType='application/vnd.google-apps.folder' and trashed=false"
         ).format(PDF_FOLDER_ID, day)
    res = svc.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        folder = svc.files().create(
            body={"name": day, "mimeType": "application/vnd.google-apps.folder",
                  "parents": [PDF_FOLDER_ID]}, fields="id").execute()
        folder_id = folder["id"]
    with open(path, "rb") as fh:
        data = fh.read()
    media = MediaIoBaseUpload(_io.BytesIO(data), mimetype="application/pdf")
    created = svc.files().create(
        body={"name": fname, "parents": [folder_id]},
        media_body=media, fields="id,webViewLink").execute()
    return created.get("webViewLink")


def _forward_pdf_to_drive(entry_id, fname, date_str):
    """Copy an issued PDF to Google Drive so it survives Render disk
    wipes: Drive API (gauge OAuth) first, the Apps Script photo sink
    (action "pdf") second. Records the URL as pdf_drive."""
    path = os.path.join(PDFS_DIR, fname)
    if not os.path.exists(path):
        return
    url = None
    try:
        url = _drive_api_upload_pdf(path, fname, date_str)
    except Exception as e:
        print("checklist pdf drive api failed: {}".format(e), flush=True)
    if not url and PHOTO_SCRIPT_URL:
        try:
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            payload = {"action": "pdf", "name": fname,
                       "entry_id": entry_id, "data_b64": b64}
            req = urllib.request.Request(
                PHOTO_SCRIPT_URL, data=json.dumps(payload).encode(),
                headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                r = json.loads(resp.read().decode("utf-8"))
            if r.get("ok"):
                url = r.get("url")
        except Exception as e:
            print("checklist pdf script forward failed: {}".format(e), flush=True)
    if url:
        with _lock:
            d = _load()
            for s in d["submissions"]:
                if s.get("id") == entry_id:
                    s["pdf_drive"] = url
                    break
            _save(d)


def _sweep_pdfs():
    """Generate PDFs for submissions that lack one and retry Drive
    forwarding for those whose copy never landed. The record of an
    edit (changes) is recovered from the audit log."""
    with _lock:
        d = _load()
        audit_changes = {a.get("id"): a.get("changes")
                         for a in d.get("audit_log", [])}
        subs = list(d["submissions"])
    gen = 0
    for rec in subs:
        if not rec.get("pdf_sha256"):
            if _issue_pdf(rec, audit_changes.get(rec.get("id"))):
                gen += 1
    fwd = 0
    with _lock:
        d = _load()
        missing = [s for s in d["submissions"]
                   if s.get("pdf_sha256") and not s.get("pdf_drive")]
    for rec in missing:
        _forward_pdf_to_drive(rec["id"], rec.get("pdf_file")
                              or _pdf_filename(rec), rec.get("date", ""))
        with _lock:
            d = _load()
            cur = next((s for s in d["submissions"]
                        if s.get("id") == rec["id"]), {})
        if cur.get("pdf_drive"):
            fwd += 1
    return {"ok": True, "generated": gen, "forwarded": fwd}


@bp.get("/api/pdf/<name>")
def pdf_download(name):
    safe = os.path.basename(name)
    path = os.path.join(PDFS_DIR, safe)
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "no such pdf"}), 404
    return send_file(path, mimetype="application/pdf",
                     as_attachment=True, download_name=safe)


@bp.post("/api/pdf-sweep")
def pdf_sweep():
    return jsonify(_sweep_pdfs())


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


@bp.post("/api/loc-photo")
def loc_photo():
    """Register/replace the location photo for one checklist point
    (multipart: system, equipment, row, file). One photo per point,
    shared by every device, so a worker who has never seen the point
    can find it. A copy is forwarded to Drive like the monitor photos,
    so it survives Render disk wipes."""
    system = request.form.get("system", "")
    equipment = request.form.get("equipment", "")
    row = request.form.get("row", "")
    f = request.files.get("file")
    if not system or not row or f is None or not f.filename:
        return jsonify({"ok": False, "error": "system, row and file required"}), 400
    import re as _re
    key = "{}|{}|{}".format(system, equipment, row)
    safe = _re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
    stored = "LOC__" + safe + ".jpg"
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    path = os.path.join(PHOTOS_DIR, stored)
    f.save(path)
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        d = _load()
        d.setdefault("loc_photos", {})[key] = {"file": stored, "updated": now}
        _save(d)
    threading.Thread(target=_forward_photo_to_drive,
                     args=(stored, path, "LOC|" + key), daemon=True).start()
    return jsonify({"ok": True, "file": stored, "updated": now})


@bp.post("/api/loc-assign")
def loc_assign():
    """Register an ALREADY-UPLOADED photo as the location photo for one
    checklist point (JSON: system, equipment, row, file). No byte copy —
    the file must already exist in checklist_photos/ (uploaded via
    POST /api/photo). One photo per point, shared by every device."""
    data = request.get_json(silent=True) or {}
    system = data.get("system", "")
    equipment = data.get("equipment", "")
    row = data.get("row", "")
    f = data.get("file", "")
    if not system or not row or not f:
        return jsonify({"ok": False, "error": "system, row and file required"}), 400
    if "/" in f or "\\" in f or not os.path.isfile(
            os.path.join(PHOTOS_DIR, os.path.basename(f))):
        return jsonify({"ok": False, "error": "photo not found on server"}), 404
    stored = os.path.basename(f)
    key = "{}|{}|{}".format(system, equipment, row)
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        d = _load()
        d.setdefault("loc_photos", {})[key] = {"file": stored, "updated": now}
        _save(d)
    return jsonify({"ok": True, "file": stored, "updated": now})


@bp.post("/api/loc-remove")
def loc_remove():
    """Remove the location-photo mapping for one checklist point
    (JSON: system, equipment, row). Idempotent: removing a mapping
    that does not exist reports removed=false, not an error. The
    photo file itself is NOT deleted from checklist_photos/ (its
    tags stay keyed by the file name), so the photo can be
    re-assigned later via POST /api/loc-assign."""
    data = request.get_json(silent=True) or {}
    system = data.get("system", "")
    equipment = data.get("equipment", "")
    row = data.get("row", "")
    if not system or not row:
        return jsonify({"ok": False, "error": "system and row required"}), 400
    key = "{}|{}|{}".format(system, equipment, row)
    with _lock:
        d = _load()
        removed = key in (d.get("loc_photos") or {})
        if removed:
            del d["loc_photos"][key]
            _save(d)
    return jsonify({"ok": True, "removed": removed})


# ---------------- pin visibility (v1.87) ----------------
# Per-item "show the Pin" setting, set by the user on the Check List
# page inside Photo Pin Tags. Keyed "system|row" — NOT per equipment:
# HOB's item list is shared across all 5 equipments (same tag at the
# same row), so one HOB row has ONE visibility for every equipment.
# Stored apart from data_checklist.json in
# data_checklist_pin_visibility.json: {"<system>|<row>": true/false}.
# Only explicitly stored keys are in the file; anything absent falls
# back to the default rule (real tag -> shown, no tag -> hidden),
# which the app computes from the defs — the server never needs
# the defs for this.
PIN_VISIBILITY_PATH = os.path.join(
    BASE_DIR, "data_checklist_pin_visibility.json")


def _load_pin_visibility():
    try:
        with open(PIN_VISIBILITY_PATH) as f:
            v = json.load(f)
        if isinstance(v, dict):
            return {str(k): bool(x) for k, x in v.items()}
    except Exception:
        pass
    return {}


def _save_pin_visibility(v):
    tmp = PIN_VISIBILITY_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(v, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PIN_VISIBILITY_PATH)


@bp.get("/api/pin-visibility")
def pin_visibility_get():
    """Whole pin-visibility map: {visibility: {"<system>|<row>": bool}}.
    Empty {} when nothing has been saved yet; the app falls back to
    the default rule (real tag -> shown) for absent keys."""
    with _lock:
        v = _load_pin_visibility()
    return jsonify({"ok": True, "visibility": v})


@bp.post("/api/pin-visibility")
def pin_visibility_set():
    """Set one item's pin visibility (JSON: system, row, show;
    equipment accepted but ignored — HOB rows are shared across
    equipments, so the key is "system|row"). Only that one key
    changes; the rest of the map is untouched."""
    data = request.get_json(silent=True) or {}
    system = data.get("system", "")
    row = data.get("row", "")
    if not system or row == "" or row is None or "show" not in data:
        return jsonify({"ok": False, "error": "system, row and show required"}), 400
    key = "{}|{}".format(system, str(row))
    with _lock:
        v = _load_pin_visibility()
        v[key] = bool(data["show"])
        _save_pin_visibility(v)
    return jsonify({"ok": True, "system": system, "row": str(row),
                    "show": bool(data["show"])})


@bp.get("/api/loc-index")
def loc_index():
    """Location-photo index for one system (+equipment when given):
    {items: {"<row>": {"file": ..., "updated": ...}}}. The photo bytes
    are served by GET /api/photo/<file>."""
    system = request.args.get("system", "")
    equipment = request.args.get("equipment", "")
    if not system:
        return jsonify({"ok": False, "error": "system required"}), 400
    with _lock:
        d = _load()
    items = {}
    for k, v in (d.get("loc_photos") or {}).items():
        parts = k.split("|")
        if len(parts) != 3 or parts[0] != system:
            continue
        if equipment and parts[1] != equipment:
            continue
        items[parts[2]] = v
    return jsonify({"ok": True, "system": system, "equipment": equipment,
                    "items": items})


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
    notes = {}
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
        if srec.get("note"):
            notes[slot] = srec["note"]
        latest = max(latest, srec.get("received_at", ""))
        for ph in srec.get("photos", []):
            day_photos.setdefault(slot, [])
            if ph not in day_photos[slot]:
                day_photos[slot].append(ph)
    return jsonify({"ok": True, "system": system, "equipment": equipment,
                    "date": date, "rounds": rounds, "notes": notes,
                    "photos": day_photos, "updated": latest})


@bp.post("/api/issue")
def issue_create():
    """Open a corrective-action issue found during a round
    (Mitti-style action tracking, 형아 2026-10-26). JSON body:
    {id, system, equipment, date, slot, row, item, desc, priority,
    created_by, created_at, device, base_id}."""
    try:
        e = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid json"}), 400
    if not isinstance(e, dict) or not e.get("id") or not e.get("desc"):
        return jsonify({"ok": False, "error": "id and desc required"}), 400
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        d = _load()
        issues = d.setdefault("issues", [])
        if any(x.get("id") == e["id"] for x in issues):
            return jsonify({"ok": True, "duplicate": True})
        issues.append({
            "id": e["id"], "system": e.get("system", ""),
            "equipment": e.get("equipment", ""), "date": e.get("date", ""),
            "slot": e.get("slot", ""), "row": e.get("row", ""),
            "item": e.get("item", ""), "desc": e.get("desc", ""),
            "priority": e.get("priority", "Normal"),
            "created_by": e.get("created_by", ""),
            "created_at": e.get("created_at", ""),
            "device": e.get("device", ""), "base_id": e.get("base_id", ""),
            "photo": "", "status": "open", "received_at": now,
            "done_by": "", "done_at": "", "done_note": "",
        })
        _save(d)
    return jsonify({"ok": True})


@bp.post("/api/issue-photo")
def issue_photo():
    """Attach the (single) photo for an issue (multipart: issue_id +
    file). Stored like the monitor photos and forwarded to Drive."""
    issue_id = request.form.get("issue_id", "")
    f = request.files.get("file")
    if not issue_id or f is None or not f.filename:
        return jsonify({"ok": False, "error": "issue_id and file required"}), 400
    import re as _re
    safe_id = _re.sub(r"[^A-Za-z0-9_.-]+", "_", issue_id)
    stored = "ISSUE__" + safe_id + ".jpg"
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    path = os.path.join(PHOTOS_DIR, stored)
    f.save(path)
    with _lock:
        d = _load()
        for x in d.get("issues", []):
            if x.get("id") == issue_id:
                x["photo"] = stored
                break
        _save(d)
    threading.Thread(target=_forward_photo_to_drive,
                     args=(stored, path, issue_id), daemon=True).start()
    return jsonify({"ok": True, "name": stored})


@bp.post("/api/issue-update")
def issue_update():
    """Close (or reopen) an issue: {id, status, by, note}. The closer's
    name and time stay on the record."""
    try:
        e = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid json"}), 400
    if not isinstance(e, dict) or not e.get("id"):
        return jsonify({"ok": False, "error": "id required"}), 400
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        d = _load()
        hit = None
        for x in d.get("issues", []):
            if x.get("id") == e["id"]:
                hit = x
                break
        if hit is None:
            return jsonify({"ok": False, "error": "no such issue"}), 404
        st = e.get("status", "")
        if st in ("open", "done"):
            hit["status"] = st
            if st == "done":
                hit["done_by"] = e.get("by", "")
                hit["done_at"] = now
                hit["done_note"] = e.get("note", "")
            else:
                hit["done_by"] = ""
                hit["done_at"] = ""
                hit["done_note"] = ""
            _save(d)
    return jsonify({"ok": True})


@bp.get("/api/issues")
def issues_list():
    """All issues, open first then newest first."""
    with _lock:
        d = _load()
    items = list(d.get("issues", []))
    items.sort(key=lambda x: x.get("received_at", ""), reverse=True)
    items.sort(key=lambda x: x.get("status") != "open")
    return jsonify({"ok": True, "issues": items})


@bp.get("/api/audit")
def audit():
    """Edit history of submitted records, newest first: who edited
    what, when, and the per-row [old, new] changes."""
    with _lock:
        d = _load()
    return jsonify({"ok": True,
                    "audit": list(reversed(d.get("audit_log", [])))})


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
    d["photo_tags"] = _load_tags()
    d["tag_setting"] = {"tag_edit_enabled": _tag_edit_enabled()}
    return jsonify(d)


# ---------------- photo pin tags (형아, 2026-10-28) ----------------
# Pins with names placed on checklist photos (location / issue /
# submission photos). Tags are keyed by the photo's stored FILE NAME
# (형아's decision: name basis, so the same-named photo on another
# device shows the same tags; renaming a file breaks the link).
# Stored apart from data_checklist.json in
# data_checklist_photo_tags.json:
#   {"<file name>": [{"id","x","y","name","by","ts"}, ...], ...}
# x/y are image ratios (0-1) so a pin stays put at any zoom/size.
# The original photo file is never touched — pins are an overlay.
# Tag Setting (admin, default On) lives in
# data_checklist_tag_setting.json; when Off, photos open View-only:
# tag saves are rejected here and the app hides Edit entry points.

TAGS_PATH = os.path.join(BASE_DIR, "data_checklist_photo_tags.json")
TAG_SETTING_PATH = os.path.join(BASE_DIR, "data_checklist_tag_setting.json")


def _load_tags():
    try:
        with open(TAGS_PATH) as f:
            t = json.load(f)
        if isinstance(t, dict):
            return t
    except Exception:
        pass
    return {}


def _save_tags(t):
    tmp = TAGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(t, f, ensure_ascii=False, indent=1)
    os.replace(tmp, TAGS_PATH)


def _tag_edit_enabled():
    try:
        with open(TAG_SETTING_PATH) as f:
            s = json.load(f)
        if isinstance(s, dict) and "tag_edit_enabled" in s:
            return bool(s["tag_edit_enabled"])
    except Exception:
        pass
    return True


def _save_tag_setting(on):
    tmp = TAG_SETTING_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"tag_edit_enabled": bool(on)}, f)
    os.replace(tmp, TAG_SETTING_PATH)


def _clean_tags(raw):
    out = []
    if not isinstance(raw, list):
        return out
    for t in raw[:200]:
        if not isinstance(t, dict):
            continue
        try:
            x = float(t.get("x"))
            y = float(t.get("y"))
        except Exception:
            continue
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            continue
        out.append({
            "id": str(t.get("id") or "")[:64],
            "x": x, "y": y,
            "name": str(t.get("name") or "")[:80],
            "by": str(t.get("by") or "")[:40],
            "ts": str(t.get("ts") or "")[:40],
        })
    return out


@bp.get("/api/tag-setting")
def tag_setting_get():
    return jsonify({"ok": True, "on": _tag_edit_enabled()})


@bp.post("/api/tag-setting")
def tag_setting_set():
    try:
        e = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid json"}), 400
    if not isinstance(e, dict) or "on" not in e:
        return jsonify({"ok": False, "error": "on required"}), 400
    with _lock:
        _save_tag_setting(bool(e["on"]))
    return jsonify({"ok": True, "on": _tag_edit_enabled()})


@bp.get("/api/photo-tags")
def photo_tags_get():
    """Tags for one photo, by stored file name:
    ?file=<name> -> {ok, file, tags, on}."""
    name = os.path.basename(request.args.get("file", ""))
    if not name:
        return jsonify({"ok": False, "error": "file required"}), 400
    with _lock:
        tags = _load_tags()
    return jsonify({"ok": True, "file": name,
                    "tags": tags.get(name, []),
                    "on": _tag_edit_enabled()})


@bp.get("/api/photo-tags-index")
def photo_tags_index():
    """Tag counts per photo file name, for badging photo lists."""
    with _lock:
        tags = _load_tags()
    return jsonify({"ok": True,
                    "counts": {k: len(v) for k, v in tags.items() if v},
                    "on": _tag_edit_enabled()})


@bp.post("/api/photo-tags")
def photo_tags_set():
    """Replace the whole tag list for one photo file name:
    {file, tags:[{id,x,y,name,by,ts}]}. Rejected when Tag Setting
    is Off (View-only mode)."""
    if not _tag_edit_enabled():
        return jsonify({"ok": False, "error": "tag setting is off"}), 403
    try:
        e = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid json"}), 400
    if not isinstance(e, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400
    name = os.path.basename(str(e.get("file") or ""))
    if not name:
        return jsonify({"ok": False, "error": "file required"}), 400
    tags = _clean_tags(e.get("tags"))
    with _lock:
        all_tags = _load_tags()
        if tags:
            all_tags[name] = tags
        else:
            all_tags.pop(name, None)
        _save_tags(all_tags)
    return jsonify({"ok": True, "file": name, "tags": tags})


# ---------------- monitor photo auto-read (vision) ----------------
VISION_MODEL = os.environ.get("CHECKLIST_VISION_MODEL", "claude-sonnet-5-5")

_SCREEN_HINTS = {
    "RO": ("The photo shows the SRP RO (reverse osmosis) HMI screen. On this screen some labels differ from the "
           "checklist tags: PI-01 is checklist PT-01, PI-02 is PT-02, FI-01 is FT-01, FI-02 is FIT-02, "
           "FI-03 is FIT-03, FI-04 is FIT-04 (Supply Flow). LT-01, AIT-01, AIT-02 and LT-02 use the same labels."),
    "NMP": ("The photo shows one page of the SRP NMP Recovery HMI (the Control page or the Transfer page). "
            "Screen tags are written WITHOUT dashes: TE005A means checklist tag TE-005A, LT001A means LT-001A, "
            "PUM006A means PUM-006A, and so on. Only one page is visible, so return only the tags you can actually see. "
            "Match every value to the tag printed directly above it, one by one - NEVER assign values by their "
            "position or order on the screen. The stacked level tags on the towers are not in numeric order "
            "(on the B tower they run LT003B, LT002B, LT001B, LT004B from top to bottom), so guessing by "
            "position swaps values. If the exact tag of a checklist item is not visible in this photo, return "
            "nothing for that item - never substitute a similar-looking tag. In particular: LT-006 (NMP Storage "
            "Tank Level) appears ONLY on the Transfer page, on tank NTK-01-010. The Control page shows a "
            "different tank, TNK-01-019 (Wastewater Buffer Tank), whose level tag is LT009 - LT009 is not a "
            "checklist item and its value must never be returned for LT-006. "
            "For the stacked tower level tags (LT001A-LT004A and LT001B-LT004B), if you cannot confidently "
            "pair each value with the tag printed above it, list those tags in \"unclear\" instead of guessing. "
            "For checklist item #51 the screen shows PUM-012 and PUM-013 separately: return the running (non-zero) "
            "one, or 0 if both are 0. For FT-002 return the instantaneous flow value, NOT the accumulated totalizer."),
    "HOB": ("The photo shows a hot oil heater burner HMI screen. The screen title names the heater: "
            "'#1 ANODE' = EAB01, '#2 ANODE' = EAB02, '#1 CATHODE' = ECB01, '#2 CATHODE' = ECB02, '#3 CATHODE' = ECB03. "
            "Also return \"screen_equipment\" with that equipment id. On this screen: FI-001 is the gas flow, "
            "FCV-001 (linkage open rate) and TIC-009 (MV) are two DIFFERENT numbers - read each from its own place and never copy one to the other. FCV-001 is the POS percent shown directly under the FCV-001 valve near the top centre of the screen (a green % number). TIC-009 (MV) is the MV percent in the TIC-009 box on the left (the red % number under 'MV'). The O2 percent value is O2I-001. "
            "UNIT RULES (important): PI-002, PI-003 and DPI-002 each appear TWICE on this screen - a green "
            "readout in psig and a black readout in bar. Return the BAR (black) value for these three tags, "
            "never the psig value. The bar twin of PI-002 is the black readout just above the PI-002 psig "
            "readout; DPI-002's bar value is its black readout (its psig readout is right below it); "
            "PI-003's bar twin is the black readout near the heater outlet line at the lower right, next to "
            "a temperature shown in degC. FI-002 also appears twice: a large gal/h number (e.g. 137500 - do "
            "NOT use it) and a plain m3/h number (e.g. 521) just above it - return the m3/h number for FI-002. "
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
        + "\nReply with ONLY a JSON object: {\"values\": {\"<row>\": <number>, ...}, \"unclear\": "
          "[\"<tag>\", ...], \"quality\": \"good\" or \"poor\"} using the row numbers above as keys for "
          "values. Include only values you can actually read on the screen - never guess or invent "
          "a value. Use plain numbers (no units, no thousands separators). "
          "\"unclear\" lists the checklist tags (spelled exactly as in the list above) whose on-screen "
          "display you could see but whose label or digits you could NOT read with confidence - including "
          "groups of similar stacked tags where you cannot be sure which value belongs to which tag. "
          "For an unclear tag, do NOT put a guessed value in \"values\"; list the tag in \"unclear\" "
          "instead. \"quality\" is \"poor\" when the photo is blurry, dark, glaring, or taken at an angle "
          "so that a substantial part of the screen is hard to read; otherwise \"good\"."
    )
    payload = {
        "model": VISION_MODEL,
        "max_tokens": 2000,
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
    known_tags = {str(it.get("tag", "")) for it in items}
    unclear = [str(t) for t in (parsed.get("unclear") or []) if str(t) in known_tags]
    quality = "poor" if str(parsed.get("quality", "")).lower() == "poor" else "good"
    return jsonify({"ok": True, "values": values, "unclear": unclear,
                    "quality": quality,
                    "screen_equipment": parsed.get("screen_equipment")})


# ---------------------------------------------------------------------------
# MNT — mark a whole round as not done (maintenance)
#
# When a patrol round could not be done (no time), the whole slot is
# marked MNT: one submission record whose every value is "MNT". It flows
# through the normal pipeline untouched, so it shows as MNT in the app's
# previous-data view (day()), the PDF, and the Excel writer queue.
# The Android app itself cannot gain a button (no app source), so the
# marking happens on this small web page, opened in the tablet browser.
# ---------------------------------------------------------------------------

from datetime import timedelta as _td


def _phoenix_now():
    """Current time in America/Phoenix (MST, no DST — always UTC-7)."""
    return datetime.now(timezone.utc) - _td(hours=7)


def _mnt_system(system_id):
    for s in _defs().get("systems", []):
        if s.get("id") == system_id:
            return s
    return None


def _mnt_values(system):
    """Every check-item row of the system, all set to MNT."""
    values = {}
    for sec in system.get("sections", []):
        for it in sec.get("items", []):
            values[str(it.get("row"))] = "MNT"
    return values


def _mnt_id(system_id, equipment, date_s, slot):
    return "{}|{}|{}|{}".format(system_id, equipment or "", date_s, slot)


def _mnt_is_mnt(rec):
    """True if a stored round is an MNT mark: flagged, or every value MNT."""
    if rec.get("mnt"):
        return True
    vals = rec.get("values") or {}
    return bool(vals) and all(v == "MNT" for v in vals.values())


def _mnt_match(recs, system_id, equipment, date_s, slot):
    """Rounds for one round, matched by fields (no locking; caller locks)."""
    return [s for s in recs
            if s.get("system") == system_id
            and (s.get("equipment") or "") == (equipment or "")
            and s.get("date") == date_s and s.get("slot") == slot]


def _mnt_round_records(system_id, equipment, date_s, slot):
    """All stored rounds for one round.

    The app uploads ids like system|equipment|date|slot|timestamp, so an
    exact-id lookup misses app-marked rounds. Match on the four fields.
    """
    with _lock:
        d = _load()
    return _mnt_match(d.get("submissions", []), system_id, equipment,
                      date_s, slot)


def _mnt_status(system_id, equipment, date_s, slot):
    """('empty'|'submitted'|'mnt', record-or-None) for one round."""
    recs = _mnt_round_records(system_id, equipment, date_s, slot)
    for s in recs:
        if not _mnt_is_mnt(s):
            return ("submitted", s)
    if recs:
        return ("mnt", recs[-1])
    return ("empty", None)


def _mnt_default_slot(system):
    """Latest slot at or before now (Phoenix); first slot if none yet."""
    hhmm = _phoenix_now().strftime("%H:%M")
    slots = system.get("slots", []) or []
    past = [s for s in slots if s <= hhmm]
    return past[-1] if past else (slots[0] if slots else "")


def _mnt_validate(system_id, equipment, date_s, slot):
    """(system, equipment, error-or-None) for a mark/unmark request."""
    system = _mnt_system(system_id)
    if system is None:
        return None, "", "Unknown system."
    equipments = system.get("equipments", []) or []
    if equipments:
        if equipment not in equipments:
            return None, "", "Pick an equipment for this system."
    else:
        equipment = ""
    try:
        datetime.strptime(date_s, "%Y-%m-%d")
    except Exception:
        return None, "", "Pick a valid date."
    if slot not in (system.get("slots", []) or []):
        return None, "", "Pick a valid time slot."
    return system, equipment, None


def _mnt_redirect(system_id, equipment, date_s, slot):
    return redirect(url_for("checklist.mnt_page", system=system_id,
                            equipment=equipment, date=date_s, slot=slot))


@bp.get("/mnt")
def mnt_page():
    systems = _defs().get("systems", []) or []
    system_id = request.args.get("system") or "NMP"
    system = _mnt_system(system_id) or (systems[0] if systems else None)
    if system is None:
        return "No checklist systems configured.", 500
    system_id = system.get("id", "")
    equipments = system.get("equipments", []) or []
    equipment = request.args.get("equipment") or ""
    if equipments and equipment not in equipments:
        equipment = equipments[0]
    if not equipments:
        equipment = ""
    date_s = request.args.get("date") or ""
    try:
        datetime.strptime(date_s, "%Y-%m-%d")
    except Exception:
        date_s = _phoenix_now().strftime("%Y-%m-%d")
    slots = system.get("slots", []) or []
    slot = request.args.get("slot") or ""
    if slot not in slots:
        slot = _mnt_default_slot(system)
    status, rec = _mnt_status(system_id, equipment, date_s, slot)
    return render_template(
        "checklist/mnt.html", systems=systems, system_id=system_id,
        equipments=equipments, equipment=equipment, date_s=date_s,
        slots=slots, slot=slot, status=status, rec=rec)


@bp.post("/mnt/mark")
def mnt_mark():
    system_id = (request.form.get("system") or "").strip()
    equipment = (request.form.get("equipment") or "").strip()
    date_s = (request.form.get("date") or "").strip()
    slot = (request.form.get("slot") or "").strip()
    system, equipment, err = _mnt_validate(system_id, equipment, date_s,
                                           slot)
    if err:
        flash(err)
        return redirect(url_for("checklist.mnt_page"))
    rid = _mnt_id(system_id, equipment, date_s, slot)
    with _lock:
        d = _load()
        recs = _mnt_match(d.get("submissions", []), system_id, equipment,
                          date_s, slot)
        if recs:
            if all(_mnt_is_mnt(s) for s in recs):
                flash("This round is already marked MNT.")
            else:
                flash("This round was already submitted — MNT "
                      "cannot overwrite a real round.")
            return _mnt_redirect(system_id, equipment, date_s, slot)
        now = datetime.now(timezone.utc).isoformat()
        rec = {
            "id": rid, "system": system_id, "equipment": equipment,
            "date": date_s, "slot": slot,
            "values": _mnt_values(system),
            "note": "MNT — round not done (no time to patrol)",
            "target_file": "", "saved_at": now, "device": "mnt-page",
            "received_at": now, "written": False, "mnt": True,
        }
        d["submissions"].append(rec)
        _save(d)
    _issue_pdf(rec)
    if WRITE_SCRIPT_URL:
        threading.Thread(target=_flush_written, daemon=True).start()
    flash("Marked {} {} {} as MNT — every item in this round shows MNT."
          .format(system_id, date_s, slot))
    return _mnt_redirect(system_id, equipment, date_s, slot)


@bp.post("/mnt/unmark")
def mnt_unmark():
    system_id = (request.form.get("system") or "").strip()
    equipment = (request.form.get("equipment") or "").strip()
    date_s = (request.form.get("date") or "").strip()
    slot = (request.form.get("slot") or "").strip()
    system, equipment, err = _mnt_validate(system_id, equipment, date_s,
                                           slot)
    if err:
        flash(err)
        return redirect(url_for("checklist.mnt_page"))
    with _lock:
        d = _load()
        recs = _mnt_match(d.get("submissions", []), system_id, equipment,
                          date_s, slot)
        # remove every MNT mark for the round, never a real submission
        mnt_ids = {s.get("id") for s in recs if _mnt_is_mnt(s)}
        if recs and not mnt_ids:
            flash("This round was really submitted — only an "
                  "MNT mark can be undone here.")
            return _mnt_redirect(system_id, equipment, date_s, slot)
        if mnt_ids:
            d["submissions"] = [s for s in d.get("submissions", [])
                                if s.get("id") not in mnt_ids]
            _save(d)
            flash("MNT mark removed for {} {} {}."
                  .format(system_id, date_s, slot))
            return _mnt_redirect(system_id, equipment, date_s, slot)
    flash("Nothing to undo — this round is not marked MNT.")
    return _mnt_redirect(system_id, equipment, date_s, slot)
