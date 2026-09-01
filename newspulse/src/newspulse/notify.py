"""Post-run alert notifications (NP-10).

Once the daily sweep has safely persisted its data (:mod:`newspulse.job`), the
operator wants one thing: to be told when something actually happened. This module
summarizes the alerts a run fired — grouped per client with a count and the top
headline — and delivers that summary through the configured channel.

Two properties are load-bearing, and each has a test:

* **No alerts, no noise.** An empty alert list produces no notification of any
  kind (no "0 alerts today" email), so a quiet day is genuinely silent. The whole
  value of the tool is that a false-alert-free morning stays trusted.

* **Notification never fails the run.** :func:`notify_alerts` is invoked *after*
  the run's articles and analyses are already committed, and it only ever reads.
  Any delivery error — a broken SMTP host, a missing desktop notifier binary, an
  email channel selected without SMTP configured — is caught and logged at ERROR,
  never re-raised. A notification problem can't roll back a sweep that succeeded.

The channel is config-selected (:func:`resolve_channel`): a desktop notification,
an email over SMTP, or off. It defaults to off when nothing is configured, so a
fresh install never surprises the operator; a generic "enable" resolves to the
desktop channel. SMTP host, port, and credentials come from the environment only
(:meth:`SmtpConfig.from_env`) — never hardcoded — and the password is never logged
(it is kept out of the dataclass ``repr`` and never interpolated into a log line).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import smtplib
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import (
    _DEFAULT_SMTP_PORT,
    _DEFAULT_SMTP_STARTTLS as _DEFAULT_STARTTLS,
    _ENV_NOTIFY_CHANNEL as _ENV_CHANNEL,
    _ENV_SMTP_HOST,
    _ENV_SMTP_PASSWORD,
    _ENV_SMTP_PORT,
    _ENV_SMTP_RECIPIENT,
    _ENV_SMTP_SENDER,
    _ENV_SMTP_STARTTLS,
    _ENV_SMTP_USERNAME,
)
from . import crisis
from .models import Analysis, Article, Client, Run

_log = logging.getLogger(__name__)

# --- Named constants (the "why" lives next to each) ----------------------------
#
# The NEWSPULSE_* env-var name spellings and the SMTP port/STARTTLS defaults live in
# newspulse.config (the single source for every NEWSPULSE_* name; see its module
# docstring) and are imported above under these local aliases. Only notify-internal
# knobs remain below.

# Wall-clock ceilings (seconds) for the two delivery boundaries. A desktop notifier
# is a local subprocess (near-instant); SMTP crosses the network, so it gets more
# room — but both are bounded so a hung notifier can never stall the caller.
_DESKTOP_TIMEOUT = 10
_SMTP_TIMEOUT = 30

# Seconds to keep the Windows PowerShell balloon process alive after ShowBalloonTip:
# a NotifyIcon is disposed when the process exits, which cancels an un-rendered
# balloon, so the process must outlive the render. Kept safely under _DESKTOP_TIMEOUT
# (the subprocess wall-clock ceiling) so the keep-alive never trips its own timeout.
_WINDOWS_BALLOON_SECONDS = 4

# What the proposal paragraph says, in the notification's own language (English,
# like the rest of this module's wording — the interface is German, the operator
# mail is not). Constants rather than inline literals because the three of them
# have to keep saying the same thing: the tool is *asking*, and nothing has
# happened yet.
_PROPOSAL_HEADER = "Crisis proposed (nothing has changed yet):"
_PROPOSAL_FOOTER = (
    "Open NewsPulse and press 'Krise erklären' to declare one, or 'Verwerfen' "
    "to stand the offer down — a dismissed story is not offered again. Until "
    "somebody does either, the sweep runs exactly as it did."
)
_PROPOSAL_TAIL = "crisis proposed for"

# Truthy/falsy spellings accepted for boolean env vars, matching common shell idiom.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


class Channel(StrEnum):
    """The delivery channel for a post-run alert summary. ``OFF`` is the safe
    default so an unconfigured install never emits a notification."""

    OFF = "off"
    DESKTOP = "desktop"
    EMAIL = "email"


# Config spellings mapped to a channel. A generic "enable" word resolves to the
# desktop channel (the recommended local default); explicit ``email`` opts into SMTP.
_CHANNEL_ALIASES: dict[str, Channel] = {
    "": Channel.OFF,
    "off": Channel.OFF,
    "none": Channel.OFF,
    "disabled": Channel.OFF,
    "false": Channel.OFF,
    "0": Channel.OFF,
    "desktop": Channel.DESKTOP,
    "on": Channel.DESKTOP,
    "true": Channel.DESKTOP,
    "1": Channel.DESKTOP,
    "yes": Channel.DESKTOP,
    "email": Channel.EMAIL,
    "smtp": Channel.EMAIL,
}


class NotifyConfigError(RuntimeError):
    """A channel is enabled but not fully configured (e.g. email without SMTP).

    Raised inside the delivery boundary and caught by :func:`notify_alerts`, which
    logs it at ERROR — a misconfiguration must not abort a run that already persisted.
    """


# --- Value objects -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FiredAlert:
    """One alert that fired during a run: which client, which headline, how important.

    The minimal projection of an ``analyses`` row needed to summarize — deliberately
    not the ORM object, so summary formatting is a pure function of plain data.
    """

    client_name: str
    headline: str
    importance_score: int
    url: str = ""


@dataclass(frozen=True, slots=True)
class ProposedCrisis:
    """A crisis the tool is *offering*, as the summary renders it.

    DEC-1 locked option A: the tool proposes and a person declares. The offer
    appears on Heute and in the notification, and nowhere else — this line rides
    along inside the alert summary that was already going out. It is deliberately
    not a notification of its own: "keine zusätzliche Benachrichtigung über die
    bestehende Alarmierung hinaus" is the acceptance criterion, so a quiet morning
    with a proposal and no alerts stays as silent as a quiet morning without one.

    The plain projection, like :class:`FiredAlert`: no ORM object reaches the
    formatting.
    """

    client_name: str
    headline: str
    outlets: int


@dataclass(frozen=True, slots=True)
class FoundOpportunity:
    """One fresh fast-lane opportunity, as its notification renders it (UHR-05).

    Unlike the crisis proposal this is *not* a ride-along: the light run exists
    to put an opening on the screen within hours (DEC-6 A), and its findings
    would otherwise wait for the next morning's alert mail — which is exactly
    the twenty-hour delay the fast lane was built against. The plain
    projection, like :class:`FiredAlert`: no ORM object reaches the formatting.
    """

    client_name: str
    headline: str
    outlets: int
    #: Whole hours left in the window when the run found it — the same number
    #: the card on Heute leads with.
    hours_left: int


@dataclass(frozen=True, slots=True)
class ClientAlertGroup:
    """One client's alerts collapsed to a count and its single most important headline."""

    client_name: str
    count: int
    top_headline: str
    top_importance: int


