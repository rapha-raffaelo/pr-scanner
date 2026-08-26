# Die vier Uhren: die Krise, das Fenster, der Plan und der Rückblick

prefix: UHR

**Type:** feature
**Complexity:** 5
**Estimated Duration:** ~3 Wochen
**Risk:** medium
**Scope:** newspulse, krise, newsjack, plan, reporting
**Test Strategy:** Unit-Tests über die vier Motoren gegen die bestehende In-Memory-SQLite-Fabrik, mit eingespeister Uhr statt gepatchtem Zeitmodul, so dass jede Frist und jedes Fenster deterministisch prüfbar ist; die Stufenrechnung der Krise, die Vorjahresrechnung des Plans und die Ursprungsbestimmung einer Story werden gegen von Hand gezählte Fixtures geprüft und nie gegen sich selbst; ein Test je Format, dass ein fehlendes Pflichtfeld zur Verweigerung führt statt zu einer erfundenen Person oder Zahl, weil das die teuerste Fehlleistung dieser Funktion wäre; ein Strukturtest über die neuen Prompt-Dateien, dass sie ihre Blöcke komponieren und keinen davon ausschreiben; `TestClient`-Abdeckung für die Krisenseite, die schnelle Spur, den Plan und den Pressespiegel, jeweils inklusive des leeren Zustands; Golden-File-Tests auf den gerenderten Pressespiegel eines gesäten Monats. Kein Test ruft ein Modell auf und kein Test geht ins Netz.

## Context

Das Werkzeug kann inzwischen fast alles, was eine Agentur an einem Dienstagvormittag
tut. Es findet die Berichterstattung, ordnet sie zu, bewertet sie, schlägt eine
Position vor, sucht die Journalisten dazu, schreibt sieben Formate, lässt sie
gegenprüfen, führt das Ausgangsbuch, misst den Anteil an der Stimme, schreibt den
Monatsbericht und misst seit Kurzem, was ein Assistent über den Markt sagt.

Alles davon spielt in einer einzigen Zeitschicht: heute. Der Sweep läuft um 06:10,
die Seite heißt Heute, der Bericht schaut auf den Monat zurück, der gerade vorbei
ist. Was fehlt, sind die anderen Uhren, in denen dieselbe Arbeit läuft.

**Die schnellste Uhr ist die Krise.** Es gibt eine Kategorie `krise`, eine
Alarmschwelle, ein Profilfeld `Krisenkontakt` und eine Benachrichtigung. Es gibt
keinen Krisenmodus. Wenn heute Morgen um 06:41 eine Verbraucherzentrale sechs
Solaranbieter abmahnt und der Mandant einer davon ist, dann steht das rot auf
Heute und danach ist der Berater allein. Kein Holding Statement in den ersten
Minuten, keine Q&A-Haltung für den Sprecher, keine Eskalationsliste, keine
Zeitleiste, die hinterher belegt, wann was entschieden wurde. Der Code weiß
selbst, dass das fehlt: in `assets.py` steht ein Kommentar, der eine Krise als
zweiten Aufrufer der Textproduktion vorsieht, den es noch nicht gibt.

**Die zweitschnellste Uhr ist das Fenster.** Die Positionierungsentwürfe sind gut
und sie sind langsam. Sie entstehen einmal am Morgen aus dem Themenradar, und sie
fragen nicht, ob das Mandat zu diesem Thema überhaupt Stehen hat. Eine Geschichte,
die um 11 Uhr bricht und um 18 Uhr durch ist, wird am nächsten Morgen um 06:10
gefunden. Dazu fehlen drei Dinge: wer die Geschichte zuerst hatte und ob die Welle
noch steigt, ob das Mandat etwas zu sagen hat, das es belegen kann, und wie lange
das noch gilt.

**Die langsamste Uhr ist der Plan.** Die Marktsignale kennen seit Kurzem Termine
in der Zukunft: eine Konsultation, die in fünf Wochen schließt, ein Call for
Speakers, der am Freitag zumacht, eine Verordnung, die im Januar in Kraft tritt.
Sie stehen einzeln im Marktumfeld und niemand setzt sie zu einem halben Jahr
zusammen. Dieselbe Information, plus die Themen mit gemessener Resonanz, plus das
Archiv des Vorjahres, ergibt genau das Dokument, das ein Kunde im Retainer-Gespräch
sehen will und das heute jedes Quartal von Hand in eine Tabelle getippt wird.

