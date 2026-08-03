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
    # Not "Lauf läuft" — a tautology naming an internal concept — and not
    # "Caching", which would name a mechanism that does not exist here: the sweep
    # fetches feeds and has Claude read them, it caches nothing. The word matches
    # the button that started it, so cause and effect line up on screen.
    "Aktualisierung läuft…": "Updating…",
    # While a sweep runs the header shows the *previous* run's time, so the
    # counts beside it are not read as the current ones.
    "Stand von": "as of",
    "Läuft…": "Running…",
    "Ein Lauf läuft bereits": "A run is already in progress",
    "Uhr": "",
    # Not "Feeds ok"/"Feed-Fehler": the count behind it also carries matching and
    # analysis failures, so the old label pointed at the wrong thing. And the
    # number is what the run newly stored, not what it examined.
    "Lauf ok": "Run ok",
    "Fehler im Lauf": "errors in run",
    "Fehler aus Feed-Abruf, Zuordnung oder Analyse — Details im Log":
        "Errors from feed fetching, matching or analysis — details in the log",
    "neue Artikel": "new articles",
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
    "am": "on",
    "für diesen Mandanten": "for this client",
    # The "nothing was ever found" state. Different advice from a quiet day: the
    # archive has nothing to offer, so the page points at the configuration.
    "Für": "For",
    "ist noch keine Berichterstattung erfasst.": "no coverage has been recorded yet.",
    "Weder heute noch früher. Das ist fast immer eine Frage der Einrichtung, nicht der Nachrichtenlage:":
        "Neither today nor earlier. That is almost always a question of setup, "
        "not of the news:",
    "Aliasse prüfen.": "Check the aliases.",
    "Die Presse schreibt selten den Registernamen — ohne passenden Alias verwirft der Abgleich jede Meldung.":
        "The press rarely writes the registered name — without a matching alias "
        "the filter discards every item.",
    "Mandant bearbeiten": "Edit client",
    "Nachlauf starten.": "Run a backfill.",
    "Der tägliche Lauf holt nur Neues. Die erste Berichterstattung über einen Mandanten liegt meist Wochen zurück.":
        "The daily sweep only fetches what is new. A client's first coverage is "
        "usually weeks old.",
    "Alle": "All",
    "Meldungen": "items",
    "Mandant": "Client",
    "Alle Mandanten": "All clients",
    "Filtern": "Filter",
    "gelesen": "read",
    "für Mandant": "for client",
    "erledigt": "done",
    "nicht relevant": "not relevant",
    "Diese Meldung handelt nicht von diesem Mandanten — aus seiner Berichterstattung entfernen":
        "This item is not about this client — remove it from their coverage",
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
    # The age of a draft. It may stand for a week, so every card says how old it
    # is — otherwise Monday's work reads as this morning's every morning.
    "aus dem Radar von heute": "from today's radar",
    "von gestern": "from yesterday",
    "vor": "",
    "Kein Anlass in den letzten": "No opening in the last",
    "Das Themen-Radar hat": "The topic radar looked at",
    "Marktmeldung(en) gesichtet — keine davon berührt den Kern des Mandanten stark genug für eine Positionierung.":
        "market item(s) — none of them touches the client's core closely enough "
        "for a position.",
    "Marktumfeld ansehen": "See the market",
    "Kein Themen-Radar für diesen Mandanten.": "No topic radar for this client.",
    "Ohne hinterlegte Themen lässt sich nicht bestimmen, welche Marktmeldung ihn angeht. Zwei bis drei Begriffe genügen.":
        "Without themes there is no way to tell which market coverage concerns "
        "them. Two or three terms are enough.",
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
    "Suchbegriffe": "Search terms",
    "Alarm-Themen": "Alert topics",
    "Keine Themen hinterlegt — kein Themen-Radar.":
        "No themes set — no topic radar.",
    "inaktiv": "inactive",
    "heute": "today",
    "im Archiv": "in archive",
    "Empfehlungen": "Recommendations",
    "Positionierung": "Positioning",
    "aus dem Markt": "from the market",
    "aus eigener Berichterstattung": "from own coverage",
    "Eine Positionierung zu dem, was im Themenfeld passiert — auch ohne eigene Berichterstattung.":
        "A position on what is happening in the field — with or without coverage "
        "of your own.",
    "Reagieren auf das, was über den Mandanten geschrieben wurde.":
        "Responding to what was written about the client.",
    "Impuls erzeugen": "Draft an opening",
    "Neuen Impuls erzeugen": "Draft another opening",
    "Aus dem Themen-Radar der letzten": "From the topic radar of the last",
    "Dafür braucht dieser Mandant Themen.": "This client needs themes for that.",
    "Impuls wird erzeugt — das dauert etwa eine Minute. Seite neu laden.":
        "Drafting — this takes about a minute. Reload the page.",
    "Das Themen-Radar für diesen Mandanten hat noch nichts gesammelt — der nächste Lauf holt es.":
        "The topic radar has collected nothing for this client yet — the next "
        "sweep will.",
    "Was dieser Mandant sagen oder tun sollte — aus dem Markt und aus seiner eigenen Berichterstattung.":
        "What this client should say or do — from the market and from its own "
        "coverage.",
    "jede Aussage nennt die Meldungen, auf die sie sich stützt, damit die Begründung nachprüfbar bleibt.":
        "every statement names the items it rests on, so the reasoning stays "
        "checkable.",
    "Reagieren auf die eigene Berichterstattung der letzten":
        "Responding to the client's own coverage of the last",
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
    "Wettbewerber vorschlagen": "Suggest competitors",
    "Wettbewerber": "Competitors",
    "Wettbewerber für": "Competitors for",
    "In Einstellungen vorschlagen lassen": "Get suggestions under Settings",
    "Keine Wettbewerber vorgeschlagen — das Modell kennt dieses Unternehmen nicht sicher genug. Lieber keiner als ein erfundener.":
        "No competitors proposed — the model does not know this company well "
        "enough. Better none than an invented one.",
    "Das Modell schlägt vor, angelegt wird erst per Klick.":
        "The model proposes; nothing is created until you click.",
    "Vorschläge — noch nichts davon angelegt. Ein Klick legt das Unternehmen als Wettbewerber an und verknüpft es.":
        "Proposals — none of them created yet. One click adds the company as a "
        "competitor and links it.",
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
    "Wird erzeugt — die Seite aktualisiert sich, sobald der Entwurf steht.":
        "Generating — the page will refresh itself once the draft is ready.",
    "Impuls wird erzeugt — die Seite aktualisiert sich, sobald er steht.":
        "Drafting — the page will refresh itself once it is ready.",
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
    # The advisory page when a mandate has no press of its own: it hands over to
    # the impulse rather than reporting emptiness, because the impulse is the tool
    # that works without coverage.
    "Über diesen Mandanten wurde im Zeitraum nichts geschrieben — es gibt also nichts zu empfehlen.":
        "Nothing was written about this client in the period, so there is nothing "
        "to recommend.",
    "Eine Empfehlung reagiert auf eigene Berichterstattung. Was ohne sie trägt, ist ein Impuls: eine Positionierung zu dem, was im Markt passiert.":
        "A recommendation responds to a client's own coverage. What works without "
        "it is an opening: a position on what is happening in the market.",
    "Impuls": "Opening",
    "Marktmeldung(en) für diesen Mandanten gesammelt, aber noch keinen Anlass daraus gemacht.":
        "market item(s) for this client, but has not made an opening of them yet.",
    "Für diesen Mandanten läuft noch kein Themen-Radar — ohne hinterlegte Themen entsteht auch kein Impuls.":
        "No topic radar is running for this client — without themes there is no "
        "opening either.",
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
    "in": "in",
    "Tage nachladen": "days backfill",
    "Letzte Läufe": "Recent runs",
    "Start": "Start",
    "Status": "Status",
    "Fehler": "Errors",
    "Clients": "Clients",
    "+ Client hinzufügen": "+ Add client",
    "Name": "Name",
    "Aliasse": "Aliases",
    "Suchbegriffe": "Search terms",
    "Alarm-Themen": "Alert topics",
    "Website": "Website",
    "Andere Schreibweisen des Unternehmens: „Zalando SE“, „About You“.":
        "Other spellings of the company: \u201cZalando SE\u201d, \u201cAbout You\u201d.",
    "entscheiden, was gefunden wird.": "decide what gets found.",
    "Zusammen mit Name und Aliassen: fehlt hier der richtige Begriff, wird die Meldung diesem Mandanten gar nicht erst zugeordnet.":
        "Together with the name and aliases: if the right term is missing here, "
        "the item is never matched to this client at all.",
    "stufen hoch, was schon gefunden wurde.": "escalate what was already found.",
    "Keine eigene Presseschau: „Rückruf“ löst nur dann Alarm aus, wenn die Meldung ohnehin über Name, Alias oder Suchbegriff erkannt wurde.":
        "Not a press scan of its own: \u201cRecall\u201d raises an alert only if "
        "the item was already recognised via the name, an alias or a search term.",
    "Themen": "Themes",
    "Marktthemen vorschlagen und gegen die echte Suche prüfen":
        "Propose market themes and test them against the real search",
    "Themen für": "Themes for",
    "werden vorgeschlagen und gegen die echte Suche geprüft — das dauert eine Minute.":
        "are being proposed and tested against the real search \u2014 this takes a minute.",
    "Themenvorschlag fehlgeschlagen": "Theme suggestion failed",
    "Vorschläge — noch nichts übernommen. Die Zahl ist gemessen: so viele Marktmeldungen fand die echte Radar-Abfrage damit in den letzten 90 Tagen. Ein Klick übernimmt das Thema und sucht sofort danach.":
        "Proposals \u2014 nothing adopted yet. The number is measured: that is how "
        "many market items the real radar query found with it over the last 90 "
        "days. One click adopts the theme and searches for it straight away.",
    "Marktmeldung(en)": "market item(s)",
    "über den Mandanten selbst": "about the client itself",
    "ohne Branchenfilter": "without the field filter",
    "keine Treffer — die Presse schreibt diesen Begriff nicht":
        "no hits \u2014 the press does not write this term",
    "Keine Themen vorgeschlagen.": "No themes proposed.",
    "Einrichtung wartet auf den laufenden Lauf": "Setup is waiting for the current run",
    "wird eingerichtet": "being set up",
    "eingerichtet": "set up",
    "Einrichtung fehlgeschlagen": "Setup failed",
    "Kürzel oder Name — „DE“ oder „Deutschland“. Bestimmt die Nachrichtenausgabe.":
        "Code or name \u2014 \u201cDE\u201d or \u201cGermany\u201d. Selects the news edition.",
    "Für das Logo und als Kontext für Vorschläge.":
        "For the logo, and as context for suggestions.",
    "Beide Felder speisen außerdem das Themen-Radar, aus dem die Impulse entstehen. Ein Thema taugt dafür nur, wenn die Presse darüber schreibt, ohne den Mandanten zu nennen.":
        "Both fields also feed the topic radar the openings are drawn from. A theme "
        "only works there if the press writes about it without naming the client.",
    "entscheiden, was gefunden wird": "decide what gets found",
    "stufen hoch, was schon gefunden wurde": "escalate what was already found",
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
    # --- Captain Comms: voice ------------------------------------------------
    # The tooltip names where the audio goes. The browser does the recognition,
    # which means Chrome sends it to Google and Safari to Apple — not something to
    # bury, given a spoken question here names mandates.
    "Frage sprechen": "Speak your question",
    "Frage sprechen — die Spracherkennung läuft im Browser, nicht in NewsPulse":
        "Speak your question — recognition runs in the browser, not in NewsPulse",
    "Antworten vorlesen": "Read answers aloud",
    "Hört zu… die Erkennung übernimmt der Browser, nicht NewsPulse.":
        "Listening… recognition is handled by the browser, not by NewsPulse.",
    "Kein Zugriff auf das Mikrofon. Im Browser für diese Seite erlauben.":
        "No microphone access. Allow it for this page in the browser.",
    "Nichts gehört. Nochmal antippen und sprechen.":
        "Heard nothing. Tap again and speak.",
    "Spracherkennung nicht verfügbar.": "Speech recognition unavailable.",
    # --- Client: Marktumfeld (the topic radar's own view) --------------------
    "Marktumfeld": "Market",
    # --- Client: the communications guide ------------------------------------
    "Guide": "Guide",
    "Kommunikations-Guide": "Communications guide",
    "Suchbegriffe sagen, worüber berichtet wird. Der Guide sagt, was dieser Mandant sagen will, was er nie sagt und in welchem Ton — und geht jedem erzeugten Text voran.":
        "Search terms say what is written about. The guide says what this client "
        "wants to say, what it never says and in what tone — and it precedes every "
        "generated text.",
    "Guide gespeichert.": "Guide saved.",
    "Vorschlag aus den Unterlagen — noch nicht gespeichert":
        "Proposal from the documents — not saved yet",
    "Übernehmen": "Apply",
    "Verwerfen": "Discard",
    "Quellen": "Sources",
    "Zeichen": "characters",
    "Vorschlag erzeugen": "Propose a guide",
    "Noch keine Unterlagen. Der Guide lässt sich auch einfach selbst schreiben.":
        "No documents yet. The guide can simply be written by hand.",
    "Hochladen": "Upload",
    "PDF, TXT oder Markdown · höchstens": "PDF, TXT or Markdown · at most",
    "Gespeichert wird der extrahierte Text, nicht die Datei.":
        "The extracted text is stored, not the file.",
    "Speichern": "Save",
    "Wirkt auf": "Feeds into",
    "Kurz halten: der Guide wird jedem erzeugten Text vorangestellt.":
        "Keep it short: the guide precedes every generated text.",
    "Positionierung: … · Kernbotschaften: … · Nie: …":
        "Positioning: … · Key messages: … · Never: …",
    "Keine Datei gewählt.": "No file selected.",
    # --- The strategy coach ---------------------------------------------------
    "Strategie-Coach": "Strategy coach",
    "Prüft den Guide gegen": "Checks the guide against",
    "Tage Berichterstattung": "days of coverage",
    "Jetzt prüfen": "Check now",
    "luecke": "gap",
    "konflikt": "conflict",
    "traegt": "holding",
    "Nichts gefunden, woran der Guide und die Berichterstattung auseinanderlaufen.":
        "Nothing found where the guide and the coverage diverge.",
    "Noch nicht geprüft. Ein Aufruf, keine Speicherung — der Bericht ist immer der aktuelle Stand.":
        "Not checked yet. One call, nothing stored — the report is always current.",
    "Beratend, nicht ausführend: der Coach ändert nichts am Guide.":
        "Advisory, not executing: the coach changes nothing in the guide.",
    "Kein Kommunikations-Guide hinterlegt.": "No communications guide set.",
    "Berichterstattung aus dem Themenfeld des Mandanten — Meldungen, die ihn nicht "
    "nennen. Aus diesem Material entstehen die Impulse, und daraus lässt sich "
    "ablesen, wer über das Thema schreibt.":
        "Coverage from the client's subject area — items that do not name them. "
        "This is the material the openings are drafted from, and it shows who "
        "writes about the subject.",
    "Letzte": "Last",
    "in": "in",
    "Das Themen-Radar hat in diesem Zeitraum nichts gefunden.":
        "The topic radar found nothing in this period.",
    "Die hinterlegten Themen sind womöglich zu eng gefasst — zwei bis drei "
    "geläufige Begriffe aus dem Feld treffen mehr als ein Dutzend Spezialbegriffe.":
        "The themes may be too narrow — two or three common terms from the field "
        "find more than a dozen specialist ones.",
    "Für diesen Mandanten ist kein Themen-Radar eingerichtet.":
        "No topic radar is set up for this client.",
    "Ohne hinterlegte Themen lässt sich nicht bestimmen, welche Marktmeldung ihn angeht.":
        "Without themes there is no way to tell which market coverage concerns them.",
    "Themen hinterlegen": "Add themes",
    "Wen ansprechen": "Who to approach",
    "Medien im Themenfeld": "Outlets on the subject",
    "Schreiben über das Thema, nicht über den Mandanten.":
        "They write about the subject, not about the client.",
    "Medien über den Mandanten": "Outlets on the client",
    "Journalistinnen und Journalisten": "Journalists",
    "Nur aus den Feeds, die einen Autor mitliefern — die meisten tun das nicht.":
        "Only from feeds that supply an author — most do not.",
    "Noch keine Daten — das Radar hat nichts gefunden.":
        "No data yet — the radar found nothing.",
    "Noch keine Berichterstattung über den Mandanten.":
        "No coverage of the client yet.",
    "Kein Feed in diesem Zeitraum hat einen Autor mitgeliefert.":
        "No feed supplied an author in this period.",
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
