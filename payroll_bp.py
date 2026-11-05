#!/usr/bin/env python3
"""Payroll — KB Link + Hanseong payroll calculation (replaces sheet formulas).

- GET  /payroll/                        company cards + period picker
- GET  /payroll/<company>              per-employee payroll for a period
- GET  /payroll/<company>/export.csv   CSV export of the payroll table
- GET  /payroll/<company>/export.xlsx  XLSX export (Payroll + Invoice)
- GET  /payroll/<company>/invoice      invoice view (number = prefix + today)
- POST /payroll/<company>/invoice/expense            add an expense line
- POST /payroll/<company>/invoice/expense/<id>/delete
- GET  /payroll/rates                   list / edit rates (name + company)
- POST /payroll/rates                   add / update / delete a rate
- GET  /payroll/<company>/entries      list + add / edit / delete entries
- POST /payroll/<company>/entries      add / update / delete an entry
- POST /payroll/<company>/import       CSV upload of time entries
- POST /payroll/<company>/aliases      map an as-entered name to a rate
  row (stored; applied automatically on later imports/payrolls)
- GET  /payroll/<company>/import-app   pick a source (CM / Hanseong
  Patrol / COA / Patrol COA) + period, preview derived hours
- POST /payroll/<company>/import-app/confirm  import the previewed
  matched rows (duplicates skipped, unmatched never imported silently)

Computation lives in payroll_calc.py (pure functions). Rates are looked
up by (employee name + company) exact match; a missing rate leaves pay
and billing blank and flags the row — never $0. Sheets stay untouched.
"""
import csv
import io
import json
import os
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (Blueprint, flash, redirect, render_template, request,
                   send_file, url_for)

from payroll_calc import (DEFAULT_OT_MULTIPLIER, DEFAULT_OT_THRESHOLD,
                          compute_payroll, find_unmatched, invoice_lines,
                          invoice_number, variance_report)
from payroll_import_app import (HOURS_RULE_TEXT, SOURCES, build_preview,
                                import_rows)

bp = Blueprint("payroll", __name__, url_prefix="/payroll",
               template_folder="templates/payroll")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data_payroll.json")
COLLECTIONS = ["companies", "rates", "entries", "expenses", "settings",
               "aliases", "variances"]

# Bank details shown on invoices — from the KB Link / Hanseong invoice
# templates (PrismXe Info Bank tab / invoice sheets, read 2026-11-05).
BANK = {
    "bank": "JPMorgan Chase Banks, N.A.",
    "branch": "California Branch - 741611",
    "routing": "122100024",
    "account": "717953189",
    "account_name": "Prism Multiple Entertainment LLC",
}
APPROVED_BY = "Irin Yoon"

PHOENIX = ZoneInfo("America/Phoenix")


def load_db():
    if not os.path.exists(DB_PATH):
        db = {c: [] for c in COLLECTIONS}
        db["settings"] = {"ot_threshold": DEFAULT_OT_THRESHOLD,
                          "ot_multiplier": DEFAULT_OT_MULTIPLIER}
        with open(DB_PATH, "w") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        return db
    with open(DB_PATH) as f:
        db = json.load(f)
    for c in COLLECTIONS:
        db.setdefault(c, [] if c != "settings" else {})
    return db


def save_db():
    with open(DB_PATH, "w") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)


DB = load_db()


# ---------------------------------------------------------------- helpers
def find_company(slug):
    slug = (slug or "").strip().lower()
    for c in DB.get("companies", []):
        if (c.get("slug") or "").lower() == slug:
            return c
    return None


def _settings():
    s = DB.get("settings") or {}
    return {
        "ot_threshold": float(s.get("ot_threshold", DEFAULT_OT_THRESHOLD)),
        "ot_multiplier": float(s.get("ot_multiplier", DEFAULT_OT_MULTIPLIER)),
    }


def _parse_date(value, default):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return default


def _period_args():
    """(start, end) dates from ?from&to; default = current Mon-Sun week."""
    today = datetime.now(PHOENIX).date()
    monday = today - timedelta(days=today.weekday())
    start = _parse_date(request.args.get("from"), monday)
    end = _parse_date(request.args.get("to"), monday + timedelta(days=6))
    if start > end:
        start, end = end, start
    return start, end


