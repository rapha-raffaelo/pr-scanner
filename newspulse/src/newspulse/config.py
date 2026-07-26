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

# Max articles handed to the analyzer in a single batched call. 20 keeps a per
# client daily call well inside interactive territory on a Claude subscription.
_DEFAULT_BATCH_SIZE = 20

# Wall-clock ceiling (seconds) for a single `claude -p` subprocess call. A batch
# of ~20 short items is comfortably interactive; 120s leaves headroom for a cold
# CLI start without ever letting one hung call stall the whole daily sweep.
_DEFAULT_ANALYZER_TIMEOUT = 120

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
WEB_PORT: int = _env_int(_ENV_WEB_PORT, _DEFAULT_WEB_PORT)


def database_url() -> str:
    """SQLAlchemy URL for the configured SQLite database file."""
    # as_posix() keeps forward slashes on every platform; a Windows path with
    # backslashes would otherwise corrupt the URL.
    return f"sqlite:///{DATABASE_PATH.as_posix()}"
