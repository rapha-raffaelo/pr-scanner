"""Tests for :mod:`newspulse.config` — the display timezone.

Timestamps are stored UTC everywhere (``UTCDateTime``); the zone resolved here is
the one they are *shown* in and the one that decides where a day starts. It is
configuration rather than a property of the host, because the host is a UTC
container: deriving it from the machine showed the Berlin reader every time two
hours early.
"""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

from newspulse import config


def _clear_zone_env(monkeypatch) -> None:
    monkeypatch.delenv("NEWSPULSE_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)


def test_defaults_to_berlin_when_nothing_is_configured(monkeypatch):
    """The regression: an unconfigured host must not fall back to its own clock.

    Railway (like every PaaS image) runs UTC, so "the machine's zone" meant the
    header, the article times and the day boundary were all an hour or two off.
    The default is where the reader is, not where the container is.
    """
    _clear_zone_env(monkeypatch)
    assert config.resolve_local_zone() == ZoneInfo("Europe/Berlin")


def test_explicit_name_wins_over_tz(monkeypatch):
    """NEWSPULSE_TIMEZONE overrides a TZ the platform or image happens to set."""
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setenv("NEWSPULSE_TIMEZONE", "Europe/Vienna")
    assert config.resolve_local_zone() == ZoneInfo("Europe/Vienna")


def test_tz_is_honored_when_no_explicit_name_is_set(monkeypatch):
    """A host that already exports TZ needs no second variable."""
    _clear_zone_env(monkeypatch)
    monkeypatch.setenv("TZ", "America/New_York")
    assert config.resolve_local_zone() == ZoneInfo("America/New_York")


def test_unknown_name_falls_through_instead_of_crashing(monkeypatch):
    """A typo must cost the override, not the dashboard."""
    _clear_zone_env(monkeypatch)
    monkeypatch.setenv("NEWSPULSE_TIMEZONE", "Europe/Berlim")
    assert config.resolve_local_zone() == ZoneInfo("Europe/Berlin")


def test_resolved_zone_is_dst_aware_not_a_frozen_offset(monkeypatch):
    """A named zone, so each day is bounded with *its own* offset.

    ``datetime.now().astimezone().tzinfo`` yields the offset in force right now;
    applied to a day in the other DST regime it shifts that day by an hour and
    misattributes articles near local midnight.
    """
    _clear_zone_env(monkeypatch)
    zone = config.resolve_local_zone()
    winter = dt.datetime(2026, 1, 15, tzinfo=zone).utcoffset()
    summer = dt.datetime(2026, 7, 15, tzinfo=zone).utcoffset()
    assert winter == dt.timedelta(hours=1)
    assert summer == dt.timedelta(hours=2)
    assert winter != summer  # a frozen offset would make these equal


def test_local_zone_reads_the_module_value(monkeypatch):
    """Callers go through the accessor, so a test (or a reload) can swap it."""
    monkeypatch.setattr(config, "LOCAL_ZONE", ZoneInfo("Pacific/Auckland"))
    assert config.local_zone() == ZoneInfo("Pacific/Auckland")


def test_the_env_file_is_actually_read(tmp_path, monkeypatch):
    """It never was. `Path("")` is `Path(".")` and truthy, so the `or` fallback
    was dead and every run read the working directory instead, swallowing the
    IsADirectoryError. A key somebody put in .env was silently ignored."""
    from newspulse import config

    env = tmp_path / ".env"
    env.write_text('NEWSPULSE_TESTVALUE="aus der Datei"\n# ein Kommentar\n', "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEWSPULSE_ENV_FILE", raising=False)
    monkeypatch.delenv("NEWSPULSE_TESTVALUE", raising=False)

    config._load_dotenv()

    assert os.environ["NEWSPULSE_TESTVALUE"] == "aus der Datei"


def test_a_real_environment_variable_still_wins(tmp_path, monkeypatch):
    """The file is for a developer's machine; the platform's own configuration
    must not be overridden by a file that shipped beside the code."""
    from newspulse import config

    (tmp_path / ".env").write_text("NEWSPULSE_TESTVALUE=aus der Datei\n", "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEWSPULSE_ENV_FILE", raising=False)
    monkeypatch.setenv("NEWSPULSE_TESTVALUE", "aus der Umgebung")

    config._load_dotenv()

    assert os.environ["NEWSPULSE_TESTVALUE"] == "aus der Umgebung"
