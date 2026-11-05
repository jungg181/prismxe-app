#!/usr/bin/env python3
"""Import time records from the app's own modules into payroll.

Sources (read directly as JSON, same files each module's load_db reads;
no other blueprint's functions are called):
  cm          data_cm.json         CM timecards
  patrol      data_patrol.json     Hanseong Patrol in/out punches
  coa         data_coa.json        COA timecards
  patrol_coa  data_patrol_coa.json Patrol COA in/out punches

Hours rules (stated in the UI next to the preview):
- CM / COA timecards: clock-in to clock-out with the module's own
  compute_hours rule — 1h meal break auto-deducted for shifts of 5h
  or more (mirrored exactly from cm_bp.compute_hours / coa_bp).
- Patrol punches: consecutive in/out pairs per worker; hours =
  elapsed, rounded to the nearest 0.5, capped at 12 per day (the
  sheet's Payroll Hours behavior). Date attribution is the clock-in
  date. An in-punch with no matching out-punch is an open shift:
  skipped and listed as "still clocked in".

Name matching: exact (employee name + company) against the rates
first, then the stored alias map (same system as CSV import). Rows
with no rate match are shown as such and are NOT imported until the
user maps the name (or skips it). Nothing is ever auto-guessed.

All functions here are pure (inputs in, preview rows out) so they
can be unit-tested with synthetic fixture files.
"""
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PHOENIX = ZoneInfo("America/Phoenix")

SOURCES = {
    "cm": {"file": "data_cm.json", "label": "CM",
           "kind": "timecards"},
    "patrol": {"file": "data_patrol.json", "label": "Hanseong Patrol",
               "kind": "punches"},
    "coa": {"file": "data_coa.json", "label": "COA",
            "kind": "timecards"},
    "patrol_coa": {"file": "data_patrol_coa.json", "label": "Patrol COA",
                   "kind": "punches"},
}

HOURS_RULE_TEXT = (
    "Hours: CM / COA — clock-in to clock-out, minus 1h meal break for "
    "shifts of 5h or more. Patrol — consecutive clock-in/out pairs, "
    "rounded to the nearest 0.5h, capped at 12h per day. Dates use the "
    "clock-in date. Open shifts (still clocked in) are skipped."
)