def _payroll_for(company, start, end):
    s = _settings()
    return compute_payroll(DB.get("entries", []), DB.get("rates", []),
                           company["name"], start, end,
                           ot_threshold=s["ot_threshold"],
                           ot_multiplier=s["ot_multiplier"],
                           aliases=DB.get("aliases", []))


def _variances_for(company, result):
    return variance_report(result["rows"], DB.get("variances", []),
                           company["name"],
                           ot_multiplier=_settings()["ot_multiplier"])


def _alias_map(company):
    """{raw name: canonical name} for this company."""
    return {(a.get("alias") or "").strip(): (a.get("canonical") or "").strip()
            for a in DB.get("aliases", [])
            if (a.get("company") or "").strip() == company["name"]}


def _money(value):
    return f"${value:,.2f}"


def _fmt_hours(value):
    if value is None:
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _period_expenses(company, start, end):
    out = []
    for ex in DB.get("expenses", []):
        if ex.get("company") != company["name"]:
            continue
        if ex.get("from") == start.isoformat() and ex.get("to") == end.isoformat():
            out.append(ex)
    return out


def _entry_key(e):
    """Identity of an entry for exact-duplicate detection on re-import.
    Raw values are kept verbatim — including a clock-out date that
    disagrees with the entry date (e.g. Jose's 10/27 row) — so the key
    uses them exactly as stored."""
    return (
        (e.get("company") or "").strip(),
        (e.get("employee") or "").strip(),
        (e.get("date") or "").strip(),
        float(e.get("payroll_hours") or 0),
        (e.get("clock_in") or "").strip(),
        (e.get("clock_out") or "").strip(),
        (e.get("clock_out_date") or "").strip(),
        e.get("gross_hours"),
        (e.get("site") or "").strip(),
        (e.get("notes") or "").strip(),
    )


_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%m/%d/%Y",
                 "%m/%d/%y", "%Y/%m/%d", "%d %b %Y")


def _parse_entry_date(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _norm_header(name):
    return (name or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")


# ------------------------------------------------------------------- home
@bp.get("/")
def index():
    today = datetime.now(PHOENIX).date()
    monday = today - timedelta(days=today.weekday())
    return render_template(
        "payroll/index.html",
        companies=DB.get("companies", []),
        default_from=monday.isoformat(),
        default_to=(monday + timedelta(days=6)).isoformat(),
    )


# ---------------------------------------------------------------- payroll
@bp.get("/<company>")
def company_payroll(company):
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    result = _payroll_for(co, start, end)
    variances = _variances_for(co, result)
    return render_template(
        "payroll/company.html", company=co, start=start.isoformat(),
        end=end.isoformat(), result=result, variances=variances,
        money=_money, fmt_hours=_fmt_hours)


@bp.get("/<company>/export.csv")
def export_csv(company):
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    result = _payroll_for(co, start, end)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Employee", "Regular Hours", "Overtime Hours", "Total Hours",
                "Staffing Rate", "Gross Pay", "Billing Rate",
                "Billing Gross Pay"]
               + [f"Hours {d}" for d in result["days"]])
    for r in result["rows"]:
        w.writerow([
            r["employee"], r["reg_hours"], r["ot_hours"], r["total_hours"],
            "" if r["staffing"] is None else r["staffing"],
            "" if r["gross_pay"] is None else f"{r['gross_pay']:.2f}",
            "" if r["billing"] is None else r["billing"],
            "" if r["billing_gross"] is None else f"{r['billing_gross']:.2f}",
        ] + [r["days"].get(d, 0) for d in result["days"]])
    t = result["totals"]
    w.writerow(["PAYROLL TOTAL", t["total_reg_hours"], t["total_ot_hours"],
                t["total_hours"], "", f"{t['total_gross_pay']:.2f}", "",
                f"{t['total_billing_gross']:.2f}"]
               + ["" for _ in result["days"]])
    fn = f"Payroll_{co['slug']}_{start.isoformat()}_to_{end.isoformat()}.csv"
    return send_file(io.BytesIO(buf.getvalue().encode("utf-8")),
                     mimetype="text/csv", as_attachment=True,
                     download_name=fn)


