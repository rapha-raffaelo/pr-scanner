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
    "Von": "By",
    "Bis": "To",
    "Quelle": "Source",
    "Alle Quellen": "All sources",
    "Keine Artikel": "No articles",
    "letzte 30 Tage": "last 30 days",
    "Unternehmen": "Company",
    "Rolle": "Role",
    "Alerts": "Alerts",
    "Anteil": "Share",
    "Wettbewerber": "Competitors",
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
    "Wettbewerb": "Competition",
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
    # --- The ledger: the release, and what came back --------------------------
    # The five states a letter can be in. "Verschickt" rather than "Freigegeben"
    # because the consultant did both, and the record is about the letter having
    # left the house. "Ohne Reaktion" is not one of them: it is "verschickt" plus
    # fourteen days, derived rather than stored, and it sits beside the badge
    # instead of replacing it.
    "Entwurf": "Draft",
    "Verschickt": "Sent",
    "Antwort": "Reply",
    "Absage": "Declined",
    "Veröffentlicht": "Published",
    "Ohne Reaktion": "No response",
    "Freigegeben am": "Released on",
    "Ergebnis am": "Outcome on",
    "seit": "for",
    "Tagen still": "days now",
    "Geschrieben": "Written",
    "noch nicht freigegeben": "not released yet",
    "Freigegeben und verschickt": "Released and sent",
    # One string, not fragments: "von X freigegeben" glued from three t() calls
    # would produce English words in German word order ("by X released and sent").
    "von %(who)s freigegeben und verschickt": "released and sent by %(who)s",
    "Ergebnis eingetragen": "Outcome recorded",
    # The other half of the same line, and the reason there are two: since the
    # mailbox is read, an outcome can be the sync's own entry, and "Ergebnis
    # eingetragen" would then name an act nobody performed.
    "Antwort im Postfach gelesen": "Reply read from the mailbox",
    "Was kam zurück": "What came back",
    "Bitte wählen": "Please choose",
    "In einem Satz: was kam zurück.": "In one sentence: what came back.",
    "Eintragen": "Record",
    "Nichts geht von hier aus raus. Der Knopf hält fest, dass Sie das Anschreiben gelesen, freigegeben und selbst verschickt haben.":
        "Nothing leaves from here. The button records that you read the letter, "
        "released it and sent it yourself.",
    # --- The letter goes out through Gmail (DEC-4 option C) -------------------
    # The strip above the impulses, and the warning that belongs with it. The
    # warning is not boilerplate: with the send scope granted, the product's own
    # founding sentence no longer holds, and the screen where that is exercised
    # is where it has to be readable.
    "Postfach verbunden": "Mailbox connected",
    # One string, not fragments — same reason as the release trail below: "lesen
    # und" plus "senden" is two English words that only happen to fall in this
    # order, and "senden" alone is generic enough to be reused later for a
    # different verb and pick up the wrong translation.
    "lesen und senden": "read and send",
    #: What the same strip says on a connection that was never granted the send
    #: permission — every mailbox connected before DEC-4's send path.
    "nur lesen": "read only",
    "Diese Variante ändert die Grundannahme.":
        "This variant changes the founding assumption.",
    "Bisher gilt: keine Engine verschickt etwas an einen Journalisten. Mit Sendrecht verlässt ein Text das Haus, weil in RauteOS ein Knopf gedrückt wurde, nicht weil jemand in seinem eigenen Postfach auf Senden geht. Der Unterschied ist klein am Bildschirm und groß, wenn eine Nachricht falsch war.":
        "Until now the rule was: no engine sends anything to a journalist. With "
        "the send permission, a text leaves the building because a button was "
        "pressed in RauteOS, not because somebody hit send in their own mailbox. "
        "The difference is small on screen and large when a message was wrong.",
    "Freigeben und senden": "Release and send",
    "Ja, senden": "Yes, send",
    # ("Abbrechen", the confirmation's second control, is already translated
    # further down with the form that first needed it.)
    "Diese Nachricht geht jetzt an": "This message now goes to",
    "Sie ist danach nicht mehr zurückzuholen. Gegengelesen wurde sie von einem zweiten Modell, nicht von einem Menschen.":
        "It cannot be recalled afterwards. It was checked by a second model, not "
        "by a human.",
    "Zwei Klicks statt einem, weil der erste in einer vollen Woche schnell passiert.":
        "Two clicks instead of one, because in a busy week the first one happens "
        "quickly.",
    "Von RauteOS gesendet am": "Sent by RauteOS on",
    "Verlauf in Gmail öffnen": "Open the thread in Gmail",
    # The chip on the answer box, and it says the mock's sentence rather than the
    # timeline's: on the card the point is *which* conversation this came out of
    # — the one this letter opened — because that is what makes it this letter's
    # answer rather than a mail that mentions the same subject.
    "Antwort im selben Verlauf": "Reply in the same thread",
    "Nicht verschickt.": "Not sent.",
    "Freigegeben und von RauteOS über Gmail verschickt":
        "Released, and sent by RauteOS through Gmail",
    # One string, not fragments — same reason as the release trail above.
    "von %(who)s freigegeben, von RauteOS über Gmail verschickt":
        "released by %(who)s, sent by RauteOS through Gmail",
    # Why the send is not on offer. Three reasons, because they need three
    # different answers, and none of them is ever "we guessed an address".
    "Kein Name zu diesem Anschreiben — nur die Redaktion.":
        "No name on this letter, only the desk.",
    "Nicht im Kontaktbuch. RauteOS leitet keine Adresse aus Name oder Medium ab.":
        "Not in the contact book. RauteOS derives no address from a name or an "
        "outlet.",
    "Im Kontaktbuch, aber ohne E-Mail-Adresse.":
        "In the contact book, but with no email address.",
    # The fourth reason, and the only one that is not about the recipient: the
    # mailbox itself was connected without the permission to compose and send.
    "Dieses Postfach ist nur zum Lesen verbunden.":
        "This mailbox is connected for reading only.",
    "Postfach neu verbinden": "Reconnect the mailbox",
    # ("Kontakt hinterlegen" is already translated further down, with the pitch
    # list that first needed it.)
    # --- The relationship file: one journalist, across all mandates -----------
    # The contact book's right-hand pane (DEC-2). "Anschreiben" is the tally of
    # letters, not the verb, so it counts things: "Letters".
    "Verlauf": "History",
    "Anschreiben": "Letters",
    "Antworten": "Replies",
    "ohne Reaktion": "no response",
    "zuletzt angeschrieben heute": "last written to today",
    "zuletzt angeschrieben gestern": "last written to yesterday",
    # One string rather than "vor" + n + "Tagen": glued fragments come out in
    # German word order once translated. Same reason as the release trail above.
    "zuletzt angeschrieben vor %(days)s Tagen": "last written to %(days)s days ago",
    "Anschreiben verschickt": "Letter sent",
    "freigegeben von Mensch": "released by a human",
    "freigegeben von %(who)s": "released by %(who)s",
    # Where a timeline line came from. The set matters more than any one of them:
    # a sentence somebody typed, a timestamp the machine took when a button was
    # pressed, and a journalist's own words read out of the mailbox are three
    # different kinds of claim, and the page has to say which it is making.
    "von Hand eingetragen": "typed by hand",
    "bei der Freigabe festgehalten": "recorded at release",
    "aus dem Postfach gelesen": "read from the mailbox",
    # One string rather than "Antwort von" + name: glued fragments come out in
    # German word order once translated.
    "Antwort von %(who)s": "Reply from %(who)s",
    # A file long enough to be shortened says so, because the four tallies above
    # it are counted off the rows that are actually shown.
    "Es werden die letzten %(n)s Anschreiben gezeigt; ältere stehen nicht auf dieser Seite.":
        "Showing the most recent %(n)s letters; older ones are not on this page.",
    "Noch kein Anschreiben freigegeben. Sobald eines an diesen Kontakt rausgeht, steht es hier.":
        "No letter released yet. The moment one goes out to this contact, it "
        "shows up here.",
    # Since the mailbox is connected the old note ("RauteOS sends no mail and
    # reads no mailbox") is simply false. What is still true, and worth saying on
    # the page that holds somebody else's words, is how narrow the read is.
    "Der Verlauf entsteht aus drei Quellen, und jede Zeile sagt, aus welcher: aus der Freigabe eines Anschreibens, aus dem, was Sie selbst festhalten, und aus den Antworten der Journalistinnen und Journalisten. Aus dem Postfach liest RauteOS dabei nur die Verläufe, die es selbst begonnen hat — alles andere wird nicht abgefragt.":
        "This history comes from three sources, and every line says which: the "
        "release of a letter, what you record yourself, and the journalists' own "
        "replies. Of the mailbox, RauteOS reads only the conversations it started "
        "itself; nothing else is ever requested.",
    # The mark on a pitch target that was already written to about this very
    # angle. Not a block — the consultant may have a reason — but the date, so
    # the second approach is a decision rather than an accident.
    "schon angeschrieben am": "already written to on",
    "Zu diesem Impuls ging an diese Adresse bereits ein freigegebenes Anschreiben raus. Ein zweites zum selben Thema ist das, was eine Beziehung kostet.":
        "A released letter about this very opening already went to this "
        "recipient. A second one on the same subject is what costs you the "
        "relationship.",
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
    "Fehler": "Error",
    "Clients": "Clients",
    "+ Client hinzufügen": "+ Add client",
    "Name": "Name",
    "Aliasse": "Aliases",
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
    "jetzt ausfüllen und speichern.": "fill it in and save.",
    "Eintrag bearbeiten": "Edit entry",
    "Neuer Kontakt": "New contact",
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
    "Mit KI ausfüllen": "Fill with AI",
    "Liest das offene Netz und schlägt Werte vor — mit Quelle, und ohne etwas zu speichern. Was hier steht, entscheiden Sie.":
        "Reads the open web and proposes values, with the source, storing nothing. "
        "What ends up here is your call.",
    "Recherche läuft — die Seite aktualisiert sich, sobald die Vorschläge stehen.":
        "Researching — the page will refresh itself once the proposals are ready.",
    "Keine Recherche.": "No research.",
    "Vorschläge aus dem Netz": "proposals from the web",
    "Feld(er) von Hand gefüllt — das Netz sagt etwas anderes":
        "field(s) filled in by hand — the web says otherwise",
    # --- The review pile: what changed, and who decided it --------------------
    "Was Sie übernehmen, wird als Ihre Angabe gespeichert — mit Quelle, aber unter Ihrem Namen, weil Sie es entschieden haben. Was Sie verwerfen, wird beim nächsten Abgleich nicht erneut vorgeschlagen.":
        "What you accept is stored as your entry, with its source but under your "
        "name, because you are the one who decided it. What you discard is not "
        "proposed again at the next check.",
    "Regel: Eine Angabe von Hand wird nie überschrieben. Das Netz widerspricht hier nur — ändern können Sie den Wert selbst, unten im Feld.":
        "Rule: an entry you made by hand is never overwritten. The web only "
        "contradicts it here; only you can change the value, in the field below.",
    "Verwerfen heißt: Dieser Widerspruch wird beim nächsten Abgleich nicht erneut gemeldet.":
        "Discarding means this contradiction is not reported again at the next "
        "check.",
    "bisher nicht gefüllt": "previously empty",
    "Ihre Angabe": "Your entry",
    "Das Netz sagt": "The web says",
    # "Übernehmen" itself is further down, where the guide page's adopt button
    # put it. One German string, one English word: two entries for it would
    # silently make the later one win on both pages.
    "Alle übernehmen": "Apply all",
    "Alle verwerfen": "Discard all",
    "Verwerfen": "Discard",
    # The check stamp, shared by the profile page and the portfolio. "Never
    # checked" is a state of its own and is said out loud: a blank reads as fine.
    "Noch nie geprüft": "Never checked",
    "Heute geprüft": "Checked today",
    "Zuletzt geprüft vor %(days)s Tagen": "Last checked %(days)s days ago",
    "Profil speichern": "Save profile",
    "Was Sie hier ändern, gilt als Ihre Angabe und wird von der KI nicht mehr überschrieben.":
        "What you change here counts as yours and the AI will not overwrite it.",
    "Was dieses Unternehmen ist, in vierzehn Zeilen. Jede Zeile speist die Impulse, die Anschreiben und Captain Comms — und jede sagt, woher sie stammt.":
        "What this company is, in fourteen lines. Every line feeds the openings, "
        "the letters and Captain Comms, and every line says where it came from.",
    "Felder gefüllt": "fields filled",
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
    "gegen sein Feld steht — Anteil, Themen und die Medien, die über die anderen schreiben und nicht über ihn. Alle Zahlen aus den letzten":
        "stands against its field: share, subjects, and the outlets that write "
        "about the others and not about them. Every figure from the last",
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
    "Quellen": "Sources",
    "Zeichen": "characters",
    "Vorschlag erzeugen": "Propose a guide",
    "Noch keine Unterlagen. Der Guide lässt sich auch einfach selbst schreiben.":
        "No documents yet. The guide can simply be written by hand.",
    "Hochladen": "Upload",
    "PDF, TXT oder Markdown · höchstens": "PDF, TXT or Markdown · at most",
    "Gespeichert wird der extrahierte Text, nicht die Datei.":
        "The extracted text is stored, not the file.",
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
    # --- The mailbox connection (OUT-03) -------------------------------------
    # "Postfach" rather than "Gmail": the panel is about the mailbox the letters
    # go out through, and the provider is an implementation detail of it.
    "Postfach": "Mailbox",
    "Postfach verbinden": "Connect mailbox",
    "Postfach trennen": "Disconnect mailbox",
    "Verbunden als": "Connected as",
    "seit": "since",
    "Kein Postfach verbunden.": "No mailbox connected.",
    "Nachrichten lesen": "Read messages",
    # What DEC-4's send path actually needs: gmail.compose, because every
    # drafts.* call refuses gmail.send. The next three are grants this tool never
    # asks for — the send scope a connection made before that change still
    # carries, and two wider ones a Workspace account may already have — and the
    # panel renders what was granted rather than what was requested.
    "Nachrichten verfassen und senden": "Compose and send messages",
    "Nachrichten senden": "Send messages",
    "Nachrichten verwalten": "Manage messages",
    "Vollzugriff auf das Postfach": "Full access to the mailbox",
    # Never asked for either, but Google adds them to a grant on its own when the
    # consent screen is an OpenID one.
    "Anmeldung bei Google": "Sign-in with Google",
    "E-Mail-Adresse des Kontos": "The account's email address",
    "Name des Kontos": "The account's name",
    "Google fragt genau diese beiden Rechte ab, mehr wird nicht angefordert:":
        "Google asks for exactly these two permissions and nothing more:",
    "Dieses Postfach ist nur zum Lesen verbunden. Zum Verschicken von Anschreiben einmal trennen und neu verbinden — Google vergibt die Rechte beim Zustimmen und erweitert sie später nicht.":
        "This mailbox is connected for reading only. To send letters, disconnect "
        "and connect it once more — Google grants the permissions at consent time "
        "and never widens them afterwards.",
    "Die Adresse stammt aus dem Gmail-Profil dieses Kontos, nicht aus einer Eingabe.":
        "The address comes from this account's Gmail profile, not from anything typed in.",
    "ohne OAuth-Client bei Google lässt sich kein Postfach verbinden. "
    "NEWSPULSE_GMAIL_CLIENT_ID und NEWSPULSE_GMAIL_CLIENT_SECRET setzen.":
        "without an OAuth client at Google no mailbox can be connected. Set "
        "NEWSPULSE_GMAIL_CLIENT_ID and NEWSPULSE_GMAIL_CLIENT_SECRET.",
    "Einrichtung in der Deployment-Notiz": "Setup in the deployment note",
    "Der Zugang liegt als Datei neben der Datenbank, nur für den Eigentümer lesbar, "
    "und nie in einer Tabelle — damit ihn weder eine Datenbankkopie noch der "
    "Excel-Export aus der Maschine trägt.":
        "The credential lives in a file beside the database, readable by its owner "
        "only, and never in a table — so that neither a database copy nor the Excel "
        "export can carry it off the machine.",
    "Postfach verbunden.": "Mailbox connected.",
    "Postfach getrennt und der Zugriff bei Google widerrufen.":
        "Mailbox disconnected and the access revoked at Google.",
    "Der Zugang wurde hier gelöscht, der Widerruf bei Google ließ sich aber nicht "
    "bestätigen. Bitte im Google-Konto unter „Drittanbieter-Apps“ nachsehen.":
        "The credential was deleted here, but the revocation at Google could not be "
        "confirmed. Please check the Google account under \"Third-party apps\".",
    "Es war kein Postfach verbunden, also gab es auch nichts zu widerrufen.":
        "No mailbox was connected, so there was nothing to revoke.",
    "Dieses Postfach ist noch verbunden, aber der OAuth-Client fehlt in der "
    "Konfiguration: neu verbinden geht erst wieder mit NEWSPULSE_GMAIL_CLIENT_ID "
    "und NEWSPULSE_GMAIL_CLIENT_SECRET. Trennen und widerrufen geht jederzeit.":
        "This mailbox is still connected, but the OAuth client is missing from the "
        "configuration: reconnecting needs NEWSPULSE_GMAIL_CLIENT_ID and "
        "NEWSPULSE_GMAIL_CLIENT_SECRET again. Disconnecting and revoking works at "
        "any time.",
    "Die Freigabe wurde bei Google abgebrochen. Es wurde nichts gespeichert.":
        "Consent was cancelled at Google. Nothing was stored.",
    "Die Rückmeldung von Google gehörte nicht zu dieser Anfrage. Es wurde nichts verbunden.":
        "Google's answer did not belong to this request. Nothing was connected.",
    "Die Verbindung mit Google ist fehlgeschlagen. Es wurde nichts gespeichert.":
        "The connection to Google failed. Nothing was stored.",
    "Der Zugriff wurde bei Google entzogen, die Verbindung wurde deshalb beendet.":
        "Access was withdrawn at Google, so the connection was ended.",
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
