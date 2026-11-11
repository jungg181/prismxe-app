#!/usr/bin/env python3
"""Payroll — payroll calculation for any company (replaces sheet formulas).

- GET  /payroll/                        company cards + period picker
- POST /payroll/companies              add a company
- POST /payroll/companies/<slug>/delete  remove an empty company
- GET/POST /payroll/companies/<slug>/edit  edit company info (rename
  migrates entries/rates/aliases/variances/expenses to the new name)
- GET  /payroll/<company>              per-employee payroll for a period
- GET  /payroll/<company>/export.csv   CSV export of the payroll table
- GET  /payroll/<company>/export.xlsx  XLSX export (Payroll + Invoice)
- GET  /payroll/<company>/invoice      invoice view (number = prefix + today)
- GET  /payroll/<company>/invoice/pdf  invoice PDF (Hanseong sheet format)
- GET  /payroll/<company>/hours/pdf    Hours per Day PDF (Letter landscape)
- POST /payroll/<company>/invoice/expense            add an expense line
- POST /payroll/<company>/invoice/expense/<id>/delete
- GET  /payroll/rates                   list / edit rates (name + company)
- GET  /payroll/<company>/rates        list / edit rates for one company
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
import re
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (Blueprint, Response, flash, jsonify, redirect,
                   render_template, request, send_file, url_for)

from payroll_calc import (DEFAULT_OT_MULTIPLIER, DEFAULT_OT_THRESHOLD,
                          compute_jobsite_pnl, compute_payroll,
                          find_unmatched, invoice_lines, invoice_number,
                          variance_report)
from payroll_import_app import (HOURS_RULE_TEXT, SOURCES, build_preview,
                                import_rows)

bp = Blueprint("payroll", __name__, url_prefix="/payroll",
               template_folder="templates/payroll")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data_payroll.json")
COLLECTIONS = ["companies", "rates", "entries", "expenses", "settings",
               "aliases", "variances", "jobsites", "jobsite_costs"]

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

# Fixed invoice header details (from the Hanseong invoice sheet template).
INVOICE_COMPANY_NAME = "Prism Multiple Entertainment LLC"
INVOICE_SITE_ADDRESS = "255 N. 56th Place. Mesa AZ 85205"
INVOICE_CONTACT_NAME = "Jungseok Kang"
INVOICE_CONTACT_PHONE = "+1 213 905 1359"
INVOICE_CONTACT_EMAIL = "info@prismxe.com"
INVOICE_TEL_NUM = "213-905-1359"
INVOICE_BANK_TELEPHONE = "+1 562 928 2278"


def _company_bank(co):
    """Bank details for a company: the company's own bank fields when
    set, otherwise the default BANK. The deposit account can differ
    per company, so each company can override it on its Edit page."""
    return {
        "bank": (co.get("bank_name") or "").strip() or BANK["bank"],
        "branch": (co.get("bank_branch") or "").strip() or BANK["branch"],
        "routing": (co.get("bank_routing") or "").strip() or BANK["routing"],
        "account": (co.get("bank_account") or "").strip() or BANK["account"],
        "account_name": (co.get("bank_account_name") or "").strip()
                        or BANK["account_name"],
        "telephone": (co.get("bank_telephone") or "").strip()
                     or INVOICE_BANK_TELEPHONE,
        "zelle": (co.get("bank_zelle") or "").strip(),
    }


def _pdf_margin(value, default, lo, hi):
    """A per-company PDF margin (mm) from the company record: a number
    within [lo, hi], otherwise the default. The user sets it on the
    Edit Company page; it controls how much whitespace the invoice
    PDF has at the top / bottom."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v < lo or v > hi:
        return default
    return v

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


def find_jobsite(job_id):
    job_id = (job_id or "").strip()
    for j in DB.get("jobsites", []):
        if (j.get("id") or "") == job_id:
            return j
    return None


def _jobsite_summary(job):
    """P&L for one jobsite: labor revenue + labor cost from the tagged
    time entries (weekly OT allocated to the days that push the week
    over 40), other purchase costs from jobsite_costs, and the
    resulting profit + margin. Returns a dict with the pnl, the
    other-cost rows, and the totals."""
    s = _settings()
    pnl = compute_jobsite_pnl(
        DB.get("entries", []), DB.get("rates", []), job["id"],
        ot_threshold=s["ot_threshold"], ot_multiplier=s["ot_multiplier"],
        aliases=DB.get("aliases", []))
    costs = sorted((c for c in DB.get("jobsite_costs", [])
                    if c.get("jobsite_id") == job["id"]),
                   key=lambda c: (c.get("date") or "", c.get("id") or ""))
    other_total = round(sum(float(c.get("amount") or 0) for c in costs), 2)
    profit = round(pnl["revenue"] - pnl["labor_cost"] - other_total, 2)
    revenue = pnl["revenue"]
    margin = round(profit / revenue * 100, 1) if revenue else None
    return {"pnl": pnl, "costs": costs, "other_total": other_total,
            "profit": profit, "margin": margin}


def _alias_map(company):
    """{raw name: canonical name} for this company."""
    return {(a.get("alias") or "").strip(): (a.get("canonical") or "").strip()
            for a in DB.get("aliases", [])
            if (a.get("company") or "").strip() == company["name"]}


def _money(value):
    # Negative amounts (discounts / deductions) read as -$2,500.00.
    if value < 0:
        return f"-${abs(value):,.2f}"
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


# ---------------------------------------------------------------- companies
def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower())
    return slug.strip("-")


@bp.post("/companies")
def add_company():
    """Add a company from the index page. It appears on the index,
    on the Rates page, and gets its own payroll / invoice / import
    pages immediately — every other route looks companies up by slug.
    """
    name = (request.form.get("name") or "").strip()
    prefix = (request.form.get("prefix") or "").strip().upper()
    prefix_end = (request.form.get("prefix_end") or "").strip().upper()
    vendor = (request.form.get("vendor") or "").strip() or name
    phone = (request.form.get("phone") or "").strip()
    ship_to = (request.form.get("ship_to") or "").strip()
    category = (request.form.get("category") or "").strip()
    bank_name = (request.form.get("bank_name") or "").strip()
    bank_branch = (request.form.get("bank_branch") or "").strip()
    bank_routing = (request.form.get("bank_routing") or "").strip()
    bank_account = (request.form.get("bank_account") or "").strip()
    bank_account_name = (request.form.get("bank_account_name") or "").strip()
    bank_telephone = (request.form.get("bank_telephone") or "").strip()
    bank_zelle = (request.form.get("bank_zelle") or "").strip()
    pdf_top_margin = (request.form.get("pdf_top_margin") or "").strip()
    pdf_bottom_margin = (request.form.get("pdf_bottom_margin") or "").strip()
    def _add_fail(msg):
        # Back to the index with the Add Company form open, so the
        # user can fix the error instead of finding it collapsed.
        flash(msg)
        return redirect(url_for(".index", show_add=1))
    if not name:
        return _add_fail("Company name is required.")
    slug = _slugify(name)
    if not slug:
        return _add_fail("Company name must contain letters or numbers.")
    if find_company(slug) is not None:
        return _add_fail(f"A company named {name} already exists.")
    if not prefix or not prefix.isalnum() or len(prefix) > 6:
        return _add_fail("Invoice prefix is required: letters and numbers "
                         "only, up to 6 characters.")
    if any((c.get("prefix") or "").upper() == prefix
           for c in DB.get("companies", [])):
        return _add_fail(f"Invoice prefix {prefix} is already used by "
                         "another company.")
    if prefix_end and (not prefix_end.isalnum() or len(prefix_end) > 6):
        return _add_fail("Prefix End is optional: letters and numbers only, "
                         "up to 6 characters.")
    DB.setdefault("companies", []).append({
        "name": name, "slug": slug, "prefix": prefix, "vendor": vendor,
        "phone": phone, "ship_to": ship_to, "category": category,
        "prefix_end": prefix_end,
        "bank_name": bank_name, "bank_branch": bank_branch,
        "bank_routing": bank_routing, "bank_account": bank_account,
        "bank_account_name": bank_account_name,
        "bank_telephone": bank_telephone, "bank_zelle": bank_zelle,
        "pdf_top_margin": pdf_top_margin,
        "pdf_bottom_margin": pdf_bottom_margin,
    })
    save_db()
    flash(f"Company {name} added. Add its rates under Rates, then import "
          "time entries.")
    return redirect(url_for(".index"))


@bp.post("/companies/<slug>/delete")
def delete_company(slug):
    """Remove a company — only when it has no entries, rates, aliases,
    variances or expenses, so payroll history can never be deleted
    by accident."""
    co = find_company(slug)
    if co is None:
        flash("Unknown company.")
        return redirect(url_for(".index"))
    name = co["name"]
    in_use = (
        any(e.get("company") == name for e in DB.get("entries", []))
        or any(r.get("company") == name for r in DB.get("rates", []))
        or any(a.get("company") == name for a in DB.get("aliases", []))
        or any(v.get("company") == name for v in DB.get("variances", []))
        or any(x.get("company") == name for x in DB.get("expenses", []))
    )
    if in_use:
        flash(f"Cannot remove {name}: it still has entries, rates or "
              "mappings. Remove those first.")
        return redirect(url_for(".index"))
    DB["companies"] = [c for c in DB.get("companies", []) if c is not co]
    save_db()
    flash(f"Company {name} removed.")
    return redirect(url_for(".index"))