@bp.get("/<company>/export.xlsx")
def export_xlsx(company):
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    start, end = _period_args()
    result = _payroll_for(co, start, end)
    inv = invoice_lines(result["rows"],
                        _period_expenses(co, start, end),
                        ot_multiplier=_settings()["ot_multiplier"])
    wb = Workbook()
    ws = wb.active
    ws.title = "Payroll"
    header_fill = PatternFill("solid", fgColor="1F3864")
    header_font = Font(bold=True, color="FFFFFF")
    headers = ["Employee", "Regular Hours", "Overtime Hours", "Total Hours",
               "Staffing Rate", "Gross Pay", "Billing Rate",
               "Billing Gross Pay"] + result["days"]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
    for r in result["rows"]:
        ws.append([
            r["employee"], r["reg_hours"], r["ot_hours"], r["total_hours"],
            r["staffing"], r["gross_pay"], r["billing"], r["billing_gross"],
        ] + [r["days"].get(d, 0) for d in result["days"]])
    t = result["totals"]
    total_row = ["PAYROLL TOTAL", t["total_reg_hours"], t["total_ot_hours"],
                 t["total_hours"], None, t["total_gross_pay"], None,
                 t["total_billing_gross"]] + [None] * len(result["days"])
    ws.append(total_row)
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "B2"

    ws2 = wb.create_sheet("Invoice")
    ws2.append(["Employee", "Type", "Hours", "Unit Price", "Total"])
    for cell in ws2[1]:
        cell.fill = header_fill
        cell.font = header_font
    for line in inv["lines"]:
        ws2.append([line["employee"], line["type"], line["qty"],
                    line["unit"], line["total"]])
    for ex in inv["expenses"]:
        ws2.append([ex["description"], "Expense", "", "", ex["amount"]])
    ws2.append(["Labour Total", "", "", "", inv["labour_total"]])
    ws2.append(["Expenses Total", "", "", "", inv["expense_total"]])
    ws2.append(["TOTAL", "", "", "", inv["total"]])
    for row in ws2.iter_rows(min_row=ws2.max_row - 2, max_row=ws2.max_row):
        for cell in row:
            cell.font = Font(bold=True)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    fn = f"Payroll_{co['slug']}_{start.isoformat()}_to_{end.isoformat()}.xlsx"
    return send_file(out,
                     mimetype="application/vnd.openxmlformats-officedocument"
                              ".spreadsheetml.sheet",
                     as_attachment=True, download_name=fn)


# ----------------------------------------------------------------- invoice
@bp.get("/<company>/invoice")
def invoice(company):
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    result = _payroll_for(co, start, end)
    inv = invoice_lines(result["rows"],
                        _period_expenses(co, start, end),
                        ot_multiplier=_settings()["ot_multiplier"])
    number = invoice_number(co["prefix"], datetime.now(PHOENIX).date())
    return render_template(
        "payroll/invoice.html", company=co, start=start.isoformat(),
        end=end.isoformat(), number=number, inv=inv, bank=BANK,
        approved_by=APPROVED_BY, money=_money)


@bp.post("/<company>/invoice/expense")
def add_expense(company):
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    desc = (request.form.get("description") or "").strip()
    try:
        amount = float(request.form.get("amount") or 0)
    except ValueError:
        amount = 0
    if desc and amount:
        DB["expenses"].append({
            "id": uuid.uuid4().hex[:12], "company": co["name"],
            "description": desc, "amount": round(amount, 2),
            "from": start.isoformat(), "to": end.isoformat(),
        })
        save_db()
        flash(f"Expense added: {desc} ${_money(amount)[1:]}.")
    else:
        flash("Enter a description and an amount.")
    return redirect(url_for(".invoice", company=co["slug"],
                            **{"from": start.isoformat(),
                               "to": end.isoformat()}))


@bp.post("/<company>/invoice/expense/<exp_id>/delete")
def delete_expense(company, exp_id):
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    before = len(DB["expenses"])
    DB["expenses"] = [e for e in DB["expenses"] if e.get("id") != exp_id]
    if len(DB["expenses"]) < before:
        save_db()
        flash("Expense removed.")
    return redirect(url_for(".invoice", company=co["slug"],
                            **{"from": start.isoformat(),
                               "to": end.isoformat()}))


