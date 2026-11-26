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

Two-way sync with Patrol (2026-11-25): imported entries keep
`source` ("patrol" / "patrol_coa") plus the first-in / last-out
punch ids, and punch rows dedupe by employee + date (not hours),
so a day corrected in payroll never re-imports its old hours.
`reverse_sync_patrol_punches` rewrites that day's closed punch
pairs to the corrected entry times (originals archived in
punch_corrections) — the Patrol timecard, hours summary and weekly
email then show the corrected times.

Name matching: employee name + company against the rates first,
then the stored alias map (same system as CSV import). Matching is
case-insensitive: source files spell some names with different
capitalisation ("Argenis cabana" vs rate "Argenis Cabana") and a
case difference is never a different person. Rows with no rate
match are shown as such and are NOT imported until the user maps
the name (or skips it). Nothing is ever auto-guessed.

All functions here are pure (inputs in, preview rows out) so they
can be unit-tested with synthetic fixture files.
"""
import json
import os
import uuid
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


def _fold(name):
    """Case-insensitive name key: strip + casefold."""
    return (name or "").strip().casefold()


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


def _time_only(value):
    """Clock-in/out times are stored as times only ("7:00", "19:00"),
    never as full datetimes — the date lives in the entry's `date`
    (and `clock_out_date` for overnight shifts). An ISO timestamp
    from a punch is reduced to its local time, "H:MM" (24h). A value
    that is already a plain time passes through unchanged."""
    dt = parse_local(value)
    if dt is None:
        return (value or "").strip()
    return "%d:%02d" % (dt.hour, dt.minute)


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
    clock_out_date, site, notes, jobsite_id}. The jobsite_id is the
    payroll jobsite (or location) the worker picked at clock-in on
    /staffing/onsite; it is None when the punch was not tagged.
    Skipped rows carry a reason ('still clocked in' when clock_out
    is missing)."""
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
            "clock_in": _time_only(t.get("clock_in")),
            "clock_out": _time_only(t.get("clock_out")),
            "clock_out_date": co.date().isoformat(),
            "site": sites.get(t.get("site_id"), ""),
            "notes": t.get("notes") or "",
            "jobsite_id": (t.get("jobsite_id") or "") or None,
            "company_pick": (t.get("company") or "") or None,
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
    first_in_id = {}  # (email, date_iso) -> in punch id
    last_out_id = {}  # (email, date_iso) -> out punch id
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
                    first_in_id.setdefault(key, open_in.get("id") or "")
                    last_out[key] = p.get("ts") or ""
                    last_out_id[key] = p.get("id") or ""
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
            "clock_in": _time_only(first_in.get((email, day), "")),
            "clock_out": _time_only(last_out.get((email, day), "")),
            "clock_out_date": (parse_local(last_out.get((email, day)))
                               .date().isoformat()
                               if last_out.get((email, day)) else None),
            "first_in_id": first_in_id.get((email, day), ""),
            "last_out_id": last_out_id.get((email, day), ""),
            "site": "",
            "notes": "",
        })
    rows.sort(key=lambda r: (r["date"], r["employee"].lower()))
    skipped.sort(key=lambda s: (s.get("clock_in") or "",
                                s.get("employee", "")))
    return rows, skipped


def _parse_hhmm(value):
    """'H:MM' (24h) -> minutes since midnight, or None."""
    if value is None:
        return None
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _fmt_hhmm(minutes):
    """Minutes since midnight -> zero-padded 'HH:MM' (24h), the
    format punch timestamps use."""
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def _closed_pairs_for_day(punches, email, day_iso):
    """Closed (in, out) punch pairs for one worker whose clock-in
    date is day_iso, plus orphan in-punches on that date (taps with
    no clock-out — e.g. a second tap minutes after the first).
    Mirrors the pairing walk in patrol_rows. Returns
    (pairs, orphans)."""
    ordered = sorted(
        (p for p in punches
         if (p.get("email") or "").strip() == email
         and parse_local(p.get("ts")) is not None),
        key=lambda p: parse_local(p.get("ts")))
    pairs, orphans = [], []
    open_in = None
    for p in ordered:
        ts = parse_local(p.get("ts"))
        action = (p.get("action") or "").strip().lower()
        if action == "in":
            if open_in is not None:
                # Previous clock-in never got a clock-out.
                if (parse_local(open_in.get("ts")).date().isoformat()
                        == day_iso):
                    orphans.append(open_in)
                open_in = p
            else:
                open_in = p
        elif action == "out":
            if open_in is None:
                continue  # stray clock-out; not this day's pair
            in_ts = parse_local(open_in.get("ts"))
            if in_ts.date().isoformat() == day_iso:
                pairs.append((open_in, p))
            open_in = None
    if open_in is not None:
        if (parse_local(open_in.get("ts")).date().isoformat()
                == day_iso):
            orphans.append(open_in)
    return pairs, orphans