**Die vierte Uhr schaut zurück.** Der Monatsbericht sagt, was die Arbeit wert war,
mit Kennzahlen und Einordnung. Was er nicht ist, ist ein Pressespiegel: die
Berichterstattung selbst, nach Ereignis gruppiert, mit Aufgriffszahl und Tonalität,
im Layout des Mandanten. Das Gruppieren kann das Werkzeug längst, die Tonalität
steht an jeder Analyse, das Branding liegt vor. Es ist nie zu einem Dokument
zusammengesetzt worden.

Diese vier hängen enger zusammen, als sie klingen. Krise und Fenster teilen sich
denselben Takt-Apparat, weil beide einen zweiten, engeren Lauf brauchen. Fenster
und Plan teilen sich dieselbe Frage nach dem Stehen. Plan und Pressespiegel teilen
sich dasselbe Archiv, einmal vorwärts und einmal rückwärts gelesen. Deshalb stehen
sie in einem Dokument und nicht in vieren.

## Decisions

### DEC-1: Wer erklärt die Krise?  [options]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** die Regel, unter der eine Krise entsteht
- **A · Das Werkzeug schlägt vor, ein Mensch erklärt** Über der Schwelle erscheint ein Vorschlag auf Heute und in der Benachrichtigung. Sonst ändert sich nichts, bis jemand „Krise erklären“ drückt. Ein Fehlalarm kostet dann einen Klick und nicht einen ganzen Vormittag im Ausnahmezustand.
- **B · Das Werkzeug erklärt selbst** Über der Schwelle springt der Modus an, der Takt wird enger, das Holding Statement wird geschrieben. Schnell, aber jede falsche Krise verbraucht Geld und Vertrauen, und die tragende Zusage des Produkts ist, dass ein Mensch freigibt.

### DEC-2: Wie liest sich die Krisenseite?  [mock]
**Status:** locked
**Chosen:** C
**Recommend:** C
**Locks as:** das Layout, gegen das die Seite gebaut und geprüft wird
- **A · Die Zeitleiste** Eine Spalte, alles in zeitlicher Ordnung: der Auslöser, jeder neue Beitrag, jeder freigegebene Text, jede Anfrage. Während der Krise von oben zu lesen, danach von unten, und ohne Zusatzarbeit der Nachbericht.
  `features/mocks/krise-zeitleiste.html`
- **B · Der Lagebericht** Ein Brett aus Kacheln: Stufe, Verbreitung je Stunde, Tonalitätsverteilung, Eskalationsliste, unsere Texte. Beantwortet in drei Sekunden, wie es steht, und verliert dafür die Chronologie.
  `features/mocks/krise-lagebericht.html`
- **C · Zwei Spalten** Links, was über uns läuft, nach Story gruppiert. Rechts, was wir dagegen gesetzt haben. Dazwischen ein Kasten, der benennt, worauf noch nichts von uns läuft. Die Krise ist genau dieser Abstand, und die Seite zeigt ihn statt ihn ausrechnen zu lassen.
  `features/mocks/krise-zwei-spalten.html`

### DEC-3: Was passiert mit der Prüfung, wenn Minuten zählen?  [options]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** die Reihenfolge von Entwurf, Prüfung und Freigabe im Krisenfall
- **A · Der Text erscheint sofort, die Prüfung läuft nach** Der Entwurf steht in dem Moment auf dem Schirm, in dem er existiert, sichtbar als ungeprüft. Der Berater liest und kürzt, während Guide-Prüfung und Gegenprüfer laufen. Freigegeben werden darf erst, wenn die Guide-Prüfung geantwortet hat; der Gegenprüfer darf nachlaufen. Die Prüfung beißt dort, wo sie beißen muss, und blockiert nicht die Minuten davor.
- **B · Erst prüfen, dann zeigen** So, wie jedes andere Format heute funktioniert. Einheitlich und in einer Krise die falsche Reihenfolge: der Berater wartet auf zwei Modellaufrufe, bevor er den ersten Satz sieht.

### DEC-4: Woraus darf ein Haken im Plan bestehen?  [options]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** die Belegregel für den Redaktionsplan
- **A · Nur was belegt ist** Ein datiertes Marktsignal, ein Thema mit gemessener Resonanz, oder ein Monat, den das Archiv im Vorjahr getragen hat. Jeder Haken löst auf eine gespeicherte Zeile auf. Das Modell formuliert die Begründung und schlägt ein Format vor, es liefert kein Datum. Ein Plan mit einem erfundenen Termin ist ein Plan, den niemand ein zweites Mal prüft.
- **B · Auch was das Modell weiß** Zusätzlich wiederkehrende Termine aus dem Modellwissen: Messen, Fristen, Feiertage. Deckt mehr Monate ab und bringt genau die Sorte Datum ins Dokument, die stimmt, bis sie einmal nicht stimmt.

