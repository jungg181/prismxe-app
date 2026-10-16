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

from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

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

from gauge_bp import bp as gauge_bp
from cm_bp import bp as cm_bp, register_jinja as register_cm_jinja
from patrol_bp import bp as patrol_bp, patrol_coa_bp
from burner_bp import bp as burner_bp
from bible_bp import bp as bible_bp

app.register_blueprint(gauge_bp)
app.register_blueprint(cm_bp)
app.register_blueprint(patrol_bp)
app.register_blueprint(patrol_coa_bp)
app.register_blueprint(burner_bp)
app.register_blueprint(bible_bp)
register_cm_jinja(app)


@app.get("/")
def portal():
    cfg = load_portal_config()
    return render_template("portal.html", companies=cfg.get("companies", []))


@app.get("/manage")
def portal_manage():
    from patrol_bp import login_required as patrol_login_required
    @patrol_login_required
    def _inner():
        cfg = load_portal_config()
        return render_template("portal_manage.html", companies=cfg.get("companies", []))
    return _inner()


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
    # https://app.prismxe.com/patrol/patrol-clock (Hanseong) and
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
