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

# Analyzer backend id. The subscription subprocess backend is the default; the
# metered API backend is opt-in (see PRD: subscription-first).
_DEFAULT_ANALYZER_BACKEND = "claude_code"

# Env var names, kept in one place so callers never spell them by hand.
_ENV_DATABASE_PATH = "NEWSPULSE_DATABASE_PATH"
_ENV_ALERT_THRESHOLD = "NEWSPULSE_ALERT_THRESHOLD"
_ENV_BATCH_SIZE = "NEWSPULSE_BATCH_SIZE"
_ENV_ANALYZER_BACKEND = "NEWSPULSE_ANALYZER_BACKEND"


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

DATABASE_PATH: Path = _env_path(_ENV_DATABASE_PATH, Path.cwd() / _DEFAULT_DB_FILENAME)
ALERT_THRESHOLD: int = _env_int(_ENV_ALERT_THRESHOLD, _DEFAULT_ALERT_THRESHOLD)
BATCH_SIZE: int = _env_int(_ENV_BATCH_SIZE, _DEFAULT_BATCH_SIZE)
ANALYZER_BACKEND: str = os.environ.get(_ENV_ANALYZER_BACKEND, _DEFAULT_ANALYZER_BACKEND)


def database_url() -> str:
    """SQLAlchemy URL for the configured SQLite database file."""
    # as_posix() keeps forward slashes on every platform; a Windows path with
    # backslashes would otherwise corrupt the URL.
    return f"sqlite:///{DATABASE_PATH.as_posix()}"
