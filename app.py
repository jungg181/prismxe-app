"""PrismXE merged app: boiler gauge + construction manager + patrol board.

Single Flask app served by gunicorn (gunicorn app:app).
- GET /            portal with links to the three apps
- /gauge/*         boiler gauge inspection app
- /cm/*            construction manager (+ /cm/onsite worker app)
- /patrol/*        equipment patrol timetable board

Env vars:
  BASE_URL    public base URL used in generated QR codes
              (default https://app.prismxe.com)
  SHEETS_URL  Google Apps Script webhook for the gauge app (optional)
  SECRET_KEY  Flask secret key (change in production)
  PORT        listen port (default 5000)
"""
import os

from flask import Flask, render_template

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

from gauge_bp import bp as gauge_bp
from cm_bp import bp as cm_bp, register_jinja as register_cm_jinja
from patrol_bp import bp as patrol_bp

app.register_blueprint(gauge_bp)
app.register_blueprint(cm_bp)
app.register_blueprint(patrol_bp)
register_cm_jinja(app)


@app.get("/")
def portal():
    return render_template("portal.html")


@app.errorhandler(404)
def not_found(e):
    return (
        "<h1>Not found</h1>"
        '<p><a href="/">Back to PrismXE portal</a></p>', 404
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