def reverse_sync_patrol_punches(patrol_data, entry):
    """Rewrite one worker-day's closed Patrol punch pairs to match a
    corrected payroll entry, archiving the replaced punches.

    The payroll entry is the manager's corrected record; the Patrol
    timecard, hours summary and weekly email all derive from the
    punches, so they pick the corrected times up automatically, and
    the nightly auto-sync re-derives the same hours and skips the
    row as a duplicate. The replaced punches are kept in
    punch_corrections on the same file — nothing is lost.

    Only closed pairs whose clock-in date is the entry's date are
    replaced, plus orphan in-taps from before the day's first pair
    (accidental double-taps). An in-punch that is still open (the
    worker is clocked in right now) is never touched, and neither is
    a pair belonging to another day (e.g. a night shift that started
    the day before). When the day has no closed pairs there is
    nothing to rewrite, so manual entries for punch-less days stay
    payroll-only.

    Returns (changed, detail). Never raises on bad input; it just
    reports why nothing was done.
    """
    employee = (entry.get("employee") or "").strip()
    day = (entry.get("date") or "").strip()
    in_m = _parse_hhmm(entry.get("clock_in"))
    out_m = _parse_hhmm(entry.get("clock_out"))
    if not employee or not day or in_m is None or out_m is None:
        return False, "entry has no employee, date or clock times"
    try:
        day_d = date.fromisoformat(day)
    except ValueError:
        return False, "bad entry date"
    # Overnight shifts (clock-out at/before clock-in) end the next day.
    out_day = day_d if out_m > in_m else day_d + timedelta(days=1)
    new_in_ts = "%sT%s:00" % (day_d.isoformat(), _fmt_hhmm(in_m))
    new_out_ts = "%sT%s:00" % (out_day.isoformat(), _fmt_hhmm(out_m))

    workers = patrol_data.get("workers", []) or []
    email = None
    name = None
    for w in workers:
        if _fold(w.get("name")) == _fold(employee):
            email = (w.get("email") or "").strip()
            name = (w.get("name") or "").strip()
            break
    punches = patrol_data.get("punches", []) or []
    if not email:
        for p in punches:
            if _fold(p.get("name")) == _fold(employee):
                email = (p.get("email") or "").strip()
                name = (p.get("name") or "").strip()
                break
    if not email:
        return False, "worker not found in Patrol"

    pairs, orphans = _closed_pairs_for_day(punches, email, day)
    if not pairs:
        return False, "no closed punches for that day"
    # Already correct: one pair, times match to the minute.
    if (len(pairs) == 1 and not orphans
            and (pairs[0][0].get("ts") or "")[:16] == new_in_ts[:16]
            and (pairs[0][1].get("ts") or "")[:16] == new_out_ts[:16]):
        return False, "punches already match"

    replaced = [p for pair in pairs for p in pair]
    first_in_ts = parse_local(pairs[0][0].get("ts"))
    # Orphan taps from before the day's first real clock-in are
    # accidental double-taps; later orphans may be a shift still
    # open right now, so they stay.
    doomed = [o for o in orphans
              if parse_local(o.get("ts")) is not None
              and first_in_ts is not None
              and parse_local(o.get("ts")) < first_in_ts]
    replaced.extend(doomed)
    replaced_ids = {p.get("id") for p in replaced}
    used_ids = {p.get("id") for p in punches}

    def _new_id():
        nid = uuid.uuid4().hex[:8]
        while nid in used_ids:
            nid = uuid.uuid4().hex[:8]
        used_ids.add(nid)
        return nid

    new_in = {"id": _new_id(), "email": email,
              "name": name or employee, "action": "in",
              "ts": new_in_ts, "source": "payroll-correction"}
    new_out = {"id": _new_id(), "email": email,
               "name": name or employee, "action": "out",
               "ts": new_out_ts, "source": "payroll-correction"}
    patrol_data["punches"] = [p for p in punches
                              if p.get("id") not in replaced_ids]
    patrol_data["punches"].extend([new_in, new_out])
    patrol_data.setdefault("punch_corrections", []).append({
        "entry_id": entry.get("id") or "",
        "employee": employee, "email": email, "date": day,
        "corrected_at": datetime.now(PHOENIX).isoformat(
            timespec="seconds"),
        "corrected_via": "payroll",
        "original_punches": replaced,
        "new_punches": [new_in, new_out],
    })
    return True, ("replaced %d punch(es) with corrected %s-%s"
                  % (len(replaced), _fmt_hhmm(in_m), _fmt_hhmm(out_m)))


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
    # Rate + alias lookups per company. A CM timecard can carry a
    # Company pick (LGES > Company, set at clock-in); that row's
    # hours go to the picked company and the rate is looked up
    # there. Without a pick, the target company (the mapping)
    # applies. `rates` / `aliases` are the full lists.
    rates_by_co = {}
    for r in rates or []:
        co = (r.get("company") or "").strip()
        if co:
            rates_by_co.setdefault(co, {})[(r.get("name") or "").strip()] = r
    aliases_by_co = {}
    for a in aliases or []:
        co = (a.get("company") or "").strip()
        if co:
            aliases_by_co.setdefault(co, {})[(a.get("alias") or "").strip()] = (a.get("canonical") or "").strip()
    # Case-insensitive lookup: source files spell some names with
    # different capitalisation ("Argenis cabana" vs rate "Argenis
    # Cabana"). A case difference is never a different person.
    for row in rows:
        eff = ((row.get("company_pick") or "").strip() or company)
        row["entry_company"] = eff
        rate_names = rates_by_co.get(eff, {})
        alias_map = aliases_by_co.get(eff, {})
        rate_by_fold = {_fold(n): n for n in rate_names}
        alias_by_fold = {_fold(a): c for a, c in alias_map.items()}
        name = (row["employee"] or "").strip()
        fold = _fold(name)
        canonical = None
        if fold in rate_by_fold:
            canonical = rate_by_fold[fold]
        elif fold in alias_by_fold and alias_by_fold[fold] in rate_names:
            canonical = alias_by_fold[fold]
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
                source_label="", use_canonical=False, source="",
                dedupe_by_date=False):
    """Decide what a confirmed app-import would add.

    Only rows with a matched rate row are imported (unmatched rows are
    returned separately so the summary can count them — they are never
    imported silently). Duplicates are skipped; the employee comparison
    is case-insensitive so a source spelling ("Argenis cabana") does
    not re-add a row already stored as "Argenis Cabana".

    Duplicate key: for punch sources (Patrol — one derived row per
    employee + date by construction) the key is company + canonical
    employee + date, so a day that was already imported never gets a
    second row even when its hours were later corrected in payroll
    (the old hours would otherwise re-import as a "new" row).
    For timecard sources (CM / COA — one row per timecard, and a
    worker can hold two timecards on one date) the key stays
    company + canonical employee + date + hours. Pass
    dedupe_by_date=True only for punch sources.

    Imported entries keep their origin: `source` is the SOURCES key
    ("patrol", "cm", ...) and `source_punches` records the first-in /
    last-out punch ids a punch row came from, so a later payroll
    correction can find the punches it corrects. Returns
    (entries_to_add, n_duplicates, unmatched_rows). Entries keep the
    RAW as-entered name, unless use_canonical is set: the nightly
    auto-sync stores the canonical rate-row name instead, so payroll
    math matches it without needing a stored alias. The source label
    is kept in Notes so the origin stays traceable.
    """
    alias_by_co = {}
    for a in aliases or []:
        co = (a.get("company") or "").strip()
        if co:
            alias_by_co.setdefault(co, {})[(a.get("alias") or "").strip()] = (a.get("canonical") or "").strip()

    def fold_canon(name, co):
        """Canonical name for duplicate comparison: resolve the alias
        (case-insensitively) in that company's alias map, then fold
        case."""
        n = (name or "").strip()
        amap = alias_by_co.get(co or "", {})
        by_fold = {_fold(a): c for a, c in amap.items()}
        return by_fold.get(_fold(n), n).casefold()

    seen = set()

    def _dup_key(eco, name, day, hours):
        base = (eco, fold_canon(name, eco), (day or "").strip())
        if dedupe_by_date:
            return base
        return base + (float(hours or 0),)

    for e in existing_entries or []:
        eco = (e.get("company") or "").strip()
        name = (e.get("employee") or "").strip()
        seen.add(_dup_key(eco, name, e.get("date"),
                          e.get("payroll_hours")))
    to_add, unmatched = [], []
    n_dupes = 0
    for row in preview_rows:
        if not row.get("matched"):
            unmatched.append(row)
            continue
        eco = (row.get("entry_company") or company or "").strip()
        key = _dup_key(eco, row["matched"], row["date"],
                       row.get("hours"))
        if key in seen:
            n_dupes += 1
            continue
        seen.add(key)
        employee = ((row["matched"] if use_canonical else
                     (row["employee"] or "").strip()))
        new_entry = {
            "company": eco,
            "employee": employee,
            "date": row["date"],
            "payroll_hours": round(float(row["hours"] or 0), 2),
            "clock_in": row.get("clock_in") or "",
            "clock_out": row.get("clock_out") or "",
            "clock_out_date": row.get("clock_out_date"),
            "gross_hours": None,
            "site": row.get("site") or "",
            "jobsite_id": (row.get("jobsite_id") or "") or None,
            "notes": (row.get("notes") or "").strip()
                     or (f"Imported from {source_label}"
                         if source_label else ""),
        }
        if source:
            new_entry["source"] = source
            _sp = {}
            if row.get("first_in_id"):
                _sp["first_in"] = row["first_in_id"]
            if row.get("last_out_id"):
                _sp["last_out"] = row["last_out_id"]
            if _sp:
                new_entry["source_punches"] = _sp
        to_add.append(new_entry)
    return to_add, n_dupes, unmatched
