#!/usr/bin/env python3
"""Payroll calculation — pure functions, no Flask, no I/O.

Rules (from the KB Link / Hanseong payroll sheets, verified 2026-11-05):
- A week runs Monday to Sunday.
- Only hours over 40 in a single week count as overtime, at 1.5x.
  No daily overtime.
- Only hours whose DATE falls inside the requested period count for
  that period. Weeks may straddle the period boundary; the part outside
  the period is excluded from that period's overtime math.
- Staffing Rate = what the worker is paid per hour.
  Billing Rate = what the client is charged per hour.
- Rate lookup is by (employee name + company) exact match. If no rate
  row exists for that pair, pay and billing stay blank (never $0) and
  the row is flagged "no rate".
- OT threshold (40) and multiplier (1.5) live in settings, passed in
  here as arguments — never hardcoded in more than one place.
"""

from datetime import date, timedelta

DEFAULT_OT_THRESHOLD = 40.0
DEFAULT_OT_MULTIPLIER = 1.5


def week_monday(d):
    """Monday of the Mon-Sun week containing date d."""
    return d - timedelta(days=d.weekday())


def _to_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _money(value):
    return round(float(value) + 0.0, 2)


def _hours(value):
    if value is None or value == "":
        return 0.0
    return round(float(value), 2)


def resolve_aliases(entries, aliases, company=None):
    """Return copies of entries with alias names replaced by their
    canonical (rate-row) name. Aliases are {alias, company, canonical}.
    Raw entry values are never changed in place — only the copies used
    for computation are resolved. An alias never guesses: it only
    applies when the exact (alias + company) pair was stored."""
    mapping = {}
    for a in aliases or []:
        alias = (a.get("alias") or "").strip()
        comp = (a.get("company") or "").strip()
        canonical = (a.get("canonical") or "").strip()
        if alias and comp and canonical:
            mapping[(alias, comp)] = canonical
    out = []
    for e in entries or []:
        e = dict(e)
        comp = (e.get("company") or "").strip()
        if company is not None and comp != company:
            out.append(e)
            continue
        key = ((e.get("employee") or "").strip(), comp)
        if key in mapping:
            e["employee"] = mapping[key]
        out.append(e)
    return out


def find_unmatched(entries, rates, aliases, company):
    """Entry names (raw, as entered) for this company that have neither
    an exact (name + company) rate row nor a stored alias. Sorted.
    These are never dropped and never auto-matched — the UI asks the
    user to map each one to a rate row (or add a rate)."""
    rate_names = {(r.get("name") or "").strip()
                  for r in rates or []
                  if (r.get("company") or "").strip() == company}
    alias_names = {(a.get("alias") or "").strip()
                   for a in aliases or []
                   if (a.get("company") or "").strip() == company}
    unmatched = set()
    for e in entries or []:
        if (e.get("company") or "").strip() != company:
            continue
        name = (e.get("employee") or "").strip()
        if not name:
            continue
        if name in rate_names or name in alias_names:
            continue
        unmatched.add(name)
    return sorted(unmatched)


def variance_report(rows, variances, company,
                    ot_multiplier=DEFAULT_OT_MULTIPLIER):
    """Flagged pay-rate variances for the computed rows.

    variances: [{company, employee (canonical name), sheet_staffing,
    note, optional sheet_billing}]. For each employee with hours in
    the period, compares the gross pay computed at the PrismXe Info
    staffing rate against the gross the sheet would pay at
    sheet_staffing. When sheet_billing is present, the billing gross
    is compared the same way. Returns
    [{employee, staffing, sheet_staffing, reg_hours, ot_hours,
    computed_gross, sheet_gross, gap, note,
    (billing, sheet_billing, computed_billing_gross,
    sheet_billing_gross, billing_gap when sheet_billing given)}] —
    gap is sheet minus computed, so a positive gap means the sheet
    pays more.
    """
    by_emp = {r["employee"]: r for r in rows}
    out = []
    for v in variances or []:
        if (v.get("company") or "").strip() != company:
            continue
        r = by_emp.get((v.get("employee") or "").strip())
        if r is None or r.get("no_rate") or r.get("staffing") is None:
            continue
        sheet_rate = float(v.get("sheet_staffing"))
        sheet_gross = _money(r["reg_hours"] * sheet_rate
                             + r["ot_hours"] * sheet_rate * ot_multiplier)
        row = {
            "employee": r["employee"],
            "staffing": r["staffing"],
            "sheet_staffing": sheet_rate,
            "reg_hours": r["reg_hours"],
            "ot_hours": r["ot_hours"],
            "computed_gross": r["gross_pay"],
            "sheet_gross": sheet_gross,
            "gap": _money(sheet_gross - r["gross_pay"]),
            "note": v.get("note") or "",
        }
        if v.get("sheet_billing") is not None:
            sheet_bill = float(v["sheet_billing"])
            sheet_billing_gross = _money(
                r["reg_hours"] * sheet_bill
                + r["ot_hours"] * sheet_bill * ot_multiplier)
            row.update({
                "billing": r["billing"],
                "sheet_billing": sheet_bill,
                "computed_billing_gross": r["billing_gross"],
                "sheet_billing_gross": sheet_billing_gross,
                "billing_gap": _money(sheet_billing_gross
                                      - r["billing_gross"]),
            })
        out.append(row)
    return out