@bp.route("/companies/<slug>/edit", methods=["GET", "POST"])
def edit_company(slug):
    """Edit a company's info. Renaming also migrates every
    entry/rate/alias/variance/expense stored under the old company
    name to the new name (all of them key off the NAME, not the
    slug), and recomputes the slug — so payroll history follows
    the rename instead of being orphaned."""
    co = find_company(slug)
    if co is None:
        flash("Unknown company.")
        return redirect(url_for(".index"))
    if request.method == "GET":
        return render_template("payroll/edit_company.html", company=co)
    old_name = co["name"]
    name = (request.form.get("name") or "").strip()
    prefix = (request.form.get("prefix") or "").strip().upper()
    prefix_end = (request.form.get("prefix_end") or "").strip().upper()
    vendor = (request.form.get("vendor") or "").strip() or name
    phone = (request.form.get("phone") or "").strip()
    ship_to = (request.form.get("ship_to") or "").strip()
    category = (request.form.get("category") or "").strip()
    bank_name = (request.form.get("bank_name") or "").strip()
    bank_branch = (request.form.get("bank_branch") or "").strip()
    bank_routing = (request.form.get("bank_routing") or "").strip()
    bank_account = (request.form.get("bank_account") or "").strip()
    bank_account_name = (request.form.get("bank_account_name") or "").strip()
    bank_telephone = (request.form.get("bank_telephone") or "").strip()
    bank_zelle = (request.form.get("bank_zelle") or "").strip()
    pdf_top_margin = (request.form.get("pdf_top_margin") or "").strip()
    pdf_bottom_margin = (request.form.get("pdf_bottom_margin") or "").strip()
    if not name:
        flash("Company name is required.")
        return render_template("payroll/edit_company.html", company=co)
    new_slug = _slugify(name)
    if not new_slug:
        flash("Company name must contain letters or numbers.")
        return render_template("payroll/edit_company.html", company=co)
    other = [c for c in DB.get("companies", []) if c is not co]
    if any((c.get("slug") or "").lower() == new_slug for c in other):
        flash(f"A company named {name} already exists.")
        return render_template("payroll/edit_company.html", company=co)
    if not prefix or not prefix.isalnum() or len(prefix) > 6:
        flash("Invoice prefix is required: letters and numbers only, "
              "up to 6 characters.")
        return render_template("payroll/edit_company.html", company=co)
    if any((c.get("prefix") or "").upper() == prefix for c in other):
        flash(f"Invoice prefix {prefix} is already used by another company.")
        return render_template("payroll/edit_company.html", company=co)
    if prefix_end and (not prefix_end.isalnum() or len(prefix_end) > 6):
        flash("Prefix End is optional: letters and numbers only, "
              "up to 6 characters.")
        return render_template("payroll/edit_company.html", company=co)
    renamed = name != old_name
    co["name"] = name
    co["slug"] = new_slug
    co["prefix"] = prefix
    co["prefix_end"] = prefix_end
    co["vendor"] = vendor
    co["phone"] = phone
    co["ship_to"] = ship_to
    co["category"] = category
    co["bank_name"] = bank_name
    co["bank_branch"] = bank_branch
    co["bank_routing"] = bank_routing
    co["bank_account"] = bank_account
    co["bank_account_name"] = bank_account_name
    co["bank_telephone"] = bank_telephone
    co["bank_zelle"] = bank_zelle
    co["pdf_top_margin"] = pdf_top_margin
    co["pdf_bottom_margin"] = pdf_bottom_margin
    if renamed:
        for coll in ("entries", "rates", "aliases", "variances",
                     "expenses"):
            for row in DB.get(coll, []):
                if row.get("company") == old_name:
                    row["company"] = name
    save_db()
    flash(f"Company {name} updated."
          + (" Entries, rates and mappings moved to the new name."
             if renamed else ""))
    return redirect(url_for(".index"))


# ---------------------------------------------------------------- payroll
@bp.get("/<company>")
def company_payroll(company):
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    result = _payroll_for(co, start, end)
    variances = _variances_for(co, result)
    # Day-by-day entries per employee in this period, so the payroll
    # page can show (and fix) the hours behind each worker's total.
    # EVERY date in the period is listed in order, even days the
    # worker did not work — those show 0h. A 0h day has no entry id;
    # saving hours there creates the entry (add_day_hours).
    # Only the entry id + hours + clock times are needed; the edit
    # posts to update_entry_hours which changes payroll_hours,
    # clock_in and clock_out together.
    from datetime import timedelta as _td, date as _date
    all_dates = []
    _d = start
    while _d <= end:
        all_dates.append(_d.isoformat())
        _d += _td(days=1)
    _by_emp_date = {}
    for e in DB.get("entries", []):
        if e.get("company") != co["name"]:
            continue
        d = e.get("date") or ""
        if not (start.isoformat() <= d <= end.isoformat()):
            continue
        _by_emp_date.setdefault((e.get("employee") or "", d), []).append(e)
    days_by_employee = {}
    for r in result["rows"]:
        emp = r["employee"]
        day_list = []
        for d in all_dates:
            # Weekday label, e.g. Mon — shown between the date and hours.
            try:
                _wd = _date.fromisoformat(d).strftime("%a")
            except ValueError:
                _wd = ""
            es = _by_emp_date.get((emp, d), [])
            if not es:
                day_list.append({"id": None, "date": d, "weekday": _wd,
                                 "payroll_hours": 0,
                                 "clock_in": "", "clock_out": ""})
            else:
                for e in es:
                    day_list.append({
                        "id": e.get("id"),
                        "date": d, "weekday": _wd,
                        "payroll_hours": e.get("payroll_hours"),
                        "clock_in": e.get("clock_in") or "",
                        "clock_out": e.get("clock_out") or "",
                    })
        # Newest date first — the latest days are what the user checks.
        days_by_employee[emp] = day_list[::-1]
    return render_template(
        "payroll/company.html", company=co, start=start.isoformat(),
        end=end.isoformat(), result=result, variances=variances,
        days_by_employee=days_by_employee,
        money=_money, fmt_hours=_fmt_hours)


@bp.post("/<company>/entries/<entry_id>/hours")
def update_entry_hours(company, entry_id):
    """Fix ONE day's hours AND clock times from the payroll page.
    payroll_hours, clock_in and clock_out are all changed together;
    site and notes are left exactly as they were. Totals (incl.
    weekly OT) are recomputed from the entries on the next payroll
    view, so the fix flows through. Clock times are stored exactly
    as typed (e.g. 7:00 AM) — they are display/record values, the
    payroll math uses payroll_hours only."""
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    try:
        hours = float(request.form.get("payroll_hours") or 0)
    except (TypeError, ValueError):
        hours = 0
    hours = max(0.0, round(hours, 2))
    clock_in = (request.form.get("clock_in") or "").strip()
    clock_out = (request.form.get("clock_out") or "").strip()
    for e in DB["entries"]:
        if e.get("id") == entry_id and e.get("company") == co["name"]:
            e["payroll_hours"] = hours
            e["clock_in"] = clock_in
            e["clock_out"] = clock_out
            save_db()
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify({"ok": True})
            flash(f"Updated for {e.get('employee')} "
                  f"on {e.get('date')}: {hours}h"
                  + (f" ({clock_in} - {clock_out})"
                     if clock_in or clock_out else "")
                  + ".")
            break
    return redirect(url_for(".company_payroll", company=co["slug"],
                            **{"from": start.isoformat(),
                               "to": end.isoformat()}))


@bp.post("/<company>/entries/add-day-hours")
def add_day_hours(company):
    """Create a day's hours from the payroll page, for a date the
    worker has no entry on (a 0h day in the Days list). Created when
    the hours are over 0, or when clock-in/out times are given
    (a 0h day needs no entry at all unless times are recorded).
    Site and notes are left blank. If an entry for that day appeared
    meanwhile (e.g. a second auto-save after the first one created
    it), update it instead of skipping — the endpoint is idempotent."""
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    employee = (request.form.get("employee") or "").strip()
    day = _parse_entry_date(request.form.get("date"))
    try:
        hours = float(request.form.get("payroll_hours") or 0)
    except (TypeError, ValueError):
        hours = 0
    hours = max(0.0, round(hours, 2))
    clock_in = (request.form.get("clock_in") or "").strip()
    clock_out = (request.form.get("clock_out") or "").strip()
    if employee and day and (hours > 0 or clock_in or clock_out):
        existing = next((e for e in DB["entries"]
                         if e.get("company") == co["name"]
                         and e.get("employee") == employee
                         and e.get("date") == day), None)
        if existing is not None:
            existing["payroll_hours"] = hours
            existing["clock_in"] = clock_in
            existing["clock_out"] = clock_out
            save_db()
            msg = (f"Updated for {employee} on {day}: {hours}h"
                   + (f" ({clock_in} - {clock_out})"
                      if clock_in or clock_out else "")
                   + ".")
        else:
            DB["entries"].append({
                "id": uuid.uuid4().hex[:12], "company": co["name"],
                "employee": employee, "date": day,
                "payroll_hours": hours,
                "clock_in": clock_in, "clock_out": clock_out,
                "clock_out_date": None,
                "gross_hours": None, "site": "", "notes": "",
            })
            save_db()
            msg = (f"Hours added for {employee} on {day}: {hours}h"
                   + (f" ({clock_in} - {clock_out})"
                      if clock_in or clock_out else "")
                   + ".")
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": True})
        flash(msg)
    elif request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect(url_for(".company_payroll", company=co["slug"],
                            **{"from": start.isoformat(),
                               "to": end.isoformat()}))


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
    number = invoice_number(co["prefix"], datetime.now(PHOENIX).date(),
                            co.get("prefix_end") or "")
    return render_template(
        "payroll/invoice.html", company=co, start=start.isoformat(),
        end=end.isoformat(), number=number, inv=inv, bank=_company_bank(co),
        approved_by=APPROVED_BY, money=_money)


