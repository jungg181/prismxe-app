"""PrismXE merged app: boiler gauge + construction manager + patrol board + burner guide.

Single Flask app served by gunicorn (gunicorn app:app).
- GET /            portal with links to the three apps
- /gauge/*         boiler gauge inspection app
- /cm/*            construction manager (+ /cm/onsite worker app)
- /patrol/*        equipment patrol timetable board
- /burner/*        burner turn-on / shut-off guided procedure

Env vars:
  BASE_URL    public base URL used in generated QR codes
              (default https://app.prismxe.com)
  SHEETS_URL  Google Apps Script webhook for the gauge app (optional)
  SECRET_KEY  Flask secret key (change in production)
  PORT        listen port (default 5000)
"""
import os
import json
import io
import hmac

from flask import (Flask, render_template, request, jsonify, send_file,
                   session, redirect, url_for)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

# Portal entry password (Render env PORTAL_PASSWORD). When set, the
# portal page and the app-catalog JSON require it once per session.
PORTAL_PASSWORD = os.environ.get("PORTAL_PASSWORD", "")


def _portal_authed():
    return session.get("portal_authed") is True

PORTAL_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "portal_config.json")

def load_portal_config():
    try:
        with open(PORTAL_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"companies": []}

def save_portal_config(cfg):
    with open(PORTAL_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "apps_registry.json")

def load_registry():
    try:
        with open(REGISTRY_PATH) as f:
            return json.load(f)
    except Exception:
        return {"web": [], "android": []}

# Display metadata for the built-in apps. The portal/manage app list is
# built AUTOMATICALLY from the blueprints registered on the Flask app, so
# a new built-in app appears in the list by itself - it cannot be
# forgotten. A blueprint with no entry here still shows up, with a
# default card derived from its name and URL prefix.
BLUEPRINT_APPS = {
    "gauge": {"name": "Gauge Check", "icon": "🔧",
              "desc": "Photograph gauges, auto-read needles, log measurements",
              "company": "Hanseong Arizona LLC", "company_icon": "🏢"},
    "patrol": {"name": "Patrol Board", "icon": "📋",
               "desc": "Equipment patrol timetable & shift monitoring",
               "company": "Hanseong Arizona LLC", "company_icon": "🏢"},
    "burner": {"name": "Burner Guide", "icon": "🔥",
               "desc": "Burner startup / shutdown guided procedure",
               "company": "Hanseong Arizona LLC", "company_icon": "🏢"},
    "cm": {"name": "Construction Manager", "icon": "🏗️",
           "desc": "Sites, workers, timecards, incidents, QA/QC",
           "company": "Construction", "company_icon": "🏗️"},
    "patrol_coa": {"name": "Patrol COA", "icon": "📋",
                   "desc": "COA patrol board",
                   "company": "COA", "company_icon": "🧹"},
    "bible": {"name": "Bible", "icon": "📖",
              "desc": "Bible reading (성경일독)",
              "company": "Tools", "company_icon": "🔧"},
    # API-only backend for the Android Check List app - the app itself is
    # listed under "Android apps" in apps_registry.json.
    "checklist": {"skip": True},
}