@dataclass(frozen=True, slots=True)
class AlertSummary:
    """The rendered summary of a run's alerts, ready for any channel.

    ``subject`` + ``body`` feed email; ``desktop_message`` is the compact one-liner a
    desktop notification shows. Built once (:func:`build_summary`) so every channel
    renders identical wording.
    """

    total: int
    client_count: int
    groups: list[ClientAlertGroup]
    subject: str
    body: str
    desktop_message: str
    #: The crises being offered, carried so a caller (or a test) can assert that
    #: an offer rode along rather than parsing it back out of the body.
    proposals: list[ProposedCrisis] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """The outcome of a notify attempt, returned rather than raised.

    ``sent`` says whether a channel actually delivered; ``reason`` explains a
    non-send (``no-alerts`` / ``channel-off`` / ``delivery-error``) so a caller (or
    a test) can assert *why* nothing went out without parsing log lines.
    """

    sent: bool
    channel: Channel
    reason: str
    alert_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """SMTP delivery settings, sourced entirely from the environment.

    ``username`` and ``password`` are marked ``repr=False`` so a credential can
    never leak into a stringified config in a log line or traceback frame. There is
    no hardcoded default host or credential anywhere: an unconfigured install has no
    ``SmtpConfig`` at all (see :meth:`from_env`).
    """

    host: str
    port: int
    sender: str
    recipient: str
    use_starttls: bool = True
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SmtpConfig | None:
        """Build an ``SmtpConfig`` from ``env``, or ``None`` if SMTP is unconfigured.

        Host and recipient are the two fields with no sensible default, so their
        absence means "email is not set up" and yields ``None`` rather than a
        half-formed config. The sender defaults to the recipient (send-to-self is the
        common single-user case). Credentials are optional: an open relay needs none.
        """
        host = (env.get(_ENV_SMTP_HOST) or "").strip()
        recipient = (env.get(_ENV_SMTP_RECIPIENT) or "").strip()
        if not host or not recipient:
            return None
        sender = (env.get(_ENV_SMTP_SENDER) or recipient).strip()
        return cls(
            host=host,
            port=_env_int(env, _ENV_SMTP_PORT, _DEFAULT_SMTP_PORT),
            sender=sender,
            recipient=recipient,
            use_starttls=_env_bool(env, _ENV_SMTP_STARTTLS, _DEFAULT_STARTTLS),
            username=(env.get(_ENV_SMTP_USERNAME) or None),
            password=(env.get(_ENV_SMTP_PASSWORD) or None),
        )


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    """The resolved notification configuration: which channel, and SMTP if email."""

    channel: Channel
    smtp: SmtpConfig | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> NotifyConfig:
        """Resolve the channel and (for email) SMTP settings from ``env``.

        ``env`` is injectable so tests exercise the resolution without touching the
        process environment; it defaults to ``os.environ`` in production use. SMTP is
        only read when the email channel is selected, so a desktop/off setup never
        needs SMTP vars present.
        """
        resolved = os.environ if env is None else env
        channel = resolve_channel(resolved.get(_ENV_CHANNEL))
        smtp = SmtpConfig.from_env(resolved) if channel is Channel.EMAIL else None
        return cls(channel=channel, smtp=smtp)


