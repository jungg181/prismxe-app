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
 * - POSTs plan images (action=uploadPlan) -> Google Drive folder "CM Plans"
 *   (shared with link). Render's disk is ephemeral, so plan/blueprint images
 *   uploaded in /cm/blueprints would otherwise vanish on every redeploy.
 *   The server stores the returned Drive URL on the plan record.
 * - POSTs COA database backups (action=saveCoaBackup) -> Google Drive folder
 *   (COA_Data.json + COA_Timecards.csv); GETs them back (action=getCoaBackup)
 *   to restore after a redeploy.
 * - Sends pay-period confirmation emails to COA workers (action=sendEmail,
 *   via the script owner's Gmail) and notifies the owner when a worker
 *   disputes their hours (action=notifyDispute).
 *
 * NOTE: after pasting new code, Deploy > Manage deployments > Edit >
 * New version, otherwise the web app keeps running the old code. The first
 * uploadPlan call will ask you to authorize Drive access.
 */

function saveTextFile(folder, name, mime, content, desc) {
  var blob = Utilities.newBlob(content, mime, name);
  var files = folder.getFilesByName(name);
  var file;
  if (files.hasNext()) {
    file = files.next();
    file.setContent(blob.getDataAsString());
  } else {
    file = folder.createFile(blob);
  }
  file.setDescription(desc);
  return file;
}

function readJsonFile(folder, name) {
  var files = folder.getFilesByName(name);
  if (!files.hasNext()) return null;
  try {
    return JSON.parse(files.next().getBlob().getDataAsString());
  } catch (e) {
    return null;
  }
}

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
    } else if (data.action === "uploadPlan") {
      // data: {image_base64, mime, filename, site_id, floor}
      var folders = DriveApp.getFoldersByName("CM Plans");
      var folder = folders.hasNext() ? folders.next() : DriveApp.createFolder("CM Plans");
      var blob = Utilities.newBlob(Utilities.base64Decode(data.image_base64),
                                   data.mime || "image/jpeg",
                                   data.filename || "plan.jpg");
      var file = folder.createFile(blob);
      file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
      file.setDescription("site=" + (data.site_id || "") + " floor=" + (data.floor || ""));
      out = { ok: true,
              url: "https://drive.google.com/thumbnail?id=" + file.getId() + "&sz=w2000",
              file_id: file.getId() };
    } else if (data.action === "saveCoaBackup") {
      // data: {coa_json, cm_json, coa_csv, folder_id} — durable backup of
      // BOTH databases into the user's Drive folder, so nothing is lost when
      // Render wipes its ephemeral disk on redeploy. {probe:true} just
      // checks the action exists (writes nothing).
      if (data.probe) {
        out = { ok: true, probe: true };
      } else {
        var coaFolder = DriveApp.getFolderById(data.folder_id);
        saveTextFile(coaFolder, "COA_Data.json", "application/json",
                     data.coa_json || "{}",
                     "COA database backup · updated " + new Date().toISOString());
        saveTextFile(coaFolder, "CM_Data.json", "application/json",
                     data.cm_json || "{}",
                     "CM database backup · updated " + new Date().toISOString());
        if (data.coa_csv) {
          saveTextFile(coaFolder, "COA_Timecards.csv", "text/csv", data.coa_csv,
                       "COA timecards · updated " + new Date().toISOString());
        }
        out = { ok: true };
      }
    } else if (data.action === "getCoaBackup") {
      // Returns {coa: {...}, cm: {...}} so the server can restore both DBs
      // after a redeploy. {probe:true} just checks the action exists.
      if (data.probe) {
        out = { ok: true, probe: true };
      } else {
        var gfolder = DriveApp.getFolderById(data.folder_id);
        var coaData = readJsonFile(gfolder, "COA_Data.json");
        var cmData = readJsonFile(gfolder, "CM_Data.json");
        if (coaData || cmData) {
          out = { ok: true, data: { coa: coaData, cm: cmData } };
        } else {
          out = { ok: false, error: "no backup found" };
        }
      }
    } else if (data.action === "sendEmail") {
      // data: {to, subject, body, html} — sends via the script owner's Gmail.
      // {probe: true} just checks the action exists (sends nothing).
      if (data.probe) {
        out = { ok: true, probe: true };
      } else {
        GmailApp.sendEmail(data.to, data.subject, data.body || "",
                           { htmlBody: data.html || data.body || "" });
        out = { ok: true };
      }
    } else if (data.action === "notifyDispute") {
      // data: {worker, period, note} — emails the script owner that a worker
      // disputed their hours.
      var owner = Session.getActiveUser().getEmail();
      var subj = "COA hours disputed by " + (data.worker || "a worker");
      var txt = (data.worker || "A worker") + " flagged a problem with their " +
                "hours for " + (data.period || "") + ".\n\nNote:\n" +
                (data.note || "(no note)") +
                "\n\nReview it in the COA timecards admin.";
      GmailApp.sendEmail(owner, subj, txt);
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
