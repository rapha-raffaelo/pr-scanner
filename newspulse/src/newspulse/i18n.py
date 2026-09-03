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
    "Heute noch nichts, und in den letzten Tagen auch nicht.": "Nothing today, and nothing in the last few days either.",
    "Heute noch nichts. Zuletzt Berichterstattung am": "Nothing today. Last coverage on",
    "der letzte Tag mit Meldungen": "the last day with stories",
    "liegt noch keine Berichterstattung vor. Angezeigt wird deshalb": "there is no coverage yet. Showing instead",
    "Ab dieser Wichtigkeit (0–10) wird eine Meldung zur Warnung — oder wenn sie eines der Alarm-Themen des Mandanten trifft, unabhängig von der Wichtigkeit. Deshalb liegen viele Warnungen unter dem Schwellenwert: bei einem Mandanten mit vielen Alarm-Themen entscheidet die Themenliste, nicht diese Zahl. Wirkt nur auf künftige Läufe.": "A story becomes an alert at or above this importance (0-10) — or whenever it hits one of the client's alert topics, whatever its importance. That is why many alerts sit below the threshold: for a client with many alert topics it is the topic list that decides, not this number. Applies to future runs only.",
    "Neu beginnen": "Start over",
    "+ Mandant hinzufügen": "+ Add a client",
    "in der Coverage Map": "in the coverage map",
    "und %(n)s weitere": "and %(n)s more",
    "Ja, eintragen": "Yes, record it",
    "Sie halten fest, dass dieser Text hinausgegangen ist. Der Einwand oben bleibt daneben stehen.": "You are recording that this text went out. The objection above stays beside it.",
    "Es hält etwas in diesem Text für unbelegt. Wenn Sie widersprechen, geht die Nachricht so hinaus.": "It holds something in this text to be unsupported. Overrule it and the message goes out as it stands.",
    "Das Zweitmodell rät ab.": "The second model advises against it.",
    "Keine Warnungen am": "No alerts on",
    # --- The crisis rail on Heute (UHR-01, DEC-1) ----------------------------
    # The offer is worded as a question and the declaration as a statement, in
    # both languages: the whole point of DEC-1 is that the tool asks and a person
    # answers, and a label that read as an announcement would be option B.
    "Krise": "Crisis",
    "Krise?": "Crisis?",
    "Krise erklären": "Declare a crisis",
    "Krise schließen": "Stand down",
    "Stufe": "Level",
    # "von", "Medien" and "negativ" are already translated further down — this
    # rail reuses them rather than restating them.
    "erklärt von": "declared by",
    "Grund: warum wird sie geschlossen?": "Reason: why is it being stood down?",
    "Als Krise eingestuft": "Filed as a crisis",
    "Medien in 24 Stunden": "outlets in 24 hours",
    "Vorgeschlagen, nicht erklärt. Bis jemand hier drückt, ändert sich nichts.":
        "Proposed, not declared. Nothing changes until somebody presses this.",
    # --- The crisis page (UHR-03, DEC-2 option C) ----------------------------
    # "Verwerfen", "Frist", "Krisenkontakt", "Sprecher", "Pressekontakt",
    # "Medien", "negativ", "positiv", "Beiträge", "bis" and the check states are
    # already translated elsewhere in this table — the page reuses them.
    "Krise offen seit": "Crisis open for",
    "Krise geschlossen": "Crisis closed",
    "offen von": "open from",
    "Tg": "d",
    "Std": "h",
    "Min": "min",
    "Erklärt von": "Declared by",
    "um": "at",
    "Grund der Schließung:": "Reason it was stood down:",
    "Solange sie offen ist, läuft der Sweep alle %(n)s Minuten.":
        "While it is open, the sweep runs every %(n)s minutes.",
    "über uns": "about us",
    "von uns": "from us",
    "offen": "open",
    "Zeitleiste": "Timeline",
    "Zwei Spalten": "Two columns",
    "Frühere Krisen dieses Mandats:": "This mandate's earlier crises:",
    "Was gesagt wird": "What is being said",
    "Was wir sagen": "What we say",
    "eine Story": "one story",
    "Storys": "stories",
    "Auslöser": "Trigger",
    "Öffnen": "Open",
    "Kein gespeicherter Beitrag im Fenster dieser Krise.":
        "No stored coverage inside this crisis's window.",
    "im Entwurf": "in draft",
    "Wird geschrieben…": "Being written…",
    "Noch kein Text im Format": "No text yet in the format",
    "Eine Anfrage ist offen": "One request is open",
    "Anfragen sind offen": "requests are open",
    "Im Profil fehlt der Krisenkontakt.": "The profile has no crisis contact.",
    "Im Kickoff nachtragen": "Add it in the kickoff",
    "Ungeprüft": "Unchecked",
    "Anfrage": "Request",
    "keine Frist genannt": "no deadline stated",
    "Wer erreichbar ist": "Who is reachable",
    "Nicht im Profil hinterlegt.": "Not on file in the profile.",
    "Noch kein Text zu dieser Krise.": "No text for this crisis yet.",
    "Beitrag": "Coverage",
    "Krise erklärt": "Crisis declared",
    "Text entworfen": "Text drafted",
    "Text freigegeben": "Text released",
    "Die Krise ist der Abstand zwischen den beiden Spalten. Links wächst, was ohne uns läuft, rechts steht, was wir dagegen gesetzt haben, und der Kasten dazwischen sagt, wo nichts steht. Die Zeitleiste liegt einen Klick entfernt und wird beim Schließen zum Nachbericht.":
        "The crisis is the distance between the two columns. On the left grows "
        "what runs without us, on the right stands what we have set against it, "
        "and the box between them says where nothing stands. The timeline sits "
        "one click away and becomes the after-action record when the crisis "
        "closes.",
    "Tagen nichts, aber im Archiv liegt ältere Berichterstattung:": "days nothing, but the archive holds older coverage:",
    "In den letzten": "In the last",
    "seit %(days)s Tagen kein Lauf": "no run for %(days)s days",
    "Der tägliche Lauf ist ausgeblieben. Die Zahlen auf dieser Seite sind so alt wie der letzte Lauf.": "The daily run did not happen. The figures on this page are as old as the last run.",
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
    # The same page with no comparison group: a ranking, not a comparison. Every
    # string here exists because the diverging wording promises a left-hand side
    # that cannot be drawn.
    "Wer über %(name)s schreibt": "Who writes about %(name)s",
    "nach Menge sortiert": "sorted by volume",
    "Welche Medien in den letzten %(days)s Tagen über diesen Mandanten geschrieben haben, und wie oft.":
        "Which outlets wrote about this mandate in the last %(days)s days, and "
        "how often.",
    "Für eine Pitch-Liste — Medien, die über den Wettbewerb schreiben und über ihn nicht — fehlt noch eine Vergleichsgruppe.":
        "A pitch list — outlets that write about the competition and not about "
        "them — needs a comparison group first.",
    "Sobald ein Wettbewerber hinterlegt ist, wird daraus der Vergleich: welche Medien über die anderen schreiben und über diesen Mandanten nicht.":
        "Once a competitor is linked this becomes the comparison: which outlets "
        "write about the others and not about this mandate.",
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
    # --- The six formats (newspulse.assets.FORMATS) --------------------------
    # Name and description per format, exactly as the definition holds them, so
    # the registry stays the one place a format is described and the English
    # reader is not shown a German card. Three of the names are the same word in
    # both languages and are listed anyway: an entry that reads as a no-op is a
    # decision, a missing one is indistinguishable from an oversight.
    #
    # Here rather than with the story that puts the format cards on the page,
    # because the guard that keeps this honest is a test over ``assets.FORMATS``:
    # it fails the moment a seventh format is defined without an English name.
    # Landing the guard with the registry means the surface story inherits six
    # translated formats instead of discovering six German ones.
    "Pressemitteilung": "Press release",
    "Die offizielle Meldung des Mandanten, zitierfähig und datiert.":
        "The client's official announcement, quotable and dated.",
    "Statement": "Statement",
    "Drei bis fünf zitierfähige Sätze einer namentlichen Person.":
        "Three to five quotable sentences from a named person.",
    "Q&A": "Q&A",
    "Die Fragen, die kommen, samt der unangenehmen.":
        "The questions that will come, the uncomfortable ones included.",
    "Talking Points": "Talking points",
    "Was gesagt wird, was nicht, und der Weg zurück zur These.":
        "What to say, what not to, and the way back to the thesis.",
    "Gastbeitrag": "Guest article",
    "Ein argumentierter Text in der ersten Person, ohne Nachrichtenaufhänger.":
        "An argued piece in the first person, with no news hook.",
    "Interview-Briefing": "Interview briefing",
    "Wer fragt, was er zuletzt schrieb, und was gesagt werden soll.":
        "Who is asking, what they wrote lately, and what to get said.",
    # --- The two crisis formats (newspulse.assets.CRISIS_FORMATS) -------------
    # Same rule, same guard: the test iterates both registries, so a crisis
    # format defined without an English card fails the suite. "Holding
    # Statement" is the same words in both languages and is listed anyway.
    "Holding Statement": "Holding statement",
    "Der erste kurze Text nach draußen: was feststeht, was geprüft wird, wer erreichbar ist.":
        "The first short text to the outside: what is established, what is "
        "being checked, who can be reached.",
    "Q&A-Haltung": "Q&A stance",
    "Was der Sprecher in der Hand hält, wenn das Telefon klingelt; offene Fragen bleiben ausdrücklich offen.":
        "What the spokesperson holds when the phone rings; open questions stay "
        "explicitly open.",
    # --- The package on an impulse (DEC-1) -----------------------------------
    # One occasion, one package: the formats are a strip on the impulse, each
    # carrying the state it is in. The four state names are the load-bearing ones
    # here — "Entwurf" and "Geprüft" are the difference between a text a model
    # wrote and a text something has read, and a reader shown the German for one
    # and the English for the other would have to guess which is which.
    "Das Paket zu diesem Anlass": "Everything for this occasion",
    "Formate": "Formats",
    "Formaten geschrieben": "formats written",
    "noch nichts geschrieben": "nothing written yet",
    # The three stages of one impulse card: the occasion, the texts written from
    # it, and what left the house. Rubrics, so they stay short in both languages.
    "Die Idee": "The idea",
    "aus dem Themen-Radar": "from the topic radar",
    "Der Versand": "Sending",
    "Neue Nachricht": "New message",
    "Geschriebene Nachrichten": "Messages written",
    "verschickt": "sent",
    "Alles kopieren": "Copy all",
    "Fehlende schreiben": "Write the missing ones",
    # The tick boxes beside the format list: which of the six the reader
    # actually wants written, answered once instead of six times.
    "Ausgewählte schreiben": "Write the selected ones",
    # The state column on each format row: what it is, or what it is waiting for.
    "wartet auf": "waiting on",
    "Einwand": "Objection",
    "Content": "Content",
    "Vorschlag": "Suggested",
    # The third way to a guide: what the tool already holds.
    "Entwurf aus Profil und Berichterstattung": "Draft from profile and coverage",
    "Liest, was im Profil steht und wie die Presse über diesen Mandanten schreibt, und schlägt daraus einen Guide vor. Schwächere Grundlage als ein Markenhandbuch oder der Kickoff — gespeichert wird nur, was Sie übernehmen.":
        "Reads what the profile records and how the press writes about this "
        "mandate, and proposes a guide from it. Weaker ground than a brand book "
        "or the kick-off — only what you accept is saved.",
    "Ohne Häkchen werden alle noch fehlenden geschrieben.":
        "With nothing ticked, every one still missing is written.",
    "Wird geschrieben": "Writing",
    "Anschreiben": "Letter",
    "Nicht geschrieben": "Not written",
    "Geprüft": "Checked",
    "Schreiben": "Write",
    "Neu schreiben": "Write again",
    "Gegenlesen lassen": "Have it read",
    # "Bearbeiten" is not repeated here: the contact book already carries it
    # further down this dict, and a second entry for the same German string is a
    # key Python silently drops — the next person to reword one of them would
    # reword the one that loses.
    "Text": "Text",
    "Titel": "Title",
    "Schlagzeile": "Headline",
    "Änderung speichern": "Save the change",
    "Die Änderung wird gespeichert und als von Hand bearbeitet vermerkt. Danach "
    "liest das Zweitmodell den Text noch einmal, bevor er freigegeben werden kann.":
        "The change is stored and recorded as a human edit. The second model then "
        "reads the text again before it can be released.",
    "Der Text wurde nach der Prüfung von Hand geändert. Vor der Freigabe muss "
    "ihn das Zweitmodell noch einmal lesen.":
        "The text was edited by hand after it was checked. The second model has "
        "to read it again before it can be released.",
    "Es wird gerade ein anderer Text geschrieben — die Knöpfe sind so lange aus.":
        "Another text is being written right now — the buttons are off until "
        "it is done.",
    "Das Zweitmodell konnte diesen Text nicht lesen. Er ist ungeprüft — "
    "wer ihn jetzt freigibt, gibt ihn ungelesen frei.":
        "The second model could not read this text. It is unchecked — "
        "releasing it now means releasing it unread.",
    "Ungeprüft: wer jetzt freigibt, gibt ungelesen frei.":
        "Unchecked: releasing it now means releasing it unread.",
    "Zu diesem Anlass gibt es eine These und noch keinen Text. %(n)s Formate stehen bereit.":
        "This occasion has a thesis and no text yet. %(n)s formats stand ready.",
    # The refusals the routes and the acts hold. Written in Python rather than in
    # markup and rendered as ``asset_notes`` / ``slot.note`` / ``slot.hint``, which
    # is why they were the ones missing: nothing that reads the template for German
    # strings can see them.
    "Es wird gerade ein Text geschrieben. Der Auftrag wurde nicht angenommen: "
    "warten Sie, bis der laufende steht, sonst wird derselbe Aufruf zweimal "
    "bezahlt.":
        "A text is being written right now. The request was not accepted: wait "
        "for the running one to finish, or the same call gets paid for twice.",
    "Es läuft gerade ein Sammellauf. Der Auftrag wurde nicht angenommen: "
    "warten Sie, bis er durch ist, und klicken Sie dann noch einmal.":
        "A collection run is going on right now. The request was not accepted: "
        "wait for it to finish and click again.",
    "Dieser Text ist freigegeben. Freigegebene Texte werden weder geändert noch "
    "ersetzt, weil sie festhalten, was tatsächlich hinausgegangen ist.":
        "This text is released. Released texts are neither changed nor replaced, "
        "because they record what actually went out.",
    "Der Text wurde neu geschrieben, während dieses Formular offen war. Die "
    "Änderung wurde nicht gespeichert, damit sie den neuen Text nicht "
    "überschreibt. Bitte den jetzt angezeigten Text bearbeiten.":
        "The text was written again while this form was open. The change was not "
        "stored, so that it cannot overwrite the new text. Please edit the text "
        "as it is shown now.",
    "Paket schreiben": "Write the package",
    "Noch nicht schreibbar.": "Not writable yet.",
    "Profil ergänzen": "Fill in the profile",
    "Guide-Prüfung": "Guide check",
    "geschrieben": "written",
    "von Hand geändert": "edited by hand",
    "zugeschrieben": "attributed to",
    "freigegeben": "released",
    "von": "of",
    "Gegengelesen von": "Second read by",
    "Zweitmodell rät ab": "Second model advises against",
    "gelesen von": "read by",
    "keine Einwände": "no objections",
    "Nicht gegengelesen — es ist kein Zweitmodell hinterlegt.":
        "Not cross-checked: no second model is configured.",
    # The guide check, which is a different question from the crosscheck and says
    # so in its own words: a No-Go is not an objection a model weighed, it is a
    # rule the client wrote down. "Verstoß" rather than "Einwand" throughout, and
    # "breach" rather than "concern" in English, for the same reason.
    "Gegen den Guide geprüft von": "Checked against the guide by",
    "kein Verstoß gegen den Guide": "no breach of the guide",
    "Verstößt gegen den Guide": "Breaches the guide",
    "verstößt gegen": "breaches",
    "Nicht gegen den Guide geprüft — für diesen Mandanten ist kein Guide hinterlegt.":
        "Not checked against the guide: no guide is stored for this client.",
    # The same state for a mandate that *does* have a guide. Deliberately names
    # no cause: the worker reports a missing second model, an unreachable
    # provider and an unusable reply as one outcome, and a page that guesses
    # between them sends the reader to fix the wrong thing. It names no *log*
    # either — a letter written before this feature existed has nothing in one —
    # and offers the only remedy that holds for every letter in this state.
    "Nicht gegen den Guide geprüft — für diesen Brief liegt kein Ergebnis vor. Erst ein neuer Entwurf wird wieder geprüft.":
        "Not checked against the guide: no result is on file for this letter. "
        "Only a new draft gets checked again.",
    "Die beanstandeten Stellen sind nicht mitgespeichert.":
        "The passages objected to were not stored with it.",
    "Guide hinterlegen": "Add a guide",
    "Bezieht sich auf": "Refers to",
    # --- The ledger: the release, and what came back --------------------------
    # The five states a letter can be in. "Verschickt" rather than "Freigegeben"
    # because the consultant did both, and the record is about the letter having
    # left the house. "Ohne Reaktion" is not one of them: it is "verschickt" plus
    # fourteen days, derived rather than stored, and it sits beside the badge
    # instead of replacing it.
    "Verschickt": "Sent",
    "Antwort": "Reply",
    "Absage": "Declined",
    "Veröffentlicht": "Published",
    "Ohne Reaktion": "No response",
    "Ergebnis am": "Outcome on",
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
    # The strip above the impulses: which mailbox this is and what it may do.
    # A band under it used to argue that granting the send permission reverses
    # the product's founding sentence. True, and it belonged to whoever grants
    # it — but the strip renders on every mandate's Impulse page, so once the
    # permission was granted the argument was repeated at a reader who had
    # already made the decision, in red, where red means something is wrong.
    "Postfach verbunden": "Mailbox connected",
    # One string, not fragments — same reason as the release trail below: "lesen
    # und" plus "senden" is two English words that only happen to fall in this
    # order, and "senden" alone is generic enough to be reused later for a
    # different verb and pick up the wrong translation.
    "lesen und senden": "read and send",
    #: What the same strip says on a connection that was never granted the send
    #: permission — every mailbox connected before DEC-4's send path.
    "nur lesen": "read only",
    "Freigeben und senden": "Release and send",
    #: The address, asked for on the letter card rather than behind a link to the
    #: contact form. Measured before this shipped: an empty contact book, three
    #: letters written, none released, none sent — every one of them stopped
    #: exactly here.
    "E-Mail von %(who)s": "Email address for %(who)s",
    "Merken": "Save",
    "name@medium.de": "name@outlet.com",
    "Wird im Kontaktbuch hinterlegt. Danach lässt sich diese Nachricht von hier aus senden.":
        "Stored in the contact book. This message can then be sent from here.",
    #: Whether a recipient is reachable, said in the picker before the letter is
    #: written rather than on the card after it.
    "Adresse liegt vor": "address on file",
    "noch keine Adresse": "no address yet",
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
    "Anschreiben gesamt": "Letters",
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
    # The offers are rendered into the fields they are offers for, so the page is
    # one list instead of a proposal list above a duplicate empty form.
    "Vorschlag, noch nicht gespeichert": "Proposed, not saved yet",
    "Export": "Export",
    # The Texte tab and its rail: one place for everything drafted for a
    # mandate, whether it hangs off an occasion or off a month.
    "Texte": "Drafts",
    "Anlass": "Occasion",
    "Anderer Zeitraum": "Another period",
    "Der Fragensatz wird vorgeschlagen — die Seite aktualisiert sich, sobald er steht.":
        "The question set is being proposed — the page refreshes as soon as it "
        "is ready.",
    "Vergleichsunternehmen. Hier steht seine Berichterstattung — Impulse, Berichte und Profil führt RauteOS nur für Mandanten.":
        "A benchmark company. What is here is its coverage — openings, reports "
        "and profiles are kept for mandates only.",
    "noch nicht erzeugt": "not drafted yet",
    "Der Mandantenmonat als Mappe: Berichterstattung, Anteil am Gespräch und die Beiträge dahinter.":
        "The mandate's month as a workbook: coverage, share of voice and the "
        "articles behind it.",
    "%(n)s Vorschläge stehen unten schon in ihren Feldern, markiert und noch nicht gespeichert.":
        "%(n)s proposals are already in their fields below, marked and not saved "
        "yet.",
    "Klicken Sie in ein Feld, um einen Wert zu ändern, oder leeren Sie es, wenn Sie ihn nicht wollen. Was Sie speichern, gilt als Ihre Angabe; unveränderte Vorschläge behalten ihre Quelle.":
        "Click into a field to change a value, or empty it if you do not want it. "
        "What you save counts as your own answer; a proposal left unchanged keeps "
        "its source.",
    "Regel: Eine Angabe von Hand wird nie überschrieben. Das Netz widerspricht hier nur — ändern können Sie den Wert selbst, unten im Feld.":
        "Rule: an entry you made by hand is never overwritten. The web only "
        "contradicts it here; only you can change the value, in the field below.",
    "Verwerfen heißt: Dieser Widerspruch wird beim nächsten Abgleich nicht erneut gemeldet.":
        "Discarding means this contradiction is not reported again at the next "
        "check.",
    "Diese Vorschläge gab es nicht mehr — der Abgleich hatte sie inzwischen ersetzt. Nichts wurde geändert; unten steht der aktuelle Stand.":
        "Those proposals were already gone; the check had replaced them in the "
        "meantime. Nothing was changed, and what you see below is current.",
    "bisher nicht gefüllt": "previously empty",
    "Ihre Angabe": "Your entry",
    # The profile's own field names. Every review row is labelled with one, and
    # the labels live in ``profile.FIELDS`` rather than in a template, so no
    # ``t("...")`` in the markup names them and the string sweep never saw them.
    # "Positionierung" and "Wettbewerber" are further up, from the pages that
    # used them first. ``test_every_german_string_on_the_review_pages_is_
    # translated`` now reads ``profile.FIELDS`` directly for exactly this reason.
    "Geschäftsführung": "Management",
    "Gegründet": "Founded",
    "Sitz": "Headquarters",
    "Mitarbeitende": "Employees",
    "Umsatz / Finanzierung": "Revenue / funding",
    "Eigentümer": "Owners",
    "Produkte": "Products",
    "Öffentliche Themen": "Public debates",
    "Reputationsrisiken": "Reputation risks",
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
    # The lead used to say "in vierzehn Zeilen". The kick-off feeds three fields
    # the web cannot answer, so the profile is no longer fourteen lines and the
    # sentence no longer counts them: a number in prose beside the live figure
    # next to it is one of them being wrong.
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
    "Vier Arten von Signal aus dem Feld des Mandanten: was berichtet wurde, was "
    "belegt ist, was kommt und wo er auftreten kann.":
        "Four kinds of signal from the client's field: what was reported, what is "
        "evidenced, what is coming, and where they can be heard.",
    # One unit, not a sentence split around an interpolated number: a translator
    # cannot reorder "letzte" / 90 / "Tage" once they are three separate strings.
    "Themen-Radar: letzte %(days)s Tage": "Topic radar: last %(days)s days",
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
    # --- Client: the three market classes (SRC-02) ---------------------------
    # Each class heading is used three times — the section head, the mute, and
    # the line that brings a muted class back — so all three read the same word.
    "Studien": "Studies",
    "Regulierungskalender": "Regulatory calendar",
    "Veranstaltungen": "Events",
    "Belege, die sich monatelang zitieren lassen — wer sie veröffentlicht hat und "
    "was gemessen wurde.":
        "Evidence that stays citable for months — who published it and what was "
        "measured.",
    "Was kommt, in welcher Reihenfolge. Der Vorlauf ist der ganze Wert — was "
    "vorbei ist, steht nicht mehr hier.":
        "What is coming, in what order. The lead time is the whole value — what "
        "has passed is no longer here.",
    "Bühnen im Feld des Mandanten. Wo ein Call for Speakers läuft, steht die "
    "Frist daneben.":
        "Stages in the client's field. Where a call for speakers is open, the "
        "deadline stands beside it.",
    # One empty line per class, each naming the class. A shared "nothing found"
    # would leave a reader unsure which of the four sections was speaking.
    "Noch keine Studie aus dem Feld dieses Mandanten gefunden.":
        "No study from this client's field found yet.",
    "Für dieses Feld steht derzeit nichts im Regulierungskalender.":
        "Nothing is currently in the regulatory calendar for this field.",
    "Keine Veranstaltung im Feld dieses Mandanten gefunden.":
        "No event found in this client's field.",
    # The studies section is the only one nothing ages out of, so it is the only
    # one with a cap — and the cap is said out loud rather than applied quietly.
    "Ältere Studien werden nicht angezeigt.": "Older studies are not shown.",
    "Herausgeber unbekannt": "Publisher unknown",
    "Veranstalter unbekannt": "Organiser unknown",
    "gilt ab": "applies from",
    "Termin": "Date",
    "Frist": "Deadline",
    "Einreichfrist": "Submission deadline",
    # Names the threshold it is marked by rather than implying one, so the line
    # stays true if the two weeks in ``_DEADLINE_SOON_DAYS`` ever move. "Höchstens"
    # rather than "unter", because the mark is inclusive: at exactly the threshold
    # the countdown beside it reads "in 2 Wochen", and "in unter 2 Wochen" there
    # contradicts it.
    "läuft in höchstens %(weeks)s Wochen ab": "closes in %(weeks)s weeks at most",
    # A door that has shut is printed as shut rather than dropped: on a conference
    # still months away, no deadline at all reads as "this one never invited
    # speakers", which is a different answer.
    "abgelaufen": "closed",
    "ohne Datum": "undated",
    "kein Datum erkannt": "no date recognised",
    # The remaining time, in the units a consultant thinks in. Under a week it
    # switches to days: "in 0 Wochen" is not a sentence, and rounding three days
    # up to a week would overstate the time the reader has.
    "morgen": "tomorrow",
    "in %(days)s Tagen": "in %(days)s days",
    "in einer Woche": "in a week",
    "in %(weeks)s Wochen": "in %(weeks)s weeks",
    # Provenance (DEC-1 B). The search half returns things that are not really
    # studies, so a reader has to be able to judge such a row as one.
    "Kuratiert": "Curated",
    "Aus einer kuratierten Quelle dieser Klasse.":
        "From a curated source for this class.",
    "Von der Feldsuche gefunden — nicht aus einer kuratierten Quelle. Vor der "
    "Verwendung prüfen.":
        "Found by the field search, not from a curated source. Check it before "
        "using it.",
    "Nur kuratierte Quellen: der Branchenbegriff dieses Mandanten kommt in der "
    "deutschen Presse zu selten vor, um damit zu suchen. Die gezielte Suche im "
    "Feld fehlt deshalb.":
        "Curated sources only: this client's industry term appears too rarely in "
        "the German press to search with. The targeted search in the field is "
        "therefore missing.",
    "Nur kuratierte Quellen: für diesen Mandanten ist keine Branche hinterlegt, "
    "und ohne sie lässt sich im Feld nicht gezielt suchen.":
        "Curated sources only: no industry is set for this client, and without one "
        "there is no way to search the field.",
    "Branche anpassen": "Adjust the industry",
    "Branche hinterlegen": "Add an industry",
    # The per-class mute, and the only place it can be undone.
    "Klasse ausblenden": "Hide this class",
    "Ausgeblendet und nicht mehr abgerufen:": "Hidden and no longer fetched:",
    "wieder einblenden": "show again",
    # --- Berichte: the review surface and the document -----------------------
    #
    # The document's chrome is here with the rest of the interface, and its
    # *content* is not: a claim, a consequence and a headline stay in the language
    # they were written in, exactly as a stored summary does. An English-speaking
    # consultant reviewing a German mandate's month gets English labels around
    # German sentences, which is the honest rendering — the alternative is a page
    # that claims RauteOS wrote something it did not.
    "Berichte": "Reports",
    "Bericht": "Report",
    "RauteOS liest den Zeitraum und schlägt Befunde vor: eine Aussage, was daraus folgt, und darunter die Beiträge, auf denen sie steht. Behalten, ändern oder verwerfen — was übrig bleibt, wird das Dokument.":
        "RauteOS reads the period and proposes findings: a claim, what follows "
        "from it, and underneath it the coverage it rests on. Keep, edit or drop "
        "— what survives becomes the document.",
    "Bericht erzeugen": "Draft report",
    "Für diesen Mandanten liegt noch kein Bericht vor. Zum Monatsersten entsteht einer automatisch; er lässt sich hier auch sofort erzeugen.":
        "There is no report for this client yet. One is drafted automatically on "
        "the first of the month; it can also be drafted here right away.",
    "Entwurf": "Draft",
    "Freigegeben": "Released",
    "Freigegeben am": "Released on",
    "Freigegeben von": "Released by",
    "Freigeben": "Release",
    "Name (optional)": "Name (optional)",
    "Mit der Freigabe steht der Name der Agentur auf dem Bericht. Er wird eingefroren: Befunde, Zahlen und Belege bleiben, wie sie freigegeben wurden, auch wenn sich die Berichterstattung später ändert.":
        "Releasing puts the agency's name on the report. It is then frozen: "
        "findings, figures and evidence stay as they were released, even when the "
        "coverage underneath them changes later.",
    "Ein freigegebener Bericht wird nicht mehr geändert und nicht neu erzeugt.":
        "A released report is neither edited nor drafted again.",
    "Dokument": "Document",
    "Der Bericht ist freigegeben und wird nicht überschrieben. Ein freigegebener Bericht wird nicht neu erzeugt.":
        "The report is released and is not overwritten. A released report is not "
        "drafted again.",
    # The two error strings that carry data keep their placeholders, so the
    # translation is chosen before the value is put into it and an English reader
    # gets an English sentence around a German term the archive could not source.
    "Der Bericht ist fehlgeschlagen: {reason}": "The report failed: {reason}",
    "Ein Befund ohne Aussage ist kein Befund. Der Text bleibt wie er war.":
        "A finding without a claim is not a finding. The text stays as it was.",
    "„{terms}“ kann RauteOS aus Archiv und Ledger nicht belegen und steht deshalb in keinem Bericht. Der Text bleibt wie er war.":
        "RauteOS cannot source „{terms}“ from the archive or the ledger, "
        "so it stands in no report. The text stays as it was.",
    "Verworfen": "Dropped",
    "Verworfen, weil": "Dropped because",
    "Warum nicht? (optional)": "Why not? (optional)",
    "Wieder aufnehmen": "Take it back up",
    "bearbeitet": "edited",
    "Aussage": "Claim",
    "Was daraus folgt": "What follows from it",
    "Kein Beleg trägt diesen Befund mehr. Er steht nicht im Dokument.":
        "No evidence carries this finding any more. It is not in the document.",
    "Beleg(e) sind nicht mehr in der Berichterstattung. Der Befund steht auf dem, was übrig ist.":
        "piece(s) of evidence are no longer in the coverage. The finding stands on "
        "what is left.",
    "Ein Teil der Belege ist inzwischen nicht mehr in der Berichterstattung des Mandanten. Der Befund steht auf dem, was hier steht.":
        "Some of the evidence is no longer in the client's coverage. The finding "
        "stands on what is printed here.",
    "kein Wert": "no figure",
    "redaktionell bearbeitet": "edited by the agency",
    # The comparison under a figure, as its parts. Composed in the template rather
    # than stored as one German phrase: the number between them is frozen at
    # release and the words around it are chrome, and only one of those two may
    # change with the reader's language.
    "vorher": "previously",
    "kein Vergleichszeitraum": "no comparison period",
    # ``reporting.Direction``. "unbekannt" is below, with the tonality it shares
    # the word with.
    "gestiegen": "up",
    "gefallen": "down",
    "unverändert": "unchanged",
    # Why a figure is missing, as ``reporting.MetricValue.note`` says it. This is
    # the line an English reader most needs on a tile that shows no number, and it
    # is the tool's own wording rather than the mandate's, so it translates. The
    # notes built from counts (see ``reporting._unnamed_note``) have no fixed
    # form to key on and fall back to German, which is what this scheme degrades
    # to everywhere by design.
    "Keine Berichterstattung im Zeitraum.": "No coverage in the period.",
    "Kein Vergleichsumfeld hinterlegt. Ein Anteil am Marktgespräch braucht die Wettbewerber, die für diesen Mandanten hinterlegt sind.":
        "No comparison set stored. A share of the market conversation needs the "
        "competitors stored for this client.",
    "Im Zeitraum wurde weder über den Mandanten noch über einen der hinterlegten Wettbewerber geschrieben. Ohne Marktgespräch gibt es keinen Anteil daran.":
        "In this period neither the client nor any of the stored competitors was "
        "written about. With no market conversation there is no share of it.",
    "Keine freigegebenen Anschreiben im Zeitraum. Ohne eigene Ansprache gibt es nichts zuzurechnen — das ist keine Null, sondern keine Frage.":
        "No released letters in the period. With no outreach of our own there is "
        "nothing to attribute: that is not a zero, it is not a question.",
    "Gezählt wird ein Beitrag, der die Botschaft als Ganzes aufnimmt oder mindestens zwei ihrer tragenden Begriffe.":
        "An item counts when it carries the message as a whole, or at least two "
        "of its load-bearing terms.",
    "Keine Kernbotschaften im Kommunikations-Guide hinterlegt. Ohne sie gibt es nichts, woran sich die Berichterstattung messen ließe.":
        "No key messages stored in the communications guide. Without them there "
        "is nothing to measure the coverage against.",
    # And why a whole report has no findings, as ``report.ReportDraft.note`` says
    # it. Three separate sentences on purpose: an empty month, a month whose
    # figures carry nothing, and a reading that produced nothing that stands up
    # are different things to put in front of a client.
    "Für diesen Zeitraum liegt keine belegbare Kennzahl vor, auf die sich eine Aussage stützen ließe.":
        "This period holds no sourceable figure a statement could rest on.",
    "Aus der Berichterstattung dieses Zeitraums ergibt sich keine belegbare Aussage.":
        "The coverage in this period yields no sourceable statement.",
    "Kennzahlen": "Figures",
    "Verteilung": "Distribution",
    "Befunde": "Findings",
    "Erstellt am": "Drafted on",
    "Vergleichsumfeld": "Comparison set",
    "keines hinterlegt": "none stored",
    "bis": "to",
    "Der Inhalt ist ab der Freigabe eingefroren und ändert sich nicht mehr mit dem Archiv.":
        "From the release onwards the content is frozen and no longer moves with "
        "the archive.",
    "Für diesen Zeitraum steht kein Befund im Bericht.":
        "No finding stands in the report for this period.",
    "Zurück zur Prüfung": "Back to the review",
    "Exportieren": "Export",
    # --- Der Pressespiegel ----------------------------------------------------
    "Pressespiegel": "Press clippings",
    "Zurück zu den Berichten": "Back to the reports",
    "Aufgriff": "pickup",
    "Aufgriffe": "pickups",
    "reichweitenstärkstes Medium": "widest-reaching outlet",
    "Im Zeitraum liegt keine Berichterstattung über den Mandanten vor. Der Pressespiegel bleibt deshalb leer — das ist eine Aussage über den Zeitraum, kein Fehler im Dokument.":
        "The period holds no coverage of the client. The clippings therefore stay "
        "empty — a statement about the period, not a fault in the document.",
    "Der Pressespiegel führt Überschrift, Medium, Datum, gespeicherte Zusammenfassung und Tonalität. Volltexte werden nicht gespeichert und deshalb nicht wiedergegeben.":
        "The clippings list headline, outlet, date, the stored summary and the "
        "tone. Full article text is never stored and therefore never reproduced.",
    "Tabelle anzeigen": "Show table",
    "Segment": "Segment",
    "Jede Zahl in diesem Bericht stammt aus der archivierten Berichterstattung und dem Ansprache-Ledger. Reichweiten, Kontaktchancen und Werbewerte werden nicht geschätzt und deshalb nicht ausgewiesen.":
        "Every figure in this report comes from the archived coverage and the "
        "outreach ledger. Reach, opportunities to see and advertising value are "
        "not estimated and therefore not stated.",
    # The finding kinds, as ``models.ReportFindingKind`` names them.
    "Sichtbarkeit": "Visibility",
    "Risiko": "Risk",
    "Wirkung": "Impact",
    "Botschaft": "Message",
    # The metric labels, as ``reporting._LABELS`` names them, plus the one
    # tonality value that had no entry yet.
    "Beiträge": "Items",
    "Beiträge in Leitmedien": "Items in lead media",
    "Anteil am Marktgespräch": "Share of the market conversation",
    "Aus eigener Ansprache": "From our own outreach",
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
    # A researched value the grounding API returned no source for. It stays on
    # offer and says so, rather than borrowing the line above it.
    "Recherche ohne Quelle": "Research with no source",
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
    # The refusal when the button is pressed for a mandate that has answered
    # nothing. It reaches the page as a caught exception rather than as a literal
    # in a template, which is why the page-scanning test cannot see it.
    "Noch keine Antwort aus dem Kickoff — ohne Antworten gibt es nichts zu "
    "destillieren.":
        "No answer from the kick-off yet, so there is nothing to distil.",
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
    # --- The brain: the standards every prompt composes from -------------------
    # "Maßstäbe" and not "Gehirn": the panel holds what a text is measured
    # against, and the architecture's name for the layer is not what the person
    # editing a sentence about tone is looking for.
    "Maßstäbe": "Standards",
    "Was das Haus für gut hält. Jeder Text, den das Werkzeug schreibt, setzt sich "
    "hieraus zusammen: Ändert sich ein Block, ändert sich jeder Prompt, der ihn einbindet.":
        "What the house holds to be good. Every text the tool writes is composed "
        "from these: change a block and every prompt that includes it changes with it.",
    "Fassung": "Version",
    # The three states a block can be in, and they must read as three different
    # things: the shipped wording, a wording the agency wrote over it, and a
    # wording that overrides a block the repository no longer has.
    "Vorgabe": "Shipped",
    "Überschrieben": "Overridden",
    "Verwaist": "Orphaned",
    "Überschrieben, aber nicht mehr ausgeliefert: Der Block wurde umbenannt oder "
    "entfernt. Die Überschreibung gilt weiter, wo ein Prompt sie einbindet — "
    "Zurücksetzen entfernt sie.":
        "Overridden but no longer shipped: the block was renamed or removed. The "
        "override still applies wherever a prompt includes it; reverting removes it.",
    "Zuletzt geändert": "Last changed",
    "Noch nie geändert": "Never changed",
    "Wortlaut": "Wording",
    "Auf Vorgabe zurücksetzen": "Restore the shipped wording",
    "Auf die Vorgabe zurückgesetzt": "Restored the shipped wording",
    "Noch keine Änderung aufgezeichnet. Der Block ist der ausgelieferte Wortlaut.":
        "No change recorded yet. The block is the shipped wording.",
    # Why an empty edit is refused. It reaches the page as a value rather than as
    # chrome — the route hands `brain.EMPTY_BLOCK_MESSAGE` to the template — and
    # is translated anyway, because a German sentence in a red box on an
    # otherwise English panel is the mixed-language failure the suite forbids.
    "Ein Block darf nicht leer sein: ein Prompt, der einen leeren Maßstab "
    "einsetzt, lässt ihn stillschweigend weg.":
        "A block must not be empty: a prompt that inserts an empty standard "
        "silently drops it.",
    # A revert to a block the repository no longer ships: the row says a wording
    # was restored, and there is no longer a wording to show for it.
    "Auf die Vorgabe zurückgesetzt — der ausgelieferte Wortlaut existiert nicht mehr.":
        "Restored the shipped wording — the shipped wording no longer exists.",
    # Who a change is recorded as when the installation has no named user. Same
    # word ClientFact.filled_by uses, and translated for the same reason: it is a
    # value the interface renders, not a stored German noun the reader must parse.
    "mensch": "human",
    # --- The stamp: which standards a generated text was written under ---------
    # Two states and they must not blur. A number is a version somebody can open
    # and read; "unbekannt" is a text from before the tool recorded any, and it
    # is deliberately not rendered as version zero — zero is a true claim (the
    # standards have never been changed here) that an old row cannot make.
    "unbekannt": "unknown",
    # Only ever rendered when the cross-check read different standards from the
    # letter it read: two model calls, and a block edited between them.
    "gegengelesen unter": "cross-checked under",
    "vor der Aufzeichnung der Maßstäbe entstanden":
        "written before the standards were recorded",
    # What the reader is looking at after following a stamp. The version counts
    # changes across the whole house and resolves to the single change that
    # produced it, so this page answers the question for one block and shows the
    # others as they are today. Said outright, because a page that let the reader
    # believe it was showing "the standards at version N" would be a better lie
    # than no link at all.
    "Von einem Text hierher gekommen:": "Followed here from a text:",
    "ist die unten markierte Änderung an diesem Block.":
        "is the change marked below, on this block.",
    # A revert is a change too, and a text stamped with one was written under the
    # shipped wording rather than under an override. Same sentence otherwise.
    "ist die unten markierte Rücksetzung: der Text entstand unter der "
    "ausgelieferten Vorgabe dieses Blocks.":
        "is the reset marked below: the text was written under this block's "
        "shipped default.",
    "Die Fassung zählt alle Änderungen am ganzen Haus — die übrigen Blöcke "
    "stehen in ihrem heutigen Wortlaut, nicht in dem von damals.":
        "The version counts every change across the whole house, so the other "
        "blocks are shown as they read today and not as they read then.",
    # --- The mailbox connection (OUT-03) -------------------------------------
    # "Postfach" rather than "Gmail": the panel is about the mailbox the letters
    # go out through, and the provider is an implementation detail of it.
    "Postfach": "Mailbox",
    "Postfach verbinden": "Connect mailbox",
    "Postfach trennen": "Disconnect mailbox",
    "Verbunden als": "Connected as",
    "seit": "since",
    "seit %(days)s Tagen still": "silent for %(days)s days",
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
    # --- KI-Sichtbarkeit ------------------------------------------------------
    # The measurement names the assistants it asked rather than speaking of "die
    # KI", so the English keeps that too: what two named models answered on a
    # stated date, never a claim about machines in general.
    "KI-Sichtbarkeit": "AI visibility",
    "Wo das Mandat steht, wenn ein Assistent nach diesem Markt gefragt wird, und was sich seit der letzten Messung bewegt hat.":
        "Where the client stands when an assistant is asked about this market, and "
        "what has moved since the last measurement.",
    "%(n)s Fragen, gemessen alle %(days)s Tage.": "%(n)s questions, measured every %(days)s days.",
    # Counted, not "1 Fragen": a set of one is the state a mandate is in for as
    # long as it takes to accept the second question, and the page is read in it.
    "Eine Frage, gemessen alle %(days)s Tage.": "One question, measured every %(days)s days.",
    "Wieder fällig ab dem %(date)s": "Due again from %(date)s",
    "Alle Fragen": "Every question",
    "Jetzt messen": "Measure now",
    # A measurement takes minutes and its answers arrive one at a time, so a page
    # loaded while one runs says so rather than reporting a half-spent set.
    "Eine Messung läuft gerade.": "A measurement is running right now.",
    "Die Zahlen unten sind die der Messung davor.":
        "The figures below are those of the measurement before it.",
    # The four bands, by distance from the brand. Only the first may name the client.
    "Marke": "Brand",
    "Auswahl": "Shortlist",
    "Problem": "Problem",
    "Stand": "Standing",
    "Messung vom": "Measured on",
    "der Fragen nennen %(name)s": "of the questions name %(name)s",
    "%(named)s von %(measured)s gemessenen Fragen": "%(named)s of %(measured)s measured questions",
    "%(points)s Prozentpunkte seit der Messung davor": "%(points)s percentage points since the previous measurement",
    "Anteil der Fragen, die das Mandat nennen, über die letzten Messungen":
        "Share of questions naming the client, over the last measurements",
    "Die gestrichelte Linie ist %(name)s, das im selben Fragensatz am häufigsten genannte Unternehmen.":
        "The dashed line is %(name)s, the company named most often in the same question set.",
    # A single measurement is a point, not a direction: said in one line rather
    # than drawn as a flat trend somebody would read as stability.
    "Erst eine Messung: es gibt noch nichts, wogegen sich das vergleichen ließe.":
        "Only one measurement so far: there is nothing yet to compare this against.",
    "%(n)s Frage(n) des Satzes wurden nicht gemessen und zählen in der Zahl oben nicht mit.":
        "%(n)s question(s) in the set were not measured and are not counted in the figure above.",
    "%(n)s Antwort(en) kamen zurück und ließen sich nicht auslesen.":
        "%(n)s answer(s) came back and could not be read.",
    "Ohne Antwort geblieben:": "No answer from:",
    "Die Messung vom": "The measurement of",
    "hat keine Antwort geliefert.": "produced no answer at all.",
    "Der Stand darunter ist der der Messung davor.": "The standing below is the one from the measurement before it.",
    "Eine frühere Messung, auf die er zurückfallen könnte, gibt es nicht.":
        "There is no earlier measurement for it to fall back on.",
    "Wer den Markt besetzt": "Who occupies the market",
    "Nennungen über %(n)s gemessene Fragen, alle Modelle": "Mentions across %(n)s measured questions, all models",
    # Counted, not "über 1 gemessene Fragen": a mandate sits on a set of one for
    # as long as it takes to accept the second question, and reads the page in it.
    "Nennungen über eine gemessene Frage, alle Modelle": "Mentions across one measured question, all models",
    "In dieser Messung wurde kein einziges Unternehmen genannt.": "This measurement named no company at all.",
    # The mandate's place, said out loud where the ranking shows only its head. The
    # one row the cut never takes is the mandate's.
    "Platz %(n)s": "Rank %(n)s",
    "Woher die Antworten kommen": "What the answers lean on",
    "genannte Quellen": "sources the models stated",
    "eigene Seite": "own site",
    "Die Modelle haben in diesen Antworten keine Quelle genannt. Das ist ein Befund, keine Lücke in der Messung.":
        "The models stated no source in these answers. That is a finding, not a gap "
        "in the measurement.",
    "Bewegung": "Movement",
    "gegenüber dem": "against",
    "%(changed)s von %(total)s Fragen verändert": "%(changed)s of %(total)s questions changed",
    "Erstmals genannt, bei %(provider)s auf Position %(position)s.":
        "Named for the first time, at %(provider)s in position %(position)s.",
    # A model may name the mandate in prose without placing it in a list at all.
    # Winning or losing that place is its own move, and is never written as a jump
    # between two ranks — one of the two sides was never measured.
    "Erstmals genannt, bei %(provider)s.": "Named for the first time, at %(provider)s.",
    "Bei %(provider)s erstmals in der Aufzählung, auf Position %(after)s.":
        "Placed in the list at %(provider)s for the first time, in position %(after)s.",
    "Bei %(provider)s nur noch erwähnt, ohne Platz in der Aufzählung.":
        "Only mentioned at %(provider)s now, with no place in the list.",
    "Bei %(provider)s nicht mehr genannt.": "No longer named at %(provider)s.",
    "Position %(before)s auf %(after)s bei %(provider)s.": "Position %(before)s to %(after)s at %(provider)s.",
    "%(n)s Frage(n) sind unverändert und stehen deshalb nicht hier. Eine einzelne Antwort schwankt; erst eine Bewegung über zwei Messungen ist eine Bewegung.":
        "%(n)s question(s) are unchanged and are therefore not listed here. A single "
        "answer varies; a move counts as a move only once it survives two measurements.",
    # Neither "verändert" nor "unverändert": two runs that overlap on no cell at
    # all have nothing to compare, and the reassuring reading of that is the one
    # nobody double-checks.
    "Keine Frage wurde in beiden Messungen gemessen, es gibt also nichts zu vergleichen. Die beiden Messungen haben verschiedene Teile des Satzes erreicht.":
        "No question was measured in both measurements, so there is nothing to "
        "compare. The two measurements reached different parts of the set.",
    "Nichts hat sich verändert: alle %(n)s vergleichbaren Fragen antworten wie in der Messung davor.":
        "Nothing changed: all %(n)s comparable questions answer as they did in the "
        "measurement before.",
    "Beobachtungen, nichts wird ausgelöst": "Observations, nothing is triggered",
    "%(n)s Frage(n) nennen überhaupt kein Unternehmen": "%(n)s question(s) name no company at all",
    "Auf diesen Feldern steht kein Wettbewerber im Weg. Was dort beantwortet wird, konkurriert mit niemandem.":
        "No competitor is in the way on those fields. Whatever answers them competes "
        "with nobody.",
    "%(n)s Frage(n) nennen den Wettbewerb, aber nicht %(name)s": "%(n)s question(s) name competitors but not %(name)s",
    "Dort wird die Kategorie beantwortet und das Mandat kommt darin nicht vor. Das ist die Lücke, die diese Messung sichtbar macht.":
        "The category is answered there and the client does not appear in it. That is "
        "the gap this measurement makes visible.",
    "Zum Wettbewerb": "To the competitor view",
    "Jede gemessene Frage nennt das Mandat. Aus dieser Messung folgt nichts, was hier stehen müsste.":
        "Every measured question names the client. Nothing follows from this "
        "measurement that would belong here.",
    "der ganze Satz, Frage für Frage": "the whole set, question by question",
    "Diese Frage aus dem Satz nehmen": "Take this question out of the set",
    "Position %(n)s": "Position %(n)s",
    "genannt": "named",
    # The three states of one cell. "nicht gemessen" and "nicht genannt" must stay
    # different sentences in every language: a missing answer and a negative answer
    # are different facts, and only one of them is about the client.
    "nicht genannt": "not named",
    "nicht gemessen": "not measured",
    "noch nicht gemessen": "not measured yet",
    # --- The question set, before anything is stored --------------------------
    "Noch kein Fragensatz": "No question set yet",
    "Ohne Fragen gibt es nichts zu messen. RauteOS schlägt einen Satz vor, gebaut aus dem Profil und dem gemessenen Branchenbegriff — gespeichert wird nur, was Sie übernehmen.":
        "Without questions there is nothing to measure. RauteOS proposes a set built "
        "from the profile and the measured industry term \u2014 only what you accept is stored.",
    "Fragen vorschlagen": "Propose questions",
    # Offered wherever the cap leaves room, because a set that already stands
    # could otherwise only be grown by retiring it down to nothing first.
    "Weitere Fragen vorschlagen": "Propose more questions",
    "Vorgeschlagene Fragen": "Proposed questions",
    "gespeichert ist noch keine": "none stored yet",
    "Erst ein Klick auf \u201eAusgewählte übernehmen\u201c legt Fragen an. Höchstens %(n)s Fragen stehen im Satz.":
        "Only \"Take the selected ones\" files a question. At most %(n)s "
        "questions stand in the set.",
    "Ausgewählte übernehmen": "Take the selected ones",
    # The findings as a tick list: what stays ticked is what the document says.
    "Behalten": "Keep",
    "Auswahl speichern": "Save the selection",
    "Was angehakt bleibt, steht im Dokument. Der Rest wird verworfen — mit dem Grund, wenn Sie einen hinterlassen.":
        "Whatever stays ticked is in the document. The rest is dropped, with the "
        "reason if you leave one.",
    "Der Vorschlag hat nichts ergeben. Meist fehlt dem Profil noch der Text zum Geschäftsfeld, aus dem eine Kaufentscheidungsfrage gebaut wird.":
        "The proposal produced nothing. Usually the profile is still missing the text "
        "about the business field a purchase question is built from.",
    "Der Fragensatz steht, gemessen wurde noch nicht. Die erste Messung läuft mit dem nächsten täglichen Lauf.":
        "The question set is in place and nothing has been measured yet. The first "
        "measurement runs with the next daily sweep.",
    "Gemessen wird, was die angebundenen Assistenten auf denselben Fragensatz antworten, nicht was \u201edie KI\u201c denkt. RauteOS misst und berichtet, es optimiert nichts und verschickt nichts.":
        "What is measured is what the connected assistants answer to the same set of "
        "questions, not what \"AI\" thinks. RauteOS measures and reports; it optimises "
        "nothing and sends nothing.",
    # --- Der Redaktionsplan (UHR-07) ------------------------------------------
    # The page a retainer conversation is held over: six months of hooks, each
    # resolving to a stored row.
    "Plan": "Plan",
    "Redaktionsplan": "Editorial plan",
    "%(n)s Monate voraus, gebaut aus datierten Marktsignalen, geprüften Themen und dem, was das Archiv im selben Monat des Vorjahres getragen hat. Jeder Haken hängt an einer gespeicherten Zeile. Kein Datum wird geraten.":
        "%(n)s months ahead, built from dated market signals, checked themes and "
        "what the archive carried in the same month a year ago. Every hook hangs "
        "on a stored row. No date is guessed.",
    "Neu berechnen": "Recompute",
    "Wird berechnet…": "Recomputing…",
    "Als Dokument": "As a document",
    "laufender Monat": "current month",
    "Haken": "hooks",
    "1 Haken": "1 hook",
    "kein Haken": "no hook",
    "verworfen": "discarded",
    "Leer.": "Empty.",
    # A month where something was found and a person refused all of it. Distinct
    # from the empty month's sentence on purpose: that one is a claim about the
    # evidence, and here the evidence exists.
    "Alle Haken in diesem Monat wurden verworfen.":
        "Every hook in this month was dropped.",
    # What the same month says in the document, which carries no refused rows.
    "Kein Termin in diesem Monat.": "No date in this month.",
    "Kein datiertes Signal, kein Archivmuster, kein Thema mit belegter Resonanz in diesem Monat. Ein leerer Monat wird nicht gefüllt, er wird gezeigt: entweder ist hier wirklich nichts, oder dem Mandat fehlt ein Thema.":
        "No dated signal, no archive pattern, no theme with evidenced resonance "
        "in this month. An empty month is not filled, it is shown: either there "
        "genuinely is nothing here, or the client is missing a theme.",
    "Kein datiertes Signal, kein Archivmuster, kein Thema mit belegter Resonanz in diesem Monat.":
        "No dated signal, no archive pattern, no theme with evidenced resonance "
        "in this month.",
    "Themen prüfen": "Review the themes",
    "Text schreiben": "Write a text",
    "In einen anderen Monat verschieben": "Move to another month",
    "Verschieben": "Move",
    "Beleg": "Evidence",
    "Der Beleg zu diesem Haken ist nicht mehr auffindbar.":
        "The evidence behind this hook can no longer be found.",
    # The Herkunftsklassen, as ``plan_view.KLASSEN`` names them.
    "Studie": "Study",
    "Regulierung": "Regulation",
    "Veranstaltung": "Event",
    "Thema": "Theme",
    "Archivmuster": "Archive pattern",
    "Marktsignal": "Market signal",
    # The hook states, as ``plan_view.STATE_LABELS`` names them.
    "Vorgeschlagen": "Proposed",
    "Angenommen": "Accepted",
    "Erledigt": "Done",
    # The honest half of the date rule: a source that names only a month yields a
    # hook that names only a month, and the page says so rather than showing the
    # first of it.
    "ohne Tag": "no day",
    "Für diesen Mandanten lässt sich noch kein Plan bauen.":
        "No plan can be built for this client yet.",
    "Ein Haken entsteht aus einem datierten Marktsignal, aus einem Thema mit gemessener Resonanz oder aus einem Monat, den das Archiv im Vorjahr getragen hat. Für dieses Mandat liegt keines davon vor — das ist eine Lücke in der Einrichtung und keine Aussage über den Markt.":
        "A hook comes from a dated market signal, from a theme with measured "
        "resonance, or from a month the archive carried a year ago. This client "
        "has none of the three — that is a gap in the setup, not a statement "
        "about the market.",
    "Keine geprüften Themen hinterlegt.": "No checked themes on file.",
    "Kein Marktsignal gefunden.": "No market signal found.",
    "Jeder Termin in diesem Plan stammt aus einer gespeicherten Zeile: einem datierten Marktsignal, einem Thema mit gemessener Resonanz oder einem Monat, den das Archiv im Vorjahr getragen hat. Kein Datum ist geschätzt.":
        "Every date in this plan comes from a stored row: a dated market signal, "
        "a theme with measured resonance, or a month the archive carried a year "
        "ago. No date is estimated.",
    "Der Monat ist die Einheit, in der eine Agentur ohnehin arbeitet: der Bericht ist monatlich, das Retainer-Gespräch ist monatlich. Der Plan ist deshalb ein Stapel Monate, und ein leerer Monat ist eine Aussage, keine Lücke im Layout.":
        "The month is the unit an agency already works in: the report is monthly, "
        "the retainer meeting is monthly. The plan is therefore a stack of "
        "months, and an empty month is a statement rather than a gap in the "
        "layout.",
    "Der Plan reicht so weit, wie belegte Termine reichen; ein leerer Monat bleibt leer, statt gefüllt zu werden.":
        "The plan reaches as far as evidenced dates reach; an empty month stays "
        "empty rather than being filled.",
    # What a refused click leaves behind, in ``plan_view``.
    "Der Plan wird gerade neu berechnet. Der Auftrag wurde nicht angenommen: warten Sie, bis der laufende steht, sonst wird derselbe Aufruf zweimal bezahlt.":
        "The plan is being recomputed right now. The request was not accepted: "
        "wait for the running one, or the same call is paid for twice.",
    "Die Neuberechnung ist mit einem Fehler abgebrochen. Der bisherige Plan steht unverändert. Details stehen im Log.":
        "The recompute stopped with an error. The plan you had stands unchanged. "
        "Details are in the log.",
    "Der gewählte Monat liegt nicht im Plan. Verschoben wird nur innerhalb der Monate, die der Plan zeigt.":
        "The month you picked is not in the plan. A hook can only be moved inside "
        "the months the plan shows.",
    # --- The fast lane on Heute (UHR-05, DEC-6 A) ---------------------------
    # The card's two load-bearing pieces are the remaining time as a number and
    # the standing's one sentence; the sentence itself is stored data and stays
    # in the language it was written in. "Std", "Min", "aufgegriffen", "Text
    # schreiben" and "Verwerfen" are already translated above and reused.
    "Gelegenheit": "Opportunity",
    "Gelegenheiten": "Opportunities",
    "verbleibend": "left",
    "Zuerst bei": "First at",
    "Stehen": "Standing",
    "Verbleibende Zeit im Fenster ab dem Ursprungsbeitrag":
        "Time left in the window that runs from the origin piece",
    "Text(e) ansehen": "text(s) to view",
    # The cap's visible name: never more than the capped number of cards per
    # mandate, cut by pickup count, and the cut says so instead of happening
    # silently. Split around the number so the sentence carries the cap the
    # route actually applies (``today._MAX_OPEN_OPPORTUNITIES``) rather than a
    # hardcoded "drei" that lies the day the constant changes.
    "weitere Gelegenheit(en) bei": "more opportunit(y/ies) for",
    "nach Aufgriffszahl gekürzt; hier stehen die":
        "trimmed by pickup count; the",
    "mit den meisten Aufgriffen.": "with the most pickups stand here.",
    # The mandate's archive: an expired or waved-off opportunity stays readable
    # there with its outcome, and only there.
    "Gelegenheiten aus der schnellen Spur": "Opportunities from the fast lane",
    "Verworfen am": "Waved off on",
    "Abgelaufen am": "Expired on",
    "Kein Text entstanden.": "No text came of it.",
    # The calendar. Spelled out rather than taken from ``locale``, for the reason
    # ``web.app`` spells them out: a de_DE locale is absent from most containers
    # and setlocale is process-global.
    "Januar": "January",
    "Februar": "February",
    "März": "March",
    "April": "April",
    "Mai": "May",
    "Juni": "June",
    "Juli": "July",
    "August": "August",
    "September": "September",
    "Oktober": "October",
    "November": "November",
    "Dezember": "December",
    "Jan": "Jan",
    "Feb": "Feb",
    "Mär": "Mar",
    "Apr": "Apr",
    "Jun": "Jun",
    "Jul": "Jul",
    "Aug": "Aug",
    "Sep": "Sep",
    "Okt": "Oct",
    "Nov": "Nov",
    "Dez": "Dec",
    # --- The reputation band on Heute (RIS-01, DEC-1 option B) ---------------
    # "Medien", "negativ", "krise" and "Mandanten" are already translated above
    # — the band reuses them rather than restating them.
    #
    # Every state and every direction is a *word* in both languages, never a
    # colour on its own: a band that says only "red" makes the reader supply the
    # sentence, and the sentence they supply is the one they already expected.
    "Reputationslage": "Reputation standing",
    # The singular to the "Medien" above: a braked single-outlet Beobachtung is
    # a common tile, and "1 Medien" / "1 outlets" is wrong in both languages —
    # the same branch the quiet line's Mandant/Mandanten already takes. Keyed
    # as the whole phrase because "Medium" is already the contacts form's
    # capitalised field label, and this one sits lowercase inside a line; the
    # count is part of the key, since one is the only count the singular has.
    "1 Medium": "1 outlet",
    # models.ReputationState members, keyed on the stored German value the same
    # way the categories above are. Capitalised at render, so the table holds
    # them exactly as the database does.
    "ruhig": "quiet",
    "beobachtung": "watch",
    "issue": "issue",
    "risiko": "risk",
    "steigend": "rising",
    "stabil": "stable",
    "fallend": "falling",
    "Richtung": "Trend",
    "überregional": "national reach",
    "namentlich genannt": "named",
    "keine Berichterstattung im Fenster": "no coverage in the window",
    # The why-line of a tile the crisis floor raised over a quiet reading:
    # coverage lay in the window and none of it was negative, so the counts the
    # other tiles carry ("N Medien · x/y negativ") have nothing to count. Said
    # as a sentence rather than as zeroes, because "0 Medien · 0/12 negativ"
    # beside a declared Krise is a broken line in both languages.
    "keine negative Berichterstattung im Fenster":
        "no negative coverage in the window",
    # The thirty is ``newspulse.reputation.BASELINE_READINGS``, spelled out here
    # in both languages because a sentence that says "its own median" without
    # saying over what is a claim the reader cannot check. Nothing in this file
    # can see that constant move, so ``test_reputation_band`` holds the pair
    # together — see
    # ``test_the_deviation_sentence_names_the_baseline_it_is_counted_over``.
    "über dem eigenen Median der letzten 30 Ablesungen":
        "above its own median of the last 30 readings",
    "Mandant ruhig": "client quiet",
    "Mandanten ruhig": "clients quiet",
    "davon ohne Berichterstattung": "of them with no coverage",
    # The quiet count's third number. A mandate the sweep has been failing on
    # keeps its last successful reading, and the count line says so rather than
    # folding it into today's calm — the tiles already carry their own date, and
    # this is the same honesty for the mandates that have no tile.
    "davon zuletzt gelesen vor dem": "of them last read before",
    # Only a mandate whose crisis was declared before it was ever swept: it has
    # a tile, and about the coverage it has nothing to say yet.
    "noch nicht abgelesen": "not read yet",
    # "Stand" is already the name of a KI-Sichtbarkeit band further up, and a
    # dictionary has one entry per key: this stamp needs its own noun anyway, and
    # "Ablesung vom" is the more exact one — it dates the reading rather than the
    # page it is rendered on.
    "Ablesung vom": "Reading of",
    "Gerechnet aus gespeicherten Zeilen, nicht geschätzt.":
        "Counted from stored rows, never estimated.",
    # --- Das Issue-Register (RIS-02, DEC-3 A / DEC-6 A) -----------------------
    # "Verwerfen", "Medien", "1 Medium", "Tage", "Tagen", "Speichern",
    # "Bearbeiten", "Krise erklären", "überregional", "Grund der Schließung:",
    # "Wirkung", "Vorschlag" and "Marktsignal" are already translated above —
    # the register reuses them.
    #
    # "Issues" stays "Issues" in both languages on purpose: it is the trade's
    # own word in German PR usage, and an invented translation would rename the
    # feature its users already have a name for.
    "Issues": "Issues",
    "Issue?": "Issue?",
    "Issue eröffnen": "Open an issue",
    # The offer names what the repetition consists of, because "the tool thinks
    # something is up" is not a sentence anyone can accept or refuse.
    "Dieselbe Sache an": "The same matter on",
    "Berichterstattung und datiertes Marktsignal derselben Sache:":
        "Coverage and a dated market signal of the same matter:",
    "Vorgeschlagen, nicht eröffnet. Bis jemand hier drückt, ändert sich nichts.":
        "Proposed, not opened. Nothing changes until somebody presses this.",
    "Offene Issues": "Open issues",
    "Kein offenes Issue. Nichts wird getragen.":
        "No open issue. Nothing is being carried.",
    "Tag alt": "day old",
    "Tage alt": "days old",
    "letzte Bewegung": "last movement",
    "Signal": "signal",
    "Signale": "signals",
    "eröffnet von": "opened by",
    "Owner": "Owner",
    "niemand benannt": "nobody named",
    "Frühindikatoren": "Early indicators",
    "Beschreibung": "Description",
    # The two graded values carry the person who set them, in both languages:
    # a value without a name would read as a measurement, and it is an opinion.
    "Wahrscheinlichkeit": "Probability",
    "gesetzt von": "set by",
    "noch nicht gesetzt": "not set yet",
    "Werte setzen": "Set values",
    "angehängt von": "attached by",
    "Grund: warum wird es geschlossen?": "Reason: why is it being closed?",
    "Issue schließen": "Close the issue",
    "Heatmap": "Heatmap",
    # The named column beside the field. "Not graded" is a statement of its
    # own, never a coordinate — an ungraded issue at the field's origin would
    # claim "harmless", and nobody made that claim.
    "Ohne Bewertung": "Not graded",
    "keins": "none",
    "Frühere Issues": "Past issues",
    "eskaliert": "escalated",
    "geschlossen": "closed",
    "geschlossen von": "closed by",
    "zur Krise": "to the crisis",
    "Der Wert liegt im Anhängen, nicht im Anlegen: derselbe Vorwurf am Montag und am Freitag ist eine Zeile mit einem Alter, einer letzten Bewegung und einer Zahl. Eskaliert ein Issue, übernimmt die Krise seine Signale und seinen Beginn.":
        "The value is in the attaching, not the opening: the same accusation on Monday and on Friday is one row with an age, a last movement and a number. When an issue escalates, the crisis takes over its signals and its beginning.",
    # The handover label on the crisis timeline (rendered by ``crisis_view``,
    # written in Python, so no template scan can see it).
    "Issue eröffnet": "Issue opened",
    # The register's refusals and notes, written in Python and rendered as
    # ``note`` — the same reason the asset notes further up are listed here:
    # nothing that reads the templates for German strings can see them.
    "Der Vorschlag stand nicht mehr: es wurde kein Issue eröffnet.":
        "The proposal no longer stood: no issue was opened.",
    "Das Issue wurde nicht geschlossen: es fehlt die Begründung.":
        "The issue was not closed: the reason is missing.",
    "Ein geschlossenes Issue eskaliert nicht.":
        "A closed issue does not escalate.",
    "Ohne Beitrag als Signal lässt sich keine Krise erklären: eine Krise braucht den Beitrag, an dem sie hängt.":
        "Without an article among its signals no crisis can be declared: a "
        "crisis needs the article it hangs on.",
    "Für dieses Mandat läuft bereits eine Krise; das Issue eskaliert nicht in eine fremde Krise.":
        "A crisis is already running for this mandate; the issue does not "
        "escalate into an unrelated one.",
    "Ein eskaliertes Issue wird über seine Krise geschlossen.":
        "An escalated issue is closed through its crisis.",
    # --- Die Stakeholder-Karte (RIS-03) ---------------------------------------
    # "Speichern", "Bearbeiten", "gesetzt von" and "Verwerfen" are already
    # translated above — the map reuses them. "Stakeholder" stays untranslated
    # in the compounds for the same reason "Issues" does: it is the trade's own
    # word in German PR usage.
    "Stakeholder-Karte": "Stakeholder map",
    "Aus dem Profil vorschlagen": "Propose from the profile",
    "Die Karte steht am Mandat, nicht am Anlass. Vorgeschlagen wird aus dem Profil, bearbeitet von einem Menschen, und jede Zeile zeigt, wer sie gesetzt hat.":
        "The map hangs on the mandate, not on the occasion. It is proposed "
        "from the profile, edited by a person, and every row shows who set it.",
    "Gruppe": "Group",
    "Betroffenheit": "How it is affected",
    "Einfluss": "Influence",
    # The three levels are values a person picks, so they read as words in
    # both languages — a number would claim a measurement nobody took.
    "hoch": "high",
    "mittel": "medium",
    "niedrig": "low",
    "Ansprechpartner": "Contact person",
    "Kanal": "Channel",
    # The named gap: the most important row of the map, never a blank cell.
    "Kein Ansprechpartner benannt.": "No contact person named.",
    "Im Profil nachtragen": "Add it in the profile",
    "Zum Profil": "To the profile",
    "Gruppe hinzufügen": "Add a group",
    "Zeile entfernen": "Remove the row",
    "Noch keine Karte. Aus dem Profil vorschlagen oder eine Gruppe von Hand anlegen.":
        "No map yet. Propose it from the profile or add a group by hand.",
    "Für dieses Mandat sind keine Profilangaben hinterlegt; ohne sie wird keine Karte erfunden.":
        "No profile entries are on file for this mandate; without them no map "
        "is invented.",
    "Stakeholder-Auswahl": "Stakeholder selection",
    # The order is a recommendation until a person sorts it, and the marker
    # says which of the two the reader is looking at.
    "Reihenfolge": "Order",
    "Reihenfolge: Empfehlung": "Order: recommendation",
    "Reihenfolge gesetzt von": "Order set by",
    "Reihenfolge speichern": "Save the order",
    "Stakeholder auswählen": "Select stakeholders",
    "Will wissen:": "Wants to know:",
    # The honest empty sentence: where no stored line supports one, none is
    # invented.
    "keine gespeicherte Angabe, aus der sich das ergibt":
        "no stored line this could rest on",
    # The map's and the selection's notes, written in Python and rendered as
    # ``stakeholder_note`` / ``note`` — nothing that reads the templates for
    # German strings can see them.
    "Ohne Profilangaben wird keine Karte erfunden. Erst das Profil füllen, dann trägt der Vorschlag.":
        "Without profile entries no map is invented. Fill the profile first, "
        "then the proposal has something to stand on.",
    "Der Vorschlag hat keine neuen Gruppen ergeben.":
        "The proposal yielded no new groups.",
    "Die Zeile wurde nicht gespeichert: es fehlt die Gruppe, oder der Name steht schon auf der Karte.":
        "The row was not saved: the group is missing, or the name is already "
        "on the map.",
    "Keine Auswahl entstanden: ohne Karte oder ohne begründbar betroffene Gruppe wird nichts gespeichert.":
        "No selection was made: without a map, or without a group whose "
        "involvement can be justified, nothing is stored.",
    "Die Reihenfolge wurde nicht gespeichert: das Formular war unvollständig.":
        "The order was not saved: the form was incomplete.",
    "Die Reihenfolge wurde nicht gespeichert: sie nennt nicht genau die Zeilen der Auswahl.":
        "The order was not saved: it does not name exactly the rows of the "
        "selection.",
    "Die Reihenfolge wurde nicht gespeichert: zwei Zeilen tragen dieselbe Nummer.":
        "The order was not saved: two rows carry the same number.",
    "Die Auswahl ist fehlgeschlagen. Die Einzelheiten stehen im Log.":
        "The selection failed. The details are in the log.",
    "Der Vorschlag ist fehlgeschlagen. Die Einzelheiten stehen im Log.":
        "The proposal failed. The details are in the log.",
    # The crisis page selects from the map but does not maintain it, so its
    # empty state is a named absence with the link to where it is filled in.
    "Noch keine Stakeholder-Karte für dieses Mandat.":
        "No stakeholder map for this mandate yet.",
    "Zur Stakeholder-Karte": "To the stakeholder map",
    # The card's model calls run on a worker thread behind one lock, so both
    # the refused second click and the run in flight have to be sayable.
    "Es läuft schon eine Anfrage für dieses Mandat. Ein zweiter Klick würde eine zweite kosten.":
        "A request for this mandate is already running. A second click would "
        "spend a second one.",
    "Die Anfrage läuft — die Karte aktualisiert sich, sobald sie steht.":
        "The request is running — the map refreshes as soon as it stands.",
    # The way back into a selection that already stands: it appends only, so
    # its empty answer is not the same sentence as "no selection at all".
    "Um neue Gruppen ergänzen": "Add newly mapped groups",
    "Die Auswahl wurde nicht ergänzt: keine weitere Gruppe der Karte ist begründbar betroffen.":
        "The selection was not added to: no further group on the map is "
        "affected in a way that can be stated.",
    "Aus der Auswahl nehmen": "Remove from the selection",
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
