"""Tests for post-run alert notifications (NP-10).

These exercise :mod:`newspulse.notify` directly — the pure summary formatter, the
channel resolution, and the orchestrator with the delivery channel mocked. No real
desktop notifier is spawned and no SMTP connection is opened: the two send
boundaries are injected as spies, so the tests stay fast and deterministic.

The load-bearing cases are ``test_notify_alerts_no_alerts_sends_nothing`` (the
no-alerts-no-send path) and ``test_notify_alerts_delivery_failure_is_logged_not_raised``
(a broken channel logs an ERROR and never rolls the run back).
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest
from sqlalchemy.orm import sessionmaker

from newspulse import notify
from newspulse.db import make_engine
from newspulse.models import Analysis, Article, Base, Category, Client
from newspulse.notify import (
    Channel,
    FiredAlert,
    NotifyConfig,
    SmtpConfig,
    build_summary,
    collect_fired_alerts,
    notify_alerts,
    resolve_channel,
)

_NOW = dt.datetime(2026, 7, 20, 18, 0, tzinfo=dt.UTC)


# --- Builders ------------------------------------------------------------------


def _alert(
    client: str = "Alpha AG",
    headline: str = "Alpha AG eroeffnet Werk",
    importance: int = 8,
    url: str = "https://hb.de/a1",
) -> FiredAlert:
    return FiredAlert(
        client_name=client, headline=headline, importance_score=importance, url=url
    )


class _Spy:
    """Records every call so a test can assert it was (or wasn't) invoked."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args) -> None:
        self.calls.append(args)


# --- Summary formatting --------------------------------------------------------


def test_build_summary_returns_none_when_no_alerts():
    """An empty alert list yields no summary — the no-notification contract."""
    assert build_summary([]) is None


def test_build_summary_counts_alerts_and_picks_top_headline_per_client():
    """Each client group carries its alert count and its highest-importance headline."""
    alerts = [
        _alert("Alpha AG", "Alpha Randnotiz", importance=4),
        _alert("Alpha AG", "Alpha Grossauftrag", importance=9),
        _alert("Beta AG", "Beta meldet Zahlen", importance=7),
    ]

    summary = build_summary(alerts)

    assert summary is not None
    assert summary.total == 3
    assert summary.client_count == 2
    by_name = {group.client_name: group for group in summary.groups}
    assert by_name["Alpha AG"].count == 2
    assert by_name["Alpha AG"].top_headline == "Alpha Grossauftrag"  # highest importance
    assert by_name["Beta AG"].count == 1


def test_build_summary_orders_most_important_client_first():
    """Groups sort by top importance so the must-see client leads the summary."""
    alerts = [
        _alert("Quiet AG", "Kleine Meldung", importance=6),
        _alert("Loud AG", "Grosse Krise", importance=10),
    ]

    summary = build_summary(alerts)

    assert [group.client_name for group in summary.groups] == ["Loud AG", "Quiet AG"]


def test_build_summary_subject_names_alert_and_client_counts():
    """The subject summarizes the totals a human scans first."""
    summary = build_summary([_alert("Alpha AG"), _alert("Beta AG", "Beta News", 5)])

    assert summary.subject == "NewsPulse: 2 alerts across 2 clients"


def test_build_summary_subject_is_singular_for_one_alert():
    """A single alert reads 'alert'/'client', not 'alerts'/'clients'."""
    summary = build_summary([_alert()])

    assert summary.subject == "NewsPulse: 1 alert across 1 client"


def test_build_summary_desktop_message_is_a_single_line():
    """A headline carrying newlines/tabs collapses to one line — a raw newline would
    break the osascript AppleScript string literal the message is embedded in."""
    summary = build_summary([_alert(headline="Krise\nbei\tAlpha")])

    assert "\n" not in summary.desktop_message
    assert "\t" not in summary.desktop_message
    assert "Krise bei Alpha" in summary.desktop_message


def test_build_summary_body_lists_client_count_and_top_headline():
    """The body has one bullet per client with its count and top headline."""
    summary = build_summary(
        [
            _alert("Alpha AG", "Alpha Grossauftrag", 9),
            _alert("Alpha AG", "Alpha Randnotiz", 4),
        ]
    )

    assert "• Alpha AG (2): Alpha Grossauftrag" in summary.body


# --- Channel resolution --------------------------------------------------------


def test_resolve_channel_defaults_to_off_when_unset():
    """Unconfigured means off — a fresh install never surprises with notifications."""
    assert resolve_channel(None) is Channel.OFF
    assert resolve_channel("") is Channel.OFF


def test_resolve_channel_maps_desktop_and_email():
    """The two explicit channels resolve to their enum members."""
    assert resolve_channel("desktop") is Channel.DESKTOP
    assert resolve_channel("email") is Channel.EMAIL
    assert resolve_channel(" EMAIL ") is Channel.EMAIL  # trimmed + case-insensitive


def test_resolve_channel_generic_enable_defaults_to_desktop():
    """A generic 'enable' word turns notifications on via the desktop default."""
    assert resolve_channel("on") is Channel.DESKTOP
    assert resolve_channel("true") is Channel.DESKTOP


def test_resolve_channel_unknown_value_disables_and_warns(caplog):
    """An unrecognized value is treated as off (with a warning), never guessed."""
    with caplog.at_level(logging.WARNING, logger="newspulse.notify"):
        result = resolve_channel("carrier-pigeon")

    assert result is Channel.OFF
    assert any("unknown notify channel" in rec.message for rec in caplog.records)


# --- Config from env -----------------------------------------------------------


def test_notify_config_from_env_off_when_unconfigured():
    """With no env vars set, the config is off and carries no SMTP settings."""
    config = NotifyConfig.from_env({})

    assert config.channel is Channel.OFF
    assert config.smtp is None


def test_notify_config_from_env_email_reads_smtp():
    """Selecting email pulls the SMTP settings from the same env mapping."""
    env = {
        "NEWSPULSE_NOTIFY_CHANNEL": "email",
        "NEWSPULSE_SMTP_HOST": "smtp.example.com",
        "NEWSPULSE_SMTP_RECIPIENT": "pr@example.com",
    }

    config = NotifyConfig.from_env(env)

    assert config.channel is Channel.EMAIL
    assert config.smtp is not None
    assert config.smtp.host == "smtp.example.com"


def test_smtp_config_from_env_requires_host_and_recipient():
    """Missing host or recipient means email is not set up — no half-formed config."""
    assert SmtpConfig.from_env({"NEWSPULSE_SMTP_HOST": "smtp.example.com"}) is None
    assert SmtpConfig.from_env({"NEWSPULSE_SMTP_RECIPIENT": "pr@example.com"}) is None


def test_smtp_config_reads_credentials_from_env():
    """Host, port, and credentials come from env — never hardcoded."""
    env = {
        "NEWSPULSE_SMTP_HOST": "smtp.example.com",
        "NEWSPULSE_SMTP_PORT": "2525",
        "NEWSPULSE_SMTP_USERNAME": "mailer",
        "NEWSPULSE_SMTP_PASSWORD": "s3cr3t",
        "NEWSPULSE_SMTP_RECIPIENT": "pr@example.com",
    }

    smtp = SmtpConfig.from_env(env)

    assert smtp is not None
    assert smtp.host == "smtp.example.com"
    assert smtp.port == 2525
    assert smtp.username == "mailer"
    assert smtp.password == "s3cr3t"
    assert smtp.sender == "pr@example.com"  # defaults to the recipient


def test_smtp_config_repr_never_leaks_password():
    """The password must never surface in a stringified config (logs, tracebacks)."""
    smtp = SmtpConfig(
        host="smtp.example.com",
        port=587,
        sender="pr@example.com",
        recipient="pr@example.com",
        username="mailer",
        password="s3cr3t",
    )

    assert "s3cr3t" not in repr(smtp)
    assert "mailer" not in repr(smtp)


# --- Orchestration: the no-send paths ------------------------------------------


def test_notify_alerts_no_alerts_sends_nothing():
    """No alerts -> no notification of any kind, even on an active channel."""
    desktop, email = _Spy(), _Spy()
    config = NotifyConfig(channel=Channel.DESKTOP)

    result = notify_alerts([], config, send_desktop=desktop, send_email=email)

    assert result.sent is False
    assert result.reason == "no-alerts"
    assert desktop.calls == []
    assert email.calls == []


def test_notify_alerts_channel_off_does_not_send_even_with_alerts():
    """Alerts fired but channel off -> nothing delivered."""
    desktop, email = _Spy(), _Spy()
    config = NotifyConfig(channel=Channel.OFF)

    result = notify_alerts([_alert()], config, send_desktop=desktop, send_email=email)

    assert result.sent is False
    assert result.reason == "channel-off"
    assert result.alert_count == 1
    assert desktop.calls == []
    assert email.calls == []


# --- Orchestration: delivery ---------------------------------------------------


def test_notify_alerts_desktop_channel_invokes_desktop_sender():
    """The desktop channel calls the desktop sender with the built summary."""
    desktop, email = _Spy(), _Spy()
    config = NotifyConfig(channel=Channel.DESKTOP)

    result = notify_alerts([_alert()], config, send_desktop=desktop, send_email=email)

    assert result.sent is True
    assert len(desktop.calls) == 1
    assert desktop.calls[0][0].total == 1  # the AlertSummary
    assert email.calls == []


def test_notify_alerts_email_channel_invokes_email_sender_with_smtp():
    """The email channel calls the email sender with the summary and SMTP config."""
    desktop, email = _Spy(), _Spy()
    smtp = SmtpConfig(
        host="smtp.example.com", port=587, sender="a@b.de", recipient="a@b.de"
    )
    config = NotifyConfig(channel=Channel.EMAIL, smtp=smtp)

    result = notify_alerts([_alert()], config, send_desktop=desktop, send_email=email)

    assert result.sent is True
    assert len(email.calls) == 1
    summary_arg, smtp_arg = email.calls[0]
    assert summary_arg.total == 1
    assert smtp_arg is smtp
    assert desktop.calls == []


# --- Orchestration: failure never fails the run --------------------------------


def test_notify_alerts_delivery_failure_is_logged_not_raised(caplog):
    """A broken channel logs an ERROR and returns; it never raises to abort the run."""
    def boom(_summary) -> None:
        raise RuntimeError("smtp connection refused")

    config = NotifyConfig(channel=Channel.DESKTOP)

    with caplog.at_level(logging.ERROR, logger="newspulse.notify"):
        result = notify_alerts([_alert()], config, send_desktop=boom)

    assert result.sent is False
    assert result.reason == "delivery-error"
    assert "smtp connection refused" in (result.error or "")
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


def test_notify_alerts_email_selected_without_smtp_logs_error(caplog):
    """Email selected but SMTP unconfigured is a caught, logged error — not a crash."""
    config = NotifyConfig(channel=Channel.EMAIL, smtp=None)

    with caplog.at_level(logging.ERROR, logger="newspulse.notify"):
        result = notify_alerts([_alert()], config, send_email=_Spy())

    assert result.sent is False
    assert result.reason == "delivery-error"
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


# --- The real email boundary (SMTP mocked) -------------------------------------


class _FakeSMTP:
    """A stand-in for ``smtplib.SMTP`` used as a context manager."""

    last: "_FakeSMTP | None" = None

    def __init__(self, host, port, timeout=None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.login_args: tuple | None = None
        self.sent_message = None
        _FakeSMTP.last = self

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, username, password) -> None:
        self.login_args = (username, password)

    def send_message(self, message) -> None:
        self.sent_message = message


def test_send_email_starts_tls_logs_in_and_sends(monkeypatch, caplog):
    """_send_email applies STARTTLS, logs in with env creds, and never logs the password."""
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)
    smtp = SmtpConfig(
        host="smtp.example.com",
        port=587,
        sender="pr@example.com",
        recipient="pr@example.com",
        username="mailer",
        password="s3cr3t",
    )
    summary = build_summary([_alert()])

    with caplog.at_level(logging.DEBUG, logger="newspulse.notify"):
        notify._send_email(summary, smtp)

    sent = _FakeSMTP.last
    assert sent is not None
    assert sent.started_tls is True
    assert sent.login_args == ("mailer", "s3cr3t")
    assert sent.sent_message["Subject"] == summary.subject
    assert "s3cr3t" not in caplog.text  # credential never logged


