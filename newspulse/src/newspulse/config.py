"""Runtime configuration for NewsPulse.

Every tunable loads from an environment variable and falls back to a module-level
named-constant default. No magic numbers are inlined at the call sites: the whole
point of this module is that the rest of the package imports named values from
here rather than hard-coding paths, thresholds, or batch sizes.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

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
_ENV_GOOGLE_NEWS = "NEWSPULSE_GOOGLE_NEWS"
_ENV_CLAUDE_CONFIG_DIR = "NEWSPULSE_CLAUDE_CONFIG_DIR"
_ENV_AUTH_USER = "NEWSPULSE_AUTH_USER"
_ENV_AUTH_PASSWORD = "NEWSPULSE_AUTH_PASSWORD"
_ENV_BASE_URL = "NEWSPULSE_BASE_URL"
_ENV_GEMINI_API_KEY = "NEWSPULSE_GEMINI_API_KEY"
_ENV_GEMINI_MODEL = "NEWSPULSE_GEMINI_MODEL"

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

# 587 is the standard mail-submission port for STARTTLS; overridable for a relay that
# listens elsewhere (e.g. 465 with implicit TLS, or a local 25 sink).
_DEFAULT_SMTP_PORT = 587
# STARTTLS on by default: mail credentials must never cross an unencrypted link, so an
# operator opts out explicitly for a plaintext relay.
_DEFAULT_SMTP_STARTTLS = True


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
WEB_HOST: str = os.environ.get(_ENV_WEB_HOST, _DEFAULT_WEB_HOST)
# NEWSPULSE_WEB_PORT wins, then PORT, then the default. PORT is what a PaaS
# injects (Railway, Render, Heroku) and the app must bind exactly it or the
# platform routes traffic to a closed socket — the explicit name still takes
# precedence so a local override is never silently ignored.
WEB_PORT: int = _env_int(_ENV_WEB_PORT, _env_int("PORT", _DEFAULT_WEB_PORT))
GOOGLE_NEWS_ENABLED: bool = _env_bool(_ENV_GOOGLE_NEWS, _DEFAULT_GOOGLE_NEWS)
CLAUDE_CONFIG_DIR: str = os.environ.get(
    _ENV_CLAUDE_CONFIG_DIR, _DEFAULT_CLAUDE_CONFIG_DIR
)
AUTH_USER: str = os.environ.get(_ENV_AUTH_USER, _DEFAULT_AUTH_USER)
AUTH_PASSWORD: str = os.environ.get(_ENV_AUTH_PASSWORD, _DEFAULT_AUTH_PASSWORD)
BASE_URL: str = os.environ.get(_ENV_BASE_URL, _DEFAULT_BASE_URL).rstrip("/")
GEMINI_API_KEY: str = os.environ.get(_ENV_GEMINI_API_KEY, _DEFAULT_GEMINI_API_KEY).strip()
GEMINI_MODEL: str = os.environ.get(_ENV_GEMINI_MODEL, _DEFAULT_GEMINI_MODEL).strip()


def gemini_configured() -> bool:
    """Whether the fallback provider can be used at all.

    A function rather than a constant so a key set after import — the Settings
    page, a test's monkeypatch — is seen without reloading the module.
    """
    return bool(os.environ.get(_ENV_GEMINI_API_KEY, GEMINI_API_KEY).strip())


def gemini_api_key() -> str:
    """The current key, read live for the same reason as above."""
    return os.environ.get(_ENV_GEMINI_API_KEY, GEMINI_API_KEY).strip()


def base_url() -> str:
    """The address links in outgoing mail should point at.

    Falls back to the configured bind, which is right locally and wrong the
    moment the tool is deployed — hence NEWSPULSE_BASE_URL, and the deployment
    guide telling you to set it.
    """
    return BASE_URL or f"http://{WEB_HOST}:{WEB_PORT}"


def database_url() -> str:
    """SQLAlchemy URL for the configured SQLite database file."""
    # as_posix() keeps forward slashes on every platform; a Windows path with
    # backslashes would otherwise corrupt the URL.
    return f"sqlite:///{DATABASE_PATH.as_posix()}"
