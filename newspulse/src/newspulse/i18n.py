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
    "Kontakte": "Contacts",
    "Einstellungen": "Settings",
    "↻ Aktualisieren": "↻ Refresh",
    "Aktualisieren": "Refresh",
    "kein Themen-Radar": "no topic radar",
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
    "Erst-Import": "Initial import",
    "noch kein täglicher Lauf": "no daily sweep yet",
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
    "liegt noch keine Berichterstattung vor — der Lauf von heute Morgen hat geholt, was gestern Abend erschienen ist. Angezeigt wird deshalb":
        "there is no coverage yet: this morning's sweep collected what appeared "
        "last night. Showing instead",
    "Trotzdem heute ansehen": "Show today anyway",
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
    "Mandant ohne Impuls": "client with no opening",
    "Mandanten ohne Impuls": "clients with no opening",
    "Marktmeldung(en) gesichtet — keine davon gab einen Anlass her.":
        "market item(s) \u2014 none of them held up.",
    "Kein Themen-Radar — ohne hinterlegte Themen lässt sich nicht bestimmen, welche Marktmeldung ihn angeht.":
        "No topic radar \u2014 without themes there is no way to tell which market "
        "item concerns them.",
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
    "Branche als Suchfilter": "Industry as a search filter",
    "Branche vorschlagen": "Suggest an industry",
    "Branchenbegriffe werden vorgeschlagen und an der echten Suche gemessen.":
        "Industry terms are being proposed and measured against the real search.",
    "Meldungen in 90 Tagen": "items in 90 days",
    "kaum Presse — als Filter unbrauchbar": "barely any press \u2014 useless as a filter",
    "Der Begriff filtert Markt-Meldungen. Nur was die Presse auch schreibt, taugt dafür.":
        "The term filters market news. Only a word the press actually writes will do.",
    "Für diesen Mandanten ist keine Branche hinterlegt — ohne sie lässt sich nicht beurteilen, wer ein vergleichbares Unternehmen ist.":
        "No industry is set for this client \u2014 without one there is no way to "
        "judge which company is comparable.",
    "Branche setzen": "Set an industry",
    "Kein überwachtes Unternehmen aus diesem Feld.": "No monitored company from this field.",
    "Wettbewerber vorschlagen lassen": "Have competitors proposed",
    "Unternehmen aus anderen Branchen anzeigen": "companies from other industries",
    "Ein Vergleich über Branchengrenzen hinweg verfälscht den Anteil — nur wählen, wenn es wirklich derselbe Markt ist.":
        "A comparison across industries distorts the share \u2014 pick one only if "
        "it really is the same market.",
    "Trotzdem hinzufügen": "Add anyway",
    "ohne Branche": "no industry",
    "Andere Branche — selten sinnvoll": "Another industry \u2014 rarely useful",
    "Wettbewerber vorschlagen": "Suggest competitors",
    "Mandanten gesamt": "clients in total",
    "Maßstab für": "Yardstick for",
    "Werden überwacht, damit Anteile vergleichbar sind — nie an sie berichtet, nie im Digest.":
        "Monitored so shares are comparable \u2014 never reported to, never in the digest.",
    "keinem Mandanten": "no client",
    "Wird überwacht, aber keinem Mandanten zugeordnet — die Meldungen zählen nirgends.":
        "Monitored but linked to no client \u2014 its items count towards nothing.",
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
    # A map with nothing to plot used to render as an empty white box with a
    # legend across it, which reads as a broken page rather than as an answer.
    "Noch kein Wettbewerber hinterlegt.": "No competitor recorded yet.",
    "Diese Karte vergleicht, welche Medien über den Wettbewerb schreiben und über diesen Mandanten nicht — ohne Vergleichsgruppe gibt es nichts zu vergleichen.":
        "This map compares which outlets write about the competition and not "
        "about this client — with no peer group there is nothing to compare.",
    "Vergleichsgruppe festlegen": "Set the peer group",
    "Zu wenig Berichterstattung im Zeitraum.": "Too little coverage in the period.",
    "Tagen kam für Mandant und Wettbewerb zusammen zu wenig zusammen, um Medien gegeneinander zu stellen":
        "days there was too little coverage of client and competition combined to "
        "set outlets against each other",
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
    # --- From a position to a message ----------------------------------------
    # The "Empfehlungen" panel became a button on the impulse: the difference
    # between the two panels was never legible, and only one of them produced
    # something sendable.
    "Personalisierte Nachricht erzeugen": "Write a personalised message",
    "Empfänger:in": "Recipient",
    "ohne feste:n Empfänger:in": "no fixed recipient",
    "Schreibt daraus ein fertiges Anschreiben: mit Betreff, Anrede und einem ersten Satz, der an die letzten Meldungen dieser Person anknüpft. Danach frei bearbeitbar.":
        "Turns it into a finished letter: subject, salutation, and an opening line "
        "that picks up what this person last wrote. Yours to edit afterwards.",
    "Schreibt daraus ein fertiges Anschreiben an eine Fachredaktion Ihrer Wahl. Für dieses Themenfeld kennt das Tool noch keine Namen.":
        "Turns it into a finished letter for a trade desk of your choosing. The "
        "tool knows no names for this field yet.",
    "Personalisierte Nachrichten": "Personalised messages",
    "Gegengelesen von": "Second read by",
    "Zweitmodell rät ab": "Second model advises against",
    "gelesen von": "read by",
    "keine Einwände": "no objections",
    "Nicht gegengelesen — es ist kein Zweitmodell hinterlegt.":
        "Not cross-checked: no second model is configured.",
    "Bezieht sich auf": "Refers to",
    "Aus Schlagzeilen und Feed-Anrissen geschrieben, nicht aus den Artikeln selbst. Vor dem Versand gegen die Meldung prüfen.":
        "Written from headlines and feed snippets, not from the articles "
        "themselves. Check it against the item before sending.",
    "Treffer gesammelt. Jeder davon ist entweder älter als":
        "hits. Every one of them is either older than",
    "Tage oder Berichterstattung über den Mandanten selbst — und gegen die eigene Presse lässt sich nicht positionieren. Ein Impuls braucht ein Thema, über das auch ohne den Mandanten geschrieben wird.":
        "days or coverage of the client itself — and there is no positioning "
        "against your own press. An opening needs a theme the press writes about "
        "without them.",
    "Themen vorschlagen": "Suggest themes",
    "erreicht das Claude-Abo sein Limit, bricht der Lauf ab — Gespeichertes bleibt, aber für diesen Zeitraum kommt nichts Neues dazu. Zum Aktivieren NEWSPULSE_GEMINI_API_KEY setzen.":
        "if the Claude subscription hits its limit the run stops — what is stored "
        "stays, but nothing new is added for that period. Set "
        "NEWSPULSE_GEMINI_API_KEY to enable it.",
    "schreibt nie über den Mandanten": "never writes about the client",
    "Schreibt über das Themenfeld, aber noch nie über diesen Mandanten.":
        "Writes about the field, but has never written about this client.",
    "Wichtigkeit für diesen Mandanten, 0–10 — vom Analysemodell vergeben":
        "Importance for this client, 0–10 — assigned by the analysis model",
    "Über alle Mandanten und Themen-Radare zusammen — die Tagesansicht zeigt davon nur die Berichterstattung über Mandanten.":
        "Across every client and topic radar combined — the day view shows only "
        "the coverage of clients.",
    "Die Nachricht wird geschrieben — die Seite aktualisiert sich, sobald sie steht.":
        "Writing the message — the page will refresh itself once it is ready.",
    "Keine Nachricht erzeugt.": "No message written.",
    "Nachricht": "Message",
    "An": "To",
    "Betreff": "Subject",
    "Fachredaktion": "trade desk",
    "Warum diese:r": "Why them",
    "verwertbare Marktmeldung(en) für diesen Mandanten — daraus ist noch kein Impuls entstanden. Der Knopf oben fragt direkt.":
        "usable market item(s) for this client — no opening has been made of them "
        "yet. The button above asks directly.",
    "Kein verwertbares Marktmaterial.": "No usable market material.",
    "Treffer gesammelt, aber keiner davon liegt in den letzten":
        "hits, but none of them falls within the last",
    "Tagen und handelt von etwas anderem als vom Mandanten selbst. Ein Impuls braucht ein Thema, über das auch ohne ihn geschrieben wird.":
        "days while being about something other than the client itself. An opening "
        "needs a theme the press writes about without them.",
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
    "Themen-Radar": "Topic radar",
    "Prüft, welche gespeicherten Radar-Treffer gar nicht zum Themenfeld ihres Mandanten gehören. Ändert nichts, zeigt nur.":
        "Checks which stored radar hits do not belong to their client's field at "
        "all. Changes nothing, only shows.",
    "Radar-Treffer prüfen": "Check radar hits",
    "Alle gespeicherten Radar-Treffer tragen ein Thema ihres Mandanten.":
        "Every stored radar hit carries one of its client's themes.",
    "Treffer tragen kein Thema ihres Mandanten. Der Artikel bleibt im Archiv — nur die Zuordnung wird gelöst.":
        "hits carry none of their client's themes. The article stays in the "
        "archive; only the link is cut.",
    "Meldung": "Item",
    "… und weitere": "… and a further",
    "Diese": "Remove these",
    "Zuordnungen entfernen": "links",
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
    "Im Tagesfeed ausblenden": "Hide in the daily feed",
    "Nur die Ansicht: die Artikel bleiben im Archiv, in den Zahlen und im Export.":
        "The view only \u2014 the articles stay in the archive, in the counts and in "
        "the export.",
    "stummgeschaltet": "muted",
    "anzeigen": "show",
    "stummgeschaltete wieder ausblenden": "hide muted again",
    "Passende Themen vorschlagen": "Suggest themes that fit",
    "Schlägt Themen des Marktumfelds vor und prüft jedes an der echten Suche, bevor es angeboten wird.":
        "Proposes themes from the client's market and tests each against the real "
        "search before offering it.",
    "Themen werden vorgeschlagen und gegen die echte Suche geprüft — das dauert eine Minute.":
        "Themes are being proposed and tested against the real search \u2014 this "
        "takes a minute.",
    "Gemessen an der echten Suche: so viele Marktmeldungen brächte jedes Thema in 90 Tagen. Ein Klick übernimmt es und sucht sofort.":
        "Measured against the real search: that is how many market items each theme "
        "would bring in over 90 days. One click adopts it and searches straight away.",
    "Wettbewerber vorschlagen": "Suggest competitors",
    "Wettbewerber werden vorgeschlagen — das dauert einen Moment.":
        "Competitors are being proposed \u2014 this takes a moment.",
    "Vorschlag fehlgeschlagen": "Proposal failed",
    "Verknüpfung lösen": "Unlink",
    "Wohin damit": "Where to send it",
    "Zuletzt geprüft": "Last checked",
    "Vergleichsgruppe": "Comparison set",
    "Firmenname": "Company name",
    "Wird als überwachtes Unternehmen angelegt und diesem Mandanten zugeordnet — nur hier, nicht bei den anderen.":
        "Created as a monitored company and linked to this mandate only \u2014 not to the others.",
    "bereits überwachte Unternehmen aus diesem Feld": "already-monitored companies from this field",
    "Aus dem Bestand wählen": "Pick from the monitored companies",
    "Kontakt hinterlegen": "add a contact",
    "Kontaktbuch": "Contact book",
    "Journalistinnen und Journalisten, die Sie kennen. Das Tool schlägt aus der Berichterstattung vor, wen man ansprechen kann — womit, steht hier, und nur was Sie hier eintragen.":
        "The journalists you know. The tool proposes who to approach from what the "
        "press published; how to reach them lives here, and only what you put in it.",
    "Noch kein Eintrag für": "No entry yet for",
    "unten ausfüllen und speichern.": "fill in below and save.",
    "Eintrag bearbeiten": "Edit entry",
    "Neuer Kontakt": "New contact",
    "Medium": "Outlet",
    "E-Mail": "Email",
    "Telefon": "Phone",
    "Themengebiet": "Beat",
    "Notizen": "Notes",
    "Löschen": "Delete",
    "Name, Medium oder Themengebiet": "Name, outlet or beat",
    "Noch keine Kontakte. Unter jedem Impuls stehen die Journalisten, die zum Thema geschrieben haben — ein Klick auf einen Namen legt hier einen Eintrag an.":
        "No contacts yet. Under every opening are the journalists who wrote about "
        "the subject \u2014 clicking a name starts an entry here.",
    "Noch keine Empfängerliste: kein Medium berichtet regelmäßig genug über dieses Themenfeld, als dass eine Empfehlung belastbar wäre.":
        "No recipients yet: no publication covers this field regularly enough for "
        "a recommendation to hold.",
    "Coverage Map ansehen": "See the coverage map",
    "noch kein Kontakt": "no contact yet",
    "Aus den Meldungen der letzten": "From the items of the last",
    "Tage. Namen stehen nur dort, wo der Feed einen Autor mitgeliefert hat — Kontaktdaten führt das Tool nicht.":
        "days. Names appear only where the feed supplied an author \u2014 the tool "
        "holds no contact details.",
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
    "Impuls und Empfehlung werden erzeugt": "drafting an opening and a recommendation",
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
    "Profil": "Profile",
    "Wettbewerb": "Competition",
    "Mit KI ausfüllen": "Fill with AI",
    "Liest das offene Netz und schlägt Werte vor — mit Quelle, und ohne etwas zu speichern. Was hier steht, entscheiden Sie.":
        "Reads the open web and proposes values, with the source, storing nothing. "
        "What ends up here is your call.",
    "Recherche läuft — die Seite aktualisiert sich, sobald die Vorschläge stehen.":
        "Researching — the page will refresh itself once the proposals are ready.",
    "Keine Recherche.": "No research.",
    "Vorschläge aus dem Netz": "proposals from the web",
    "Ausgewählte übernehmen": "Accept selected",
    "Verwerfen": "Discard",
    "Profil speichern": "Save profile",
    "Was Sie hier ändern, gilt als Ihre Angabe und wird von der KI nicht mehr überschrieben.":
        "What you change here counts as yours and the AI will not overwrite it.",
    # The lead used to say "in vierzehn Zeilen". The kick-off feeds three fields
    # the web cannot answer, so the profile is no longer fourteen lines and the
    # sentence no longer counts them: a number in prose beside the live figure
    # next to it is one of them being wrong.
    "Felder gefüllt": "fields filled",
    "Von": "By",
    "Quelle": "Source",
    "Unternehmen": "Company",
    "Anteil": "Share",
    "Noch keine Vergleichsgruppe.": "No peer group yet.",
    "Ein Anteil ohne Vergleich ist immer 100 % und sagt nichts. Unten lässt sich ein Wettbewerber hinzufügen.":
        "A share with nothing to compare against is always 100% and says nothing. "
        "Add a competitor below.",
    "Woran der Wettbewerb gerade gemessen wird": "What the competition is being written about",
    "Im Zeitraum nichts gefunden. Ein stiller Wettbewerber ist selbst eine Beobachtung.":
        "Nothing in the period. A quiet competitor is itself an observation.",
    "Medien schreiben über den Wettbewerb und nie über": "outlets write about the competition and never about",
    "Ganze Coverage Map": "Full coverage map",
    "Noch keine: dafür braucht es Medien, die mehrfach über den Wettbewerb geschrieben haben.":
        "None yet: that needs outlets which have written about the competition "
        "more than once.",
    # The lead is split around the mandate's name and the window, so the two
    # fragments either side of it need entries of their own.
    "Wie": "How",
    "gegen sein Feld steht — Anteil, Themen und die Medien, die über die anderen schreiben und nicht über ihn. Alle Zahlen aus den letzten":
        "stands against its field: share, subjects, and the outlets that write "
        "about the others and not about them. Every figure from the last",
    "Tagen": "days",
    "Fragen": "Ask",
    "Frage stellen…": "Ask a question…",
    # --- Captain Comms: voice ------------------------------------------------
    # The tooltip names where the audio goes. The browser does the recognition,
    # which means Chrome sends it to Google and Safari to Apple — not something to
    # bury, given a spoken question here names mandates.
    "Frage sprechen": "Speak your question",
    "Frage sprechen — die Spracherkennung läuft im Browser, nicht in RauteOS":
        "Speak your question — recognition runs in the browser, not in RauteOS",
    "Antworten vorlesen": "Read answers aloud",
    "Hört zu… die Erkennung übernimmt der Browser, nicht RauteOS.":
        "Listening… recognition is handled by the browser, not by RauteOS.",
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
    # --- Client: the kick-off questionnaire (ONB-01) --------------------------
    # The question texts are chrome, not data: they live in ``onboarding.py`` as a
    # module constant, the same way ``profile.FIELDS`` does, and a consultant
    # reading the tool in English should be asking in English. What is *not*
    # translated is the answer, ever — it is the client's own sentence.
    "Kickoff": "Kickoff",
    "Zwanzig Fragen, die keine Recherche beantworten kann — welcher Satz nie "
    "gedruckt werden darf, wer zitiert werden will, welche Behauptung des "
    "Wettbewerbs widerlegbar ist. Jede Antwort sagt, was sie speist.":
        "Twenty questions no research can answer: which sentence must never be "
        "printed, who wants to be quoted, which competitor claim can be "
        "disproved. Every answer says what it feeds.",
    "Aus diesen Antworten entstehen Profilfelder, No-Gos und die "
    "Vergleichsgruppe. Übernommen wird davon nichts von selbst — was hier steht, "
    "ist die Antwort, nicht die Regel.":
        "These answers become profile fields, no-gos and the comparison set. "
        "None of it is adopted by itself: what stands here is the answer, not "
        "the rule.",
    # The five sections, and the short form each gets in the progress rail.
    "Was das Unternehmen ist": "What the company is",
    "Vier Fragen, die jeder Text braucht. Antworten hier ersetzen, was die "
    "Recherche geraten hat.":
        "Four questions every text needs. Answers here replace what the research "
        "guessed.",
    "Was gesagt werden darf, und was nie": "What may be said, and what never",
    "Sagen und schweigen": "Saying and staying silent",
    "Der Teil, den kein Modell erraten kann. Aus diesen Antworten entsteht der "
    "Guide, gegen den später jeder Text geprüft wird.":
        "The part no model can guess. These answers become the guide every later "
        "text is checked against.",
    "Was erreicht werden soll": "What this is meant to achieve",
    "Ziele": "Goals",
    "Ohne Ziel ist jede Berichterstattung gleich viel wert, und das ist sie nie.":
        "Without a goal every piece of coverage is worth the same, and it never is.",
    "Medien und Beziehungen": "Outlets and relationships",
    "Wo Sie vorkommen müssen, und wen Sie dort schon kennen.":
        "Where you have to appear, and who you already know there.",
    "Zusammenarbeit": "Working together",
    "Wie wir arbeiten, damit im Ernstfall niemand erst suchen muss.":
        "How we work, so nobody has to go looking when it matters.",
    # The twenty questions, each with the line under it.
    "Was verkaufen Sie, in einem Satz, ohne Fachbegriffe?":
        "What do you sell, in one sentence, without jargon?",
    "Wenn der Satz eine Erklärung braucht, ist es noch nicht der Satz.":
        "If the sentence needs an explanation, it is not yet the sentence.",
    "Wer spricht für das Unternehmen, und wozu?":
        "Who speaks for the company, and on what?",
    "Name, Rolle, und für welche Themen diese Person zitierbar ist.":
        "Name, role, and which subjects this person can be quoted on.",
    "Wen halten Sie für Ihren wichtigsten Wettbewerber, und warum?":
        "Who do you consider your most important competitor, and why?",
    "Wichtig für den Share of Voice. Die Vergleichsgruppe ist sonst geraten.":
        "It decides the share of voice. Otherwise the comparison set is guesswork.",
    "Wer trifft die Kaufentscheidung, und wen müssen wir dafür erreichen?":
        "Who makes the buying decision, and who must we reach to get it?",
    "Nicht die Branche, sondern die Person, die am Ende unterschreibt.":
        "Not the industry, but the person who ends up signing.",
    "Welchen Satz sollen wir über Sie nie schreiben?":
        "Which sentence should we never write about you?",
    "Wörtlich, so wie er nicht dastehen soll.":
        "Verbatim, exactly as it must not appear.",
    "Gibt es ein Thema, zu dem Sie grundsätzlich schweigen?":
        "Is there a subject you stay silent on as a matter of policy?",
    "Laufende Verfahren, Preise, Kundennamen, eine Personalie.":
        "Pending proceedings, prices, client names, a personnel matter.",
    "Was behaupten Ihre Wettbewerber, das schlicht nicht stimmt?":
        "What do your competitors claim that is simply untrue?",
    "Die ergiebigste Frage im Fragebogen. Hier liegen die Thesen.":
        "The most productive question in the set. This is where the arguments are.",
    "Welche Wörter benutzen Sie über sich selbst, und welche nie?":
        "Which words do you use about yourselves, and which never?",
    "Heißt es Kunden oder Partner, Mitarbeitende oder Team? Ein falsches Wort "
    "fällt sofort auf.":
        "Is it customers or partners, staff or team? A wrong word is noticed "
        "immediately.",
    "Welche Zahlen dürfen genannt werden, und welche nie?":
        "Which figures may be named, and which never?",
    "Umsatz, Kundenzahl, Finanzierung. Was nicht raus darf, muss hier stehen.":
        "Revenue, customer count, funding. Whatever must not go out has to be "
        "written here.",
    "Wer gibt einen Text frei, bevor er rausgeht?":
        "Who signs off on a text before it goes out?",
    "Name und Rolle. Und ob das auch für ein einzelnes Zitat gilt.":
        "Name and role. And whether that also applies to a single quote.",
    "Was soll in zwölf Monaten über Sie in der Presse stehen, das heute nicht dasteht?":
        "In twelve months, what should the press say about you that it does not "
        "say today?",
    "Ein Satz, den Sie in einem Artikel lesen wollen.":
        "One sentence you want to read in an article.",
    "Was steht in den nächsten Monaten an, worüber man berichten könnte?":
        "What is coming up in the next few months that could be reported on?",
    "Produkt, Zahlen, Personalie, Standort, Studie — mit ungefährem Datum.":
        "Product, figures, an appointment, a site, a study, with a rough date.",
    "Welche Entscheidung soll die Berichterstattung bei Ihren Kunden auslösen?":
        "Which decision should the coverage trigger in your customers?",
    "PR ohne beabsichtigte Wirkung ist Dekoration.":
        "PR with no intended effect is decoration.",
    "Woran würden Sie in einem Jahr sehen, dass sich das gelohnt hat?":
        "A year from now, what would show you this was worth it?",
    "Eine Zahl oder ein konkretes Ereignis, kein Gefühl.":
        "A number or a concrete event, not a feeling.",
    "In welchem Medium müssen Sie vorkommen, damit Ihre Kunden es sehen?":
        "Which outlet must you appear in for your customers to see it?",
    "Ein Fachtitel zählt hier mehr als die FAZ, wenn dort eingekauft wird.":
        "A trade title counts for more than the FAZ here, if that is where the "
        "buying happens.",
    "Zu welchen Journalistinnen und Journalisten haben Sie schon einen Draht?":
        "Which journalists do you already have a line to?",
    "Name und Titel reichen. Ein bestehender Kontakt ist mehr wert als eine "
    "kalte Liste.":
        "A name and a title are enough. One existing contact is worth more than a "
        "cold list.",
    "Gab es eine Berichterstattung, die schiefging?":
        "Has any coverage gone wrong?",
    "Was passiert ist, und was daraus gilt. Das erklärt eine Empfindlichkeit "
    "besser als jede Regel.":
        "What happened, and what holds because of it. That explains a sensitivity "
        "better than any rule.",
    "Wofür stehen Sie für ein Interview zur Verfügung, und wofür nie?":
        "What will you give an interview on, and what never?",
    "Trennt die gute Anfrage von der, die Ärger macht.":
        "Separates the good request from the one that causes trouble.",
    "Wer ist bei Ihnen unser erster Ansprechpartner, und wie schnell erreichen wir ihn?":
        "Who is our first point of contact with you, and how fast can we reach them?",
    "Auch: wer entscheidet, wenn diese Person im Urlaub ist.":
        "Also: who decides while that person is on leave.",
    "Wen rufen wir an, wenn abends um sieben etwas passiert?":
        "Who do we call when something happens at seven in the evening?",
    "Name und Nummer. Diese Frage wird sonst genau einmal zu spät gestellt.":
        "A name and a number. Otherwise this question gets asked exactly once, "
        "too late.",
    # What an answer feeds: the verb, the targets and the named slots inside them.
    "Füllt": "Fills",
    "Wird": "Becomes a",
    "No-Go": "No-go",
    "Themenfelder": "Subject areas",
    "Geschäftsfeld": "Line of business",
    "Kernbotschaft": "Key message",
    "Sprecher": "Spokespeople",
    "Zielgruppe": "Target audience",
    "Tonalität": "Tone",
    "Freigabe": "Sign-off",
    "Zielbild": "Objective",
    "Zielmedien": "Target outlets",
    "Pressekontakt": "Press contact",
    "Krisenkontakt": "Crisis contact",
    "wird in jedem Anschreiben verwendet": "used in every letter",
    "jeder Text wird dagegen geprüft": "every text is checked against it",
    "Material für Impulse": "material for the openings",
    # The three states of a question, and the controls that move between them.
    "gespeichert": "saved",
    "übergangen": "passed over",
    "Übergehen": "Pass over",
    "Doch beantworten": "Answer it after all",
    # "Hinzufügen" — the list question's button — is already in the table above.
    "Antwort löschen": "Delete answer",
    "Gefragt, aber keine Antwort — anders als noch nicht gefragt":
        "Asked, but no answer — which is not the same as not yet asked",
    "Eintrag entfernen": "Remove entry",
    # The placeholders from the locked mock. Each says the shape of the answer,
    # which is why they are not a second copy of the help line above the field.
    "Weitere Person, Rolle, Themen": "Another person, role, subjects",
    "Unternehmen, und in einem Halbsatz warum":
        "The company, and in half a sentence why",
    "Thema, und ob Schweigen oder eine Sprachregelung gilt":
        "The subject, and whether it is silence or an agreed wording",
    "Behauptung, und womit Sie dagegenhalten können":
        "The claim, and what you can hold against it",
    "Weiterer Name, Titel": "Another name and title",
    "und": "and",
    # The progress rail. The figure is stated in words as well as drawn as a bar:
    # a bar says "some" where a consultant needs "eight".
    "beantwortet oder übergangen": "answered or passed over",
    "zuletzt": "last",
    "Frage ist noch offen": "question is still open",
    "Fragen sind noch offen": "questions are still open",
    "Eine offene Frage ist ein Befund, keine Lücke im Formular.":
        "An open question is a finding, not a gap in a form.",
    "Keine Frage mehr offen.": "No question left open.",
    "Eine Antwort wird beim Verlassen des Feldes gespeichert, ein Listeneintrag "
    "beim Hinzufügen. Der Fragebogen muss nicht in einem Zug fertig werden, und "
    "was fehlt, bleibt sichtbar offen statt geraten.":
        "An answer is stored as you leave the field, a list entry when you add it. "
        "The questionnaire does not have to be finished in one sitting, and what "
        "is missing stays visibly open rather than guessed.",
    # --- Client: what the kick-off answers become (ONB-02) --------------------
    # The three profile fields the questionnaire feeds and the web cannot. Their
    # labels are already above as target slots; only the hints are new here.
    "Wer zitiert werden darf, und wozu.": "Who may be quoted, and on what.",
    "Die Titel, in denen dieses Unternehmen vorkommen muss.":
        "The titles this company has to appear in.",
    "Wer abends erreichbar ist, mit Nummer.":
        "Who can be reached in the evening, with a number.",
    # The profile's proposal list, which now carries two kinds of proposal and
    # says on every row which one it is.
    "Vorschläge für das Profil": "Proposals for the profile",
    "Was dieses Unternehmen ist, Zeile für Zeile. Jede Zeile speist die Impulse, die Anschreiben und Captain Comms — und jede sagt, woher sie stammt.":
        "What this company is, line by line. Every line feeds the openings, the "
        "letters and Captain Comms, and every line says where it came from.",
    "Kickoff-Fragebogen": "Kick-off questionnaire",
    "Angabe des Mandanten": "The client's own statement",
    "ersetzt die bisherige Angabe, die sichtbar bleibt":
        "replaces the current value, which stays visible",
    # DEC-2 option A: the answer wins, and the disagreement stays legible.
    "Vorher": "Previously",
    "Alte Angabe verwerfen": "Discard the old value",
    # The completeness line, on the profile and on the portfolio.
    "Fragen aus dem Kickoff beantwortet oder übergangen":
        "kick-off questions answered or passed over",
    "Kein Fragebogen beantwortet — für dieses Mandat gibt es noch kein Fundament.":
        "No questionnaire answered: this mandate has no foundation yet.",
    "Zum Fragebogen": "To the questionnaire",
    "Fundament": "Foundation",
    "kein Fragebogen": "no questionnaire",
    # The guide drafted from the answers.
    "Entwurf aus dem Kickoff": "Draft from the kick-off",
    "Entwurf aus dem Kickoff — noch nicht gespeichert":
        "Draft from the kick-off, not saved yet",
    "Ohne Antwort im Kickoff, deshalb nicht im Entwurf":
        "Unanswered in the kick-off, and therefore not in the draft",
    "Zu lang für den Guide, deshalb nicht im Entwurf":
        "Too long for the guide, and therefore not in the draft",
    "Antworten aus dem Fragebogen. No-Gos übernimmt der Entwurf wörtlich; "
    "unbeantwortete Abschnitte bleiben leer.":
        "answers from the questionnaire. The draft takes no-gos verbatim; "
        "unanswered sections stay empty.",
    "Der Kickoff-Fragebogen ist noch unbeantwortet — daraus lässt sich kein "
    "Entwurf machen.":
        "The kick-off questionnaire is still unanswered, so there is nothing to "
        "draft from.",
    # The competitors the client named, offered for the comparison set.
    "Im Kickoff genannt": "Named in the kick-off",
    "In die Vergleichsgruppe": "Add to the comparison set",
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