def build_catalog():
    """The full app list for the portal and /manage, assembled from:
    1. blueprints registered on the app (automatic - can never go stale),
    2. apps_registry.json (external websites + Android apps),
    3. portal_config.json custom links whose URL is not already listed.
    """
    groups = []
    by_name = {}
    seen_urls = set()

    def add_group(name, icon):
        if name not in by_name:
            g = {"name": name, "icon": icon, "apps": []}
            by_name[name] = g
            groups.append(g)
        return by_name[name]

    for bp_name, bp in app.blueprints.items():
        meta = BLUEPRINT_APPS.get(bp_name)
        if meta is None:
            prefix = getattr(bp, "url_prefix", None) or ("/" + bp_name)
            meta = {"name": bp_name.replace("_", " ").title(), "icon": "📱",
                    "desc": "Auto-added app", "company": "Apps",
                    "company_icon": "📁"}
        if meta.get("skip"):
            continue
        prefix = getattr(bp, "url_prefix", None) or ("/" + bp_name)
        url = prefix.rstrip("/") + "/"
        g = add_group(meta["company"], meta["company_icon"])
        g["apps"].append({"name": meta["name"], "icon": meta["icon"],
                          "desc": meta["desc"], "url": url})
        seen_urls.add(url)

    reg = load_registry()
    for e in (reg.get("web") or []) + (reg.get("android") or []):
        url = (e.get("url") or "").strip()
        if url and url in seen_urls:
            continue
        g = add_group(e.get("group", "More"), e.get("group_icon", "📁"))
        g["apps"].append({"name": e.get("name", ""), "icon": e.get("icon", "📱"),
                          "desc": e.get("desc", ""), "url": url})
        if url:
            seen_urls.add(url)

    cfg = load_portal_config()
    for co in cfg.get("companies", []):
        for a in (co.get("apps") or []):
            url = (a.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            g = add_group(co.get("name", "Custom"), co.get("icon", "📁"))
            g["apps"].append({"name": a.get("name", ""), "icon": a.get("icon", "📱"),
                              "desc": a.get("desc", ""), "url": url})
            seen_urls.add(url)

    return [g for g in groups if g["apps"]]

from gauge_bp import bp as gauge_bp
from cm_bp import bp as cm_bp, register_jinja as register_cm_jinja
from patrol_bp import bp as patrol_bp, patrol_coa_bp
from burner_bp import bp as burner_bp
from bible_bp import bp as bible_bp
from checklist_bp import bp as checklist_bp

app.register_blueprint(gauge_bp)
app.register_blueprint(cm_bp)
app.register_blueprint(patrol_bp)
app.register_blueprint(patrol_coa_bp)
app.register_blueprint(burner_bp)
app.register_blueprint(bible_bp)
app.register_blueprint(checklist_bp)
register_cm_jinja(app)


# --- Legacy company-URL redirects (2026-10-28) ---
# Patrol and the gauge Check List moved under /Hanseong/. Old URLs keep
# working via 308 (method-preserving) redirects so QR codes, bookmarks,
# and the APKs do not break.
_LEGACY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH"]


def _legacy_redirect(new_base, path):
    url = new_base + ("/" + path if path else "/")
    if request.query_string:
        url += "?" + request.query_string.decode("utf-8", "replace")
    return redirect(url, code=308)


@app.route("/patrol", defaults={"path": ""}, methods=_LEGACY_METHODS, strict_slashes=False)
@app.route("/patrol/<path:path>", methods=_LEGACY_METHODS, strict_slashes=False)
def legacy_patrol_redirect(path):
    return _legacy_redirect("/Hanseong/patrol", path)


@app.route("/gauge", defaults={"path": ""}, methods=_LEGACY_METHODS, strict_slashes=False)
@app.route("/gauge/<path:path>", methods=_LEGACY_METHODS, strict_slashes=False)
def legacy_gauge_redirect(path):
    return _legacy_redirect("/Hanseong/gauge", path)


@app.route("/", methods=["GET", "POST"])
def portal():
    if PORTAL_PASSWORD and not _portal_authed():
        if request.method == "POST" and hmac.compare_digest(
                request.form.get("password") or "", PORTAL_PASSWORD):
            session["portal_authed"] = True
            return redirect(url_for("portal"))
        return render_template("portal_login.html",
                               error=(request.method == "POST"))
    return render_template("portal.html", companies=build_catalog())


@app.get("/manage")
def portal_manage():
    from patrol_bp import login_required as patrol_login_required
    @patrol_login_required
    def _inner():
        cfg = load_portal_config()
        return render_template("portal_manage.html",
                               companies=cfg.get("companies", []),
                               catalog=build_catalog())
    return _inner()


@app.get("/api/apps")
def api_apps():
    """The auto-built app catalog as JSON (same list the portal shows)."""
    if PORTAL_PASSWORD and not _portal_authed():
        return jsonify({"error": "Password required."}), 401
    return jsonify({"companies": build_catalog()})


@app.post("/api/portal/save")
def api_portal_save():
    from patrol_bp import login_required as patrol_login_required
    @patrol_login_required
    def _inner():
        data = request.get_json(force=True, silent=True) or {}
        companies = data.get("companies", [])
        # Basic sanitization
        clean = []
        for co in companies[:20]:
            apps = []
            for a in (co.get("apps") or [])[:20]:
                apps.append({
                    "name": str(a.get("name", ""))[:60],
                    "icon": str(a.get("icon", "📱"))[:8],
                    "desc": str(a.get("desc", ""))[:120],
                    "url": str(a.get("url", ""))[:200],
                })
            clean.append({
                "name": str(co.get("name", "Untitled"))[:60],
                "icon": str(co.get("icon", "📁"))[:8],
                "apps": apps,
            })
        save_portal_config({"companies": clean})
        return jsonify({"ok": True})
    return _inner()


@app.get("/portal/backup")
def portal_backup():
    """Public backup of portal folder config (ephemeral-disk protection)."""
    cfg = load_portal_config()
    data = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
    buf = io.BytesIO(data)
    return send_file(buf, mimetype="application/json", as_attachment=True,
                     download_name="portal-config-backup.json")


@app.get("/.well-known/assetlinks.json")
def assetlinks():
    # Android App Links verification: lets the Prism Patrol apps open
    # https://app.prismxe.com/Hanseong/patrol/patrol-clock (Hanseong) and
    # https://app.prismxe.com/patrol-coa/patrol-clock (COA) directly.
    # Each fingerprint is set from that company's Patrol Settings once its
    # APK is built; the fallback is the debug cert both apps are currently
    # signed with (verified 2026-10-11 from the shipped APKs via apksigner).
    from patrol_bp import DB, patrol_coa_db
    fallback = '72:1F:83:61:60:90:76:BF:EC:EA:32:ED:C9:62:C9:85:' \
               'BF:51:C1:57:E6:01:61:8B:7C:08:F5:CC:09:46:E7:A7'
    sha_h = DB.get('config', {}).get('apk_cert_sha256', '') or fallback
    sha_c = patrol_coa_db.get('config', {}).get('apk_cert_sha256', '') or fallback
    return [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "com.prismxe.patrol",
                "sha256_cert_fingerprints": [sha_h],
            },
        },
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "com.prismxe.patrol.coa",
                "sha256_cert_fingerprints": [sha_c],
            },
        },
    ], 200, {"Content-Type": "application/json"}


@app.errorhandler(404)
def not_found(e):
    return (
        "<h1>Not found</h1>"
        '<p><a href="/">Back to PrismXE portal</a></p>', 404
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