# ------------------------------------------------------------------- rates
@bp.route("/rates", methods=["GET", "POST"])
def rates():
    if request.method == "POST":
        action = request.form.get("action") or ""
        if action == "update":
            rid = request.form.get("id") or ""
            for r in DB["rates"]:
                if r.get("id") == rid:
                    try:
                        r["staffing"] = float(request.form.get("staffing"))
                    except (TypeError, ValueError):
                        r["staffing"] = None
                    try:
                        r["billing"] = float(request.form.get("billing"))
                    except (TypeError, ValueError):
                        r["billing"] = None
                    save_db()
                    flash(f"Rate updated for {r['name']} ({r['company']}).")
                    break
        elif action == "add":
            name = (request.form.get("name") or "").strip()
            comp = (request.form.get("company") or "").strip()
            if name and comp:
                try:
                    staffing = float(request.form.get("staffing"))
                except (TypeError, ValueError):
                    staffing = None
                try:
                    billing = float(request.form.get("billing"))
                except (TypeError, ValueError):
                    billing = None
                DB["rates"].append({
                    "id": "rate-" + uuid.uuid4().hex[:8], "name": name,
                    "company": comp, "staffing": staffing,
                    "billing": billing,
                })
                save_db()
                flash(f"Rate added for {name} ({comp}).")
            else:
                flash("Name and company are required.")
        elif action == "delete":
            rid = request.form.get("id") or ""
            before = len(DB["rates"])
            DB["rates"] = [r for r in DB["rates"] if r.get("id") != rid]
            if len(DB["rates"]) < before:
                save_db()
                flash("Rate removed.")
        return redirect(url_for(".rates"))
    grouped = {}
    for r in sorted(DB["rates"],
                    key=lambda x: ((x.get("company") or ""),
                                   (x.get("name") or "").lower())):
        grouped.setdefault(r.get("company") or "", []).append(r)
    return render_template("payroll/rates.html", grouped=grouped,
                           companies=DB.get("companies", []),
                           prefill_name=request.args.get("name") or "",
                           prefill_company=request.args.get("company") or "")


# ----------------------------------------------------------------- entries
@bp.route("/<company>/entries", methods=["GET", "POST"])
def entries(company):
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    if request.method == "POST":
        action = request.form.get("action") or ""
        if action == "delete":
            eid = request.form.get("id") or ""
            before = len(DB["entries"])
            DB["entries"] = [e for e in DB["entries"]
                             if e.get("id") != eid]
            if len(DB["entries"]) < before:
                save_db()
                flash("Entry removed.")
        else:
            employee = (request.form.get("employee") or "").strip()
            day = _parse_entry_date(request.form.get("date"))
            try:
                hours = float(request.form.get("payroll_hours") or 0)
            except ValueError:
                hours = 0
            entry = {
                "company": co["name"], "employee": employee, "date": day,
                "payroll_hours": round(hours, 2),
                "clock_in": (request.form.get("clock_in") or "").strip(),
                "clock_out": (request.form.get("clock_out") or "").strip(),
                "clock_out_date": None, "gross_hours": None,
                "site": (request.form.get("site") or "").strip(),
                "notes": (request.form.get("notes") or "").strip(),
            }
            if not employee or not day:
                flash("Employee and date are required.")
            elif action == "update":
                eid = request.form.get("id") or ""
                for e in DB["entries"]:
                    if e.get("id") == eid:
                        # Keep raw values the form does not edit
                        # (e.g. a clock-out date that disagrees with the
                        # entry date) — never silently "fix" them.
                        entry["clock_out_date"] = e.get("clock_out_date")
                        entry["gross_hours"] = e.get("gross_hours")
                        e.update(entry)
                        save_db()
                        flash("Entry updated.")
                        break
            else:
                entry["id"] = uuid.uuid4().hex[:12]
                DB["entries"].append(entry)
                save_db()
                flash(f"Entry added: {employee}, {day}, {hours}h.")
        return redirect(url_for(".entries", company=co["slug"],
                                **{"from": start.isoformat(),
                                   "to": end.isoformat()}))
    rows = [e for e in DB.get("entries", [])
            if e.get("company") == co["name"]
            and start.isoformat() <= (e.get("date") or "") <= end.isoformat()]
    rows.sort(key=lambda e: ((e.get("date") or ""),
                             (e.get("employee") or "").lower()))
    edit_id = request.args.get("edit") or ""
    edit_entry = next((e for e in DB["entries"] if e.get("id") == edit_id),
                      None)
    names = sorted({r["name"] for r in DB.get("rates", [])
                    if r.get("company") == co["name"]})
    unmatched = find_unmatched(
        [e for e in DB.get("entries", [])
         if start.isoformat() <= (e.get("date") or "") <= end.isoformat()],
        DB.get("rates", []), DB.get("aliases", []), co["name"])
    return render_template(
        "payroll/entries.html", company=co, start=start.isoformat(),
        end=end.isoformat(), rows=rows, edit_entry=edit_entry,
        names=names, unmatched=unmatched,
        alias_map=_alias_map(co),
        aliases=[a for a in DB.get("aliases", [])
                 if (a.get("company") or "").strip() == co["name"]])