# Delivery boundaries, injectable so a test can substitute a spy for the real
# subprocess/SMTP call ("with the delivery channel mocked").
DesktopSender = Callable[[AlertSummary], None]
EmailSender = Callable[[AlertSummary, SmtpConfig], None]


# --- Env helpers ---------------------------------------------------------------


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    """Read an int from ``env``, falling back to ``default`` if unset or unparseable."""
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("invalid %s=%r; using default %d", name, raw, default)
        return default


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Read a boolean from ``env`` (common truthy/falsy spellings), else ``default``."""
    raw = env.get(name)
    if raw is None:
        return default
    key = raw.strip().lower()
    if key in _TRUTHY:
        return True
    if key in _FALSY:
        return False
    _log.warning("invalid %s=%r; using default %s", name, raw, default)
    return default


def resolve_channel(raw: str | None) -> Channel:
    """Map a config value to a :class:`Channel`, defaulting to ``OFF``.

    Unconfigured (``None``/empty) is ``OFF`` so a fresh install is silent; a generic
    "enable" word resolves to ``DESKTOP`` (the default active channel); an
    unrecognized value is treated as ``OFF`` with a warning rather than guessing.
    """
    if raw is None:
        return Channel.OFF
    channel = _CHANNEL_ALIASES.get(raw.strip().lower())
    if channel is None:
        _log.warning("unknown notify channel %r; notifications disabled", raw)
        return Channel.OFF
    return channel


# --- Summary formatting (pure) -------------------------------------------------


def _group_alerts(alerts: Sequence[FiredAlert]) -> list[ClientAlertGroup]:
    """Collapse fired alerts into per-client groups, most-urgent client first.

    Each client's top headline is its highest-importance alert (ties broken by
    headline text for determinism). Groups are ordered by top importance, then count,
    then name — again fully deterministic so the same alerts always render identically.
    """
    by_client: dict[str, list[FiredAlert]] = {}
    for alert in alerts:
        by_client.setdefault(alert.client_name, []).append(alert)
    groups: list[ClientAlertGroup] = []
    for name, client_alerts in by_client.items():
        top = sorted(client_alerts, key=lambda a: (-a.importance_score, a.headline))[0]
        groups.append(
            ClientAlertGroup(
                client_name=name,
                count=len(client_alerts),
                top_headline=top.headline,
                top_importance=top.importance_score,
            )
        )
    groups.sort(key=lambda g: (-g.top_importance, -g.count, g.client_name))
    return groups


def _format_subject(total: int, clients: int) -> str:
    """One-line subject: alert and client counts, pluralized."""
    alert_word = "alert" if total == 1 else "alerts"
    client_word = "client" if clients == 1 else "clients"
    return f"NewsPulse: {total} {alert_word} across {clients} {client_word}"


def _format_body(
    groups: Sequence[ClientAlertGroup], proposals: Sequence[ProposedCrisis] = ()
) -> str:
    """Multi-line body: one bullet per client with its count and top headline.

    A proposal, when there is one, is a paragraph under the bullets and not a
    bullet among them. It is a different kind of sentence: the alerts are a
    report on what happened, and this is a question with a button behind it.
    """
    total = sum(group.count for group in groups)
    header = f"{total} alert(s) fired across {len(groups)} client(s):"
    bullets = [
        f"• {group.client_name} ({group.count}): {group.top_headline}"
        for group in groups
    ]
    if not proposals:
        return "\n".join([header, "", *bullets])
    offered = [
        f"• {p.client_name}: {p.headline} ({p.outlets} outlet(s))" for p in proposals
    ]
    return "\n".join(
        [
            header,
            "",
            *bullets,
            "",
            _PROPOSAL_HEADER,
            *offered,
            "",
            _PROPOSAL_FOOTER,
        ]
    )


def _one_line(text: str) -> str:
    """Collapse every run of whitespace (newlines, tabs) to a single space.

    A desktop notification is a single line, and a raw newline in a feed headline
    would break the ``osascript`` AppleScript string literal the message is embedded
    in (AppleScript cannot span a string across a bare newline) — so the compact
    message is normalized to one physical line before it reaches any channel.
    """
    return " ".join(text.split())


def _format_desktop_message(
    groups: Sequence[ClientAlertGroup], proposals: Sequence[ProposedCrisis] = ()
) -> str:
    """Compact one-liner for a desktop notification (title carries the counts).

    A proposal is appended as a short tail rather than replacing the lead: a
    desktop notification has one line, and dropping the alerts for the offer
    would hide the coverage the offer is *about*.
    """
    top = groups[0]
    lead = f"{top.client_name} ({top.count}): {top.top_headline}"
    others = len(groups) - 1
    message = f"{lead} +{others} more client(s)" if others else lead
    if proposals:
        names = ", ".join(p.client_name for p in proposals)
        message = f"{message} — {_PROPOSAL_TAIL}: {names}"
    return _one_line(message)


def build_summary(
    alerts: Sequence[FiredAlert], proposals: Sequence[ProposedCrisis] = ()
) -> AlertSummary | None:
    """Render an :class:`AlertSummary`, or ``None`` when there are no alerts.

    Returning ``None`` for the empty case is the contract behind "no alerts, no
    notification": callers branch on it and never send an empty summary.

    A proposal does **not** change that, and that is deliberate rather than an
    oversight. DEC-1's offer appears "auf Heute und in der Benachrichtigung", and
    the acceptance criterion beside it forbids any notification beyond the
    alerting that already fires. So an offer rides along inside a summary that
    was going out anyway; on a morning with no alerts it waits on Heute, where it
    costs nobody an interruption.
    """
    if not alerts:
        return None
    groups = _group_alerts(alerts)
    total = sum(group.count for group in groups)
    offered = list(proposals)
    return AlertSummary(
        total=total,
        client_count=len(groups),
        groups=groups,
        subject=_format_subject(total, len(groups)),
        body=_format_body(groups, offered),
        desktop_message=_format_desktop_message(groups, offered),
        proposals=offered,
    )


# --- Collecting a run's fired alerts -------------------------------------------


def collect_fired_alerts(session: Session, *, since: dt.datetime) -> list[FiredAlert]:
    """The alerts a run fired: analyses flagged ``is_alert`` and analyzed since ``since``.

    Reads only (a join across analyses/articles/clients); it never writes, so it can
    never roll back the run whose data was already committed. ``since`` bounds this to
    the run's own window — typically the run's ``started_at`` — so a re-notify never
    re-announces yesterday's alerts.
    """
    rows = session.execute(
        select(
            Client.name,
            Article.title,
            Article.url,
            Analysis.importance_score,
        )
        .join(Analysis, Analysis.client_id == Client.id)
        .join(Article, Article.id == Analysis.article_id)
        .where(Analysis.is_alert.is_(True))
        .where(Analysis.analyzed_at >= since)
    ).all()
    return [
        FiredAlert(
            client_name=row.name,
            headline=row.title,
            importance_score=row.importance_score,
            url=row.url,
        )
        for row in rows
    ]


# --- Delivery boundaries -------------------------------------------------------


def _applescript_quote(value: str) -> str:
    """Quote a string as an AppleScript literal (escape backslash and double-quote).

    The command is run via ``subprocess`` in argv form (no shell), so this only needs
    to close AppleScript string escaping — a feed headline can carry quotes.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _desktop_command(summary: AlertSummary) -> list[str]:
    """The platform's desktop-notification command (argv form, no shell)."""
    if sys.platform == "darwin":
        script = (
            f"display notification {_applescript_quote(summary.desktop_message)} "
            f"with title {_applescript_quote(summary.subject)}"
        )
        return ["osascript", "-e", script]
    if sys.platform.startswith("linux"):
        return ["notify-send", summary.subject, summary.desktop_message]
    if sys.platform.startswith("win"):
        # Best-effort Windows toast via a PowerShell balloon; email is the recommended
        # channel for a headless scheduled task (see docs/scheduling.md). The process
        # must outlive ShowBalloonTip — a PowerShell that exits immediately disposes the
        # NotifyIcon before Windows renders the balloon — so it sleeps briefly, then
        # disposes explicitly.
        ps = (
            "[reflection.assembly]::LoadWithPartialName('System.Windows.Forms') > $null; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
            f"$n.ShowBalloonTip({_WINDOWS_BALLOON_SECONDS * 1000}, "
            f"{_powershell_quote(summary.subject)}, "
            f"{_powershell_quote(summary.desktop_message)}, 'Info'); "
            f"Start-Sleep -Seconds {_WINDOWS_BALLOON_SECONDS}; $n.Dispose()"
        )
        return ["powershell", "-NoProfile", "-Command", ps]
    raise NotifyConfigError(f"no desktop notifier for platform {sys.platform!r}")