### DEC-5: Wie liest sich der Redaktionsplan?  [mock]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** das Layout, gegen das die Seite gebaut und geprüft wird
- **A · Monat für Monat** Ein Stapel Monate, Haken darin, mit Datum, Beleg und einem Knopf. Der Monat ist die Einheit, in der eine Agentur ohnehin arbeitet, und ein leerer Monat ist eine Aussage statt einer Lücke im Layout.
  `features/mocks/plan-monate.html`
- **B · Themen als Bahnen** Sechs Monate quer, eine Zeile je Thema. Zeigt auf einen Blick, welches Thema trägt und wo sich Termine stapeln. Braucht Breite und wird beeindruckend aussehen, bevor es benutzt wird.
  `features/mocks/plan-bahnen.html`
- **C · Die nächste Sache zuerst** Eine Liste nach Datum, das Fällige oben mit der Zahl der verbleibenden Tage. Kein Kalenderbild, nichts Dekoratives, dafür kein Gefühl für die Form eines halben Jahres.
  `features/mocks/plan-liste.html`

### DEC-6: Wie schnell ist die schnelle Spur wirklich?  [options]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** der Takt, in dem Gelegenheiten und Krisen gefunden werden
- **A · Ein zweiter, leichter Lauf alle drei Stunden** Liest nur den Themenradar der aktiven Mandate, analysiert keine Mandantenberichterstattung nach, schreibt keine Profildaten. Ein Modellaufruf fällt erst an, wenn eine Story mindestens zwei Medien trägt, also selten. Damit ist eine Gelegenheit im Schnitt nach anderthalb Stunden auf dem Schirm statt nach zwanzig.
- **B · Nur der Morgenlauf** Kostet nichts und findet ein Fenster meistens, nachdem es zu ist.
- **C · Auf Knopfdruck** Der Berater drückt „Jetzt prüfen“, wenn etwas passiert. Setzt voraus, dass er es schon weiß, und genau das ist die Arbeit, die abgenommen werden soll.

## Stories

### UHR-01: Die Krise als Objekt, und der engere Takt

Eine Krise ist bisher ein rotes Kärtchen auf Heute. Sie wird hier zu einer Zeile,
die einen Anfang, eine Stufe, eine erklärende Person und ein Ende hat, und zu der
einzigen Bedingung im Werkzeug, unter der der Sweep seinen Takt ändert.

Die Stufe wird gerechnet, nicht geschätzt. Zahl der Medien, ob die Reichweite
national oder regional ist, der Anteil negativer Tonalität an der Story und ob das
Mandat namentlich genannt ist: alles vier steht schon in gespeicherten Zeilen. Ein
Modell, das eine Krisenstufe schätzt, liefert eine Zahl, die niemand nachrechnen
kann, und in genau der Stunde, in der jemand sie nachrechnen möchte.

Der engere Takt ist die einzige Nebenwirkung einer Krise, und er ist eng begrenzt:
er liest die Quellen des betroffenen Mandats und sonst nichts. Ein Krisenlauf legt
keine Positionierungsentwürfe an, macht keine Profilarbeit und rührt kein anderes
Mandat an. Der Zustand steht in der Tabelle, nicht im Gedächtnis des Threads, damit
ein Neustart weder eine Krise verliert noch eine zweite anlegt.

**Decisions:** DEC-1

**Acceptance:**
  - Ein Vorschlag entsteht, wenn eine Analyse die Kategorie `krise` mit Importance 8 oder höher trägt, oder wenn drei Medien innerhalb von 24 Stunden dieselbe Story mit negativer Tonalität tragen
  - Ein Vorschlag ändert sonst nichts: kein engerer Takt, kein Text, keine zusätzliche Benachrichtigung über die bestehende Alarmierung hinaus
  - `declare()` schreibt eine Krise mit auslösendem Artikel, erklärender Person und Zeitpunkt; `close()` verlangt einen Grund, setzt den Endzeitpunkt und lässt die Zeile lesbar stehen
  - Je Mandant ist höchstens eine Krise offen; ein zweites `declare()` gibt die bestehende zurück, statt eine zweite anzulegen
  - Die Stufe von 1 bis 5 wird aus Medienzahl, Reichweite, Tonalitätsanteil und namentlicher Nennung gerechnet, nie von einem Modell, und ein Test prüft sie gegen ein von Hand gerechnetes Fixture
  - Solange eine Krise offen ist, läuft der Sweep alle `NEWSPULSE_CRISIS_SWEEP_MINUTES` Minuten (Vorgabe 60) und liest ausschließlich die Quellen des betroffenen Mandats
  - Ein Krisenlauf legt keine Positionierungsentwürfe an und schreibt keine Profildaten
  - Ein Absturz während eines Krisenlaufs hinterlässt keine hängende Krise und keinen doppelten Lauf: der Zustand wird aus der Tabelle gelesen