# ------------------------------------------------------------------ import
@bp.post("/<company>/import")
def import_csv(company):
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("Choose a CSV file to import.")
        return redirect(url_for(".entries", company=co["slug"],
                                **{"from": start.isoformat(),
                                   "to": end.isoformat()}))
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("Could not read the file — it must be UTF-8 CSV.")
        return redirect(url_for(".entries", company=co["slug"],
                                **{"from": start.isoformat(),
                                   "to": end.isoformat()}))
    reader = csv.DictReader(io.StringIO(text))
    existing = {_entry_key(e) for e in DB.get("entries", [])}
    imported = skipped = bad = 0
    for raw in reader:
        row = {_norm_header(k): (v or "").strip()
               for k, v in (raw or {}).items() if k is not None}
        employee = row.get("employee", "")
        day = _parse_entry_date(row.get("date", ""))
        if not employee or not day:
            bad += 1
            continue
        try:
            hours = float(row.get("payrollhours") or 0)
        except ValueError:
            bad += 1
            continue
        try:
            gross = float(row.get("grosshours")) \
                if row.get("grosshours") not in (None, "") else None
        except ValueError:
            gross = None
        entry = {
            "id": uuid.uuid4().hex[:12], "company": co["name"],
            "employee": employee, "date": day,
            "payroll_hours": round(hours, 2),
            "clock_in": row.get("clockin", ""),
            "clock_out": row.get("clockout", ""),
            "clock_out_date": _parse_entry_date(
                row.get("clockoutdate", "")),
            "gross_hours": gross,
            "site": row.get("site", ""),
            "notes": row.get("notes", ""),
        }
        if _entry_key(entry) in existing:
            skipped += 1
            continue
        existing.add(_entry_key(entry))
        DB["entries"].append(entry)
        imported += 1
    if imported:
        save_db()
    msg = f"Imported {imported} row(s), skipped {skipped} duplicate(s)."
    if bad:
        msg += f" {bad} row(s) could not be read (missing employee/date or bad hours)."
    # Unmatched names are kept, never dropped — flag them for mapping.
    unmatched = find_unmatched(DB.get("entries", []), DB.get("rates", []),
                               DB.get("aliases", []), co["name"])
    if unmatched:
        msg += (" Names without a rate (kept as entered, pay left blank): "
                + ", ".join(unmatched)
                + ". Map them below.")
    flash(msg)
    return redirect(url_for(".entries", company=co["slug"],
                            **{"from": start.isoformat(),
                               "to": end.isoformat()}))