def _powershell_quote(value: str) -> str:
    """Quote a string as a PowerShell single-quoted literal (double any single-quote)."""
    return "'" + value.replace("'", "''") + "'"


def _send_desktop(summary: AlertSummary) -> None:
    """Deliver ``summary`` as a local desktop notification.

    Raises on any failure (notifier binary missing, non-zero exit, timeout); the
    caller (:func:`notify_alerts`) turns that into a logged ERROR, never a crash.
    """
    subprocess.run(  # noqa: S603 — argv form, no shell; args are our own strings
        _desktop_command(summary),
        check=True,
        capture_output=True,
        timeout=_DESKTOP_TIMEOUT,
    )


def _send_email(summary: AlertSummary, smtp: SmtpConfig) -> None:
    """Deliver ``summary`` as an email over SMTP.

    Credentials come from ``smtp`` (sourced from env) and are never logged. STARTTLS
    is applied before any ``login`` so a password never crosses the wire in the clear.
    Raises on any SMTP failure; the caller logs it as an ERROR.
    """
    message = EmailMessage()
    message["Subject"] = summary.subject
    message["From"] = smtp.sender
    message["To"] = smtp.recipient
    message.set_content(summary.body)
    with smtplib.SMTP(smtp.host, smtp.port, timeout=_SMTP_TIMEOUT) as server:
        if smtp.use_starttls:
            server.starttls()
        if smtp.username and smtp.password:
            server.login(smtp.username, smtp.password)
        server.send_message(message)