**Files:** `newspulse/src/newspulse/crisis.py` (new), `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0037_crisis.py` (new), `newspulse/src/newspulse/config.py`, `newspulse/src/newspulse/job.py`, `newspulse/src/newspulse/web/scheduler.py`, `newspulse/tests/test_crisis.py` (new), `newspulse/tests/test_migration.py`

### UHR-02: Das Holding Statement und die Q&A-Haltung

Zwei neue Formate auf dem bestehenden Formatvertrag, und die einzigen beiden im
Werkzeug, bei denen Minuten zählen.

Ein Holding Statement ist der schwerste kurze Text im Fach, weil fast alles daran
ein Fehler sein kann: eine Zahl, die sich als falsch herausstellt, ein zugesagter
Zeitpunkt, der nicht gehalten wird, eine Schuldzuweisung, die morgen zitiert wird.
Deshalb bekommt es einen eigenen Standardblock in der Wissensschicht, und dieser
Block gilt für beide Formate: keine Zahl, die nicht in einer belegten Quelle steht,
kein zugesagter Zeitpunkt, keine Schuldzuweisung, und der ausdrückliche Satz,
worauf gerade geprüft wird. Ein Block statt zwei ausgeschriebener Prompts, damit
eine Änderung an dieser Haltung an einer Stelle passiert.

Die Q&A-Haltung ist das, was der Sprecher in der Hand hat, wenn das Telefon
klingelt. Sie darf offen bleiben: eine Frage ohne belegte Antwort trägt ein
ausdrückliches „noch offen“ mit Begründung. Das ist der ganze Wert des Formats.
Eine geratene Antwort in einem Krisenbriefing ist schlimmer als eine Lücke.

**Depends on:** UHR-01
**Decisions:** DEC-3

**Acceptance:**
  - Ein Holding Statement entsteht aus Guide, Profil (Sprecher und Krisenkontakt) und den Beiträgen, die zur Krise zählen, und nennt keine Zahl und keinen Zeitpunkt, die nicht in einer dieser Quellen stehen
  - Fehlt ein Pflichtfeld, etwa ein Sprecher im Profil, wird der Text verweigert und das fehlende Feld benannt, statt eine Person zu erfinden; der vorherige Text zu diesem Format bleibt unberührt stehen
  - Die Q&A-Haltung liefert zwischen sechs und zwölf Fragen, jede mit einer belegten Antwort oder einem ausdrücklichen „noch offen“ samt Begründung
  - Der Block `crisis_discipline` steht in beiden Prompts und ist in keinem von beiden ausgeschrieben; der bestehende Strukturtest über die Prompt-Dateien deckt das ab
  - Beide Texte hängen über `Asset.crisis_id` an der Krise und sind darüber auffindbar
  - Der Entwurf wird gespeichert und angezeigt, bevor eine Prüfung gelaufen ist, und trägt sichtbar den Zustand ungeprüft
  - Eine Freigabe ist gesperrt, solange die Guide-Prüfung nicht geantwortet hat; ein noch laufender Gegenprüfer sperrt die Freigabe nicht
  - Eine Prüfung, die nicht laufen kann, verliert den bereits bezahlten Text nicht: er steht als ungeprüft da und nie als unbeanstandet

**Files:** `newspulse/src/newspulse/assets.py`, `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0038_crisis_assets.py` (new), `newspulse/src/newspulse/prompts/holding_statement.txt` (new), `newspulse/src/newspulse/prompts/krisen_qa.txt` (new), `newspulse/src/newspulse/blocks/crisis_discipline.txt` (new), `newspulse/src/newspulse/brain.py`, `newspulse/tests/test_crisis_assets.py` (new), `newspulse/tests/test_brain.py`

### UHR-03: Die Krisenseite

Die Seite, auf der ein Vormittag stattfindet, und danach das Dokument, mit dem er
begründet wird.

Sie erscheint nicht auf Vorrat. Ein Mandat, das nie eine Krise hatte, hat auch
keinen Krisenreiter; ein leerer Krisenreiter im Alltag ist eine tägliche kleine
Beunruhigung ohne Gegenwert. Der Vorschlag dagegen erscheint dort, wo der Morgen
sowieso beginnt: auf Heute, mit zwei Knöpfen.

