#!/usr/bin/env python3
"""성경일독 (Bible Reading Through) — Korean Bible reading app.

- GET /bible/                    home: dashboard, continue reading, progress stats
- GET /bible/read/<book>/<ch>    reader: verses, TTS read-aloud, voice follow-along
- GET /bible/plan                1-year reading plan
- GET /bible/auth                register / login page
- APIs under /bible/api/*        books, chapter text, auth, progress
- GET /bible/data/backup         JSON backup of users + progress

Bible text: Open Korean Bible dataset (CC BY 4.0), 개역한글-style text,
stored as data/bible/NN.json (compact per-book files).
"""
import hashlib
import json
import os
import secrets
import threading
import urllib.request
from datetime import date
from functools import wraps
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, session

bp = Blueprint("bible", __name__, url_prefix="/bible",
               template_folder="templates/bible")

BASE = Path(__file__).resolve().parent
BIBLE_DIR = BASE / "data" / "bible"
USERS_PATH = BASE / "data" / "bible_users.json"

TOTAL_CHAPTERS = 1189

# ---------------------------------------------------------------- sheets sync
# Set BIBLE_SHEETS_URL to the deployed Apps Script web app URL (same
# saveData/getData protocol as the patrol app). Empty = local-only mode.
BIBLE_SHEETS_URL = os.environ.get("BIBLE_SHEETS_URL", "")