# ----------------------------------------------------------------- aliases
@bp.post("/<company>/aliases")
def save_alias(company):
    """Map an as-entered name to an existing rate row (or remove a map).

    The mapping is stored and applied automatically on every later
    import and payroll run. Nothing is ever auto-guessed: only the
    exact (alias + company) pair the user saved here is applied.
    """
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    action = request.form.get("action") or ""
    if action == "delete":
        aid = request.form.get("id") or ""
        before = len(DB["aliases"])
        DB["aliases"] = [a for a in DB["aliases"] if a.get("id") != aid]
        if len(DB["aliases"]) < before:
            save_db()
            flash("Name mapping removed.")
    else:
        alias = (request.form.get("alias") or "").strip()
        canonical = (request.form.get("canonical") or "").strip()
        if alias and canonical:
            known = {(r.get("name") or "").strip()
                     for r in DB.get("rates", [])
                     if (r.get("company") or "").strip() == co["name"]}
            if canonical not in known:
                flash(f"No rate row named '{canonical}' for "
                      f"{co['name']} — add the rate first.")
            else:
                for a in DB["aliases"]:
                    if ((a.get("alias") or "").strip() == alias
                            and (a.get("company") or "").strip()
                            == co["name"]):
                        a["canonical"] = canonical
                        save_db()
                        flash(f"'{alias}' is now mapped to "
                              f"'{canonical}'.")
                        break
                else:
                    DB["aliases"].append({
                        "id": "alias-" + uuid.uuid4().hex[:8],
                        "company": co["name"], "alias": alias,
                        "canonical": canonical,
                    })
                    save_db()
                    flash(f"'{alias}' is now mapped to '{canonical}'. "
                          f"Future imports map it automatically.")
        else:
            flash("Pick a rate row to map the name to.")
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/payroll/"):
        return redirect(nxt)
    return redirect(url_for(".entries", company=co["slug"],
                            **{"from": start.isoformat(),
                               "to": end.isoformat()}))


# ------------------------------------------------------- import from app
@bp.get("/<company>/import-app")
def import_app(company):
    """Pick a source + period, preview derived hours, then confirm.

    Preview-first, always: nothing is imported from this page. The
    preview re-derives rows from the source files on every load, so
    the confirm step sees exactly what the user saw."""
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    source = (request.args.get("source") or "").strip().lower()
    preview = None
    preview_url = ""
    if source in SOURCES:
        preview = build_preview(BASE_DIR, source, co["name"],
                                DB.get("rates", []), DB.get("aliases", []),
                                start, end)
        preview_url = url_for(".import_app", company=co["slug"],
                              source=source,
                              **{"from": start.isoformat(),
                                 "to": end.isoformat()})
    unmatched = sorted({r["employee"]
                        for r in (preview["rows"] if preview else [])
                        if not r.get("matched")})
    names = sorted({r["name"] for r in DB.get("rates", [])
                    if r.get("company") == co["name"]})
    return render_template(
        "payroll/import_app.html", company=co,
        start=start.isoformat(), end=end.isoformat(),
        sources=SOURCES, source=source, preview=preview,
        unmatched=unmatched, names=names, preview_url=preview_url,
        hours_rule=HOURS_RULE_TEXT)


@bp.post("/<company>/import-app/confirm")
def import_app_confirm(company):
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    source = (request.form.get("source") or "").strip().lower()
    if source not in SOURCES:
        flash("Pick a source first.")
        return redirect(url_for(".import_app", company=co["slug"],
                                **{"from": start.isoformat(),
                                   "to": end.isoformat()}))
    # Re-derive from the source files — never trust posted row data.
    preview = build_preview(BASE_DIR, source, co["name"],
                            DB.get("rates", []), DB.get("aliases", []),
                            start, end)
    to_add, n_dupes, unmatched = import_rows(
        preview["rows"], DB.get("entries", []), co["name"],
        DB.get("aliases", []), source_label=preview["label"])
    for e in to_add:
        e["id"] = uuid.uuid4().hex[:12]
        DB["entries"].append(e)
    if to_add:
        save_db()
    msg = (f"Imported {len(to_add)} row(s) from {preview['label']}, "
           f"skipped {n_dupes} duplicate(s).")
    if unmatched:
        names = sorted({r["employee"] for r in unmatched})
        msg += (f" {len(unmatched)} row(s) without a rate match were NOT "
                f"imported: " + ", ".join(names)
                + ". Map them on the preview and confirm again.")
    if preview["skipped"]:
        msg += (f" {len(preview['skipped'])} open shift(s) skipped "
                f"(still clocked in).")
    flash(msg)
    return redirect(url_for(".entries", company=co["slug"],
                            **{"from": start.isoformat(),
                               "to": end.isoformat()}))