Was fehlt, wird als Fehlendes gezeigt. Wenn im Profil kein Krisenkontakt steht,
dann ist das die wichtigste Information auf der Seite und nicht eine leere Zeile,
und sie verlinkt dorthin, wo sie nachgetragen wird.

**Depends on:** UHR-02
**Decisions:** DEC-1, DEC-2

**Acceptance:**
  - Die Seite entspricht dem in der Krisenseiten-Entscheidung festgelegten Mock
  - Der Reiter „Krise“ erscheint nur bei Mandaten, für die je eine Krise erklärt wurde
  - Ein Vorschlag steht auf Heute und auf der Mandantenkarte mit den Knöpfen „Krise erklären“ und „Verwerfen“; Verwerfen legt dieselbe Story für dieses Mandat still
  - Die Seite zeigt die Beiträge der Krise nach Story gruppiert mit Aufgriffszahl, und die Texte, die wir dazu freigegeben haben, mit ihrem Prüfzustand
  - Offene Anfragen aus dem verbundenen Postfach stehen mit ihrer Frist auf der Seite
  - Ein fehlender Krisenkontakt erscheint als benannte Lücke mit Link in den Kickoff, nicht als leere Zeile
  - Schließen verlangt einen Grund; die geschlossene Krise bleibt mit ihrer vollständigen Chronologie lesbar
  - Alle sichtbaren Zeichenketten liegen deutsch und englisch in `i18n.py` vor

**Files:** `newspulse/src/newspulse/web/routes/crisis_view.py` (new), `newspulse/src/newspulse/web/templates/client_crisis.html` (new), `newspulse/src/newspulse/web/templates/_client_tabs.html`, `newspulse/src/newspulse/web/app.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/src/newspulse/notify.py`, `newspulse/src/newspulse/web/static/app.css`, `newspulse/tests/test_crisis_view.py` (new)

### UHR-04: Stehen, Ursprung und das Fenster

Der Motor der schnellen Spur, und drei Fragen, die das Werkzeug bisher nicht
stellt.

Wer hatte es zuerst. Die Story-Gruppierung zählt heute Aufgriffe, benennt aber
nicht den Ursprung. Für eine Gelegenheit ist genau das die Zahl, die zählt: eine
Geschichte, deren erster Beitrag vier Stunden alt ist und die gerade ihr drittes
Medium bekommt, steigt noch. Eine, deren erster Beitrag von gestern ist, ist durch.

Hat das Mandat etwas zu sagen. Das ist die Frage, die die Positionierungsentwürfe
nicht stellen, und sie ist der Unterschied zwischen einem Beitrag und einer
Peinlichkeit. Sie wird gegen Profil, Guide und Archiv beantwortet und kennt drei
Antworten: belegt, dünn, keins. Nur „belegt“ erzeugt eine Gelegenheit.

Wie lange gilt das noch. Ein Fenster läuft ab dem Ursprungsbeitrag und schließt von
selbst, auch wenn nie wieder ein Lauf stattfindet. Eine Gelegenheit ohne Verfall
ist eine Aufgabenliste, die nur wächst.

Der leichte Lauf ist bewusst arm: Themenradar der aktiven Mandate, sonst nichts.
Ein Modellaufruf fällt erst an, wenn eine Story die Medienschwelle überschreitet,
womit die allermeisten Läufe nichts kosten.

**Depends on:** UHR-01
**Decisions:** DEC-6

**Acceptance:**
  - Eine Gelegenheit entsteht nur aus einer Story mit mindestens zwei Medien, in der das Mandat selbst nicht vorkommt
  - Der Ursprung einer Story ist ihr frühester Beitrag; spätere gelten als Aufgriffe, und bei gleicher Zeitangabe entscheidet die Abrufreihenfolge, nicht der Zufall
  - Das Stehen wird gegen Profil, Guide und Archiv geprüft und ist genau eine von drei Antworten; „dünn“ und „keins“ erzeugen keine Gelegenheit und werden mit Begründung verworfen gespeichert
  - Ein Fenster läuft `NEWSPULSE_NEWSJACK_WINDOW_HOURS` Stunden ab dem Ursprungsbeitrag (Vorgabe 36) und gilt danach als abgelaufen, auch ohne dass ein Lauf stattgefunden hat
  - Dieselbe Story wird je Mandat höchstens einmal zur Gelegenheit; ein zweiter Lauf legt nichts Neues an
  - Der leichte Lauf liest ausschließlich den Themenradar aktiver Mandate, analysiert keine Mandantenberichterstattung nach und schreibt keine Profildaten
  - Ein Lauf ohne Story über der Medienschwelle verbraucht keinen Modellaufruf
  - Der Block `standing` steht im Prompt und ist dort nicht ausgeschrieben