def _dispatch(
    summary: AlertSummary,
    config: NotifyConfig,
    send_desktop: DesktopSender,
    send_email: EmailSender,
) -> None:
    """Route the summary to the configured channel (``OFF`` is handled by the caller)."""
    if config.channel is Channel.DESKTOP:
        send_desktop(summary)
    elif config.channel is Channel.EMAIL:
        if config.smtp is None:
            raise NotifyConfigError(
                "email channel selected but SMTP is not configured "
                "(set NEWSPULSE_SMTP_HOST and NEWSPULSE_SMTP_RECIPIENT)"
            )
        send_email(summary, config.smtp)


# --- Orchestration -------------------------------------------------------------


def notify_alerts(
    alerts: Sequence[FiredAlert],
    config: NotifyConfig,
    *,
    proposals: Sequence[ProposedCrisis] = (),
    send_desktop: DesktopSender | None = None,
    send_email: EmailSender | None = None,
) -> NotifyResult:
    """Summarize ``alerts`` and deliver via ``config``'s channel; never raises.

    The two send callbacks are injectable so a test can mock the delivery channel;
    they default to the real desktop/SMTP boundaries. The order of the guards encodes
    the acceptance:

    * no alerts -> no notification of any kind (returns ``no-alerts``);
    * channel off -> nothing delivered even when alerts fired (``channel-off``);
    * any delivery error is caught and logged at ERROR (``delivery-error``) so a
      broken SMTP config can never roll back a run whose data is already persisted.
    """
    desktop = send_desktop or _send_desktop
    email = send_email or _send_email

    summary = build_summary(alerts, proposals)
    if summary is None:
        _log.debug("no alerts fired; no notification sent")
        return NotifyResult(sent=False, channel=config.channel, reason="no-alerts")
    if config.channel is Channel.OFF:
        _log.debug(
            "notifications disabled (channel=off); %d alert(s) not delivered",
            summary.total,
        )
        return NotifyResult(
            sent=False, channel=Channel.OFF, reason="channel-off", alert_count=summary.total
        )
    try:
        _dispatch(summary, config, desktop, email)
    except Exception as exc:  # noqa: BLE001 — notification must never fail the run
        _log.error(
            "notification via %s failed: %s; run data already persisted, not rolled back",
            config.channel.value,
            exc,
        )
        return NotifyResult(
            sent=False,
            channel=config.channel,
            reason="delivery-error",
            alert_count=summary.total,
            error=str(exc),
        )
    _log.info(
        "delivered %d-alert notification for %d client(s) via %s",
        summary.total,
        summary.client_count,
        config.channel.value,
    )
    return NotifyResult(
        sent=True, channel=config.channel, reason="delivered", alert_count=summary.total
    )


