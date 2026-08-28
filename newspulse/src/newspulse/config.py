"""Runtime configuration for NewsPulse.

Every tunable loads from an environment variable and falls back to a module-level
named-constant default. No magic numbers are inlined at the call sites: the whole
point of this module is that the rest of the package imports named values from
here rather than hard-coding paths, thresholds, or batch sizes.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_log = logging.getLogger(__name__)

# --- Named-constant defaults (the "why" lives next to each) --------------------

# The SQLite file lands in the current working directory unless overridden, so a
# freshly cloned checkout works with no configuration.
_DEFAULT_DB_FILENAME = "newspulse.db"

# importance_score (0..10) at or above which an article is auto-flagged as an
# alert even when it matches no explicit alert_topic. 7/10 is "clearly important".
_DEFAULT_ALERT_THRESHOLD = 7

# Max articles handed to the analyzer in a single batched call.
#
# Was 20, chosen when the curated registry was the only source and a client saw a
# handful of stories a day. Per-client Google News searches raised that several
# fold, so batches now run consistently full and were observed timing out against
# _DEFAULT_ANALYZER_TIMEOUT — and a batch that gives up drops *every* article in
# it. Halving the batch both shortens each call (so it finishes inside the
# ceiling) and halves how much coverage one give-up can cost.
_DEFAULT_BATCH_SIZE = 10

# Wall-clock ceiling (seconds) for a single `claude -p` subprocess call. Raised
# from 120s after observing real batches exceed it: 120 left no headroom for a
# cold CLI start on top of a full batch, and the cost of overshooting is dropped
# coverage, while the cost of a longer ceiling is only a slower failure on the
# rare genuinely-hung call.
_DEFAULT_ANALYZER_TIMEOUT = 180

# Analyzer backend id. The subscription subprocess backend is the default; the
# metered API backend is opt-in (see PRD: subscription-first).
_DEFAULT_ANALYZER_BACKEND = "claude_code"

# How many mandate profiles one background refresh pass may re-read. Each costs a
# live web search plus a model call, so this is a spend ceiling before it is
# anything else: five a day drains a sixty-mandate portfolio inside a fortnight
# without a burst anybody notices. Raise it on a large portfolio that has to
# catch up, lower it when the grounded provider is rationing.
_DEFAULT_PROFILE_REFRESH_PER_RUN = 5

# Dashboard bind address. Loopback by default because this is a single-user local
# tool (DEC-3), not a shared service — nothing should be exposed on the network
# unless the operator opts in via NEWSPULSE_WEB_HOST.
_DEFAULT_WEB_HOST = "127.0.0.1"
_DEFAULT_WEB_PORT = 8000

# Env var names, kept in one place so callers never spell them by hand.
_ENV_DATABASE_PATH = "NEWSPULSE_DATABASE_PATH"
_ENV_ALERT_THRESHOLD = "NEWSPULSE_ALERT_THRESHOLD"
_ENV_BATCH_SIZE = "NEWSPULSE_BATCH_SIZE"
_ENV_ANALYZER_TIMEOUT = "NEWSPULSE_ANALYZER_TIMEOUT"
_ENV_ANALYZER_BACKEND = "NEWSPULSE_ANALYZER_BACKEND"
_ENV_PROFILE_REFRESH_PER_RUN = "NEWSPULSE_PROFILE_REFRESH_PER_RUN"
_ENV_GOOGLE_NEWS = "NEWSPULSE_GOOGLE_NEWS"
_ENV_CLAUDE_CONFIG_DIR = "NEWSPULSE_CLAUDE_CONFIG_DIR"
_ENV_AUTH_USER = "NEWSPULSE_AUTH_USER"
_ENV_AUTH_PASSWORD = "NEWSPULSE_AUTH_PASSWORD"
_ENV_GOOGLE_CLIENT_ID = "NEWSPULSE_GOOGLE_CLIENT_ID"
_ENV_GOOGLE_CLIENT_SECRET = "NEWSPULSE_GOOGLE_CLIENT_SECRET"
_ENV_GMAIL_CLIENT_ID = "NEWSPULSE_GMAIL_CLIENT_ID"
_ENV_GMAIL_CLIENT_SECRET = "NEWSPULSE_GMAIL_CLIENT_SECRET"
_ENV_GMAIL_CLIENT_ID_BARE = "GMAIL_CLIENT_ID"
_ENV_GMAIL_CLIENT_SECRET_BARE = "GMAIL_CLIENT_SECRET"
_ENV_ALLOWED_EMAILS = "NEWSPULSE_ALLOWED_EMAILS"
_ENV_SESSION_SECRET = "NEWSPULSE_SESSION_SECRET"
_ENV_BASE_URL = "NEWSPULSE_BASE_URL"
_ENV_GEMINI_API_KEY = "NEWSPULSE_GEMINI_API_KEY"
_ENV_GEMINI_MODEL = "NEWSPULSE_GEMINI_MODEL"
_ENV_VISIBILITY = "NEWSPULSE_VISIBILITY"
_ENV_VISIBILITY_EVERY_DAYS = "NEWSPULSE_VISIBILITY_EVERY_DAYS"

# The OAuth client of the Google Cloud project the mailbox is connected through
# (DEC-5: an *Internal* app inside RAUTE's own Workspace, so no Google
# verification and no annual security assessment). Empty means the integration is
# not offered at all — Settings says so rather than rendering a button that would
# fail at Google with invalid_client.
#
# Both halves come from the environment, exactly like GEMINI_API_KEY. The refresh
# token cannot: it is obtained at runtime, so it goes to a 0600 file beside the
# database (see newspulse.gmail_link) and never into a table, where a database
# copy or the Excel export would carry it out of the machine.
_DEFAULT_GMAIL_CLIENT_ID = ""
_DEFAULT_GMAIL_CLIENT_SECRET = ""

# Where Google sends the consent result. Derived from BASE_URL rather than
# configured on its own: the value must match an authorised redirect URI in the
# Cloud console *verbatim*, and two independently-spelled copies of the same
# address is how a working client id starts answering redirect_uri_mismatch.
GMAIL_CALLBACK_PATH = "/settings/gmail/callback"

# Gemini is the *fallback* provider, used only when the Claude subscription
# reports that its usage limit is exhausted (see newspulse.quota). It is off
# unless a key is present: a fallback that engages without the operator having
# chosen it would move client coverage to a second vendor silently.
#
# Note what this changes legally, not just technically. The subscription path
# sends article text and client notes to Anthropic, which is already the
# processor. Enabling this adds Google as a second processor for the same
# material, which is a data-protection decision — hence opt-in by key, never a
# default. The deployment guide says so where the key is configured.
_DEFAULT_GEMINI_API_KEY = ""

# Flash rather than Pro: this is batch classification against a fixed schema,
# where throughput and cost matter more than reasoning depth, and the fallback
# exists to keep the morning sweep alive rather than to match Claude's output.
# Overridable because model names age faster than this code.
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Dashboard credentials. Empty means no authentication, which is only tolerable
# on a loopback bind — web.auth refuses to start a network-reachable server
# without them. Read from the environment and never logged, the same posture as
# the SMTP password.
_DEFAULT_AUTH_USER = ""
_DEFAULT_AUTH_PASSWORD = ""

# The address the dashboard is reachable at, used in links that leave the app
# (the morning digest). Defaults to the local bind; a deployment must set it or
# every email points at the recipient's own machine.
_DEFAULT_BASE_URL = ""

# Which Claude Code account the analyzer runs under. The `claude` CLI keeps its
# credentials in a config directory (default ~/.claude) and reads whichever one
# CLAUDE_CONFIG_DIR names. Pointing at a directory therefore selects an account
# — it is still the subscription login stored there, never an API key, so this
# changes *whose* subscription is used and never *how* it is billed.
#
# Empty means "inherit the environment", i.e. whatever account the process that
# started NewsPulse was already using.
_DEFAULT_CLAUDE_CONFIG_DIR = ""

# One Google News search per active client, on top of the registry feeds. On by
# default: it is where most regional and trade coverage of a client is found,
# and the registry alone cannot reach it. Set NEWSPULSE_GOOGLE_NEWS=0 to run on
# the curated registry only — one extra HTTP request per client per run, against
# an endpoint Google publishes but does not contract to keep stable.
_DEFAULT_GOOGLE_NEWS = True

# Whether a mandate is measured against what an assistant answers about its
# market at all. On by default, like the Google News search above and for the
# same reason: the deployment that needs it is the one where nobody thought
# about it. Set NEWSPULSE_VISIBILITY=0 to switch the whole feature off — no
# proposal is generated, no measurement is run, and no model call is spent.
#
# Note what enabling it costs, because it is not free the way a search is. One
# measurement puts a whole accepted set (capped at
# ``newspulse.visibility.MAX_QUESTIONS``) to every configured provider, so it is
# the largest single burst of model calls this tool makes.
_DEFAULT_VISIBILITY = True

# How long one measurement stands before the next one is due. Weekly, because
# that is the resolution the answer actually has: the same question asked twice
# on the same day returns different words, so a daily figure would report
# sampling noise as movement, and a monthly one would notice a change five weeks
# after a consultant could have said something about it.
#
# It is also the spend ceiling. Seven days times two providers times a
# twenty-four question set is what one mandate costs a week; a day would be
# seven times that for a number nobody could read.
_DEFAULT_VISIBILITY_EVERY_DAYS = 7

_ENV_WEB_HOST = "NEWSPULSE_WEB_HOST"
_ENV_WEB_PORT = "NEWSPULSE_WEB_PORT"

# Notification channel + SMTP env-var names (NP-10). Kept here with the other
# NEWSPULSE_* names so every env-var spelling has one home; notify.py imports these
# and resolves them per-call against an injectable env mapping (it never resolves a
# notify value at import time, so no resolved notify setting lives in this module).
_ENV_NOTIFY_CHANNEL = "NEWSPULSE_NOTIFY_CHANNEL"
_ENV_SMTP_HOST = "NEWSPULSE_SMTP_HOST"
_ENV_SMTP_PORT = "NEWSPULSE_SMTP_PORT"
_ENV_SMTP_USERNAME = "NEWSPULSE_SMTP_USERNAME"
_ENV_SMTP_PASSWORD = "NEWSPULSE_SMTP_PASSWORD"
_ENV_SMTP_SENDER = "NEWSPULSE_SMTP_SENDER"
_ENV_SMTP_RECIPIENT = "NEWSPULSE_SMTP_RECIPIENT"
_ENV_SMTP_STARTTLS = "NEWSPULSE_SMTP_STARTTLS"

# The zone every *displayed* time is rendered in — run times, article times, the
# header date — and the one that defines a "day" for the Today page, the archive
# and the morning digest. Stored timestamps stay UTC; this is a display setting.
#
# A named zone with a default, rather than the host's own zone, because the host
# is a container: Railway (and every other PaaS image) runs UTC, so deriving the
# zone from /etc/localtime showed a Berlin reader every time two hours early and
# rolled the day over at 02:00 local. The reader's zone is a property of the
# reader, not of the machine that happens to serve them — so it is configuration,
# and its default is where the reader is.
_DEFAULT_TIMEZONE = "Europe/Berlin"
_ENV_TIMEZONE = "NEWSPULSE_TIMEZONE"

# The conventional spelling a Docker host or shell profile may already export.
# Honored after the explicit name so a deployment can override it without having
# to change the container's own clock.
_ENV_TZ = "TZ"

# 587 is the standard mail-submission port for STARTTLS; overridable for a relay that
# listens elsewhere (e.g. 465 with implicit TLS, or a local 25 sink).
_DEFAULT_SMTP_PORT = 587
# STARTTLS on by default: mail credentials must never cross an unencrypted link, so an
# operator opts out explicitly for a plaintext relay.
_DEFAULT_SMTP_STARTTLS = True


def _load_dotenv() -> None:
    """Read ``.env`` next to the working directory into the environment.

    There has been a ``.env.example`` in this repo since the first week and
    nothing ever read the file it describes: every setting came from a real
    environment variable, which is right on Railway (the platform injects them)
    and wrong on a laptop, where the operator does exactly what the example file
    invites — *"ich kann dir hier in's .env file einen Gemini Key legen"* — and
    nothing happens.

    Hand-parsed rather than a dependency, the same call this codebase makes for
    two HTTP requests to Gemini: the format is ``KEY=value`` and there is nothing
    to get right beyond quotes and comments.

    A real environment variable always wins. The file is for a developer's
    machine; on a server the platform's own configuration must not be silently
    overridden by a file that happened to be committed alongside the code.
    """
    configured = os.environ.get("NEWSPULSE_ENV_FILE", "").strip()
    path = Path(configured) if configured else Path.cwd() / ".env"
    try:
        text = path.read_text("utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back to ``default`` if unset or
    unparseable (a typo in a shell profile must not crash startup)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        # Don't crash on a typo, but make the fallback visible so the operator
        # can tell their override didn't take effect.
        _log.warning("Invalid %s=%r, falling back to default %d", name, raw, default)
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from the environment, falling back on an unrecognized value.

    Same posture as :func:`_env_int`: a typo must not crash startup, but the
    fallback is logged so a silently-ignored override is visible.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().casefold()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    _log.warning("Invalid %s=%r, falling back to default %s", name, raw, default)
    return default


# --- Resolved settings ---------------------------------------------------------
#
# These resolve ONCE, at import time, from the environment and ``Path.cwd()``.
# Consequences:
#  * An env-var override (e.g. ``NEWSPULSE_DATABASE_PATH``) must be set *before*
#    this module is first imported — mutating the env afterwards has no effect.
#  * Tests that need a different database must monkeypatch ``config.DATABASE_PATH``
#    directly (not the env var), since re-import won't happen within a process.

DATABASE_PATH: Path = _env_path(_ENV_DATABASE_PATH, Path.cwd() / _DEFAULT_DB_FILENAME)
ALERT_THRESHOLD: int = _env_int(_ENV_ALERT_THRESHOLD, _DEFAULT_ALERT_THRESHOLD)
BATCH_SIZE: int = _env_int(_ENV_BATCH_SIZE, _DEFAULT_BATCH_SIZE)
ANALYZER_TIMEOUT: int = _env_int(_ENV_ANALYZER_TIMEOUT, _DEFAULT_ANALYZER_TIMEOUT)
ANALYZER_BACKEND: str = os.environ.get(_ENV_ANALYZER_BACKEND, _DEFAULT_ANALYZER_BACKEND)
PROFILE_REFRESH_PER_RUN: int = _env_int(
    _ENV_PROFILE_REFRESH_PER_RUN, _DEFAULT_PROFILE_REFRESH_PER_RUN
)
WEB_HOST: str = os.environ.get(_ENV_WEB_HOST, _DEFAULT_WEB_HOST)
# NEWSPULSE_WEB_PORT wins, then PORT, then the default. PORT is what a PaaS
# injects (Railway, Render, Heroku) and the app must bind exactly it or the
# platform routes traffic to a closed socket — the explicit name still takes
# precedence so a local override is never silently ignored.
WEB_PORT: int = _env_int(_ENV_WEB_PORT, _env_int("PORT", _DEFAULT_WEB_PORT))
GOOGLE_NEWS_ENABLED: bool = _env_bool(_ENV_GOOGLE_NEWS, _DEFAULT_GOOGLE_NEWS)
VISIBILITY_ENABLED: bool = _env_bool(_ENV_VISIBILITY, _DEFAULT_VISIBILITY)
# Zero or less means "no window": every request measures. A legitimate setting
# for an operator working a single mandate by hand, and a bad default for a
# portfolio, which is why it is not the default.
VISIBILITY_EVERY_DAYS: int = _env_int(
    _ENV_VISIBILITY_EVERY_DAYS, _DEFAULT_VISIBILITY_EVERY_DAYS
)
CLAUDE_CONFIG_DIR: str = os.environ.get(
    _ENV_CLAUDE_CONFIG_DIR, _DEFAULT_CLAUDE_CONFIG_DIR
)
AUTH_USER: str = os.environ.get(_ENV_AUTH_USER, _DEFAULT_AUTH_USER)
AUTH_PASSWORD: str = os.environ.get(_ENV_AUTH_PASSWORD, _DEFAULT_AUTH_PASSWORD)

# Sign in with Google. Empty means the dashboard falls back to basic auth, which
# is what keeps a deployment that has not been given an OAuth client yet from
# locking everybody out (see web.auth).
GOOGLE_CLIENT_ID: str = os.environ.get(_ENV_GOOGLE_CLIENT_ID, "")
GOOGLE_CLIENT_SECRET: str = os.environ.get(_ENV_GOOGLE_CLIENT_SECRET, "")
# The same credential can serve sign-in and the mailbox, so one Google Cloud
# client is enough; whichever pair is set is the one that is used.
# Who may sign in. A list rather than a table: two addresses do not need a user
# model, and the third one is an env var away.
_DEFAULT_ALLOWED_EMAILS = "raphaelmankopf@gmail.com,lucas.neurauter@gmail.com"
ALLOWED_EMAILS: str = os.environ.get(_ENV_ALLOWED_EMAILS, _DEFAULT_ALLOWED_EMAILS)
# Optional. Left empty, web.google_auth generates one and keeps it beside the
# database, so sessions survive a restart without anyone inventing a secret.
SESSION_SECRET: str = os.environ.get(_ENV_SESSION_SECRET, "")
BASE_URL: str = os.environ.get(_ENV_BASE_URL, _DEFAULT_BASE_URL).rstrip("/")
GEMINI_API_KEY: str = os.environ.get(_ENV_GEMINI_API_KEY, _DEFAULT_GEMINI_API_KEY).strip()
GEMINI_MODEL: str = os.environ.get(_ENV_GEMINI_MODEL, _DEFAULT_GEMINI_MODEL).strip()
# One definition. The unprefixed spelling is read as a fallback because that is
# how the credential is named in the deployment, and a merge that left two
# definitions of this constant had the second one silently drop that fallback.
GMAIL_CLIENT_ID: str = (
    os.environ.get(_ENV_GMAIL_CLIENT_ID)
    or os.environ.get(_ENV_GMAIL_CLIENT_ID_BARE)
    or _DEFAULT_GMAIL_CLIENT_ID
).strip()
GMAIL_CLIENT_SECRET: str = (
    os.environ.get(_ENV_GMAIL_CLIENT_SECRET)
    or os.environ.get(_ENV_GMAIL_CLIENT_SECRET_BARE)
    or _DEFAULT_GMAIL_CLIENT_SECRET
).strip()


def resolve_local_zone() -> dt.tzinfo:
    """Resolve the display zone as a DST-aware :class:`~zoneinfo.ZoneInfo`.

    A named zone, never the fixed offset ``datetime.now().astimezone().tzinfo``
    hands back: that offset reflects the DST state *right now* and, applied to a
    day in the other DST regime, shifts that local day by an hour and
    misattributes articles near local midnight.

    ``NEWSPULSE_TIMEZONE`` wins, then ``TZ``, then :data:`_DEFAULT_TIMEZONE`. An
    unrecognized name falls through to the next candidate — a typo must not crash
    the dashboard — and UTC is the last resort, reached only on a host with no tz
    database at all. Reads the environment on each call so a test can set either
    variable; callers use the value cached in :data:`LOCAL_ZONE`.
    """
    candidates = (
        (_ENV_TIMEZONE, os.environ.get(_ENV_TIMEZONE)),
        (_ENV_TZ, os.environ.get(_ENV_TZ)),
        ("default", _DEFAULT_TIMEZONE),
    )
    for source, raw in candidates:
        name = (raw or "").strip()
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            _log.warning("Unknown timezone %s=%r, ignoring", source, name)
    _log.warning("No usable timezone (missing tz database?); showing times in UTC")
    return dt.UTC


#: Resolved once at import, like every other setting here: the display zone does
#: not change during a process, and it is a zone rather than a frozen offset so
#: bounding any day uses that day's own DST offset.
LOCAL_ZONE: dt.tzinfo = resolve_local_zone()


def local_zone() -> dt.tzinfo:
    """The zone displayed times are rendered in.

    A function so a test can monkeypatch :data:`LOCAL_ZONE` and be seen, the same
    posture as :func:`gemini_configured`.
    """
    return LOCAL_ZONE


# The daily sweep, and whether anything runs it. Both are read per call rather
# than at import so a platform variable set after start-up is still seen.
#
# This exists because nothing on the deployed host was running the sweep at all:
# the compose file's cron container and the launchd/Windows entries under
# schedule/ are all host-side, and the platform's start command is the web
# process alone. Everything the tool produces unattended — coverage, digest,
# positioning drafts — therefore waited for somebody to press a button.
ENV_SCHEDULER = "NEWSPULSE_SCHEDULER"
ENV_DAILY_AT = "NEWSPULSE_DAILY_AT"

# Early enough that the day is ready before the first person opens it, late
# enough that the night's coverage is syndicated. The same time the compose cron
# used, so a host that already had one does not shift its rhythm.
_DEFAULT_DAILY_AT = "06:10"


def scheduler_enabled() -> bool:
    """Whether the web process should run the daily sweep itself.

    On by default: the deployment that needs it is the one where nobody thought
    about it, and a scheduler that has to be switched on is a scheduler that is
    off. Set NEWSPULSE_SCHEDULER=0 where an external cron already does the job.
    """
    raw = os.environ.get(ENV_SCHEDULER, "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def daily_run_at() -> tuple[int, int]:
    """The local (hour, minute) for the daily sweep. Falls back on a bad value.

    A malformed variable must not stop the sweep from ever running — that is the
    exact failure this module was added to fix — so it is logged by the caller
    through the default rather than raised.
    """
    raw = os.environ.get(ENV_DAILY_AT, _DEFAULT_DAILY_AT).strip()
    try:
        hour, _, minute = raw.partition(":")
        at = (int(hour), int(minute or 0))
    except ValueError:
        return (6, 10)
    if not (0 <= at[0] <= 23 and 0 <= at[1] <= 59):
        return (6, 10)
    return at


# The one condition in the tool under which the sweep changes its cadence. While
# a mandate is in a declared crisis its own sources are re-read this often, and
# nothing else happens on that tick — no positioning drafts, no profile work, no
# other mandate (see ``job.run_crisis``).
#
# An hour, because that is the resolution the source actually has: a news search
# refreshes in minutes but the coverage behind it does not, and a ten-minute
# cadence would spend ten times the requests to find the same three articles. It
# is also the spend ceiling, since every reading that finds something new costs
# an analyzer batch.
ENV_CRISIS_SWEEP_MINUTES = "NEWSPULSE_CRISIS_SWEEP_MINUTES"
_DEFAULT_CRISIS_SWEEP_MINUTES = 60

# The floor a configured value is clamped to. Zero or a negative number would
# make the crisis sweep due on every scheduler tick — a fetch a minute against
# the same feed, for the whole time a crisis is open — so a typo cannot buy that.
_MIN_CRISIS_SWEEP_MINUTES = 5


def crisis_sweep_minutes() -> int:
    """How often an open crisis re-reads its mandate's sources.

    Read per call rather than at import, like :func:`daily_run_at`, so a platform
    variable set after start-up is still seen — and so the value a running
    scheduler uses is the one currently configured.
    """
    value = _env_int(ENV_CRISIS_SWEEP_MINUTES, _DEFAULT_CRISIS_SWEEP_MINUTES)
    return max(value, _MIN_CRISIS_SWEEP_MINUTES)


def gemini_configured() -> bool:
    """Whether the fallback provider can be used at all.

    A function rather than a constant so a key set after import — the Settings
    page, a test's monkeypatch — is seen without reloading the module.
    """
    return bool(gemini_api_key())


def gemini_api_key() -> str:
    """The current key, read live for the same reason as above."""
    return os.environ.get(_ENV_GEMINI_API_KEY, GEMINI_API_KEY).strip()


def review_api_key() -> str:
    """The key for the second model that cross-reads one outreach letter.

    Deliberately a wider net than :func:`gemini_api_key`, and deliberately a
    separate function, because the two ask for different consent.

    The namespaced key turns Google into a *fallback processor of all client
    coverage*: every headline the sweep analyses can end up there when the Claude
    subscription hits its limit. That has to be an explicit decision, so it reads
    one explicitly named variable and nothing else.

    Cross-reading a single pitch before a human sends it is the smaller act, and
    it is the one the operator just asked for — "ich kann dir hier in's .env file
    einen Gemini Key legen". So the bare ``GEMINI_API_KEY`` counts here: it is
    what every Google example prints and what a developer already has exported,
    and refusing to see it would mean reporting "kein Zweitmodell hinterlegt"
    with a working key sitting in the environment.
    """
    return (
        os.environ.get(_ENV_GEMINI_API_KEY)
        or os.environ.get("GEMINI_API_KEY")
        or GEMINI_API_KEY
    ).strip()


def review_model() -> str:
    """Which model does that reading.

    Each candidate is stripped *before* it is weighed, not after the chain has
    already settled on one. ``NEWSPULSE_GEMINI_MODEL="  "`` is a truthy string, so
    stripping at the end let a set-but-blank variable win the chain and return
    ``""`` — which reaches the provider as ``model=""`` and, since GDC-02, is
    persisted and printed under the letter as the model that read it. A variable
    someone set to whitespace means "I did not name one", so it falls through to
    the one that is actually configured.
    """
    for candidate in (
        os.environ.get(_ENV_GEMINI_MODEL),
        os.environ.get("GEMINI_MODEL"),
        GEMINI_MODEL,
    ):
        named = (candidate or "").strip()
        if named:
            return named
    return ""


def review_configured() -> bool:
    """Whether a second model can read a letter at all."""
    return bool(review_api_key())


def gmail_configured() -> bool:
    """Whether a mailbox can be connected at all.

    Both halves are required: an id without a secret gets through the consent
    screen and then fails at the token exchange, which is a worse failure than
    never offering the button, because it happens after the operator has already
    granted access to their mailbox.
    """
    return bool(gmail_client_id() and gmail_client_secret())


def gmail_redirect_uri() -> str:
    """The address Google returns consent to — :func:`base_url` plus the callback.

    Whatever this returns has to be registered in the Cloud console character for
    character, which is why it is derived rather than configured: a deployment
    that sets NEWSPULSE_BASE_URL already said where it lives.
    """
    return f"{base_url()}{GMAIL_CALLBACK_PATH}"


def base_url() -> str:
    """The address links in outgoing mail should point at.

    Three sources, in order. An explicit NEWSPULSE_BASE_URL wins. Failing that,
    the platform's own public hostname, because the bind address is useless
    here: a container listens on 0.0.0.0, and "http://0.0.0.0:8080" is not an
    address anything can come back to. That mattered little while this only
    signed the links in outgoing mail; it decides whether sign-in works at all,
    because Google compares the redirect URI it is given against the one
    registered on the OAuth client and refuses the whole request on a mismatch.
    """
    if BASE_URL:
        return BASE_URL
    public = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if public:
        return f"https://{public.rstrip('/')}"
    return f"http://{WEB_HOST}:{WEB_PORT}"


def google_client_id() -> str:
    return os.environ.get(_ENV_GOOGLE_CLIENT_ID, GOOGLE_CLIENT_ID).strip()


def google_client_secret() -> str:
    return os.environ.get(_ENV_GOOGLE_CLIENT_SECRET, GOOGLE_CLIENT_SECRET).strip()


def gmail_client_id() -> str:
    """The OAuth client id, prefixed name first.

    The unprefixed ``GMAIL_CLIENT_ID`` is read as a fallback because that is
    what a Google credential is called everywhere else, and it is what somebody
    pasting one into a deployment reaches for. Every other setting here carries
    the NEWSPULSE_ prefix and this one does too when it is set; the fallback
    exists so a correct-looking variable is not silently ignored.
    """
    return (
        os.environ.get(_ENV_GMAIL_CLIENT_ID)
        or os.environ.get(_ENV_GMAIL_CLIENT_ID_BARE)
        or GMAIL_CLIENT_ID
    ).strip()


def gmail_client_secret() -> str:
    return (
        os.environ.get(_ENV_GMAIL_CLIENT_SECRET)
        or os.environ.get(_ENV_GMAIL_CLIENT_SECRET_BARE)
        or GMAIL_CLIENT_SECRET
    ).strip()


def allowed_emails() -> str:
    return os.environ.get(_ENV_ALLOWED_EMAILS, ALLOWED_EMAILS).strip()


def session_secret() -> str:
    return os.environ.get(_ENV_SESSION_SECRET, SESSION_SECRET).strip()


def database_url() -> str:
    """SQLAlchemy URL for the configured SQLite database file."""
    # as_posix() keeps forward slashes on every platform; a Windows path with
    # backslashes would otherwise corrupt the URL.
    return f"sqlite:///{DATABASE_PATH.as_posix()}"