**Files:** `newspulse/src/newspulse/newsjack.py` (new), `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0039_newsjack.py` (new), `newspulse/src/newspulse/prompts/newsjack.txt` (new), `newspulse/src/newspulse/blocks/standing.txt` (new), `newspulse/src/newspulse/stories.py`, `newspulse/src/newspulse/config.py`, `newspulse/src/newspulse/job.py`, `newspulse/tests/test_newsjack.py` (new)

### UHR-05: Die schnelle Spur auf Heute

Eine Gelegenheit gehört nicht in einen eigenen Reiter, sondern über die
Tagesberichterstattung, weil sie genau dann wertlos wird, wenn man sie erst
suchen muss.

Die Karte trägt die verbleibende Zeit als Zahl, nicht als Farbe, und sie trägt in
einem Satz, worauf das Stehen des Mandats beruht. Beides zusammen ist die
Entscheidung, die der Berater in zehn Sekunden trifft: lohnt sich das, und habe
ich noch Zeit dafür.

Gedeckelt wird bewusst. Drei offene Gelegenheiten je Mandat sind eine Auswahl,
zehn sind wieder das Rauschen, gegen das dieses Werkzeug gebaut wurde. Wird
gekürzt, dann steht das dort, statt still zu geschehen.

**Depends on:** UHR-04
**Decisions:** DEC-6

**Acceptance:**
  - Eine offene Gelegenheit steht auf Heute über der Tagesberichterstattung, mit der verbleibenden Zeit als Zahl
  - Die Karte nennt Ursprungsbeitrag, Medium, Zahl der Aufgriffe und in einem Satz die Grundlage des Stehens
  - „Text schreiben“ öffnet die Formatauswahl mit der Gelegenheit als Anlass; der entstehende Text hängt an ihr und ist von ihr aus auffindbar
  - Eine abgelaufene Gelegenheit verschwindet von Heute und bleibt im Archiv des Mandats mit ihrem Ausgang lesbar
  - Verwerfen legt die Gelegenheit still, verlangt keine Begründung und lässt dieselbe Story für dieses Mandat nicht wiederkommen
  - Es stehen nie mehr als drei offene Gelegenheiten je Mandat auf Heute; darüber hinaus wird nach Aufgriffszahl gekürzt und die Kürzung sichtbar benannt
  - Ohne offene Gelegenheit sieht Heute genau so aus wie heute, ohne leeren Platzhalter
  - Alle sichtbaren Zeichenketten liegen deutsch und englisch in `i18n.py` vor

**Files:** `newspulse/src/newspulse/web/routes/today.py`, `newspulse/src/newspulse/web/templates/today.html`, `newspulse/src/newspulse/web/templates/partials/newsjack_card.html` (new), `newspulse/src/newspulse/web/routes/assets_view.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/src/newspulse/notify.py`, `newspulse/src/newspulse/web/static/app.css`, `newspulse/tests/test_newsjack_view.py` (new)

### UHR-06: Die Haken

Der Motor des Plans, und der Ort, an dem entschieden wird, dass ein Plan nichts
enthält, was nicht belegt ist.

Drei Quellen, alle drei schon vorhanden. Ein Marktsignal mit einem Datum in der
Zukunft ist der härteste Haken: die Verordnung tritt in Kraft, die Konsultation
schließt, der Call for Speakers läuft ab. Ein Thema mit gemessener Resonanz sagt,
worüber die Fachpresse dieses Mandats überhaupt schreibt. Und das Archiv des
Vorjahres sagt, in welchem Monat ein Thema getragen hat, was die einzige Quelle
für die wiederkehrenden Termine ohne festes Datum ist.

Das Modell schreibt hier nur Prosa. Es formuliert, warum ein Termin für dieses
Mandat einer ist, und schlägt ein Format vor. Datum und Beleg kommen aus der
Datenbank, und ein Haken, dessen Beleg nicht auflösbar ist, wird nicht gespeichert.

Neu berechnen darf nicht die Arbeit von Menschen wegwerfen. Ein Haken, den jemand
angenommen, verworfen oder in einen anderen Monat verschoben hat, überlebt jede
Neuberechnung; ersetzt wird nur, was noch niemand angefasst hat.

