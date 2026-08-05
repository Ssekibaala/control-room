/**
 * GTL Control Room - scheduler only.
 *
 * This is the ENTIRE Apps Script. It does not read Gmail, does not
 * touch any mailbox, mail.teletracfleets.com isn't a Gmail account
 * so GmailApp couldn't read it anyway. This script's only job is to
 * be a free, reliable clock that pings the deployed app once a day.
 * All the real work (reading email, downloading reports, analysis)
 * happens in Python, currently deployed on Northflank (see DEPLOY.md).
 *
 * SETUP:
 *   1. script.google.com -> New project, paste this in.
 *   2. Replace APP_URL and API_KEY below.
 *   3. Run `setupDailyTrigger` once manually to install the trigger
 *      (Apps Script will ask you to authorize it, that's normal).
 *   4. Check Triggers (clock icon, left sidebar) to confirm it's set
 *      for 4:15 AM daily.
 */

const APP_URL = 'https://your-app-name.your-northflank-domain/api/import';
const API_KEY = 'replace-with-the-same-value-as-IMPORT_API_KEY-on-your-deploy';

function runDailyImport() {
  const options = {
    method: 'get',
    headers: { 'X-API-Key': API_KEY },
    muteHttpExceptions: true,   // so a failure doesn't just throw, we can log it below
  };

  const response = UrlFetchApp.fetch(APP_URL, options);
  const status = response.getResponseCode();
  const body = response.getContentText();

  console.log(`Import request finished, status ${status}`);
  console.log(body);

  if (status !== 200) {
    // Optional: email yourself on failure instead of only checking logs.
    // MailApp.sendEmail('you@example.com', 'GTL import failed', body);
  }
}

/** Run this ONCE manually from the Apps Script editor to install the trigger. */
function setupDailyTrigger() {
  // Remove any existing triggers for this function first, so re-running
  // this setup doesn't create duplicates that fire twice.
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'runDailyImport') ScriptApp.deleteTrigger(t);
  });

  ScriptApp.newTrigger('runDailyImport')
    .timeBased()
    .atHour(4)
    .nearMinute(15)
    .everyDays(1)
    .create();

  console.log('Daily trigger installed for ~4:15 AM.');
}
