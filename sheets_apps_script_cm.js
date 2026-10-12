/**
 * Google Apps Script — Construction Manager (prismxe-app /cm) data backup.
 *
 * Setup (about 5 minutes):
 * 1. Create a new Google Sheet (sheet.new) — name it e.g. "CM Data".
 *    (Use a NEW spreadsheet, not the boiler or patrol one — this script stores
 *    the whole CM DB in a "Data" sheet.)
 * 2. Extensions > Apps Script. Delete any existing code, paste this entire file.
 * 3. Deploy > New deployment > type: Web app
 *    - Execute as: Me
 *    - Who has access: Anyone
 * 4. Deploy, authorize, and copy the Web app URL.
 * 5. Give the URL to Muse — it goes into the Render service as the
 *    CM_SHEETS_URL env var on "prismxe-app".
 *
 * The server then:
 * - POSTs the full CM DB (sites, workers, timecards, incidents, daily_logs,
 *   labour, materials, defects, qa_checks, pins, plans, dispatches, etc.)
 *   on every save_db() call (action=saveData) → "Data" sheet
 * - POSTs each clock in/out as a row (action=timecard) → "Timecards" sheet
 *   (human-readable audit log)
 * - GETs ?action=getData on startup to restore everything after a Render
 *   redeploy wipes the ephemeral disk
 */

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var out = { ok: false };

    if (data.action === "saveData") {
      var dsh = ss.getSheetByName("Data");
      if (!dsh) dsh = ss.insertSheet("Data");
      dsh.getRange("A1").setValue(JSON.stringify(data.data));
      dsh.getRange("B1").setValue(new Date().toISOString());
      out = { ok: true };
    } else if (data.action === "timecard") {
      var tsh = ss.getSheetByName("Timecards");
      if (!tsh) {
        tsh = ss.insertSheet("Timecards");
        tsh.appendRow(["ts", "worker", "site", "action", "clock_in", "clock_out", "hours", "id"]);
      }
      var t = data.timecard || {};
      tsh.appendRow([t.ts, t.worker, t.site, t.action, t.clock_in, t.clock_out, t.hours, t.id]);
      out = { ok: true };
    } else {
      out = { ok: false, error: "unknown action: " + data.action };
    }
    return ContentService.createTextOutput(JSON.stringify(out))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    if (e.parameter.action === "getData") {
      var dsh = ss.getSheetByName("Data");
      var raw = dsh ? dsh.getRange("A1").getValue() : "";
      return ContentService.createTextOutput(JSON.stringify({
        ok: true, data: raw ? JSON.parse(raw) : null
      })).setMimeType(ContentService.MimeType.JSON);
    }
    return ContentService.createTextOutput(JSON.stringify({ ok: false, error: "unknown action" }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
