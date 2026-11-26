#!/usr/bin/env python3
"""Nightly auto-sync: pull closed timecards from the app's own modules
into payroll, without a manual preview/confirm step.

Mappings (user-approved 2026-11-17):
  PrismXE          <- CM (data_cm.json)
  Hanseong         <- Hanseong Patrol (data_patrol.json)
  COA - Carpenter  <- COA (data_coa.json)
  COA - Drywall    <- COA (data_coa.json)
  KB Link: no app timecard source; unchanged (CSV import as before).
  Myeongjin: no app timecard source; unchanged.

Rules (same as the manual Import from App):
- Only rows with a matched rate row (exact, alias, or
  case-insensitive name match) are imported. Unmatched rows are
  skipped and listed in the report — never imported silently.
- Exact duplicates (company + canonical employee + date + hours)
  are skipped for timecard sources. For punch sources (Patrol —
  one derived row per employee + date by construction) the key is
  company + canonical employee + date, so a day corrected in
  payroll never re-imports its old hours as a second row.
- Imported entries keep `source` (the app module they came from)
  plus the first-in / last-out punch ids, so a later payroll
  correction can rewrite the matching Patrol punches
  (see reverse_sync_patrol_punches in payroll_import_app.py).
- Open shifts (still clocked in) are skipped.
- Entries are stored under the CANONICAL rate-row name so payroll
  math matches them without needing a stored alias.

GO_LIVE: only rows dated on/after 2026-11-11 are imported. Earlier
rows were already handled (the PrismXE screenshot import and the
COA manual rebuilds), and the sources disagree with payroll on some
of them (corrected hours, split shifts); importing them would create
second entries for the same day. The window is [GO_LIVE, today], so
re-runs stay idempotent: already-imported rows are duplicates.

Run with the app STOPPED (it holds data_payroll.json in memory, so
editing the file while it runs would be overwritten on the next
save):
  systemctl stop prismxe
  cp data_payroll.json data_payroll.json.bak-autosync-YYYYMMDD-HHMMSS
  python3 payroll_auto_sync.py
  systemctl start prismxe

Prints a JSON summary to stdout. Exit 0 on success (even when
nothing new was imported); exit 1 on any load/save error.
"""
import json
import os
import sys
import uuid
from datetime import date
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from payroll_import_app import SOURCES, build_preview, import_rows  # noqa: E402

PHOENIX = ZoneInfo("America/Phoenix")
GO_LIVE = date(2026, 11, 11)

MAPPINGS = [
    ("PrismXE", "cm"),
    ("Hanseong", "patrol"),
    ("COA - Carpenter", "coa"),
    ("COA - Drywall", "coa"),
]

PAYROLL_PATH = os.path.join(BASE_DIR, "data_payroll.json")


def main():
    today = date.today()
    # Use Phoenix local date even if the server clock is UTC.
    try:
        from datetime import datetime
        today = datetime.now(PHOENIX).date()
    except Exception:
        pass
    end = today
    start = GO_LIVE

    try:
        with open(PAYROLL_PATH) as f:
            payroll = json.load(f)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"cannot load data_payroll.json: {exc}"}))
        return 1
    if not isinstance(payroll, dict):
        print(json.dumps({"ok": False, "error": "data_payroll.json is not an object"}))
        return 1

    entries = payroll.get("entries", [])
    rates = payroll.get("rates", [])
    aliases = payroll.get("aliases", [])
    if not isinstance(entries, list):
        print(json.dumps({"ok": False, "error": "entries is not a list"}))
        return 1

    summary = {"ok": True, "window": [start.isoformat(), end.isoformat()],
               "companies": {}, "total_added": 0}
    for company, source in MAPPINGS:
        preview = build_preview(BASE_DIR, source, company, rates,
                                aliases, start, end)
        to_add, n_dupes, unmatched = import_rows(
            preview["rows"], entries, company, aliases,
            source_label=preview["label"], use_canonical=True,
            source=source,
            dedupe_by_date=(SOURCES[source]["kind"] == "punches"))
        for e in to_add:
            e["id"] = uuid.uuid4().hex[:12]
            entries.append(e)
        summary["companies"][company] = {
            "source": source,
            "added": len(to_add),
            "duplicates_skipped": n_dupes,
            "unmatched_rows": len(unmatched),
            "unmatched_names": sorted({(u.get("employee") or "")
                                       for u in unmatched}),
            "open_shifts_skipped": len(preview["skipped"]),
            "added_rows": [{"employee": a["employee"], "date": a["date"],
                            "hours": a["payroll_hours"]} for a in to_add],
        }
        summary["total_added"] += len(to_add)

    payroll["entries"] = entries
    try:
        with open(PAYROLL_PATH, "w") as f:
            json.dump(payroll, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"cannot save data_payroll.json: {exc}"}))
        return 1

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