def compute_payroll(entries, rates, company, start, end,
                    ot_threshold=DEFAULT_OT_THRESHOLD,
                    ot_multiplier=DEFAULT_OT_MULTIPLIER,
                    aliases=None):
    """Compute one company's payroll for an inclusive date period.

    entries: iterable of dicts with keys company, employee, date
        (YYYY-MM-DD), payroll_hours (float). Extra keys are ignored.
    rates: iterable of dicts with keys name, company, staffing, billing.
        staffing/billing may be None (blank in the source sheet).
    company: company display name, e.g. "KB Link" or "Hanseong".
    start, end: date objects (inclusive) or YYYY-MM-DD strings.

    Returns a dict:
      rows:   per-employee dicts sorted by name:
              employee, reg_hours, ot_hours, total_hours,
              staffing, billing (None when no rate row),
              gross_pay, billing_gross (None when no rate row),
              no_rate (bool), days {YYYY-MM-DD: hours}
      totals: total_reg_hours, total_ot_hours, total_hours,
              total_gross_pay, total_billing_gross (sums over rated rows),
              no_rate_count
      days:   list of YYYY-MM-DD strings in the period, in order
    """
    start = _to_date(start)
    end = _to_date(end)
    if start > end:
        start, end = end, start

    # Resolve stored aliases first: "Francisco" (as entered in the time
    # source) computes under "Francisco Ernesto Martinez Bastidas".
    entries = resolve_aliases(entries, aliases, company)

    rate_by_key = {}
    for r in rates or []:
        name = (r.get("name") or "").strip()
        comp = (r.get("company") or "").strip()
        if name and comp:
            rate_by_key[(name, comp)] = r

    days = []
    d = start
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)
    day_set = set(days)

    # Per-employee, per-week hours. per_day_all covers ALL dates in the
    # database — the OT cap is evaluated on the whole Mon-Sun week, so
    # a week that starts before the period still counts its earlier
    # days toward the 40h threshold (e.g. viewing 10/16–10/31, the
    # Oct 12–15 hours still fill the cap for the Oct 12–18 week).
    # per_day / weekly cover only in-period dates, which is what is
    # actually paid.
    weekly = {}   # employee -> {monday_iso: hours} (in-period only)
    per_day = {}  # employee -> {date_iso: hours} (in-period only)
    per_day_all = {}  # employee -> {date_iso: hours} (all dates)
    employees = []
    for e in entries or []:
        if (e.get("company") or "").strip() != company:
            continue
        name = (e.get("employee") or "").strip()
        if not name:
            continue
        try:
            d = _to_date(e.get("date"))
        except (ValueError, TypeError):
            continue
        h = _hours(e.get("payroll_hours"))
        diso = d.isoformat()
        if name not in per_day_all:
            per_day_all[name] = {}
        per_day_all[name][diso] = per_day_all[name].get(diso, 0.0) + h
        if diso not in day_set:
            continue
        if name not in weekly:
            weekly[name] = {}
            per_day[name] = {}
            employees.append(name)
        monday = week_monday(d).isoformat()
        weekly[name][monday] = weekly[name].get(monday, 0.0) + h
        per_day[name][diso] = per_day[name].get(diso, 0.0) + h

    rows = []
    for name in sorted(employees):
        # Per-day OT split on the FULL week (including days outside the
        # period) so the 40h cap reflects the whole Mon-Sun week. Only
        # in-period days are paid; out-of-period days just fill the cap.
        # OT lands on the later days of the week, so the per-day reg/OT
        # adds up exactly to the totals.
        days_ot = {}
        reg = 0.0
        ot = 0.0
        for monday_iso in weekly[name]:
            monday = date.fromisoformat(monday_iso)
            running = 0.0
            for i in range(7):
                d = monday + timedelta(days=i)
                diso = d.isoformat()
                h = per_day_all[name].get(diso, 0.0)
                if h <= 0:
                    continue
                reg_part = max(0.0, min(h, ot_threshold - running))
                ot_part = round(h - reg_part, 2)
                running += h
                if diso in day_set:
                    days_ot[diso] = ot_part
                    reg += reg_part
                    ot += ot_part
        reg = round(reg, 2)
        ot = round(ot, 2)
        rate = rate_by_key.get((name, company))
        staffing = rate.get("staffing") if rate else None
        billing = rate.get("billing") if rate else None
        staffing = float(staffing) if staffing is not None else None
        billing = float(billing) if billing is not None else None
        if staffing is None or billing is None:
            # No usable rate row: pay/billing stay blank, never $0.
            rows.append({
                "employee": name, "reg_hours": reg, "ot_hours": ot,
                "total_hours": round(reg + ot, 2),
                "staffing": staffing, "billing": billing,
                "gross_pay": None, "billing_gross": None,
                "no_rate": True,
                "days": {day: round(per_day[name].get(day, 0.0), 2)
                         for day in days},
                "days_ot": {day: round(days_ot.get(day, 0.0), 2)
                            for day in days},
            })
            continue
        gross = _money(reg * staffing + ot * staffing * ot_multiplier)
        bill = _money(reg * billing + ot * billing * ot_multiplier)
        rows.append({
            "employee": name, "reg_hours": reg, "ot_hours": ot,
            "total_hours": round(reg + ot, 2),
            "staffing": staffing, "billing": billing,
            "gross_pay": gross, "billing_gross": bill,
            "no_rate": False,
            "days": {day: round(per_day[name].get(day, 0.0), 2)
                     for day in days},
            "days_ot": {day: round(days_ot.get(day, 0.0), 2)
                        for day in days},
        })

    totals = {
        "total_reg_hours": round(sum(r["reg_hours"] for r in rows), 2),
        "total_ot_hours": round(sum(r["ot_hours"] for r in rows), 2),
        "total_hours": round(sum(r["total_hours"] for r in rows), 2),
        "total_gross_pay": _money(sum(
            r["gross_pay"] for r in rows if r["gross_pay"] is not None)),
        "total_billing_gross": _money(sum(
            r["billing_gross"] for r in rows
            if r["billing_gross"] is not None)),
        "no_rate_count": sum(1 for r in rows if r["no_rate"]),
    }
    weekdays = {day: date.fromisoformat(day).strftime("%a")
                for day in days}
    return {"rows": rows, "totals": totals, "days": days,
            "weekdays": weekdays}