**Decisions:** DEC-4

**Acceptance:**
  - Ein Haken entsteht aus genau einer belegten Quelle: einem Marktsignal mit Datum in der Zukunft, einem Thema mit gemessener Resonanz, oder einem Vorjahresmonat mit getragener Berichterstattung
  - Jeder Haken trägt die Kennung der Zeile, aus der er stammt; ein Haken ohne auflösbaren Beleg wird nicht gespeichert
  - Ein Datum wird nie geraten: trägt die Quelle nur einen Monat, trägt der Haken einen Monat und keinen Tag
  - Das Modell liefert Begründung und Formatvorschlag und weder Datum noch Beleg; ein Test mit eingespeister Erzeugung prüft, dass ein vom Modell genanntes Datum verworfen wird
  - Neu berechnen ersetzt nur unangetastete Haken; angenommene, verworfene und verschobene überleben
  - Ein Monat ohne Beleg bleibt leer und wird als leer gespeichert, statt mit einem allgemeinen Thema gefüllt zu werden
  - Der Plan reicht sechs Monate ab dem laufenden Monat; ältere Haken fallen aus dem Plan, ohne gelöscht zu werden
  - Die Vorjahresrechnung ist in einem Test gegen ein von Hand gezähltes Archiv-Fixture geprüft

**Files:** `newspulse/src/newspulse/plan.py` (new), `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0040_plan_hooks.py` (new), `newspulse/src/newspulse/prompts/plan_hooks.txt` (new), `newspulse/src/newspulse/config.py`, `newspulse/src/newspulse/job.py`, `newspulse/tests/test_plan.py` (new)

### UHR-07: Der Redaktionsplan als Seite

Die Seite, die im Retainer-Gespräch auf dem Tisch liegt.

Jeder Haken zeigt seinen Beleg als Link auf die gespeicherte Zeile, weil das die
einzige Eigenschaft ist, die diesen Plan von einer hübschen Liste unterscheidet:
man kann jede Zeile anklicken und nachsehen, woher sie kommt.

Ein leerer Monat wird ausgeschrieben, nicht übersprungen. Und ein Mandat ohne
geprüfte Themen und ohne Marktsignale bekommt keinen leeren Plan, sondern den Satz,
was ihm fehlt, mit dem Link dorthin.

**Depends on:** UHR-06
**Decisions:** DEC-5

**Acceptance:**
  - Die Seite entspricht dem in der Redaktionsplan-Entscheidung festgelegten Mock
  - Jeder Haken zeigt Datum, Begründung, Herkunftsklasse und seinen Beleg als Link auf die gespeicherte Zeile
  - „Text schreiben“ öffnet die Formatauswahl mit dem Haken als Anlass und dem vorgeschlagenen Format vorausgewählt; der entstehende Text hängt am Haken
  - Verworfene und in einen anderen Monat verschobene Haken behalten ihren Zustand über eine Neuberechnung hinweg
  - Ein leerer Monat erscheint als Satz mit Link auf die Themen des Mandats
  - Ein Mandat ohne geprüfte Themen und ohne Marktsignale zeigt statt eines leeren Plans, was ihm fehlt
  - Der Plan ist als Dokument herunterladbar, und die heruntergeladene Fassung enthält keine Links zurück in die Anwendung
  - Alle sichtbaren Zeichenketten liegen deutsch und englisch in `i18n.py` vor

**Files:** `newspulse/src/newspulse/web/routes/plan_view.py` (new), `newspulse/src/newspulse/web/templates/client_plan.html` (new), `newspulse/src/newspulse/web/templates/_client_tabs.html`, `newspulse/src/newspulse/web/app.py`, `newspulse/src/newspulse/web/routes/assets_view.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/src/newspulse/web/static/app.css`, `newspulse/tests/test_plan_view.py` (new)

### UHR-08: Der Pressespiegel

Die Berichterstattung selbst als Dokument, und das kleinste Stück in diesem
Dokument, weil fast alles dafür schon vorliegt.

Gruppiert wird nach Story und nicht nach Datum, weil ein Kunde nicht vierzehn
Zeilen über dasselbe Ereignis lesen will, sondern eine Zeile mit der Zahl vierzehn
daran. Genau diese Gruppierung existiert bereits und wird hier zum ersten Mal in
etwas eingesetzt, das das Haus verlässt.

Kein Volltext, weil keiner gespeichert wird. Ein Pressespiegel aus Überschrift,
Medium, Datum, gespeicherter Zusammenfassung und Tonalität ist das, was diese
Datenlage hergibt, und er ist damit auch rechtlich der unproblematischere.

