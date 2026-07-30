"""Interface language: German (default) and English.

Keyed on the German string itself rather than on invented ids. Two reasons:

* A missing translation degrades to German, which is a working sentence. A
  key-based scheme shows ``settings.clients.add`` to the operator instead, which
  is worse than the wrong language.
* The templates stay readable. ``t("Lauf starten")`` says what it renders;
  ``t("run.start")`` requires a second file to understand.

The cost is that editing a German string silently un-translates it. That is an
acceptable trade for a two-language interface, and the test suite checks that
every key here is still reachable.

**Scope: chrome only.** Article headlines, summaries and the analyzer's own
output stay in the language they were written in — they are data, not interface.
Switching to English does not retranslate a stored German summary, and pretending
otherwise would be a lie about what the tool did.
"""

from __future__ import annotations

DEFAULT_LANGUAGE = "de"
LANGUAGES = ("de", "en")

# The cookie holding the choice. A cookie rather than a setting row: this is a
# per-reader preference, not configuration of the installation.
COOKIE_NAME = "newspulse_lang"
# A year: the choice is stable, and re-asking every session would be noise.
COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# German source string -> English. Ordered roughly by where it appears.
_EN: dict[str, str] = {
    # --- Chrome / navigation -------------------------------------------------
    "Heute": "Today",
    "Mandanten": "Clients",
    "Archiv": "Archive",
    "Einstellungen": "Settings",
    "↻ Aktualisieren": "↻ Refresh",
    "Feeds jetzt abrufen und analysieren — läuft im Hintergrund":
        "Fetch and analyse feeds now — runs in the background",
    "Letzter Lauf": "Last run",
    "Lauf läuft…": "Run in progress…",
    "Uhr": "",
    "Feeds ok": "Feeds ok",
    "Feed-Fehler": "feed errors",
    "Artikel geprüft": "articles checked",
    "Noch kein Lauf": "No run yet",
    "Sprache": "Language",
    # --- Today ---------------------------------------------------------------
    "Kategorie": "Category",
    "Alle Kategorien": "All categories",
    "Tag": "Day",
    "Anzeigen": "Show",
    "ausgeblendet": "hidden",
    "Warnungen": "Alerts",
    "Keine Warnungen heute.": "No alerts today.",
    "Gesamter Tag": "Whole day",
    "aus": "from",
    "Artikeln": "articles",
    "Keine Berichterstattung für": "No coverage for",
    "Alle": "All",
    "Meldungen": "items",
    "Mandant": "Client",
    "Alle Mandanten": "All clients",
    "Filtern": "Filter",
    "gelesen": "read",
    "für Mandant": "for client",
    "erledigt": "done",
    "aufgegriffen": "pickups",
    "auch bei": "also in",
    "weiteren": "more",
    "Wichtigkeit": "Importance",
    "Wirkung auf die Reputation des Mandanten":
        "Effect on the client's reputation",
    # --- Today: the impulse column (positioning drafts) ----------------------
    # "Impuls" is the consultant's own word for this: a prompt to say something,
    # not a task. "Opening" carries the same sense in English — a moment that has
    # opened up — where a literal "impulse" would read as a mood.
    "Impulse": "Openings",
    "Kein Anlass heute.": "No opening today.",
    "Kopieren": "Copy",
    "These": "Thesis",
    "Nicht die These": "Not the thesis",
    "Kontext": "Context",
    "Glaubwürdigkeit": "Credibility",
    "Ableitbare Aussagen": "Derivable statements",
    "Entwurf aus Schlagzeilen und Feed-Anrissen, nicht aus den vollständigen "
    "Artikeln. Vor dem Versand prüfen.":
        "Drafted from headlines and feed snippets, not from the full articles. "
        "Check before sending.",
    "positiv": "positive",
    "neutral": "neutral",
    "negativ": "negative",
    # models.Category members. Stored as German values, so the enum is the key —
    # translating them at render keeps the database and the analyzer contract
    # untouched while the interface follows the reader.
    "produkt": "product",
    "personalie": "people",
    "krise": "crisis",
    "regulatorik": "regulatory",
    "finanzen": "finance",
    "wettbewerb": "competition",
    "sonstiges": "other",
    # --- Clients -------------------------------------------------------------
    "Ohne Branche": "No industry",
    "inaktiv": "inactive",
    "heute": "today",
    "im Archiv": "in archive",
    "Empfehlungen": "Recommendations",
    "Coverage Map": "Coverage map",
    "Übersicht": "Overview",
    "Gespräch zurücksetzen": "Reset conversation",
    "Refresh": "Refresh",
    "Feeds speichern": "Save feeds",
    "alle": "all",
    "Excel": "Excel",
    "Noch keine Mandanten. Unter": "No clients yet. Add or import them under",
    "anlegen oder importieren.": ".",
    "vs": "vs",
    # --- Archive -------------------------------------------------------------
    "Monat": "Month",
    "Alle Monate": "All months",
    "Publisher": "Publisher",
    "Alle Publisher": "All publishers",
    "Medien-Tier": "Media tier",
    "Alle Tiers": "All tiers",
    "Tier 1 — Leitmedien": "Tier 1 — National press",
    "Tier 2 — Fach- & Regionalpresse": "Tier 2 — Trade & regional",
    "Tier 3 — Finanz-Ticker": "Tier 3 — Finance tickers",
    "Suche": "Search",
    "Schlagzeile oder Zusammenfassung": "Headline or summary",
    "Zurücksetzen": "Reset",
    "Artikel": "articles",
    "Seite": "page",
    "← Neuer": "← Newer",
    "Älter →": "Older →",
    "Keine Artikel für die gewählten Filter.": "No articles for these filters.",
    # --- Client detail -------------------------------------------------------
    "Branche": "Industry",
    "Land": "Country",
    "Aliase": "Aliases",
    "Keywords": "Keywords",
    "Alert-Themen": "Alert topics",
    "Von": "From",
    "Bis": "To",
    "Quelle": "Source",
    "Alle Quellen": "All sources",
    "Keine Artikel": "No articles",
    "letzte 30 Tage": "last 30 days",
    "Unternehmen": "Company",
    "Rolle": "Role",
    "Alerts": "Alerts",
    "Anteil": "Share",
    "Wettbewerber": "Competitor",
    "entfernen": "remove",
    "Wettbewerber hinzufügen": "Add competitor",
    "Hinzufügen": "Add",
    "Noch keine Wettbewerber hinterlegt — ohne Vergleichsgruppe ist ein Anteil immer 100 % und damit ohne Aussage.":
        "No competitors set — without a comparison group a share is always 100% "
        "and says nothing.",
    "Ein Wettbewerber ist selbst ein überwachtes Unternehmen — nur so ist seine Meldungszahl vergleichbar. Unter Einstellungen anlegen und dort als":
        "A competitor is itself a monitored company — that is what makes its "
        "count comparable. Create it under Settings and mark it as",
    "markieren, damit er nicht im Morgen-Digest auftaucht.":
        "so it stays out of the morning digest.",
    # --- Coverage map --------------------------------------------------------
    "Pitch-Lücken": "Pitch gaps",
    "berichten über Wettbewerber, nie über": "cover competitors, never",
    "Wer schreibt über wen": "Who writes about whom",
    "Wettbewerb links": "competitors left",
    "rechts": "right",
    "nach Ungleichgewicht sortiert": "sorted by imbalance",
    "Wettbewerb": "Competitors",
    "Vollständige Tabelle": "Full table",
    "Medien": "outlets",
    "Medium": "Outlet",
    # --- Advice --------------------------------------------------------------
    "Zeitraum": "Period",
    "Letzte 7 Tage": "Last 7 days",
    "Letzte 30 Tage": "Last 30 days",
    "Letzte 90 Tage": "Last 90 days",
    "Neu erzeugen": "Regenerate",
    "Empfehlungen erzeugen": "Generate recommendations",
    "im Zeitraum": "in period",
    "Wird erzeugt — das dauert etwa eine Minute. Seite neu laden.":
        "Generating — this takes about a minute. Reload the page.",
    "Lage": "Situation",
    "Erzeugt am": "Generated",
    "aus": "from",
    "Artikeln.": "articles.",
    "Maßnahmen": "Actions",
    "Grundlage": "Based on",
    "Meldung(en)": "item(s)",
    "Keine Maßnahmen vorgeschlagen — im Zeitraum gab es nichts, das eine Reaktion erfordert.":
        "No actions proposed — nothing in this period requires a response.",
    "Noch keine Empfehlungen erzeugt.": "No recommendations generated yet.",
    "reaktiv": "reactive",
    "proaktiv": "proactive",
    "beobachten": "monitor",
    "heute": "today",
    "diese woche": "this week",
    "laufend": "ongoing",
    # --- Settings ------------------------------------------------------------
    "Lauf starten": "Start a run",
    "Seit letztem Lauf (Standard)": "Since last run (default)",
    "Jetzt aktualisieren": "Refresh now",
    "Letzte": "Last",
    "Tage nachladen": "days backfill",
    "Letzte Läufe": "Recent runs",
    "Start": "Start",
    "Status": "Status",
    "Fehler": "Errors",
    "Clients": "Clients",
    "+ Client hinzufügen": "+ Add client",
    "Name": "Name",
    "Aliasse": "Aliases",
    "Suchbegriffe": "Keywords",
    "Alarm-Themen": "Alert topics",
    "Website": "Website",
    "Aktionen": "Actions",
    "Bearbeiten": "Edit",
    "Schließen": "Close",
    "Abbrechen": "Cancel",
    "Speichern": "Save",
    "Deaktivieren": "Deactivate",
    "Reaktivieren": "Reactivate",
    "aktiv": "active",
    "gesamt": "total",
    "Import (Excel / CSV)": "Import (Excel / CSV)",
    "Vorschau": "Preview",
    "Vorschau zeigt die ersten": "Preview shows the first",
    "Vorschläge auf Basis der Berichterstattung der letzten":
        "Suggestions based on coverage from the last",
    "Entwurf, keine Entscheidung": "A draft, not a decision",
    "jede Maßnahme nennt die Meldungen, auf die sie sich stützt, damit die Begründung nachprüfbar bleibt. Die Bewertung stützt sich nur auf Schlagzeilen und Kurzfassungen, nicht auf die vollständigen Artikel.":
        "every action cites the coverage it rests on, so the reasoning stays "
        "checkable. The assessment draws only on headlines and short summaries, "
        "not on full articles.",
    "Die Lücken oben sind Publikationen, die regelmäßig über den Wettbewerb berichten und nie über":
        "The gaps above are publications that cover the competition regularly and never",
    "also eine Pitch-Liste, keine Statistik.": "— a pitch list, not a statistic.",
    "Importieren": "Import",
    "Alarm-Schwellenwert": "Alert threshold",
    "Feeds": "Feeds",
    "Noch keine Clients. Über das Formular oder den Import anlegen.":
        "No clients yet. Add one with the form or the import.",
    # --- Assistant drawer ----------------------------------------------------
    "Captain Comms": "Captain Comms",
    # Streaming status and failure text, read inside the drawer.
    "Kontext": "Context",
    "verbunden": "connected",
    "denkt nach": "thinking",
    "Keine Frage gestellt.": "No question asked.",
    "(keine Berichterstattung)": "(no coverage)",
    "claude CLI nicht gefunden.": "claude CLI not found.",
    "Start fehlgeschlagen": "Could not start",
    "claude beendet mit": "claude exited with",
    "Zeitüberschreitung nach": "Timed out after",
    "Unerwarteter Fehler": "Unexpected error",
    "Antwort zu lang, abgebrochen.": "Answer too long, stopped.",
    "Keine Antwort erhalten.": "No answer received.",
    "Fehler": "Error",
    "Verbindung unterbrochen.": "Connection lost.",
    "Kommunikationsstrategie zur Berichterstattung auf dieser Seite":
        "Communication strategy for the coverage on this page",
    "Zahlen sagen mehr als jede Selbstdarstellung.":
        "Numbers say more than any self-presentation.",
    "Strategische Einordnung der Berichterstattung, die gerade auf dem Schirm ist — belegt aus dem Archiv. Captain Comms sieht Schlagzeilen und Kurzfassungen, nicht die vollständigen Artikel, und entscheidet nichts: er berät.":
        "A strategic read of the coverage on screen — evidenced from the archive. "
        "Captain Comms sees headlines and short summaries, not full articles, and "
        "decides nothing: he advises.",
    "Welches Narrativ setzt sich gerade durch — und wer treibt es?":
        "Which narrative is taking hold — and who is driving it?",
    "Wo müssen wir reagieren, wo ist Schweigen die bessere Strategie?":
        "Where must we respond, and where is silence the better strategy?",
    "Welche Botschaft sollten wir diese Woche setzen?":
        "What message should we set this week?",
    "Was übersehen wir gerade?": "What are we missing?",
    "Portfolio": "Portfolio",
    "Fragen": "Ask",
    "Frage stellen…": "Ask a question…",
    "Tage": "days",
}

_TABLES = {"de": {}, "en": _EN}


def normalize(language: str | None) -> str:
    """A supported language code, defaulting to German."""
    code = (language or "").strip().lower()[:2]
    return code if code in LANGUAGES else DEFAULT_LANGUAGE


def translate(text: str, language: str | None = None) -> str:
    """``text`` in ``language``; the German original when untranslated."""
    return _TABLES[normalize(language)].get(text, text)


def known_keys() -> tuple[str, ...]:
    """Every German string with a translation — what the tests check against."""
    return tuple(_EN)


__all__ = [
    "COOKIE_MAX_AGE",
    "COOKIE_NAME",
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "known_keys",
    "normalize",
    "translate",
]
