# Scheduling the daily run and alert notifications

NewsPulse is meant to run once a day, before you sit down, so the dashboard
already knows what happened to your clients overnight. This page ships
ready-to-install scheduler artifacts for macOS and Windows and gives the exact
install command for each. It also documents the notification settings the daily
job reads after each run.

Both artifacts are **templates** that carry `__TOKENS__` you replace with
**absolute paths** for your machine (a scheduler cannot resolve `~` or a relative
path). Each section below gives the substitution and the install command.

> The machine running the job must be logged in to Claude Code — the analyzer
> shells out to the `claude` CLI (subscription auth). See the project README.

First, find the absolute path to the `newspulse` console script the scheduler
will invoke:

```sh
# macOS / Linux
cd newspulse && echo "$(pwd)/.venv/bin/newspulse"
```

```bat
REM Windows (from the newspulse folder)
echo %CD%\.venv\Scripts\newspulse.exe
```

---

## macOS (launchd)

The artifact is [`src/newspulse/schedule/com.newspulse.daily.plist`](../src/newspulse/schedule/com.newspulse.daily.plist).
It runs `newspulse run` every day at **07:00 local time**.

Replace the three tokens with absolute paths, writing the filled-in copy straight
into your per-user `LaunchAgents` directory. Adjust the three paths to your setup:

```sh
mkdir -p ~/Library/LaunchAgents

sed \
  -e 's#__NEWSPULSE_BIN__#/Users/you/newspulse/.venv/bin/newspulse#' \
  -e 's#__NEWSPULSE_DB__#/Users/you/newspulse/newspulse.db#' \
  -e 's#__NEWSPULSE_LOG__#/Users/you/Library/Logs#' \
  newspulse/src/newspulse/schedule/com.newspulse.daily.plist \
  > ~/Library/LaunchAgents/com.newspulse.daily.plist
```

Then load it (the exact install command — uses the absolute path to the
installed plist):

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.newspulse.daily.plist
```

Verify, run once now to test, and (later) uninstall:

```sh
launchctl print gui/$(id -u)/com.newspulse.daily          # verify it is loaded
launchctl kickstart -k gui/$(id -u)/com.newspulse.daily   # run once now
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.newspulse.daily.plist  # uninstall
```

To change the time, edit `StartCalendarInterval` (`Hour`/`Minute`) in the plist
and re-`bootout`/`bootstrap`. If the Mac is asleep at the scheduled time, launchd
runs the job at the next wake.

---

## Windows (Task Scheduler)

The artifact is [`src/newspulse/schedule/newspulse-daily.cmd`](../src/newspulse/schedule/newspulse-daily.cmd).
Edit it and replace the two tokens with absolute paths:

- `__NEWSPULSE_BIN__` → e.g. `C:\Users\you\newspulse\.venv\Scripts\newspulse.exe`
- `__NEWSPULSE_DB__`  → e.g. `C:\Users\you\newspulse\newspulse.db`

Save the edited copy somewhere stable, e.g.
`C:\Users\you\newspulse\schedule\newspulse-daily.cmd`.

Register it to run every day at **07:00** (the exact install command — uses the
absolute path to the saved script):

```bat
schtasks /Create ^
  /TN "NewsPulse Daily" ^
  /TR "C:\Users\you\newspulse\schedule\newspulse-daily.cmd" ^
  /SC DAILY ^
  /ST 07:00
```

Verify, run once now to test, and (later) uninstall:

```bat
schtasks /Query /TN "NewsPulse Daily"            REM verify it is registered
schtasks /Run   /TN "NewsPulse Daily"            REM run once now
schtasks /Delete /TN "NewsPulse Daily" /F        REM uninstall
```

A scheduled task runs in a non-interactive session where a desktop toast may not
surface; on Windows, prefer the **email** channel (below).

---

## Alert notifications

After a run, if any alerts fired, the job can deliver a one-shot summary — the
client, the alert count, and the top headline per client. A quiet day (no alerts)
sends **nothing**: there is no "0 alerts" noise.

The channel is selected by `NEWSPULSE_NOTIFY_CHANNEL`. It defaults to **off** when
unset, so an unconfigured install never notifies:

| `NEWSPULSE_NOTIFY_CHANNEL` | Behavior                                             |
| -------------------------- | ---------------------------------------------------- |
| unset / `off`              | No notification (default).                           |
| `desktop`                  | Local desktop notification (macOS/Linux/Windows).    |
| `email`                    | Email via SMTP (see below).                          |
| `on` / `true`              | Enables notifications via the desktop channel.       |

A notification failure never fails the run: the sweep's data is already persisted
before the notification is attempted, so a broken SMTP config just logs an ERROR
to the rotating log — it never rolls the run back.

### Email (SMTP)

Set the channel to `email` and provide the SMTP settings. **Credentials come from
the environment only — never hardcode them into the scripts, and they are never
written to the log.**

| Variable                     | Required | Notes                                         |
| ---------------------------- | -------- | --------------------------------------------- |
| `NEWSPULSE_SMTP_HOST`        | yes      | SMTP server hostname.                         |
| `NEWSPULSE_SMTP_RECIPIENT`   | yes      | Where the summary is sent.                    |
| `NEWSPULSE_SMTP_PORT`        | no       | Default `587` (STARTTLS submission).          |
| `NEWSPULSE_SMTP_USERNAME`    | no       | Login user, if the relay requires auth.       |
| `NEWSPULSE_SMTP_PASSWORD`    | no       | Login password (keep this out of scripts).    |
| `NEWSPULSE_SMTP_SENDER`      | no       | `From:` address; defaults to the recipient.   |
| `NEWSPULSE_SMTP_STARTTLS`    | no       | `true` (default) upgrades the connection.     |

Example (macOS/Linux shell profile, or the launchd plist's
`EnvironmentVariables`):

```sh
export NEWSPULSE_NOTIFY_CHANNEL=email
export NEWSPULSE_SMTP_HOST=smtp.example.com
export NEWSPULSE_SMTP_RECIPIENT=pr@example.com
export NEWSPULSE_SMTP_USERNAME=mailer
export NEWSPULSE_SMTP_PASSWORD=...   # from a secret store, not committed
```

On Windows, uncomment and fill in the `NEWSPULSE_SMTP_*` lines in
`newspulse-daily.cmd`, but source the password from an existing environment
variable or a secret manager rather than typing it into the file.