# --- Collecting a run's fired alerts from the DB -------------------------------


@pytest.fixture
def session():
    """A session against a fresh in-memory SQLite database (schema from the models)."""
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _seed_analysis(session, *, client, title, url, is_alert, importance, analyzed_at):
    article = Article(
        title=title,
        url=url,
        source="Handelsblatt",
        published_at=_NOW,
        fetched_at=_NOW,
        summary_text="snippet",
        language="de",
        title_hash=url,  # any distinct value; dedup isn't under test here
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            is_relevant=True,
            summary="s",
            category=Category.KRISE,
            relevance_score=importance,
            importance_score=importance,
            is_alert=is_alert,
            reasoning="r",
            analyzed_at=analyzed_at,
        )
    )
    session.commit()


def test_collect_fired_alerts_returns_only_flagged_rows_in_window(session):
    """Only ``is_alert`` analyses from within the window become FiredAlerts."""
    client = Client(name="Alpha AG")
    session.add(client)
    session.commit()
    _seed_analysis(
        session, client=client, title="Krise bei Alpha", url="https://x/1",
        is_alert=True, importance=9, analyzed_at=_NOW,
    )
    _seed_analysis(
        session, client=client, title="Belanglos", url="https://x/2",
        is_alert=False, importance=3, analyzed_at=_NOW,  # not an alert
    )
    _seed_analysis(
        session, client=client, title="Gestern", url="https://x/3",
        is_alert=True, importance=8, analyzed_at=_NOW - dt.timedelta(days=2),  # before window
    )

    alerts = collect_fired_alerts(session, since=_NOW - dt.timedelta(hours=1))

    assert len(alerts) == 1
    assert alerts[0].headline == "Krise bei Alpha"
    assert alerts[0].client_name == "Alpha AG"
    assert alerts[0].importance_score == 9


def test_collect_fired_alerts_feeds_build_summary_end_to_end(session):
    """A run's fired alerts flow straight into a formatted summary."""
    alpha = Client(name="Alpha AG")
    beta = Client(name="Beta AG")
    session.add_all([alpha, beta])
    session.commit()
    _seed_analysis(
        session, client=alpha, title="Alpha Grossauftrag", url="https://x/a",
        is_alert=True, importance=9, analyzed_at=_NOW,
    )
    _seed_analysis(
        session, client=beta, title="Beta Rueckruf", url="https://x/b",
        is_alert=True, importance=7, analyzed_at=_NOW,
    )

    summary = build_summary(collect_fired_alerts(session, since=_NOW - dt.timedelta(hours=1)))

    assert summary is not None
    assert summary.total == 2
    assert summary.client_count == 2
    assert summary.groups[0].client_name == "Alpha AG"  # importance 9 leads