def compute_jobsite_pnl(entries, rates, jobsite_id,
                        ot_threshold=DEFAULT_OT_THRESHOLD,
                        ot_multiplier=DEFAULT_OT_MULTIPLIER,
                        aliases=None):
    """Profit & loss for ONE jobsite, from the time entries tagged
    with that jobsite id.

    Overtime is weekly per employee (hours over 40 in a Mon-Sun week
    at 1.5x), so it belongs to the week as a whole. When a worker
    splits a week across jobsites, the OT hours are the ones that
    push the week over 40 — i.e. the LATER days in the week. So each
    entry is split into a regular and an overtime portion by walking
    the week's entries in date order and counting the first 40 hours
    as regular. That makes the per-jobsite revenue and labor cost
    add up exactly to the company payroll for the same entries.

    entries: iterable of dicts with keys company, employee, date
        (YYYY-MM-DD), payroll_hours, jobsite_id. Extra keys ignored.
    rates: iterable of dicts with keys name, company, staffing, billing.
    Returns {reg_hours, ot_hours, total_hours, revenue, labor_cost,
             no_rate (sorted employee names with hours but no rate),
             lines: [{employee, date, hours, reg_hours, ot_hours,
                      revenue, labor_cost, no_rate}] — one per tagged
             entry, in date order}. Money rounded to 2 decimals.
    """
    entries = resolve_aliases(entries, aliases)
    rate_by_key = {}
    for r in rates or []:
        name = (r.get("name") or "").strip()
        comp = (r.get("company") or "").strip()
        if name and comp:
            rate_by_key[(name, comp)] = r

    # Group ALL entries (any jobsite) by (employee, week) so the
    # weekly 40h threshold is applied to the worker's whole week,
    # not just the hours on this jobsite.
    by_week = {}   # (employee, monday_iso) -> [entry, ...]
    for e in entries or []:
        name = (e.get("employee") or "").strip()
        if not name:
            continue
        try:
            d = _to_date(e.get("date"))
        except (ValueError, TypeError):
            continue
        h = _hours(e.get("payroll_hours"))
        monday = week_monday(d).isoformat()
        by_week.setdefault((name, monday), []).append((d, e, h))

    reg_total = 0.0
    ot_total = 0.0
    revenue = 0.0
    labor = 0.0
    no_rate = set()
    lines = []
    for (name, _monday), items in sorted(by_week.items()):
        # Chronological order within the week; the id breaks ties so
        # the split is deterministic.
        items.sort(key=lambda t: (t[0], str(t[1].get("id") or "")))
        used = 0.0
        comp = None
        for d, e, h in items:
            # Split this entry's hours: the part that keeps the week
            # at or under 40 is regular, the rest is overtime.
            reg_h = max(0.0, min(h, ot_threshold - used))
            ot_h = max(0.0, h - reg_h)
            used += h
            if (e.get("jobsite_id") or "") != jobsite_id:
                continue
            if comp is None:
                comp = (e.get("company") or "").strip()
            rate = rate_by_key.get((name, comp))
            staffing = rate.get("staffing") if rate else None
            billing = rate.get("billing") if rate else None
            staffing = float(staffing) if staffing is not None else None
            billing = float(billing) if billing is not None else None
            if staffing is None or billing is None:
                no_rate.add(name)
                lines.append({
                    "employee": name, "date": d.isoformat(),
                    "hours": round(h, 2), "reg_hours": round(reg_h, 2),
                    "ot_hours": round(ot_h, 2),
                    "revenue": None, "labor_cost": None, "no_rate": True,
                })
                continue
            rev = reg_h * billing + ot_h * billing * ot_multiplier
            lab = reg_h * staffing + ot_h * staffing * ot_multiplier
            reg_total += reg_h
            ot_total += ot_h
            revenue += rev
            labor += lab
            lines.append({
                "employee": name, "date": d.isoformat(),
                "hours": round(h, 2), "reg_hours": round(reg_h, 2),
                "ot_hours": round(ot_h, 2),
                "revenue": _money(rev), "labor_cost": _money(lab),
                "no_rate": False,
            })
    lines.sort(key=lambda x: (x["date"], x["employee"].lower()))
    return {
        "reg_hours": round(reg_total, 2),
        "ot_hours": round(ot_total, 2),
        "total_hours": round(reg_total + ot_total, 2),
        "revenue": _money(revenue),
        "labor_cost": _money(labor),
        "no_rate": sorted(no_rate),
        "lines": lines,
    }