def sheets_post(payload, timeout=12):
    if not BIBLE_SHEETS_URL:
        return
    try:
        req = urllib.request.Request(
            BIBLE_SHEETS_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except Exception as e:
        print(f"bible sheets_post failed: {e}", flush=True)

def sheets_get_data(timeout=15):
    if not BIBLE_SHEETS_URL:
        return None
    try:
        with urllib.request.urlopen(BIBLE_SHEETS_URL + "?action=getData",
                                    timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"bible sheets_get_data failed: {e}", flush=True)
        return None

def sheets_post_async(payload):
    if BIBLE_SHEETS_URL:
        threading.Thread(target=sheets_post, args=(payload,), daemon=True).start()

def maybe_restore_from_sheets():
    """On startup: if local user DB is missing/empty, try Sheets restore."""
    try:
        if USERS_PATH.exists() and USERS_PATH.stat().st_size > 10:
            return
    except Exception:
        pass
    remote = sheets_get_data()
    if isinstance(remote, dict) and remote.get("users"):
        try:
            save_users_local(remote)
            print("bible: restored users from Sheets", flush=True)
        except Exception as e:
            print(f"bible restore failed: {e}", flush=True)

# ---------------------------------------------------------------- data helpers

def load_books_meta():
    with open(BIBLE_DIR / "books.json", encoding="utf-8") as f:
        return json.load(f)

def load_book(book_num):
    with open(BIBLE_DIR / f"{book_num:02d}.json", encoding="utf-8") as f:
        return json.load(f)

def load_users():
    try:
        with open(USERS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}

def save_users_local(data):
    tmp = str(USERS_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, USERS_PATH)

def save_users(data):
    save_users_local(data)
    sheets_post_async({"action": "saveData", "data": data})

def hash_pw(password, salt):
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

def current_user_email():
    return session.get("bible_user")

def login_required_json(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user_email():
            return jsonify({"error": "login_required"}), 401
        return fn(*a, **kw)
    return wrapper

# ---------------------------------------------------------------- pages

@bp.get("/")
def home():
    return render_template("bible_home.html")

@bp.get("/read/<int:book>/<int:chapter>")
def read(book, chapter):
    return render_template("bible_read.html", book=book, chapter=chapter)

@bp.get("/plan")
def plan():
    return render_template("bible_plan.html")

@bp.get("/auth")
def auth():
    return render_template("bible_auth.html")

# ---------------------------------------------------------------- APIs

@bp.get("/api/books")
def api_books():
    return jsonify(load_books_meta())

@bp.get("/api/chapter/<int:book>/<int:chapter>")
def api_chapter(book, chapter):
    try:
        b = load_book(book)
    except Exception:
        return jsonify({"error": "book not found"}), 404
    chapters = b["chapters"]
    if chapter < 1 or chapter > len(chapters):
        return jsonify({"error": "chapter not found"}), 404
    verses = [{"n": i + 1, "t": t} for i, t in enumerate(chapters[chapter - 1])]
    # prev/next chapter (across books)
    books = load_books_meta()
    prev_ref, next_ref = None, None
    if chapter > 1:
        prev_ref = {"book": book, "chapter": chapter - 1}
    elif book > 1:
        pb = load_book(book - 1)
        prev_ref = {"book": book - 1, "chapter": len(pb["chapters"])}
    if chapter < len(chapters):
        next_ref = {"book": book, "chapter": chapter + 1}
    elif book < 66:
        next_ref = {"book": book + 1, "chapter": 1}
    bname = next((x["name"] for x in books if x["n"] == book), "")
    return jsonify({
        "book": book, "book_name": bname, "chapter": chapter,
        "verses": verses, "prev": prev_ref, "next": next_ref,
        "chapter_count": len(chapters),
    })

@bp.post("/api/register")
def api_register():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    name = (data.get("name") or "").strip()
    password = data.get("password") or ""
    if not email or "@" not in email:
        return jsonify({"error": "이메일을 입력해 주세요."}), 400
    if not name:
        return jsonify({"error": "이름을 입력해 주세요."}), 400
    if len(password) < 4:
        return jsonify({"error": "비밀번호는 4자 이상 입력해 주세요."}), 400
    users = load_users()
    if email in users["users"]:
        return jsonify({"error": "이미 등록된 이메일입니다. 로그인해 주세요."}), 400
    salt = secrets.token_hex(8)
    users["users"][email] = {
        "name": name,
        "salt": salt,
        "pw": hash_pw(password, salt),
        "created": date.today().isoformat(),
        "read": {},          # str(book) -> [chapters]
        "last": None,        # {"book": n, "chapter": n}
        "plan_start": date.today().isoformat(),
    }
    save_users(users)
    session["bible_user"] = email
    return jsonify({"ok": True, "name": name})

@bp.post("/api/login")
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    users = load_users()
    u = users["users"].get(email)
    if not u or u.get("pw") != hash_pw(password, u.get("salt", "")):
        return jsonify({"error": "이메일 또는 비밀번호가 맞지 않습니다."}), 401
    session["bible_user"] = email
    return jsonify({"ok": True, "name": u.get("name", "")})

@bp.post("/api/logout")
def api_logout():
    session.pop("bible_user", None)
    return jsonify({"ok": True})

@bp.get("/api/me")
def api_me():
    email = current_user_email()
    if not email:
        return jsonify({"user": None})
    users = load_users()
    u = users["users"].get(email)
    if not u:
        session.pop("bible_user", None)
        return jsonify({"user": None})
    read_count = sum(len(v) for v in u.get("read", {}).values())
    return jsonify({
        "user": {
            "email": email,
            "name": u.get("name", ""),
            "read_count": read_count,
            "total_chapters": TOTAL_CHAPTERS,
            "last": u.get("last"),
            "plan_start": u.get("plan_start"),
        }
    })

@bp.get("/api/progress")
@login_required_json
def api_progress():
    users = load_users()
    u = users["users"].get(current_user_email(), {})
    return jsonify({"read": u.get("read", {}), "last": u.get("last")})

@bp.post("/api/progress")
@login_required_json
def api_save_progress():
    """Save last position and/or merge read chapters.
    Body: {"last": {"book": n, "chapter": n}, "mark_read": {"book": n, "chapter": n}}"""
    data = request.get_json(force=True, silent=True) or {}
    users = load_users()
    email = current_user_email()
    u = users["users"].get(email)
    if not u:
        return jsonify({"error": "login_required"}), 401
    if "last" in data and data["last"]:
        last = data["last"]
        try:
            u["last"] = {"book": int(last["book"]), "chapter": int(last["chapter"])}
        except (KeyError, ValueError, TypeError):
            pass
    mr = data.get("mark_read")
    if mr:
        try:
            b, c = str(int(mr["book"])), int(mr["chapter"])
            lst = u.setdefault("read", {}).setdefault(b, [])
            if c not in lst:
                lst.append(c)
                lst.sort()
        except (KeyError, ValueError, TypeError):
            pass
    # full read-map replace (from client merge) — only accept sane shape
    rm = data.get("read_map")
    if isinstance(rm, dict):
        clean = {}
        for k, v in rm.items():
            try:
                bk = str(int(k))
                clean[bk] = sorted({int(x) for x in v})
            except (ValueError, TypeError):
                continue
        u["read"] = clean
    save_users(users)
    read_count = sum(len(v) for v in u.get("read", {}).values())
    return jsonify({"ok": True, "read_count": read_count, "last": u.get("last")})

@bp.get("/api/plan/today")
def api_plan_today():
    """1-year plan: ~1189 chapters over 365 days from plan_start (or today)."""
    email = current_user_email()
    start = date.today().isoformat()
    if email:
        users = load_users()
        u = users["users"].get(email, {})
        start = u.get("plan_start") or start
    try:
        d0 = date.fromisoformat(start)
    except ValueError:
        d0 = date.today()
    day_idx = max(0, (date.today() - d0).days)  # 0-based
    # chapters per day: distribute 1189 over 365
    per_day = TOTAL_CHAPTERS / 365.0
    start_ch = int(day_idx * per_day)
    end_ch = int((day_idx + 1) * per_day)
    if end_ch <= start_ch:
        end_ch = start_ch + 1
    # map global chapter index -> (book, chapter)
    books = load_books_meta()
    flat = []
    for b in books:
        for c in range(1, b["chapters"] + 1):
            flat.append((b["n"], c))
    portion = flat[start_ch:min(end_ch, len(flat))]
    items = []
    for bn, ch in portion:
        bname = next((x["name"] for x in books if x["n"] == bn), "")
        items.append({"book": bn, "book_name": bname, "chapter": ch})
    return jsonify({
        "day": day_idx + 1,
        "start_date": start,
        "items": items,
        "done": day_idx >= 365,
    })

@bp.get("/data/backup")
def data_backup():
    return jsonify(load_users())


# try Sheets restore at import (worker startup)
maybe_restore_from_sheets()
