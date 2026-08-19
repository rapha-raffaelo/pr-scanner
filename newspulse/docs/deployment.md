# Deploying NewsPulse

The tool was built to run on one machine (DEC-3). Putting it on a server is
supported, but it changes one thing fundamentally: **the loopback bind was the
entire security model.** Everything below follows from replacing it.

Three routes. Pick by how much host you want to own.

---

## Route A — Cloudflare Tunnel (fastest, no server)

Keeps running on the Mac. Cloudflare terminates TLS and does the authenticating,
so you never expose a port and never manage a host.

```sh
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create newspulse
cloudflared tunnel route dns newspulse newspulse.example.com
cloudflared tunnel run --url http://127.0.0.1:8000 newspulse
```

Then, in the Cloudflare dashboard, add an **Access** application for that
hostname with an email policy listing exactly the people who may see it.

Nothing else changes: the app stays on `127.0.0.1`, so its own auth stays off
and Access is the gate. **Do not skip the Access policy** — a tunnel without it
publishes the portfolio to anyone with the URL.

Trade-off: available only while the Mac is awake and the tunnel is running.

---

## Route B — Railway (managed, deploys on push)

Right when you already have Railway capacity. It builds from the Dockerfile,
redeploys on every push, and keeps a volume across deploys.

1. **New project → Deploy from GitHub repo.** No build configuration needed:
   the `Dockerfile` is at the repository root, where Railway auto-detects it.

   **Leave Root Directory unset.** It must stay empty for the root `Dockerfile`
   to be found, and Railway reads `railway.json` from the repository root
   regardless — it [does not apply Root Directory to that
   file](https://docs.railway.com/deployments/monorepo), so setting one breaks
   detection without moving the config to match.

   The Dockerfile sits at the root rather than beside the app so that the build
   never depends on the config file being read at all: `railway.json` only adds
   the healthcheck and restart policy. Its `COPY` paths are therefore prefixed
   with `newspulse/`, and compose builds from the same repo-root context, so a
   build that works locally cannot fail here for a path reason.

2. **Attach a volume at `/data`.** One mount holds both the archive and the
   `claude` login — Railway gives a service one volume, and there is nothing to
   gain from separating two things that are lost together anyway.

3. **Variables** (Settings → Variables):

   ```
   NEWSPULSE_AUTH_USER=lucas
   NEWSPULSE_AUTH_PASSWORD=<long and random>
   NEWSPULSE_BASE_URL=https://<your>.up.railway.app
   NEWSPULSE_CLAUDE_CONFIG_DIR=/data/claude
   ```

   Railway injects `PORT`; the app binds it automatically. Do not set
   `NEWSPULSE_WEB_PORT` — it would override the platform and the router would
   reach a closed socket.

   Displayed times default to `Europe/Berlin`, so nothing has to be set for a
   German reader. Set `NEWSPULSE_TIMEZONE=<IANA name>` if the reader is
   elsewhere. Do **not** leave it to the container's own clock: that is UTC on
   Railway, which is what once rendered a 10:00 sweep as "Letzter Lauf 08:00
   Uhr".

4. **Log the CLI in, once, inside the running service.** This cannot be done
   from your Mac: on macOS the Claude credentials live in the **Keychain**, not
   in `~/.claude`, so there is no file to copy. It has to be a device flow on
   the host.

   ```sh
   railway ssh
   CLAUDE_CONFIG_DIR=/data/claude claude login
   ```

   The credentials land on the volume and survive redeploys. **They do not
   survive deleting the volume** — if you ever recreate it, repeat this step, and
   until you do, analysis and Captain Comms both fail.

5. **The daily sweep** as a second service from the same repo, with a cron
   schedule and the start command:

   ```sh
   uv run newspulse run && uv run newspulse digest
   ```

   It needs the same variables and the same volume.

   **Railway's cron is UTC**, and `NEWSPULSE_TIMEZONE` does not change that — it
   is a display setting, not the container's clock. Convert the time you want by
   hand: `10 4 * * *` is 06:10 in Berlin summer time, `10 5 * * *` in winter.
   Pick one and accept the hour of drift across the DST switch, or shift it twice
   a year.

**Cost note.** The sweep spends most of its wall-clock waiting on `claude -p`,
so it is cheap; the dashboard is idle almost all day. The thing to watch is not
CPU but the volume, since the archive grows with every run.

The subscription is the other budget. Beyond the analysis batches, each run
spends at most **one extra call per mandate** — the positioning draft, and only
for a mandate whose topic radar found something new that morning. A mandate with
no keywords and no alert topics has no radar and costs nothing extra.

## Route C — a small always-on host (Docker)

Any €5/month VPS. Everything needed is in the repo.

### 1. Credentials

```sh
cp .env.example .env
# NEWSPULSE_AUTH_USER, NEWSPULSE_AUTH_PASSWORD (long and random),
# NEWSPULSE_BASE_URL=https://your-domain
```

The app **refuses to start** bound to anything but loopback while those are
unset. That is deliberate: forgetting the password here is not an inconvenience,
it is publishing client coverage.

### 2. Log the Claude CLI in, once

Analysis and Captain Comms both shell out to `claude`. The container ships the
CLI but no credentials; the login lives in a mounted directory:

```sh
mkdir -p claude data
docker compose run --rm -v "$PWD/claude:/claude" -e CLAUDE_CONFIG_DIR=/claude web claude login
```

Follow the device flow once. The resulting `./claude` is a **secret** — back it
up like one, and keep it out of the image and out of git.

> This runs a shared tool on one person's subscription. For two people that is
> usually fine; if it grows, switch the analyzer to
> `NEWSPULSE_ANALYZER_BACKEND=claude_api` with an API key. Note that this covers
> the analyzer only — Captain Comms streams through the CLI and has no API path
> yet, so it would stop working.

### 3. TLS, then start

Basic auth over plain HTTP sends the password in clear on every request, which
is worse than none because it looks like protection. Terminate TLS in front —
Caddy is two lines:

```
newspulse.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

```sh
docker compose up -d
docker compose logs -f web
```

Compose binds the app to `127.0.0.1:8000`, so only the proxy can reach it even
if the firewall is wrong.

### 4. Check it

```sh
curl -sI https://newspulse.example.com/            # expect 401
curl -sI -u lucas:PASSWORD https://newspulse.example.com/   # expect 200
docker compose exec web uv run newspulse check-feeds
```

---

## Without Docker (systemd)

```ini
# /etc/systemd/system/newspulse.service
[Unit]
Description=NewsPulse dashboard
After=network-online.target

[Service]
User=newspulse
WorkingDirectory=/opt/newspulse
EnvironmentFile=/opt/newspulse/.env
ExecStart=/usr/local/bin/uv run newspulse-web
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```cron
# The sweep, and the brief that follows it.
10 6 * * * cd /opt/newspulse && /usr/local/bin/uv run newspulse run && /usr/local/bin/uv run newspulse digest
```

---

## Notifications on a server

The alert channel defaults to `off`, so nothing is emitted until configured.
Do **not** set it to `desktop` on a host: that shells out to `notify-send`,
which does not exist in a headless container, and every alert would fail (logged,
never fatal — notification failure cannot break a run). Use `email`:

```
NEWSPULSE_NOTIFY_CHANNEL=email
NEWSPULSE_SMTP_HOST=…
NEWSPULSE_SMTP_RECIPIENT=…
```

## Before you hand out the URL

- `curl -I https://…/` returns **401**, not 200.
- The site is **HTTPS**. Basic auth on HTTP is not protection.
- `./claude` and `.env` are backed up, and in neither git nor the image.
- `./data/newspulse.db` is backed up — it is the whole archive, one file.
- `NEWSPULSE_BASE_URL` is set, or the digest mails a dead link.
- Consider `NEWSPULSE_ALERT_THRESHOLD=8`: at 7 roughly a third of coverage
  flags, and an alert list nobody trusts is one nobody reads.

---

## The AI fallback (Gemini)

Analysis, the advisor and Captain Comms all run on the Claude subscription. When
that subscription hits its usage limit, the nightly sweep stops scoring and the
morning shows a gap in the archive that nobody asked for. Setting a Gemini key
lets those paths continue on a backup provider.

```
NEWSPULSE_GEMINI_API_KEY=<key from aistudio.google.com>
NEWSPULSE_GEMINI_MODEL=gemini-2.5-flash        # optional
```

Nothing else changes: with no key set, every path behaves exactly as before.

**It engages only on quota errors.** A missing CLI, a broken login, a parse
failure or a network fault still fails on the primary, loudly. That restriction
is the point — falling back on *any* error would turn a bug into a nightly bill,
and would have hidden the very login problem that took this deployment three
attempts to diagnose.

Two consequences worth knowing before you set the key:

- **A second data processor.** Article text and client notes would go to Google
  as well as Anthropic. For agency work under GDPR that is a contractual
  question, not just a technical one. This is why the key is opt-in and why
  nothing falls back without it.
- **Mixed scoring is avoided, not merged.** If the limit is hit part-way through
  a client, that client is re-analysed from scratch on the fallback and the
  partial Claude result is discarded. Scores are compared against each other —
  in the ranking, in the alert threshold, in share of voice — so one consistent
  second-choice reading beats a spliced one.

Captain Comms switches only *before* the first word reaches the drawer. Once an
answer has started there is no honest way to swap models mid-sentence, so the
error stands and you ask again.

Settings shows whether the fallback is armed, so its absence is visible on an
ordinary day rather than on the morning it was needed.

---

## The mailbox (Gmail)

Optional, and off until both variables below are set: with no client id the
Settings panel says the integration is not set up and offers no button.

**This assumes a Google Workspace account on a domain you control.** That is not
a preference, it is what makes the whole thing legal to run without an audit —
see "On a personal @gmail.com" at the end.

### 1. A Google Cloud project

console.cloud.google.com → **New project**. Any name; it exists to hold one
OAuth client.

Then **APIs & Services → Library → Gmail API → Enable**. Nothing else needs
enabling: RauteOS talks to the Gmail API and to Google's OAuth endpoints, and to
nothing else at Google.

### 2. The consent screen, as an *Internal* app

**APIs & Services → OAuth consent screen → User type: Internal.**

This is the load-bearing choice (DEC-5). Gmail read access is a *restricted*
scope. An app that asks for one from users outside its own organisation needs
Google verification plus a recurring third-party security assessment — weeks of
work and a running cost. An **Internal** app, publishable only inside your own
Workspace and usable only by accounts in it, needs neither.

Internal is only offered when the Cloud project belongs to a Workspace
organisation. If the radio button is greyed out, you are on a personal account —
skip to the last section.

Add the two scopes, and only these two:

```
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
```

They are exactly what DEC-4 licenses ("lesen und senden"), and they are what the
consent screen will show the person connecting. RauteOS asks for no others: the
list lives in one tuple in `gmail_link.py`, so what Google displays and what the
code can do are the same sentence. Do **not** add `gmail.modify` or
`mail.google.com` "to be safe" — they would licence deleting and relabelling
mail, which nothing here does.

### 3. The OAuth client

**Credentials → Create credentials → OAuth client ID → Web application.**

Under **Authorised redirect URIs**, add exactly:

```
<NEWSPULSE_BASE_URL>/settings/gmail/callback
```

e.g. `https://newspulse.up.railway.app/settings/gmail/callback`. Google matches
this character for character. The app derives its own copy from
`NEWSPULSE_BASE_URL`, so the two cannot drift — but **changing that variable
means re-registering the URI**, or the next connection attempt dies at Google
with `redirect_uri_mismatch`.

Copy the client id and secret into the environment, beside the other secrets:

```
NEWSPULSE_GMAIL_CLIENT_ID=<...>.apps.googleusercontent.com
NEWSPULSE_GMAIL_CLIENT_SECRET=<...>
```

Restart, open **Einstellungen → Postfach**, press **Postfach verbinden**, and
grant consent as the mailbox that sends the letters. The panel then names the
address it read back from that account's Gmail profile — not one anybody typed.

### 4. What lands on the volume

The consent produces a **refresh token**, and unlike every other secret here it
is obtained at runtime, so it cannot come from an environment variable. It is
written to a file beside the database:

```
/data/gmail_token.json     # mode 0600, owner only
```

Never to a table. That is deliberate: the database is copied for backups and
exported to Excel, and a credential in it would ride along both times. Treat the
file like `./claude` and `.env` — a secret, backed up as one, in neither git nor
the image.

Consequences worth knowing:

- **It survives a redeploy, not a deleted volume.** Recreate the volume and the
  panel is honestly back to "kein Postfach verbunden"; connect once more.
- **Google can end it from its side.** Remove RauteOS under *Google account →
  Security → Third-party apps*, and the next refresh fails with `invalid_grant`.
  The panel then shows disconnected *with that reason*, rather than throwing.
- **Trennen revokes.** The disconnect button revokes the token at Google and
  deletes the file. Stored letters, replies and contacts are untouched — nothing
  about the mailbox connection is a reason to lose the record of what was sent.

### On a personal @gmail.com

There is no Internal option, so the app would be **External**. Two ways from
there and neither is good:

- **Testing mode** — no verification needed, but Google expires refresh tokens
  after roughly seven days. The connection dies every week, which for a daily
  sync means it dies every Monday.
- **Published** — needs Google verification *and* an annual third-party security
  assessment (restricted scopes), which is a five-figure recurring cost for a
  one-mailbox tool.

So on a personal account DEC-5 has to go a different way: an app password over
IMAP (a full-mailbox credential that cannot be scoped, DEC-5 option B), or a
dedicated forwarding address (option C). Neither is what is built here. Move the
mailbox to a Workspace domain if you want this feature.