def _opportunity_summary(found: Sequence[FoundOpportunity]) -> AlertSummary:
    """Render fresh opportunities for delivery. Same wording on every channel,
    like :func:`build_summary`; the caller guarantees ``found`` is non-empty."""
    clients = sorted({item.client_name for item in found})
    word = "opportunity" if len(found) == 1 else "opportunities"
    subject = (
        f"NewsPulse: {len(found)} newsjack {word} for {', '.join(clients)}"
    )
    bullets = [
        f"• {item.client_name}: {item.headline} "
        f"({item.outlets} outlet(s), ~{item.hours_left}h left)"
        for item in found
    ]
    body = "\n".join(
        [
            "The fast lane found an opening. The window is already running:",
            "",
            *bullets,
            "",
            "Open NewsPulse — the card on Heute carries the remaining time and "
            "what the standing rests on.",
        ]
    )
    lead = found[0]
    message = f"{lead.client_name}: {lead.headline} (~{lead.hours_left}h left)"
    if len(found) > 1:
        message = f"{message} +{len(found) - 1} more"
    return AlertSummary(
        total=len(found),
        client_count=len(clients),
        groups=[],
        subject=subject,
        body=body,
        desktop_message=_one_line(message),
    )


def notify_opportunities(
    found: Sequence[FoundOpportunity],
    config: NotifyConfig | None = None,
    *,
    send_desktop: DesktopSender | None = None,
    send_email: EmailSender | None = None,
) -> NotifyResult:
    """Announce the light run's fresh opportunities; never raises.

    The guards mirror :func:`notify_alerts`, and the first one carries the
    fast lane's own promise: **no finding, no noise.** A run over a quiet radar
    — the great majority of them, eight a day — sends nothing at all, so the
    channel stays trusted for the runs that do. Only *fresh* rows belong here
    (what :func:`newspulse.job.run_newsjack` just stored), never the standing
    stock, or every three-hour tick would re-announce the same opening.
    """
    resolved = config or NotifyConfig.from_env()
    desktop = send_desktop or _send_desktop
    email = send_email or _send_email
    if not found:
        return NotifyResult(
            sent=False, channel=resolved.channel, reason="no-opportunities"
        )
    if resolved.channel is Channel.OFF:
        return NotifyResult(
            sent=False,
            channel=Channel.OFF,
            reason="channel-off",
            alert_count=len(found),
        )
    summary = _opportunity_summary(found)
    try:
        _dispatch(summary, resolved, desktop, email)
    except Exception as exc:  # noqa: BLE001 — notification must never fail the run
        _log.error(
            "opportunity notification via %s failed: %s; "
            "run data already persisted, not rolled back",
            resolved.channel.value,
            exc,
        )
        return NotifyResult(
            sent=False,
            channel=resolved.channel,
            reason="delivery-error",
            alert_count=len(found),
            error=str(exc),
        )
    _log.info(
        "delivered %d-opportunity notification via %s",
        len(found),
        resolved.channel.value,
    )
    return NotifyResult(
        sent=True,
        channel=resolved.channel,
        reason="delivered",
        alert_count=len(found),
    )