def invoice_number(prefix, today=None, suffix=""):
    """Invoice number: company prefix + today's date MMDDYYYY + the
    company's Prefix End code (suffix), exactly like the sheets
    (prefix from the Company mapping). An empty suffix leaves the
    number unchanged."""
    today = today or date.today()
    return f"{prefix}{today.strftime('%m%d%Y')}{suffix or ''}"


def invoice_lines(rows, expenses=None, ot_multiplier=DEFAULT_OT_MULTIPLIER):
    """Build invoice lines from computed payroll rows.

    Per employee: a Regular line then an Overtime line at the billing
    rate (OT unit = billing rate x 1.5), qty = hours. Zero-qty OT lines
    are included, like the sheets. Employees without a rate row cannot
    be priced and are reported in `excluded` instead of being billed $0.

    expenses: iterable of dicts with description + amount.
    Returns {lines, excluded, labour_total, expense_total,
             sub_total, total} — all money rounded to 2 decimals.
    """
    lines = []
    excluded = []
    for r in rows:
        if r.get("no_rate") or r.get("billing") is None:
            excluded.append(r["employee"])
            continue
        billing = float(r["billing"])
        lines.append({
            "employee": r["employee"], "type": "Regular",
            "qty": r["reg_hours"], "unit": _money(billing),
            "total": _money(r["reg_hours"] * billing),
        })
        lines.append({
            "employee": r["employee"], "type": "Overtime",
            "qty": r["ot_hours"], "unit": _money(billing * ot_multiplier),
            "total": _money(r["ot_hours"] * billing * ot_multiplier),
        })
    labour_total = _money(sum(line["total"] for line in lines))
    expense_rows = []
    for ex in expenses or []:
        expense_rows.append({
            "id": ex.get("id"),
            "description": (ex.get("description") or "").strip(),
            "amount": _money(ex.get("amount") or 0),
        })
    expense_total = _money(sum(ex["amount"] for ex in expense_rows))
    return {
        "lines": lines,
        "excluded": excluded,
        "expenses": expense_rows,
        "labour_total": labour_total,
        "expense_total": expense_total,
        "sub_total": _money(labour_total + expense_total),
        "total": _money(labour_total + expense_total),
    }