**Acceptance:**
  - Der Pressespiegel eines Zeitraums gruppiert die Beiträge nach Story und nennt je Story die Aufgriffszahl und das reichweitenstärkste Medium
  - Jeder Beitrag steht mit Medium, Datum, Überschrift, gespeicherter Zusammenfassung und Tonalität; kein Volltext erscheint im Dokument
  - Das Dokument trägt Logo und Farbe des Mandats aus dem bestehenden Branding und nennt den Zeitraum in der Kopfzeile
  - Ein Zeitraum ohne Berichterstattung ergibt ein Dokument mit einem erklärenden Satz statt einer leeren Seite
  - Die heruntergeladene Fassung enthält keine Links zurück in die Anwendung, die auf dem Bildschirm stehen
  - Der Dateiname nennt Mandat und Zeitraum in derselben Form, die der Bericht bereits bildet
  - Ein Golden-File-Test über einen gesäten Monat macht jede Formulierungsänderung als Diff prüfbar

**Files:** `newspulse/src/newspulse/clippings.py` (new), `newspulse/src/newspulse/web/routes/report.py`, `newspulse/src/newspulse/web/templates/press_clippings.html` (new), `newspulse/src/newspulse/reporting.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_clippings.py` (new), `newspulse/tests/fixtures/clippings/` (new)

## Deferred

- **Dark Site**: eine vorbereitete, im Krisenfall freischaltbare Themenseite. Gehört auf die Infrastruktur des Mandanten, nicht in dieses Werkzeug.
- **Automatische Freigabe eines Holding Statements**: nie. Die tragende Zusage des Produkts ist, dass ein Mensch liest, ändert und freigibt, und in einer Krise ist sie am meisten wert.
- **Krisen-Nachbericht als eigenes Dokument**: die Zeitleiste der geschlossenen Krise ist der Nachbericht. Ein zweites, generiertes Dokument darüber wäre eine Zusammenfassung von etwas, das schon lesbar ist.
- **Anfrageplattformen nach Art von HARO**: im deutschen Markt gibt es kein Äquivalent mit einer anbindbaren Schnittstelle. Anfragen kommen hier über das verbundene Postfach, und das ist gebaut.
- **Pressespiegel als gesetztes PDF mit Seitenumbrüchen**: der Druckdialog des Browsers auf ein sauber gesetztes Dokument reicht für die erste Fassung. Eine eigene PDF-Erzeugung kommt, wenn der Kunde Seitenzahlen verlangt.
- **Redaktionsplan über mehrere Mandate hinweg**: erst sinnvoll, wenn mehrere Mandate Haken tragen. Vorher ist es eine Ansicht auf eine leere Menge.
- **Wettbewerbskrise**: eine Krise beim Wettbewerber ist eine Gelegenheit, keine Krise, und wird von der schnellen Spur gefunden. Kein eigener Mechanismus.

## Summary

Heute endet das Werkzeug bei der Frage, was heute passiert ist. Bricht eine Krise
los, steht sie rot auf Heute und der Berater ist ab da allein: kein erster Text in
den ersten Minuten, keine Haltung für den Sprecher, keine Liste, wer nachts
erreichbar ist, und hinterher kein Beleg, wann was entschieden wurde. Danach gibt
es dafür eine Seite, auf der die Krise als Ganzes läuft, ein Holding Statement, das
in Minuten dasteht und nichts erfindet, eine Q&A-Haltung, die offene Fragen offen
nennt, und einen engeren Takt, der nur läuft, solange die Krise offen ist.

Zwischen den Tagen entsteht eine schnelle Spur. Eine Marktgeschichte, die vormittags
bricht, wird heute am nächsten Morgen gefunden, wenn sie durch ist; danach steht sie
innerhalb von Stunden auf Heute, mit der Angabe, wer sie zuerst hatte, wie lange sie
noch trägt und worauf sich das Mandat berufen kann, wenn es etwas dazu sagt.

Und die lange Sicht bekommt zum ersten Mal ein Dokument. Statt einzelner Termine im
Marktumfeld gibt es einen Redaktionsplan über sechs Monate, in dem jeder Termin
anklickbar auf die Zeile zurückführt, aus der er stammt, und in dem ein leerer Monat
als leer dasteht statt gefüllt zu werden. Dazu kommt der Pressespiegel: die
Berichterstattung eines Zeitraums nach Ereignis gruppiert, mit Aufgriffszahl und
Tonalität, im Layout des Mandanten, als Dokument, das das Haus verlassen kann.
