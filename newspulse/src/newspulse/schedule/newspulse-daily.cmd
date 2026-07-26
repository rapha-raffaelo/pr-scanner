@echo off
REM ===========================================================================
REM  NewsPulse daily sweep - Windows Task Scheduler command script.
REM
REM  This is a TEMPLATE. Replace the two __TOKENS__ below with ABSOLUTE paths
REM  before registering the task. See docs\scheduling.md for the exact
REM  `schtasks /Create` command that runs this once per day.
REM
REM    __NEWSPULSE_BIN__  absolute path to newspulse.exe
REM                       (e.g. C:\Users\you\newspulse\.venv\Scripts\newspulse.exe)
REM    __NEWSPULSE_DB__   absolute path to the SQLite database file
REM
REM  Notification note: a scheduled task runs in a non-interactive session, where
REM  a desktop toast may not surface. On Windows, prefer the email channel (set
REM  NEWSPULSE_NOTIFY_CHANNEL=email and the NEWSPULSE_SMTP_* vars below).
REM ===========================================================================

setlocal

set "NEWSPULSE_DATABASE_PATH=__NEWSPULSE_DB__"

REM  Notifications default to OFF. A Task Scheduler job runs in a non-interactive
REM  session where a desktop toast may not surface, so shipping "desktop" here would
REM  silently drop notifications. Pick a channel that actually delivers below: email
REM  is recommended on Windows; desktop is best-effort.
set "NEWSPULSE_NOTIFY_CHANNEL=off"

REM  --- Email channel (recommended on Windows; uncomment and fill in) ----------
REM  set "NEWSPULSE_NOTIFY_CHANNEL=email"
REM  set "NEWSPULSE_SMTP_HOST=smtp.example.com"
REM  set "NEWSPULSE_SMTP_PORT=587"
REM  set "NEWSPULSE_SMTP_USERNAME=%NEWSPULSE_SMTP_USERNAME%"
REM  set "NEWSPULSE_SMTP_PASSWORD=%NEWSPULSE_SMTP_PASSWORD%"
REM  set "NEWSPULSE_SMTP_RECIPIENT=pr@example.com"

REM  --- Desktop channel (best-effort; may not surface from a scheduled task) ---
REM  set "NEWSPULSE_NOTIFY_CHANNEL=desktop"

"__NEWSPULSE_BIN__" run
set "EXITCODE=%ERRORLEVEL%"

endlocal & exit /b %EXITCODE%