def _invoice_pdf_bytes(co, start, end, number, inv, bank):
    """Build the invoice PDF matching the Hanseong sheet format (Letter
    portrait, compact so a 20-worker invoice fits on one page): PRISM
    logo + blue Invoice title, contact block (email on the same line as
    the phone, to its right), dark-navy VENDOR / SHIP TO bars, P.O.
    table, items table (NO/DESCRIPTION/UNIT PRICE/UOM/QTY/TOTAL,
    Regular (RH) then Overtime (OT) per employee), Bank Account Details
    box + SUB TOTAL / FREIGHT / SHIPPING / HANDLING / TOTAL stack,
    Approved by with signature line. The (RH)/(OT) legend sits side by
    side on one line. The DPO reference footer is intentionally left
    out. The bottom section is ALWAYS pinned to the bottom of the page
    (its position is computed from the bottom margin upward), so a short
    invoice has the bank box + totals + approval at the very bottom and
    a long invoice never overlaps them; very long invoices flow to
    page 2. Top/bottom margins come from the company record
    (pdf_top_margin / pdf_bottom_margin, mm; defaults 5 / 5)."""
    from fpdf import FPDF

    NAVY = (10, 48, 65)
    BLUE = (51, 51, 153)
    LEFT = 17.8
    RIGHT = 197.8
    W = RIGHT - LEFT  # 180.0
    # Compact type: 8pt body, 4.5mm header rows, 3.6mm item rows, tight
    # section gaps — so a 20-worker invoice still fits on one page.
    BODY = 8
    ROW = 4.5
    DATA_ROW = 3.6
    PAGE_H = 279.4
    TOP = _pdf_margin(co.get("pdf_top_margin"), 5, 0, 20)
    BOTTOM_MARGIN = _pdf_margin(co.get("pdf_bottom_margin"), 5, 5, 30)

    def money(v):
        # Negative amounts (discounts / deductions) read as -$2,500.00.
        if v < 0:
            return f"-${abs(v):,.2f}"
        return f"${v:,.2f}"

    def qty(v):
        return f"{v:.2f}"

    def md(d):
        return f"{d.month}/{d.day}"

    def mdy(d):
        return f"{d.month}/{d.day}/{d.year}"

    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=True, margin=BOTTOM_MARGIN)
    pdf.set_margins(LEFT, TOP, 17.8)
    pdf.add_page()
    # Thin grid lines (0.1mm instead of the 0.2mm default).
    pdf.set_line_width(0.1)

    # --- Top row: logo (left) + Invoice title (blue, right) ---
    logo_path = os.path.join(BASE_DIR, "static", "prism-logo.png")
    if os.path.exists(logo_path):
        pdf.image(logo_path, x=LEFT, y=TOP, w=46)
    pdf.set_xy(124, TOP + 0.5)
    pdf.set_font("helvetica", "B", 20)
    pdf.set_text_color(*BLUE)
    pdf.cell(60, 9, "Invoice", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    # --- Contact block (left) / NUMBER + CATEGORY + duration (right) ---
    # 4 mm line height. The email sits on the SAME line as the phone,
    # to its right — so the block is 4 lines, not 5.
    y0 = TOP + 13
    pdf.set_xy(LEFT, y0)
    pdf.set_font("helvetica", "B", BODY)
    pdf.cell(95, 4, INVOICE_COMPANY_NAME + ".", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", BODY)
    pdf.cell(95, 4, INVOICE_SITE_ADDRESS, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(95, 4, INVOICE_CONTACT_NAME, new_x="LMARGIN", new_y="NEXT")
    # Phone + email on one line: phone left, email right after it.
    phone_txt = f"Mobile: {INVOICE_CONTACT_PHONE}"
    pw = pdf.get_string_width(phone_txt)
    pdf.cell(pw, 4, phone_txt, new_x="RIGHT", new_y="TOP")
    pdf.cell(4, 4, "", new_x="RIGHT", new_y="TOP")
    pdf.set_text_color(*BLUE)
    pdf.set_font("helvetica", "U", BODY)
    pdf.cell(95 - pw - 4, 4, INVOICE_CONTACT_EMAIL,
            link=f"mailto:{INVOICE_CONTACT_EMAIL}",
            new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", BODY)
    pdf.set_text_color(0, 0, 0)

    pdf.set_xy(115, y0)
    pdf.set_font("helvetica", "B", BODY)
    pdf.cell(83, 4, f"NUMBER : {number}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(115, y0 + 4)
    pdf.set_font("helvetica", "", BODY)
    cat = (co.get("category") or "").strip().replace(" / ", " ")
    pdf.cell(83, 4, f"CATEGORY : {cat}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(115, y0 + 13)
    pdf.cell(83, 4, f"Payroll duration. ({md(start)}~{md(end)})",
            new_x="LMARGIN", new_y="NEXT")

    # --- VENDOR / SHIP TO dark bars (tight: right after the contact block) ---
    yb = TOP + 31
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", BODY)
    pdf.set_xy(LEFT, yb)
    pdf.cell(84, 6, " VENDOR", fill=True, new_x="RIGHT", new_y="TOP")
    pdf.set_xy(122, yb)
    pdf.cell(76, 6, " SHIP TO", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    # TO / COMPANY / ADDRESS (left) + ship-to value (right)
    pdf.set_xy(LEFT, yb + 6.5)
    pdf.set_font("helvetica", "B", BODY)
    pdf.cell(84, 4, "TO :", new_x="RIGHT", new_y="TOP")
    pdf.set_xy(122, yb + 6.5)
    pdf.set_font("helvetica", "", BODY)
    pdf.cell(76, 4, (co.get("ship_to") or "").strip(),
            new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(LEFT, yb + 10.5)
    pdf.set_font("helvetica", "B", BODY)
    pdf.cell(84, 4, f"COMPANY : {(co.get('vendor') or '').strip()}",
            new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(LEFT, yb + 14.5)
    pdf.cell(84, 4, "ADDRESS :", new_x="LMARGIN", new_y="NEXT")

    # --- TEL NUM + RH/OT legend (side by side on ONE line) ---
    # TEL NUM is the vendor's phone: the company's own phone when set
    # (a lone "-" is a deliberate "no number yet" and shows as "-"),
    # otherwise the default number.
    tel_num = (co.get("phone") or "").strip() or INVOICE_TEL_NUM
    yt = TOP + 50
    pdf.set_xy(LEFT, yt)
    pdf.set_font("helvetica", "B", BODY)
    pdf.cell(60, 4, f"TEL NUM : {tel_num}",
            new_x="LMARGIN", new_y="NEXT")
    # (RH) and (OT) next to each other on the same line.
    pdf.set_xy(124, yt)
    pdf.set_font("helvetica", "", BODY)
    rh_txt = "(RH) REGULAR HR."
    rh_w = pdf.get_string_width(rh_txt)
    pdf.cell(rh_w, 4, rh_txt, new_x="RIGHT", new_y="TOP")
    pdf.cell(4, 4, "", new_x="RIGHT", new_y="TOP")
    pdf.cell(60, 4, "(OT) OVER TIME HR", new_x="LMARGIN", new_y="NEXT")

    # --- P.O. table (tight: right after TEL NUM / legend) ---
    yp = TOP + 55
    po_cols = [("P.O. DATE", 45), ("REQUISITIONER", 45),
               ("SHIPPED VIA", 30), ("F.O.B. POINT", 30),
               ("PROJECT NAME", 30)]
    pdf.set_xy(LEFT, yp)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", BODY)
    for i, (label, cw) in enumerate(po_cols):
        last = i == len(po_cols) - 1
        pdf.cell(cw, ROW, label, border=1, align="C", fill=True,
                new_x="LMARGIN" if last else "RIGHT",
                new_y="NEXT" if last else "TOP")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("helvetica", "", BODY)
    po_vals = [(f"{mdy(start)}~{mdy(end)}", 45, "C"),
               (INVOICE_CONTACT_NAME, 45, "C"), ("-", 30, "C"),
               ("", 30, "C"), ("", 30, "C")]
    pdf.set_x(LEFT)
    for i, (txt, cw, al) in enumerate(po_vals):
        last = i == len(po_vals) - 1
        pdf.cell(cw, ROW, txt, border=1, align=al,
                new_x="LMARGIN" if last else "RIGHT",
                new_y="NEXT" if last else "TOP")

    # --- Items table (dark header; rows flow across pages if long) ---
    yi = TOP + 65
    cols = [("NO", 13.8, "C"), ("DESCRIPTION", 60.4, "C"),
            ("UNIT PRICE", 31.9, "C"), ("UOM", 14.8, "C"),
            ("QTY", 16.2, "C"), ("TOTAL", 42.9, "C")]
    pdf.set_xy(LEFT, yi)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", BODY)
    for i, (label, cw, al) in enumerate(cols):
        last = i == len(cols) - 1
        pdf.cell(cw, ROW, label, border=1, align=al, fill=True,
                new_x="LMARGIN" if last else "RIGHT",
                new_y="NEXT" if last else "TOP")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("helvetica", "", BODY)
    emp_no = 0
    for line in inv["lines"]:
        if line["type"] == "Regular":
            emp_no += 1
            no = f"A{emp_no}"
            desc = line["employee"]
            tag = "(RH)"
            # RH row: top border only (no bottom) -- it groups with the
            # OT row below, so the two rows of one worker have no line
            # between them.
            bdr = "LTR"
        else:
            no = ""
            desc = line["employee"]
            tag = "(OT)"
            # OT row: bottom border only (no top).
            bdr = "LBR"
        # NO
        pdf.set_x(LEFT)
        pdf.cell(13.8, DATA_ROW, no, border=bdr, align="C",
                new_x="RIGHT", new_y="TOP")
        # DESCRIPTION: name left, (RH)/(OT) right
        x_desc = pdf.get_x()
        y_row = pdf.get_y()
        pdf.cell(60.4, DATA_ROW, f" {desc}", border=bdr, align="L",
                new_x="RIGHT", new_y="TOP")
        # overlay the tag right-aligned inside the description column
        pdf.set_xy(x_desc + 60.4 - 14, y_row)
        pdf.cell(14, DATA_ROW, tag, align="R", new_x="RIGHT", new_y="TOP")
        pdf.cell(31.9, DATA_ROW, money(line["unit"]), border=bdr, align="R",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(14.8, DATA_ROW, "1", border=bdr, align="C",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(16.2, DATA_ROW, qty(line["qty"]), border=bdr, align="R",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(42.9, DATA_ROW, money(line["total"]), border=bdr, align="R",
                new_x="LMARGIN", new_y="NEXT")
    for ex in inv["expenses"]:
        pdf.set_x(LEFT)
        pdf.cell(13.8, DATA_ROW, "", border=1, new_x="RIGHT", new_y="TOP")
        pdf.cell(60.4, DATA_ROW, f" {ex['description']}", border=1, align="L",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(31.9, DATA_ROW, money(ex["amount"]), border=1, align="R",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(14.8, DATA_ROW, "1", border=1, align="C",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(16.2, DATA_ROW, "1.00", border=1, align="R",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(42.9, DATA_ROW, money(ex["amount"]), border=1, align="R",
                new_x="LMARGIN", new_y="NEXT")
    if inv["excluded"]:
        pdf.set_font("helvetica", "", 8)
        pdf.set_x(LEFT)
        pdf.cell(W, 4, "Not billed: " + ", ".join(inv["excluded"]) +
                " - no rate.", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", BODY)

    # --- Bottom section: ALWAYS pinned to the page bottom. ---
    # Its top (yb2) is computed from the bottom margin upward:
    # approval underline at PAGE_H - BOTTOM_MARGIN, approval text 7mm
    # above it, a 6mm gap, then the bank box. The table is filled down
    # to yb2, so a short invoice has the bottom at the very bottom and
    # a long invoice never overlaps it. If the table runs past yb2 on
    # this page, the bottom moves to a fresh page (also pinned).
    bank_lines = [
        ("Bank Name: ", bank["bank"]),
        ("Branch Name: ", bank["branch"]),
        ("Bank Telephone No.: ", bank.get("telephone") or ""),
        ("Account Name: ", bank["account_name"]),
        ("Routing #: ", bank["routing"]),
        ("Account #: ", bank["account"]),
        ("Zelle: ", bank.get("zelle") or ""),
    ]
    box_h = 4 + 3.4 * len(bank_lines)
    approval_underline_y = PAGE_H - BOTTOM_MARGIN
    ya = approval_underline_y - 7
    yb2 = ya - 6 - (box_h + 1)
    y_after = pdf.get_y()
    if y_after > yb2:
        # Table runs past where the bottom must start: fresh page.
        pdf.add_page()
        y_after = pdf.get_y()
    # (No border is drawn in the empty space between the table and
    # the bottom section: it stays blank white, so the bottom sits at
    # the very bottom of the page with no stray grid lines.)
    # Bank box (left)
    pdf.set_xy(LEFT + 2, yb2)
    pdf.set_font("helvetica", "BU", BODY)
    pdf.cell(76, 4, "Bank Account Details:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 8)
    for label, val in bank_lines:
        pdf.set_x(LEFT + 2)
        pdf.set_font("helvetica", "B", 8)
        lw = pdf.get_string_width(label)
        pdf.cell(lw, 3.4, label, new_x="RIGHT", new_y="TOP")
        pdf.set_font("helvetica", "", 8)
        pdf.cell(76 - lw, 3.4, val, new_x="LMARGIN", new_y="NEXT")
    # box border around the bank block
    pdf.rect(LEFT, yb2, 80, box_h + 1, style="D")
    # Totals stack (right)
    pdf.set_xy(118, yb2)
    pdf.set_font("helvetica", "B", BODY)
    totals_rows = [
        ("SUB TOTAL", money(inv["sub_total"]), True),
        ("FREIGHT", "", False),
        ("SHIPPING", "", False),
        ("HANDLING", "", False),
        ("TOTAL", money(inv["total"]), True),
    ]
    for label, amt, bold in totals_rows:
        pdf.set_font("helvetica", "B" if bold else "", BODY)
        pdf.set_x(118)
        pdf.cell(37, ROW, label, border=1, align="R",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(43, ROW, amt, border=1, align="R",
                new_x="LMARGIN", new_y="NEXT")

    # --- Approved by + signature line (pinned: underline at the
    # bottom margin) ---
    pdf.set_xy(LEFT, ya)
    pdf.set_font("helvetica", "B", BODY)
    appr = (f"Approved by Prism ME LLC. Chief Executive Officer "
            f"{APPROVED_BY}")
    tw = pdf.get_string_width(appr)
    cx = (LEFT + RIGHT - tw) / 2
    pdf.set_x(cx)
    pdf.cell(tw, 5, appr, new_x="LMARGIN", new_y="NEXT")
    pdf.line(cx, approval_underline_y, cx + tw, approval_underline_y)

    return bytes(pdf.output())

def _hours_pdf_bytes(co, start, end, result):
    """Build the Payroll Recap PDF for one company + period from a
    compute_payroll result (rows carry days / days_ot; result carries
    days + weekdays). One week per page."""
    from fpdf import FPDF
    from datetime import date as _date, timedelta

    LEFT = 10.0
    RIGHT = 269.4
    PAGE_H = 215.9
    TOP = 10.0
    BOTTOM = 10.0
    ROW_H = 8.0
    TITLE_H = 8.0
    # Column widths (mm). Day columns fill the remaining width.
    NAME_W = 34.0
    SHIFT_W = 20.0
    POS_W = 16.0
    WTOTAL_W = 17.0
    REG_W = 16.0
    OT_W = 16.0
    INVREG_W = 16.0
    INVOT_W = 16.0
    INVTOT_W = 19.0
    FIXED_W = (NAME_W + SHIFT_W + POS_W + WTOTAL_W + REG_W + OT_W
               + INVREG_W + INVOT_W + INVTOT_W)
    DAY_W = (RIGHT - LEFT - FIXED_W) / 7.0
    YELLOW = (255, 255, 0)
    RED = (220, 0, 0)
    NAVY = (10, 48, 65)
    MUTED = (100, 116, 139)

    def hrs1(v):
        """Hours with at least one decimal ('8.0', '11.5', '10.75')."""
        if v is None or v == 0:
            return ""
        s = f"{v:.2f}".rstrip("0")
        if s.endswith("."):
            s += "0"
        return s

    def hrs2(v):
        """Hours with two decimals ('40.00', '7.50')."""
        if v is None:
            return ""
        return f"{v:.2f}"

    def money(v):
        """Money with $ and commas ('$2,818.75')."""
        if v is None:
            return ""
        return f"${v:,.2f}"

    def mdy_short(d):
        """MM/DD for the first date of the pay-period label."""
        return d.strftime("%m/%d")

    def mdy_short_y(d):
        """MM/DD/YY for the second date of the pay-period label."""
        return d.strftime("%m/%d/%y")

    days = result["days"]
    weekdays = result["weekdays"]
    rows = result["rows"]

    pdf = FPDF(orientation="L", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(LEFT, TOP, 10.0)
    pdf.set_line_width(0.1)

    # Column x positions (fixed layout; day columns use DAY_W).
    # Order: NAME, SHIFT, POSITION, MON..SUN, WEEK TOTAL, REG, OT,
    # INV REG, INV OT, INV TOTAL.
    def col_x(i):
        """(x, width) for column i of the recap table."""
        x = LEFT
        widths = [NAME_W, SHIFT_W, POS_W]
        widths += [DAY_W] * 7
        widths += [WTOTAL_W, REG_W, OT_W, INVREG_W, INVOT_W, INVTOT_W]
        for j in range(i):
            x += widths[j]
        return x, widths[i]

    def put_text(x, y, w, txt, align="R", size=8, bold=False,
                 color=(0, 0, 0)):
        """Draw txt on one line, baseline at y, horizontally placed
        inside [x, x+w] by align."""
        if not txt:
            return
        pdf.set_font("helvetica", "B" if bold else "", size)
        pdf.set_text_color(*color)
        tw = pdf.get_string_width(txt)
        if align == "R":
            tx = x + w - 1.0 - tw
        elif align == "C":
            tx = x + (w - tw) / 2.0
        else:
            tx = x + 1.0
        pdf.text(tx, y, txt)

    def cell(x, y, w, txt, align="R", size=8, bold=False,
             color=(0, 0, 0), fill=None):
        """A bordered ROW_H cell with a single vertically-centered line."""
        if fill is not None:
            pdf.set_fill_color(*fill)
            pdf.rect(x, y, w, ROW_H, style="F")
        pdf.rect(x, y, w, ROW_H)
        put_text(x, y + 5.3, w, txt, align=align, size=size, bold=bold,
                 color=color)

    def _parse_day(s):
        return _date.fromisoformat(s)

    def _week_label(n):
        return f"WEEK {n} (HOURS)"

    def draw_week_page(week_days, week_no):
        """One week's Payroll Recap page. week_days are the dates of
        this week within the period."""
        first = _parse_day(week_days[0])
        last = _parse_day(week_days[-1])
        pay_period = f"{mdy_short(first)}-{mdy_short_y(last)}"
        pdf.add_page()
        # Title row: 'Payroll Recap' left, 'PAY PERIOD: ...' right.
        y = TOP
        pdf.set_xy(LEFT, y)
        pdf.set_font("helvetica", "B", 14)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(0, TITLE_H, "Payroll Recap", new_x="LMARGIN",
                new_y="NEXT")
        put_text(LEFT, y + 5.5, RIGHT - LEFT,
                 f"PAY PERIOD: {pay_period}", align="R", size=11,
                 bold=True)
        y += TITLE_H + 2.0
        # Group header row (row 1) + column header row (row 2).
        # The three INVOICE columns are tall cells spanning both rows.
        hdr1_y = y
        hdr2_y = y + ROW_H
        # EMPLOYEE NAME spans NAME+SHIFT+POSITION.
        x0, _ = col_x(0)
        x1, _ = col_x(3)
        pdf.rect(x0, hdr1_y, x1 - x0, ROW_H)
        put_text(x0, hdr1_y + 5.3, x1 - x0, "EMPLOYEE NAME",
                 align="C", size=8, bold=True)
        # WEEK N (HOURS) spans the day columns + WEEK TOTAL.
        x0, _ = col_x(3)
        x1, _ = col_x(11)
        pdf.rect(x0, hdr1_y, x1 - x0, ROW_H)
        put_text(x0, hdr1_y + 5.3, x1 - x0, _week_label(week_no),
                 align="C", size=8, bold=True)
        # TOTAL HOURS spans REG. TIME + OVER TIME.
        x0, _ = col_x(11)
        x1, _ = col_x(13)
        pdf.rect(x0, hdr1_y, x1 - x0, ROW_H)
        put_text(x0, hdr1_y + 5.3, x1 - x0, "TOTAL HOURS",
                 align="C", size=8, bold=True)
        # INVOICE columns: tall cells spanning both header rows.
        inv_labels = ["INVOICE RATE (REG)", "INVOICE RATE (OT)",
                      "INVOICE TOTAL"]
        for k, label in enumerate(inv_labels):
            x, w = col_x(13 + k)
            fill = YELLOW if k == 2 else None
            if fill is not None:
                pdf.set_fill_color(*fill)
                pdf.rect(x, hdr1_y, w, 2 * ROW_H, style="F")
            pdf.rect(x, hdr1_y, w, 2 * ROW_H)
            # Center the (possibly two-line) label in the tall cell.
            pdf.set_font("helvetica", "B", 8)
            pdf.set_text_color(0, 0, 0)
            tw = pdf.get_string_width(label)
            if tw <= w - 2:
                pdf.text(x + (w - tw) / 2.0, hdr1_y + ROW_H + 1.5,
                         label)
            else:
                # Wrap into two lines.
                words = label.split()
                line1 = ""
                for wd in words:
                    trial = (line1 + " " + wd).strip()
                    if pdf.get_string_width(trial) <= w - 2:
                        line1 = trial
                    else:
                        break
                line2 = label[len(line1):].strip()
                tw1 = pdf.get_string_width(line1)
                tw2 = pdf.get_string_width(line2)
                pdf.text(x + (w - tw1) / 2.0, hdr1_y + ROW_H - 0.5,
                         line1)
                pdf.text(x + (w - tw2) / 2.0, hdr1_y + ROW_H + 4.5,
                         line2)
        # Column header row (row 2): NAME / SHIFT / POSITION / days /
        # WEEK TOTAL / REG. TIME / OVER TIME.
        col_labels = ["NAME", "SHIFT", "POSITION"]
        for j, lab in enumerate(col_labels):
            x, w = col_x(j)
            cell(x, hdr2_y, w, lab, align="C", size=8, bold=True)
        for j, d in enumerate(week_days):
            x, w = col_x(3 + j)
            pdf.rect(x, hdr2_y, w, ROW_H)
            put_text(x, hdr2_y + 3.3, w, weekdays.get(d, ""),
                     align="C", size=7, color=MUTED)
            put_text(x, hdr2_y + 7.3, w, str(_parse_day(d).day),
                     align="C", size=8, bold=True, color=RED)
        for j, lab in enumerate(["WEEK TOTAL", "REG. TIME",
                                 "OVER TIME"]):
            x, w = col_x(10 + j)
            cell(x, hdr2_y, w, lab, align="C", size=7, bold=True)
        y = hdr2_y + ROW_H
        # Data rows + column totals.
        day_tot = {d: 0.0 for d in week_days}
        wtotal_sum = 0.0
        reg_sum = 0.0
        ot_sum = 0.0
        inv_sum = 0.0
        for r in rows:
            w_tot = 0.0
            w_ot = 0.0
            for d in week_days:
                t = r["days"].get(d) or 0.0
                o = r["days_ot"].get(d) or 0.0
                day_tot[d] += t
                w_tot += t
                w_ot += o
            w_reg = round(w_tot - w_ot, 2)
            wtotal_sum += w_tot
            reg_sum += w_reg
            ot_sum += w_ot
            br = r.get("billing")
            if br is not None:
                br = float(br)
            if br is None:
                inv_reg = inv_ot = inv_tot = None
            else:
                inv_reg = br
                inv_ot = round(br * 1.5, 2)
                inv_tot = round(w_reg * br + w_ot * br * 1.5, 2)
                inv_sum += inv_tot
            # NAME
            x, w = col_x(0)
            label = (r["employee"] if len(r["employee"]) <= 22
                     else r["employee"][:21] + "...")
            cell(x, y, w, label, align="L", size=8)
            # SHIFT / POSITION (blank — the app does not store them).
            for j in (1, 2):
                x, w = col_x(j)
                cell(x, y, w, "", align="C", size=8)
            # Day cells: total hours ('-' when not worked).
            for j, d in enumerate(week_days):
                x, w = col_x(3 + j)
                t = r["days"].get(d) or 0.0
                cell(x, y, w, hrs1(t) if t else "-", align="C",
                     size=8)
            # WEEK TOTAL / REG. TIME / OVER TIME. Blank when the
            # employee did not work this week.
            x, w = col_x(10)
            cell(x, y, w, hrs1(w_tot) if w_tot else "", align="R",
                 size=8, bold=True)
            x, w = col_x(11)
            cell(x, y, w, hrs2(w_reg) if w_tot else "", align="R",
                 size=8)
            x, w = col_x(12)
            cell(x, y, w, hrs2(w_ot) if w_tot else "", align="R",
                 size=8)
            # INVOICE RATE (REG) / (OT) / TOTAL (TOTAL column yellow).
            # The rates show even with no hours; the total is blank.
            x, w = col_x(13)
            cell(x, y, w, money(inv_reg) if inv_reg is not None else "",
                 align="R", size=8)
            x, w = col_x(14)
            cell(x, y, w, money(inv_ot) if inv_ot is not None else "",
                 align="R", size=8)
            x, w = col_x(15)
            cell(x, y, w,
                 money(inv_tot) if (inv_tot is not None and w_tot)
                 else "", align="R", size=8, bold=True, fill=YELLOW)
            y += ROW_H
        # COLUMN TOTAL row (yellow).
        x, w = col_x(0)
        cell(x, y, w, "COLUMN TOTAL", align="L", size=8, bold=True,
             fill=YELLOW)
        for j in (1, 2):
            x, w = col_x(j)
            cell(x, y, w, "", align="C", size=8, fill=YELLOW)
        for j, d in enumerate(week_days):
            x, w = col_x(3 + j)
            cell(x, y, w, hrs1(day_tot[d]), align="C", size=8,
                 bold=True, fill=YELLOW)
        x, w = col_x(10)
        cell(x, y, w, hrs1(wtotal_sum), align="R", size=8, bold=True,
             fill=YELLOW)
        x, w = col_x(11)
        cell(x, y, w, hrs2(reg_sum), align="R", size=8, bold=True,
             fill=YELLOW)
        x, w = col_x(12)
        cell(x, y, w, hrs2(ot_sum), align="R", size=8, bold=True,
             fill=YELLOW)
        for j in (13, 14):
            x, w = col_x(j)
            cell(x, y, w, "", align="R", size=8, fill=YELLOW)
        x, w = col_x(15)
        cell(x, y, w, money(inv_sum), align="R", size=8, bold=True,
             fill=YELLOW)
        return y + ROW_H

    # Group the period's days into Mon-Sun weeks.
    weeks = []
    cur = []
    cur_monday = None
    for d in days:
        dd = _parse_day(d)
        monday = dd - timedelta(days=dd.weekday())
        if cur_monday is None or monday != cur_monday:
            if cur:
                weeks.append(cur)
            cur = [d]
            cur_monday = monday
        else:
            cur.append(d)
    if cur:
        weeks.append(cur)
    if not weeks:
        weeks = [[]]

    for i, w in enumerate(weeks, start=1):
        draw_week_page(w, i)

    return bytes(pdf.output())


def _timesheets_pdf_bytes(co, start, end, result):
    """Build the per-employee timesheet PDF for one company + period
    from a compute_payroll result (rows carry days / days_ot /
    total_hours / reg_hours / ot_hours)."""
    from datetime import timedelta

    from fpdf import FPDF

    LEFT = 12.0
    RIGHT = 203.9
    PAGE_H = 279.4
    TOP = 12.0
    BOTTOM = 12.0
    ORANGE = (180, 83, 9)      # dark amber — readable on white
    MUTED = (100, 116, 139)
    NAVY = (10, 48, 65)
    HDR_FILL = (230, 235, 240)
    OUT_FILL = (243, 244, 246)
    COLS = 7
    CELL_W = (RIGHT - LEFT) / COLS

    def hrs(v):
        if v is None or v == 0:
            return ""
        return f"{v:.2f}".rstrip("0").rstrip(".")

    def mdy(d):
        return f"{d.strftime('%b')} {d.day}, {d.year}"

    rows = result["rows"]

    class _PDF(FPDF):
        pass

    pdf = _PDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(LEFT, TOP, 12.0)
    pdf.set_line_width(0.1)

    # Weeks (Mon-Sun) overlapping the period.
    first_mon = start - timedelta(days=start.weekday())
    last_sun = end + timedelta(days=(6 - end.weekday()))
    weeks = []
    d = first_mon
    while d <= last_sun:
        weeks.append(d)
        d += timedelta(days=7)
    CELL_H = 30.0 if len(weeks) <= 5 else 26.0
    WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def put_text(x, y, w, txt, align="R", size=8, bold=False,
                 color=(0, 0, 0)):
        """Draw txt on one line, baseline at y, horizontally placed
        inside [x, x+w] by align, via pdf.text() with a computed x."""
        if not txt:
            return
        pdf.set_font("helvetica", "B" if bold else "", size)
        pdf.set_text_color(*color)
        tw = pdf.get_string_width(txt)
        if align == "R":
            tx = x + w - 1.5 - tw
        elif align == "C":
            tx = x + (w - tw) / 2.0
        else:
            tx = x + 1.5
        pdf.text(tx, y, txt)

    def draw_employee_page(r):
        """One page: calendar of this employee's hours + summary +
        signature block."""
        pdf.add_page()
        y = TOP
        # Title + employee name + period.
        pdf.set_xy(LEFT, y)
        pdf.set_font("helvetica", "B", 14)
        pdf.set_text_color(*NAVY)
        pdf.cell(0, 7, f"{co['name']} - Employee Timesheet",
                new_x="LMARGIN", new_y="NEXT")
        y += 8
        pdf.set_xy(LEFT, y)
        pdf.set_font("helvetica", "B", 16)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(0, 8, r["employee"], new_x="LMARGIN", new_y="NEXT")
        y += 9
        pdf.set_xy(LEFT, y)
        pdf.set_font("helvetica", "", 16)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(0, 8, f"{mdy(start)} - {mdy(end)}",
                new_x="LMARGIN", new_y="NEXT")
        y += 10
        # Weekday header row.
        for i, wd in enumerate(WD):
            x = LEFT + i * CELL_W
            pdf.set_fill_color(*HDR_FILL)
            pdf.rect(x, y, CELL_W, 7.0, style="F")
            pdf.rect(x, y, CELL_W, 7.0)
            put_text(x, y + 5.0, CELL_W, wd, align="C", size=8,
                     bold=True)
        y += 7.0
        # Calendar cells.
        for mon in weeks:
            for i in range(COLS):
                day = mon + timedelta(days=i)
                x = LEFT + i * CELL_W
                in_period = start <= day <= end
                if not in_period:
                    pdf.set_fill_color(*OUT_FILL)
                    pdf.rect(x, y, CELL_W, CELL_H, style="F")
                    pdf.rect(x, y, CELL_W, CELL_H)
                    continue
                pdf.rect(x, y, CELL_W, CELL_H)
                # Date number, top-left, bigger (12pt) with a divider
                # line under it so the date reads separately from the
                # hours below.
                put_text(x, y + 6.5, CELL_W, str(day.day), align="L",
                         size=12, bold=True, color=(0, 0, 0))
                pdf.set_draw_color(180, 180, 180)
                pdf.set_line_width(0.1)
                pdf.line(x + 1, y + 8.5, x + CELL_W - 1, y + 8.5)
                pdf.set_draw_color(0, 0, 0)
                t = r["days"].get(day.isoformat(), 0.0) or 0.0
                o = r["days_ot"].get(day.isoformat(), 0.0) or 0.0
                if t:
                    reg = round(t - o, 2)
                    # Regular hours, large and centered in the space
                    # below the date divider.
                    reg_y = y + 8.5 + (CELL_H - 8.5) / 2 + 2.0
                    put_text(x, reg_y, CELL_W,
                             hrs(reg), align="C", size=13, bold=True)
                    # Overtime, same size as Regular (13pt), bottom-
                    # right in orange.
                    if o:
                        put_text(x, y + CELL_H - 3.0, CELL_W, hrs(o),
                                 align="R", size=13, bold=True,
                                 color=ORANGE)
            y += CELL_H
        # Summary: Regular / Overtime / Total.
        y += 6
        pdf.set_xy(LEFT, y)
        pdf.set_font("helvetica", "B", 10)
        pdf.set_text_color(0, 0, 0)
        summary = (f"Regular (R/T): {hrs(r['reg_hours'])}h    "
                   f"Overtime (O/T): {hrs(r['ot_hours'])}h    "
                   f"Total: {hrs(r['total_hours'])}h")
        pdf.cell(0, 6, summary, new_x="LMARGIN", new_y="NEXT")
        y += 10
        # Signature block, pinned to the page bottom: the Date line is
        # always at DATE_LINE_Y, so the block sits at the same place
        # on every page no matter how many calendar weeks there are.
        # Rule -> gap 7 -> statements (en 6mm/line, es 5mm/line) ->
        # gap 2 -> signature line (at +6) -> date line (at +16).
        DATE_LINE_Y = 262.0
        stmt_en = ("I confirm that the hours shown above are correct and I "
                   "agree to the contents of this document.")
        stmt_es = ("Confirmo que las horas indicadas arriba son correctas "
                   "y estoy de acuerdo con el contenido de este documento.")
        pdf.set_font("helvetica", "", 11)
        en_lines = max(1, -(-pdf.get_string_width(stmt_en)
                            // (RIGHT - LEFT)))
        pdf.set_font("helvetica", "", 9)
        es_lines = max(1, -(-pdf.get_string_width(stmt_es)
                            // (RIGHT - LEFT)))
        block_h = 25 + en_lines * 6 + es_lines * 5
        y = max(DATE_LINE_Y - block_h, y + 8)
        pdf.set_draw_color(0, 0, 0)
        pdf.line(LEFT, y, RIGHT, y)
        y += 7
        pdf.set_xy(LEFT, y)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("helvetica", "", 11)
        pdf.multi_cell(0, 6, stmt_en, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 9)
        pdf.multi_cell(0, 5, stmt_es, new_x="LMARGIN", new_y="NEXT")
        y = pdf.get_y() + 2
        # Signature line.
        pdf.set_font("helvetica", "", 9)
        pdf.set_text_color(0, 0, 0)
        pdf.set_xy(LEFT, y)
        pdf.cell(38, 6, "Employee Signature:")
        pdf.line(LEFT + 40, y + 6, LEFT + 130, y + 6)
        # Date line.
        pdf.set_xy(LEFT, y + 10)
        pdf.cell(38, 6, "Date:")
        pdf.line(LEFT + 40, y + 16, LEFT + 100, y + 16)

    for r in rows:
        draw_employee_page(r)

    return bytes(pdf.output())


@bp.get("/<company>/invoice/pdf")
def invoice_pdf(company):
    """Download the invoice as a PDF in the Hanseong sheet format.
    The invoice page can pass per-download overrides as query params
    (phone / top / bottom); they apply to this download only and do
    not change the company's saved values."""
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    result = _payroll_for(co, start, end)
    inv = invoice_lines(result["rows"],
                        _period_expenses(co, start, end),
                        ot_multiplier=_settings()["ot_multiplier"])
    number = invoice_number(co["prefix"], datetime.now(PHOENIX).date(),
                            co.get("prefix_end") or "")
    co = dict(co)
    phone = (request.args.get("phone") or "").strip()
    if phone:
        co["phone"] = phone
    top = (request.args.get("top") or "").strip()
    if top:
        co["pdf_top_margin"] = top
    bottom = (request.args.get("bottom") or "").strip()
    if bottom:
        co["pdf_bottom_margin"] = bottom
    data = _invoice_pdf_bytes(co, start, end, number, inv,
                              _company_bank(co))
    filename = f"{co['name']}_{number}.pdf"
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


@bp.get("/<company>/hours/pdf")
def hours_pdf(company):
    """Download a Payroll Recap PDF (Letter landscape), one week per
    page: employee name / shift / position, MON-SUN daily hours + week
    total, reg/OT split, invoice rate (reg, OT at 1.5x) and invoice
    total, with a yellow COLUMN TOTAL row."""
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    result = _payroll_for(co, start, end)
    data = _hours_pdf_bytes(co, start, end, result)
    filename = (f"{co['name']}_Hours_per_Day_"
                f"{start.isoformat()}_{end.isoformat()}.pdf")
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


@bp.get("/<company>/timesheets/pdf")
def timesheets_pdf(company):
    """Download one printable timesheet page per employee (Letter
    portrait): each page shows that employee's hours as a Monday-first
    calendar grid (Regular as the main number, OT below-right in
    orange), a Regular / Overtime / Total summary, and a signature
    block where the employee confirms the hours and agrees to the
    document contents."""
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    start, end = _period_args()
    result = _payroll_for(co, start, end)
    data = _timesheets_pdf_bytes(co, start, end, result)
    filename = (f"{co['name']}_Employee_Timesheets_"
                f"{start.isoformat()}_{end.isoformat()}.pdf")
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


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
        elif action == "update_all":
            # Bulk save: one Save all button posts every row's
            # staffing_<id> / billing_<id> at once, so the user does
            # not have to press Save on each row one by one.
            updated = 0
            for r in DB["rates"]:
                sid = request.form.get("staffing_" + str(r.get("id")))
                bid = request.form.get("billing_" + str(r.get("id")))
                if sid is None and bid is None:
                    continue
                try:
                    r["staffing"] = float(sid) if sid not in (None, "") else None
                except (TypeError, ValueError):
                    r["staffing"] = None
                try:
                    r["billing"] = float(bid) if bid not in (None, "") else None
                except (TypeError, ValueError):
                    r["billing"] = None
                updated += 1
            if updated:
                save_db()
                flash(f"{updated} rate(s) updated.")
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


@bp.route("/<company>/rates", methods=["GET", "POST"])
def company_rates(company):
    """Rates for ONE company only. Each company has its own rates
    page (linked from its row on the payroll index), so rates are
    edited per company instead of all mixed together."""
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    if request.method == "POST":
        action = request.form.get("action") or ""
        if action == "update":
            rid = request.form.get("id") or ""
            for r in DB["rates"]:
                if r.get("id") == rid and r.get("company") == co["name"]:
                    try:
                        r["staffing"] = float(request.form.get("staffing"))
                    except (TypeError, ValueError):
                        r["staffing"] = None
                    try:
                        r["billing"] = float(request.form.get("billing"))
                    except (TypeError, ValueError):
                        r["billing"] = None
                    save_db()
                    flash(f"Rate updated for {r['name']} ({co['name']}).")
                    break
        elif action == "update_all":
            # Bulk save: one Save all button posts every row's
            # staffing_<id> / billing_<id> at once, so the user does
            # not have to press Save on each row one by one.
            updated = 0
            for r in DB["rates"]:
                if r.get("company") != co["name"]:
                    continue
                sid = request.form.get("staffing_" + str(r.get("id")))
                bid = request.form.get("billing_" + str(r.get("id")))
                if sid is None and bid is None:
                    continue
                try:
                    r["staffing"] = float(sid) if sid not in (None, "") else None
                except (TypeError, ValueError):
                    r["staffing"] = None
                try:
                    r["billing"] = float(bid) if bid not in (None, "") else None
                except (TypeError, ValueError):
                    r["billing"] = None
                updated += 1
            if updated:
                save_db()
                flash(f"{updated} rate(s) updated for {co['name']}.")
        elif action == "add":
            name = (request.form.get("name") or "").strip()
            if name:
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
                    "company": co["name"], "staffing": staffing,
                    "billing": billing,
                })
                save_db()
                flash(f"Rate added for {name} ({co['name']}).")
            else:
                flash("Employee name is required.")
        elif action == "delete":
            rid = request.form.get("id") or ""
            before = len(DB["rates"])
            DB["rates"] = [r for r in DB["rates"]
                           if not (r.get("id") == rid
                                   and r.get("company") == co["name"])]
            if len(DB["rates"]) < before:
                save_db()
                flash("Rate removed.")
        return redirect(url_for(".company_rates", company=co["slug"]))
    rows = sorted((r for r in DB["rates"]
                   if r.get("company") == co["name"]),
                  key=lambda x: (x.get("name") or "").lower())
    # Employees who have payroll entries for this company but no rate
    # row at all — they never show up in the rates list, so the user
    # cannot tell who is missing a rate. List them here with an
    # inline Add so each one can be given a rate on the spot.
    entry_names = sorted({(e.get("employee") or "").strip()
                          for e in DB.get("entries", [])
                          if e.get("company") == co["name"]
                          and (e.get("employee") or "").strip()})
    rated = {(r.get("name") or "").strip() for r in rows}
    missing = [n for n in entry_names if n not in rated]
    return render_template("payroll/rates_company.html", company=co,
                           rates=rows, missing=missing,
                           prefill_name=request.args.get("name") or "")


# ---------------------------------------------------------------- jobsites
@bp.route("/jobsites", methods=["GET", "POST"])
def jobsites():
    """Jobsites: one per job won. Each jobsite pulls its labor revenue
    + labor cost from the time entries tagged with it (weekly OT
    allocated to the days that push the week over 40) and its other
    purchase costs from jobsite_costs; profit = revenue - labor -
    other. The list shows the P&L summary per jobsite."""
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        name = (request.form.get("name") or "").strip()
        company = (request.form.get("company") or "").strip()
        site = (request.form.get("site") or "").strip()
        if not code:
            flash("Jobsite ID is required.")
        elif any((j.get("code") or "").strip().lower() == code.lower()
                 for j in DB.get("jobsites", [])):
            flash(f"A jobsite with ID {code} already exists.")
        elif not company:
            flash("Company is required.")
        else:
            DB["jobsites"].append({
                "id": "job-" + uuid.uuid4().hex[:8], "code": code,
                "name": name or code, "company": company,
                "site": site, "status": "active",
            })
            save_db()
            flash(f"Jobsite {code} added.")
        return redirect(url_for(".jobsites"))
    summaries = []
    for j in sorted(DB.get("jobsites", []),
                    key=lambda x: (x.get("code") or "").lower()):
        s = _jobsite_summary(j)
        summaries.append({"job": j, "summary": s})
    return render_template("payroll/jobsites.html", summaries=summaries,
                           companies=DB.get("companies", []))


@bp.post("/jobsites/<job_id>/delete")
def delete_jobsite(job_id):
    job = find_jobsite(job_id)
    if job is None:
        return "Unknown jobsite", 404
    has_entries = any((e.get("jobsite_id") or "") == job["id"]
                      for e in DB.get("entries", []))
    has_costs = any((c.get("jobsite_id") or "") == job["id"]
                    for c in DB.get("jobsite_costs", []))
    if has_entries or has_costs:
        flash(f"Cannot remove {job['code']}: it still has time entries "
              "or purchase costs. Reassign them first.")
    else:
        DB["jobsites"] = [j for j in DB.get("jobsites", [])
                          if j.get("id") != job["id"]]
        save_db()
        flash(f"Jobsite {job['code']} removed.")
    return redirect(url_for(".jobsites"))


@bp.route("/jobsites/<job_id>")
def jobsite_detail(job_id):
    job = find_jobsite(job_id)
    if job is None:
        return "Unknown jobsite", 404
    s = _jobsite_summary(job)
    return render_template("payroll/jobsite.html", job=job, summary=s,
                           money=_money, fmt_hours=_fmt_hours)


@bp.post("/jobsites/<job_id>/costs")
def add_jobsite_cost(job_id):
    job = find_jobsite(job_id)
    if job is None:
        return "Unknown jobsite", 404
    desc = (request.form.get("description") or "").strip()
    day = _parse_entry_date(request.form.get("date"))
    try:
        amount = float(request.form.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if desc and amount:
        DB["jobsite_costs"].append({
            "id": "jcost-" + uuid.uuid4().hex[:8],
            "jobsite_id": job["id"], "description": desc,
            "amount": round(amount, 2), "date": day,
        })
        save_db()
        flash(f"Cost added to {job['code']}: {desc} "
              f"${_money(amount)[1:]}.")
    else:
        flash("Enter a description and an amount.")
    return redirect(url_for(".jobsite_detail", job_id=job["id"]))


@bp.post("/jobsites/<job_id>/costs/<cost_id>/delete")
def delete_jobsite_cost(job_id, cost_id):
    job = find_jobsite(job_id)
    if job is None:
        return "Unknown jobsite", 404
    before = len(DB.get("jobsite_costs", []))
    DB["jobsite_costs"] = [
        c for c in DB.get("jobsite_costs", [])
        if not (c.get("id") == cost_id
                and c.get("jobsite_id") == job["id"])]
    if len(DB["jobsite_costs"]) < before:
        save_db()
        flash("Cost removed.")
    return redirect(url_for(".jobsite_detail", job_id=job["id"]))


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
        elif action == "move_company":
            # Move one worker's entries to another company (department).
            # The user picks the destination themselves — nothing is
            # guessed. Moves ALL of that employee's entries in this
            # company (not just the shown period), because a department
            # is a property of the worker. The Note field is applied
            # to every moved entry as typed.
            employee = (request.form.get("employee") or "").strip()
            to_slug = (request.form.get("to_company") or "").strip()
            notes = (request.form.get("notes") or "").strip()
            target = find_company(to_slug) if to_slug else None
            if not employee or target is None:
                flash("Pick a company to move to.")
            elif target["name"] == co["name"]:
                flash("That is already the current company.")
            else:
                count = 0
                for e in DB["entries"]:
                    if (e.get("company") == co["name"]
                            and (e.get("employee") or "") == employee):
                        e["company"] = target["name"]
                        e["notes"] = notes
                        count += 1
                if count:
                    save_db()
                    flash(f"Moved {count} entries for {employee} "
                          f"from {co['name']} to {target['name']}.")
                else:
                    flash(f"No entries found for {employee} "
                          f"in {co['name']}.")
        elif action == "assign_jobsite":
            # Tag every entry in this company + period that has no
            # jobsite yet with the chosen jobsite. Entries already
            # tagged to another jobsite are left alone — reassign
            # those one by one with Edit.
            job = find_jobsite(request.form.get("jobsite_id") or "")
            if job is None:
                flash("Pick a jobsite first.")
            else:
                count = 0
                for e in DB["entries"]:
                    if e.get("company") != co["name"]:
                        continue
                    if not (start.isoformat() <= (e.get("date") or "")
                            <= end.isoformat()):
                        continue
                    if (e.get("jobsite_id") or ""):
                        continue
                    e["jobsite_id"] = job["id"]
                    count += 1
                if count:
                    save_db()
                    flash(f"{count} entries tagged to {job['code']}.")
                else:
                    flash("No untagged entries in this period.")
        else:
            employee = (request.form.get("employee") or "").strip()
            day = _parse_entry_date(request.form.get("date"))
            try:
                hours = float(request.form.get("payroll_hours") or 0)
            except ValueError:
                hours = 0
            jobsite_id = (request.form.get("jobsite_id") or "").strip()
            entry = {
                "company": co["name"], "employee": employee, "date": day,
                "payroll_hours": round(hours, 2),
                "clock_in": (request.form.get("clock_in") or "").strip(),
                "clock_out": (request.form.get("clock_out") or "").strip(),
                "clock_out_date": None, "gross_hours": None,
                "site": (request.form.get("site") or "").strip(),
                "notes": (request.form.get("notes") or "").strip(),
                "jobsite_id": jobsite_id or None,
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
    # Most common note per unmatched name in this company, so the
    # "Move to another company" form can prefill the Note field.
    from collections import Counter as _Counter
    unmatched_notes = {}
    for _n in unmatched:
        _notes = _Counter((e.get("notes") or "")
                          for e in DB.get("entries", [])
                          if e.get("company") == co["name"]
                          and (e.get("employee") or "") == _n)
        unmatched_notes[_n] = _notes.most_common(1)[0][0] if _notes else ""
    return render_template(
        "payroll/entries.html", company=co, start=start.isoformat(),
        end=end.isoformat(), rows=rows, edit_entry=edit_entry,
        names=names, unmatched=unmatched,
        unmatched_notes=unmatched_notes,
        companies=sorted(DB.get("companies", []),
                         key=lambda c: (c.get("name") or "").lower()),
        jobsites=sorted(DB.get("jobsites", []),
                        key=lambda j: (j.get("code") or "").lower()),
        jobsite_by_id={j.get("id"): j for j in DB.get("jobsites", [])},
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


# ----------------------------------------------------------------- employee
@bp.route("/<company>/employee", methods=["GET", "POST"])
def employee_page(company):
    """One page to edit everything about an employee in this company:
    their name, their company (department), their employee ID and their
    rate. Their time entries are listed below with an Edit link each.
    Renaming updates the rate row, the entries and the name mappings
    together; moving updates the rate row and the entries together. A
    rename is refused when the new name already has a rate row or
    entries in the target company, and an employee ID is refused when
    another worker in the target company already uses it."""
    co = find_company(company)
    if co is None:
        return "Unknown company", 404
    name = (request.args.get("name") or "").strip()
    if not name:
        return "Unknown employee", 404
    if request.method == "POST":
        new_name = (request.form.get("new_name") or "").strip() or name
        to_slug = (request.form.get("to_company") or "").strip()
        target = find_company(to_slug) if to_slug else co
        if target is None:
            target = co
        try:
            staffing = float(request.form.get("staffing"))
        except (TypeError, ValueError):
            staffing = None
        try:
            billing = float(request.form.get("billing"))
        except (TypeError, ValueError):
            billing = None
        employee_id = (request.form.get("employee_id") or "").strip()
        # Refuse a rename that would collide with an existing worker
        # in the target company.
        if new_name != name:
            collision = any(
                r.get("company") == target["name"]
                and (r.get("name") or "") == new_name
                for r in DB.get("rates", []))
            if not collision:
                collision = any(
                    e.get("company") == target["name"]
                    and (e.get("employee") or "") == new_name
                    for e in DB.get("entries", []))
            if collision:
                flash(f"Cannot rename to {new_name}: that name already "
                      f"exists in {target['name']}.")
                return redirect(url_for(".employee_page",
                                        company=co["slug"], name=name))
        # Refuse an employee ID already used by another worker
        # anywhere (the ID identifies one person across companies).
        if employee_id:
            dup = next((r for r in DB.get("rates", [])
                        if (r.get("employee_id") or "").strip() == employee_id
                        and (r.get("name") or "") != new_name), None)
            if dup is not None:
                flash(f"Employee ID {employee_id} is already used by "
                      f"{dup.get('name')} ({dup.get('company')}).")
                return redirect(url_for(".employee_page",
                                        company=co["slug"], name=name))
        # 1. Rename (within the current company).
        if new_name != name:
            for r in DB.get("rates", []):
                if (r.get("company") == co["name"]
                        and (r.get("name") or "") == name):
                    r["name"] = new_name
            for e in DB.get("entries", []):
                if (e.get("company") == co["name"]
                        and (e.get("employee") or "") == name):
                    e["employee"] = new_name
            for a in DB.get("aliases", []):
                if (a.get("company") or "").strip() == co["name"]:
                    if (a.get("alias") or "") == name:
                        a["alias"] = new_name
                    if (a.get("canonical") or "") == name:
                        a["canonical"] = new_name
        # 2. Move to another company (the renamed worker).
        if target["name"] != co["name"]:
            for r in DB.get("rates", []):
                if (r.get("company") == co["name"]
                        and (r.get("name") or "") == new_name):
                    r["company"] = target["name"]
            for e in DB.get("entries", []):
                if (e.get("company") == co["name"]
                        and (e.get("employee") or "") == new_name):
                    e["company"] = target["name"]
            for a in DB.get("aliases", []):
                if ((a.get("company") or "").strip() == co["name"]
                        and ((a.get("alias") or "") == new_name
                             or (a.get("canonical") or "") == new_name)):
                    a["company"] = target["name"]
        # 3. Rate for the (possibly new) name + company.
        rate = next((r for r in DB.get("rates", [])
                     if r.get("company") == target["name"]
                     and (r.get("name") or "") == new_name), None)
        if rate is not None:
            rate["staffing"] = staffing
            rate["billing"] = billing
            rate["employee_id"] = employee_id or None
        elif staffing is not None or billing is not None or employee_id:
            DB["rates"].append({
                "id": "rate-" + uuid.uuid4().hex[:8], "name": new_name,
                "company": target["name"], "staffing": staffing,
                "billing": billing, "employee_id": employee_id or None,
            })
        save_db()
        flash(f"Updated {new_name} ({target['name']}).")
        return redirect(url_for(".employee_page",
                                company=target["slug"], name=new_name))
    rate = next((r for r in DB.get("rates", [])
                 if r.get("company") == co["name"]
                 and (r.get("name") or "") == name), None)
    entries = sorted(
        [e for e in DB.get("entries", [])
         if e.get("company") == co["name"]
         and (e.get("employee") or "") == name],
        key=lambda e: (e.get("date") or ""))
    aliases = [a for a in DB.get("aliases", [])
               if (a.get("company") or "").strip() == co["name"]
               and ((a.get("alias") or "") == name
                    or (a.get("canonical") or "") == name)]
    total_hours = round(sum(float(e.get("payroll_hours") or 0)
                            for e in entries), 2)
    return render_template(
        "payroll/employee.html", company=co, name=name, rate=rate,
        entries=entries, aliases=aliases, total_hours=total_hours,
        companies=sorted(DB.get("companies", []),
                         key=lambda c: (c.get("name") or "").lower()),
        money=_money, fmt_hours=_fmt_hours)


def _group_employees():
    """Group rate rows + time entries into persons (one worker across
    all companies).

    A person is keyed by employee_id when one is set, else by name. A
    rate row without an ID joins the person with the same name, so a
    worker who has an ID in one company and no ID yet in another is
    still one person. Returns a list of person dicts, sorted by name:
      {key, name, employee_id, companies: {company: rate row},
       entries: [...]}
    """
    persons = {}
    by_name = {}

    def _person_for(name, employee_id):
        eid = (employee_id or "").strip() or None
        if eid:
            key = ("id", eid)
            if key not in persons:
                # Merge in a no-ID person with this name, if any.
                nkey = ("name", name)
                if nkey in persons:
                    p = persons.pop(nkey)
                    p["key"] = key
                    p["employee_id"] = eid
                    persons[key] = p
                else:
                    persons[key] = {"key": key, "name": name,
                                    "employee_id": eid, "companies": {},
                                    "entries": []}
            by_name[name] = key
            return persons[key]
        key = by_name.get(name)
        if key is not None and key in persons:
            return persons[key]
        key = ("name", name)
        if key not in persons:
            persons[key] = {"key": key, "name": name,
                            "employee_id": None, "companies": {},
                            "entries": []}
            by_name.setdefault(name, key)
        return persons[key]

    for r in DB.get("rates", []):
        p = _person_for(r.get("name") or "", r.get("employee_id"))
        p["companies"][r.get("company")] = r
        if (r.get("employee_id") or "").strip():
            p["employee_id"] = (r.get("employee_id") or "").strip()
    for e in DB.get("entries", []):
        p = _person_for(e.get("employee") or "", None)
        p["entries"].append(e)
    return sorted(persons.values(), key=lambda p: (p["name"] or "").lower())


def _person_url_key(person):
    """The URL key for a person: their employee ID when set, else name."""
    return person.get("employee_id") or person.get("name") or ""


def _find_person(key):
    """Find one person by employee_id or by name (URL key)."""
    key = (key or "").strip()
    if not key:
        return None
    for p in _group_employees():
        if (p.get("employee_id") or "") == key:
            return p
        if (p.get("name") or "") == key:
            return p
    return None


def _person_db_rows(person):
    """The DB rows belonging to one person: (rate rows, entries,
    aliases). A rate row belongs to the person when its employee_id
    matches the person's ID or its name matches the person's name."""
    eid = person.get("employee_id")
    name = person.get("name") or ""
    rates = [r for r in DB.get("rates", [])
             if ((eid and (r.get("employee_id") or "").strip() == eid)
                 or (r.get("name") or "") == name)]
    entries = [e for e in DB.get("entries", [])
               if (e.get("employee") or "") == name]
    aliases = [a for a in DB.get("aliases", [])
               if (a.get("alias") or "") == name
               or (a.get("canonical") or "") == name]
    return rates, entries, aliases


@bp.get("/employees")
def employees():
    """Employees, company first then employee: every company with its
    workers (name, ID, rates, entries, hours in that company). One
    worker can appear under several companies."""
    persons = _group_employees()
    companies = sorted(DB.get("companies", []),
                       key=lambda c: (c.get("name") or "").lower())
    # Per company: the persons who have a rate row or entries there.
    by_company = {c["name"]: [] for c in companies}
    for p in persons:
        p_companies = set(p["companies"].keys())
        for e in p["entries"]:
            p_companies.add(e.get("company"))
        for cname in p_companies:
            if cname not in by_company:
                continue
            rate = p["companies"].get(cname)
            centries = [e for e in p["entries"]
                        if e.get("company") == cname]
            hours = round(sum(float(e.get("payroll_hours") or 0)
                              for e in centries), 2)
            by_company[cname].append({
                "person": p, "rate": rate,
                "entries": len(centries), "hours": hours})
    for cname in by_company:
        by_company[cname].sort(key=lambda r: (r["person"]["name"] or "").lower())
    return render_template(
        "payroll/employees.html", companies=companies,
        by_company=by_company, total=len(persons),
        fmt_hours=_fmt_hours)


@bp.post("/employees")
def employees_add():
    """Add a new employee to a company (creates their rate row)."""
    name = (request.form.get("name") or "").strip()
    to_slug = (request.form.get("to_company") or "").strip()
    co = find_company(to_slug) if to_slug else None
    employee_id = (request.form.get("employee_id") or "").strip()
    try:
        staffing = float(request.form.get("staffing"))
    except (TypeError, ValueError):
        staffing = None
    try:
        billing = float(request.form.get("billing"))
    except (TypeError, ValueError):
        billing = None
    if not name or co is None:
        flash("Pick a company and enter a name.")
        return redirect(url_for(".employees"))
    if any(r.get("company") == co["name"]
           and (r.get("name") or "") == name
           for r in DB.get("rates", [])):
        flash(f"{name} already has a rate row in {co['name']}.")
        return redirect(url_for(".employees"))
    if employee_id:
        dup = next((r for r in DB.get("rates", [])
                    if (r.get("employee_id") or "").strip() == employee_id
                    and (r.get("name") or "") != name), None)
        if dup is not None:
            flash(f"Employee ID {employee_id} is already used by "
                  f"{dup.get('name')} ({dup.get('company')}).")
            return redirect(url_for(".employees"))
    DB["rates"].append({
        "id": "rate-" + uuid.uuid4().hex[:8], "name": name,
        "company": co["name"], "staffing": staffing,
        "billing": billing, "employee_id": employee_id or None,
    })
    save_db()
    flash(f"Added {name} to {co['name']}.")
    return redirect(url_for(".employee_detail",
                            person=_person_url_key(
                                _find_person(employee_id or name))))


@bp.get("/employees/<person>")
def employee_detail(person):
    """One page to manage everything about one worker: their name and
    employee ID, their rate in every company they belong to, adding
    them to another company, and all their time entries."""
    p = _find_person(person)
    if p is None:
        return "Unknown employee", 404
    rates, entries, aliases = _person_db_rows(p)
    companies = sorted(DB.get("companies", []),
                       key=lambda c: (c.get("name") or "").lower())
    in_companies = {r.get("company") for r in rates}
    other_companies = [c for c in companies
                       if c["name"] not in in_companies]
    entries_sorted = sorted(entries, key=lambda e: (e.get("date") or ""))
    total_hours = round(sum(float(e.get("payroll_hours") or 0)
                            for e in entries), 2)
    return render_template(
        "payroll/employee_detail.html", person=p, rates=rates,
        entries=entries_sorted, aliases=aliases,
        companies=companies, other_companies=other_companies,
        total_hours=total_hours, money=_money, fmt_hours=_fmt_hours)


@bp.post("/employees/<person>")
def employee_detail_post(person):
    """Save changes for one worker: their name/ID, one company's rate,
    adding them to another company, or removing them from one."""
    p = _find_person(person)
    if p is None:
        return "Unknown employee", 404
    action = request.form.get("action") or ""
    rates, entries, aliases = _person_db_rows(p)
    old_name = p["name"]
    old_id = p.get("employee_id")

    if action == "save_person":
        new_name = (request.form.get("new_name") or "").strip() or old_name
        new_id = (request.form.get("employee_id") or "").strip() or None
        # Refuse a rename that would collide with a different person,
        # and an ID already used by a different person.
        for other in _group_employees():
            if other["key"] == p["key"]:
                continue
            if (other.get("name") or "") == new_name:
                flash(f"Cannot rename to {new_name}: that name already "
                      f"belongs to another employee.")
                return redirect(url_for(".employee_detail",
                                        person=_person_url_key(p)))
            if new_id and (other.get("employee_id") or "") == new_id:
                flash(f"Employee ID {new_id} is already used by "
                      f"{other.get('name')}.")
                return redirect(url_for(".employee_detail",
                                        person=_person_url_key(p)))
        for r in rates:
            r["name"] = new_name
            r["employee_id"] = new_id
        for e in entries:
            e["employee"] = new_name
        for a in aliases:
            if (a.get("alias") or "") == old_name:
                a["alias"] = new_name
            if (a.get("canonical") or "") == old_name:
                a["canonical"] = new_name
        if not rates and new_id:
            # No rate row to hold the ID: create one in the company of
            # their earliest entry (or the first company).
            first_co = None
            if entries:
                first_co = sorted(entries,
                                  key=lambda e: (e.get("date") or ""))[0].get("company")
            if not first_co and DB.get("companies"):
                first_co = DB["companies"][0]["name"]
            if first_co:
                DB["rates"].append({
                    "id": "rate-" + uuid.uuid4().hex[:8], "name": new_name,
                    "company": first_co, "staffing": None, "billing": None,
                    "employee_id": new_id,
                })
        save_db()
        flash(f"Updated {new_name}.")
        return redirect(url_for(".employee_detail",
                                person=new_id or new_name))

    if action == "save_rate":
        to_slug = (request.form.get("to_company") or "").strip()
        co = find_company(to_slug) if to_slug else None
        if co is None:
            flash("Unknown company.")
            return redirect(url_for(".employee_detail",
                                    person=_person_url_key(p)))
        try:
            staffing = float(request.form.get("staffing"))
        except (TypeError, ValueError):
            staffing = None
        try:
            billing = float(request.form.get("billing"))
        except (TypeError, ValueError):
            billing = None
        rate = next((r for r in rates
                     if r.get("company") == co["name"]), None)
        if rate is None:
            flash(f"{old_name} is not in {co['name']} yet — "
                  f"add them to that company first.")
        else:
            rate["staffing"] = staffing
            rate["billing"] = billing
            save_db()
            flash(f"Updated {old_name}'s rate in {co['name']}.")
        return redirect(url_for(".employee_detail",
                                person=_person_url_key(p)))

    if action == "add_affiliation":
        to_slug = (request.form.get("to_company") or "").strip()
        co = find_company(to_slug) if to_slug else None
        if co is None:
            flash("Pick a company.")
            return redirect(url_for(".employee_detail",
                                    person=_person_url_key(p)))
        if any(r.get("company") == co["name"] for r in rates):
            flash(f"{old_name} is already in {co['name']}.")
            return redirect(url_for(".employee_detail",
                                    person=_person_url_key(p)))
        try:
            staffing = float(request.form.get("staffing"))
        except (TypeError, ValueError):
            staffing = None
        try:
            billing = float(request.form.get("billing"))
        except (TypeError, ValueError):
            billing = None
        DB["rates"].append({
            "id": "rate-" + uuid.uuid4().hex[:8], "name": old_name,
            "company": co["name"], "staffing": staffing,
            "billing": billing, "employee_id": old_id,
        })
        save_db()
        flash(f"Added {old_name} to {co['name']}.")
        return redirect(url_for(".employee_detail",
                                person=_person_url_key(p)))

    if action == "remove_affiliation":
        to_slug = (request.form.get("to_company") or "").strip()
        co = find_company(to_slug) if to_slug else None
        if co is None:
            flash("Unknown company.")
            return redirect(url_for(".employee_detail",
                                    person=_person_url_key(p)))
        if any(e.get("company") == co["name"] for e in entries):
            flash(f"{old_name} has time entries in {co['name']} — "
                  f"remove those first.")
            return redirect(url_for(".employee_detail",
                                    person=_person_url_key(p)))
        before = len(DB["rates"])
        DB["rates"] = [r for r in DB["rates"]
                       if not (r.get("company") == co["name"]
                               and (r.get("name") or "") == old_name)]
        if len(DB["rates"]) < before:
            save_db()
            flash(f"Removed {old_name} from {co['name']}.")
        return redirect(url_for(".employee_detail",
                                person=_person_url_key(p)))

    return redirect(url_for(".employee_detail", person=_person_url_key(p)))


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