def collect_proposals(session: Session) -> list[ProposedCrisis]:
    """The crises the tool is offering right now, one per mandate at most.

    Read-only, like everything else this module does after a run: it asks
    :func:`newspulse.crisis.propose`, which writes nothing, changes no cadence and
    drafts no text. A mandate already in a declared crisis has nothing left to be
    offered and is skipped by ``propose`` itself.

    Mandates only, and active ones only — the same roster Heute puts the offer in
    front of, so the mail and the page never disagree about whether there is a
    question outstanding.
    """
    mandates = session.scalars(
        select(Client)
        .where(Client.is_competitor.is_(False), Client.active.is_(True))
        .order_by(Client.name)
    ).all()
    offered: list[ProposedCrisis] = []
    for mandate in mandates:
        proposal = crisis.propose(session, mandate)
        if proposal is not None:
            offered.append(
                ProposedCrisis(
                    client_name=mandate.name,
                    headline=proposal.headline,
                    outlets=proposal.outlets,
                )
            )
    return offered


def notify_after_run(
    session: Session,
    run: Run,
    *,
    config: NotifyConfig | None = None,
    send_desktop: DesktopSender | None = None,
    send_email: EmailSender | None = None,
) -> NotifyResult:
    """Collect ``run``'s fired alerts and notify — the wiring point for a completed run.

    Resolves the config from the environment when not supplied, reads the alerts
    flagged since the run started, and hands off to :func:`notify_alerts`. Read-only
    and non-raising: safe to call unconditionally after a sweep has committed.

    DEC-1's offer is collected here and travels *inside* that same notification —
    never as one of its own. See :func:`build_summary`.
    """
    resolved = config or NotifyConfig.from_env()
    alerts = collect_fired_alerts(session, since=run.started_at)
    return notify_alerts(
        alerts,
        resolved,
        # Only worth collecting when something will carry them: with no alerts,
        # build_summary returns None and nothing goes out (see its docstring), so
        # a quiet morning skips the per-mandate clustering pass entirely. The
        # offer waits on Heute either way.
        proposals=collect_proposals(session) if alerts else (),
        send_desktop=send_desktop,
        send_email=send_email,
    )


__all__ = [
    "Channel",
    "NotifyConfigError",
    "FiredAlert",
    "FoundOpportunity",
    "notify_opportunities",
    "ProposedCrisis",
    "ClientAlertGroup",
    "AlertSummary",
    "NotifyResult",
    "SmtpConfig",
    "NotifyConfig",
    "resolve_channel",
    "build_summary",
    "collect_fired_alerts",
    "collect_proposals",
    "notify_alerts",
    "notify_after_run",
]