def parse_local(value):
    """Parse an ISO timestamp; naive values are America/Phoenix local
    (that is how the CM/patrol modules store them). Returns an aware
    Phoenix datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=PHOENIX)
    return dt.astimezone(PHOENIX)


def compute_hours_mirrored(clock_in_iso, clock_out_iso):
    """Mirror of cm_bp.compute_hours / coa_bp.compute_hours:
    shift hours, 1h meal break auto-deducted for shifts >= 5h."""
    ci, co = parse_local(clock_in_iso), parse_local(clock_out_iso)
    if not ci or not co:
        return 0.0
    h = (co - ci).total_seconds() / 3600.0
    if h >= 5:
        h -= 1.0
    return round(max(h, 0.0), 2)


def _round_half(hours):
    return round(hours * 2) / 2


def load_source(app_dir, source):
    """Read one source JSON file. Missing/unreadable -> empty dict."""
    info = SOURCES.get(source)
    if info is None:
        return {}
    path = os.path.join(app_dir, info["file"])
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _in_period(d, start, end):
    return start <= d <= end


def timecard_rows(data, start, end):
    """Derive preview rows from CM/COA-style timecards.

    Returns (rows, skipped). A row is one closed timecard:
    {employee, date (clock-in date), hours, clock_in, clock_out,
    clock_out_date, site, notes}. Skipped rows carry a reason
    ('still clocked in' when clock_out is missing)."""
    workers = {w.get("id"): (w.get("name") or "").strip()
               for w in data.get("workers", [])}
    sites = {s.get("id"): (s.get("name") or "").strip()
             for s in data.get("sites", [])}
    rows, skipped = [], []
    for t in data.get("timecards", []) or []:
        ci = parse_local(t.get("clock_in"))
        if ci is None:
            skipped.append({
                "employee": workers.get(t.get("worker_id"), ""),
                "clock_in": t.get("clock_in") or "",
                "reason": "missing clock-in time"})
            continue
        day = ci.date()
        if not _in_period(day, start, end):
            continue
        name = workers.get(t.get("worker_id"), "")
        co = parse_local(t.get("clock_out"))
        if co is None:
            skipped.append({
                "employee": name,
                "clock_in": t.get("clock_in") or "",
                "reason": "still clocked in"})
            continue
        rows.append({
            "employee": name,
            "date": day.isoformat(),
            "hours": compute_hours_mirrored(t.get("clock_in"),
                                            t.get("clock_out")),
            "clock_in": t.get("clock_in") or "",
            "clock_out": t.get("clock_out") or "",
            "clock_out_date": co.date().isoformat(),
            "site": sites.get(t.get("site_id"), ""),
            "notes": t.get("notes") or "",
        })
    rows.sort(key=lambda r: (r["date"], r["employee"].lower()))
    return rows, skipped


def patrol_rows(data, start, end):
    """Derive preview rows from patrol-style in/out punches.

    Pairs consecutive in/out punches per worker (by email), rounds
    each pair to the nearest 0.5h, and caps each (worker, clock-in
    date) at 12h. One row per (employee, date). Unpaired in-punches
    are skipped as open shifts; out-punches with no clock-in are
    skipped as stray."""
    workers = {w.get("email"): (w.get("name") or "").strip()
               for w in data.get("workers", [])}
    by_email = {}
    for p in data.get("punches", []) or []:
        email = (p.get("email") or "").strip()
        if not email:
            continue
        by_email.setdefault(email, []).append(p)
    # Display name per worker: the registered worker name first,
    # otherwise the name carried on the punches themselves.
    name_by_email = {}
    for email, punches in by_email.items():
        name = workers.get(email, "")
        if not name:
            for p in punches:
                if (p.get("name") or "").strip():
                    name = (p.get("name") or "").strip()
                    break
        name_by_email[email] = name or email
    pairs = {}   # (email, date_iso) -> [rounded pair hours]
    first_in = {}  # (email, date_iso) -> in ISO
    last_out = {}  # (email, date_iso) -> out ISO
    skipped = []
    for email, punches in by_email.items():
        name = name_by_email[email]
        ordered = sorted(
            (p for p in punches if parse_local(p.get("ts")) is not None),
            key=lambda p: parse_local(p.get("ts")))
        open_in = None
        for p in ordered:
            ts = parse_local(p.get("ts"))
            action = (p.get("action") or "").strip().lower()
            if action == "in":
                if open_in is not None:
                    # Previous clock-in never got a clock-out.
                    prev = parse_local(open_in.get("ts"))
                    if prev is not None and _in_period(
                            prev.date(), start, end):
                        skipped.append({
                            "employee": name,
                            "clock_in": open_in.get("ts") or "",
                            "reason": "still clocked in"})
                    open_in = p
                else:
                    open_in = p
            elif action == "out":
                if open_in is None:
                    skipped.append({
                        "employee": name,
                        "clock_in": p.get("ts") or "",
                        "reason": "clock-out with no clock-in"})
                    continue
                in_ts = parse_local(open_in.get("ts"))
                day = in_ts.date()
                if _in_period(day, start, end):
                    elapsed = (ts - in_ts).total_seconds() / 3600.0
                    key = (email, day.isoformat())
                    pairs.setdefault(key, []).append(
                        _round_half(max(elapsed, 0.0)))
                    first_in.setdefault(key, open_in.get("ts") or "")
                    last_out[key] = p.get("ts") or ""
                open_in = None
        if open_in is not None:
            prev = parse_local(open_in.get("ts"))
            if prev is not None and _in_period(prev.date(), start, end):
                skipped.append({
                    "employee": name,
                    "clock_in": open_in.get("ts") or "",
                    "reason": "still clocked in"})
    rows = []
    for (email, day), hours_list in sorted(pairs.items()):
        rows.append({
            "employee": name_by_email.get(email, email),
            "date": day,
            "hours": round(min(12.0, sum(hours_list)), 2),
            "clock_in": first_in.get((email, day), ""),
            "clock_out": last_out.get((email, day), ""),
            "clock_out_date": (parse_local(last_out.get((email, day)))
                               .date().isoformat()
                               if last_out.get((email, day)) else None),
            "site": "",
            "notes": "",
        })
    rows.sort(key=lambda r: (r["date"], r["employee"].lower()))
    skipped.sort(key=lambda s: (s.get("clock_in") or "",
                                s.get("employee", "")))
    return rows, skipped


def build_preview(app_dir, source, company, rates, aliases, start, end):
    """Preview rows for one source + target company.

    Each row carries the raw employee name plus the matched rate row
    (canonical name) or None when there is no exact rate match and no
    stored alias. Returns {source, label, rows, skipped}."""
    info = SOURCES.get(source)
    if info is None:
        return {"source": source, "label": "", "rows": [], "skipped": []}
    data = load_source(app_dir, source)
    if info["kind"] == "punches":
        rows, skipped = patrol_rows(data, start, end)
    else:
        rows, skipped = timecard_rows(data, start, end)
    rate_names = {(r.get("name") or "").strip(): r
                  for r in rates or []
                  if (r.get("company") or "").strip() == company}
    alias_map = {(a.get("alias") or "").strip(): (a.get("canonical") or "").strip()
                 for a in aliases or []
                 if (a.get("company") or "").strip() == company}
    for row in rows:
        name = (row["employee"] or "").strip()
        canonical = None
        if name in rate_names:
            canonical = name
        elif name in alias_map and alias_map[name] in rate_names:
            canonical = alias_map[name]
        row["matched"] = canonical
        if canonical:
            rate = rate_names[canonical]
            row["staffing"] = rate.get("staffing")
            row["billing"] = rate.get("billing")
        else:
            row["staffing"] = None
            row["billing"] = None
    return {"source": source, "label": info["label"],
            "rows": rows, "skipped": skipped}


def import_rows(preview_rows, existing_entries, company, aliases,
                source_label=""):
    """Decide what a confirmed app-import would add.

    Only rows with a matched rate row are imported (unmatched rows are
    returned separately so the summary can count them — they are never
    imported silently). Exact duplicates — same company + canonical
    employee + date + hours — are skipped. Returns
    (entries_to_add, n_duplicates, unmatched_rows). Entries keep the
    RAW as-entered name; the alias system resolves it at payroll time.
    The source label is kept in Notes so the origin stays traceable.
    """
    alias_map = {(a.get("alias") or "").strip(): (a.get("canonical") or "").strip()
                 for a in aliases or []
                 if (a.get("company") or "").strip() == company}
    seen = set()
    for e in existing_entries or []:
        if (e.get("company") or "").strip() != company:
            continue
        name = (e.get("employee") or "").strip()
        canonical = alias_map.get(name, name)
        seen.add((company, canonical, (e.get("date") or "").strip(),
                  float(e.get("payroll_hours") or 0)))
    to_add, unmatched = [], []
    n_dupes = 0
    for row in preview_rows:
        if not row.get("matched"):
            unmatched.append(row)
            continue
        key = (company, row["matched"], row["date"],
               float(row["hours"] or 0))
        if key in seen:
            n_dupes += 1
            continue
        seen.add(key)
        to_add.append({
            "company": company,
            "employee": (row["employee"] or "").strip(),
            "date": row["date"],
            "payroll_hours": round(float(row["hours"] or 0), 2),
            "clock_in": row.get("clock_in") or "",
            "clock_out": row.get("clock_out") or "",
            "clock_out_date": row.get("clock_out_date"),
            "gross_hours": None,
            "site": row.get("site") or "",
            "notes": (row.get("notes") or "").strip()
                     or (f"Imported from {source_label}"
                         if source_label else ""),
        })
    return to_add, n_dupes, unmatched
