# 02 raute + kopf Desk

> Wenn wir morgens ins System gehen, brauchen wir keinen Newsfeed oder eine
> Artikelliste, sondern ein echtes Arbeitsdashboard.
>
> Also ungefähr:
> - Wie viele aktive Kunden haben wir?
> - Wie viele Actions haben wir diese Woche oder diesen Monat für sie gemacht?
>   (Vertraglich zugesicherte Aktionen vs. bereits gelieferte Aktionen)
> - Bei welchen Kunden passiert gerade etwas Relevantes?
> - Wie viele neue Opportunities wurden erkannt?
> - Was benötigt meine/unsere Entscheidung?
> - Wo laufen gerade Aktionen und was ist der aktuelle Stand?
> - Bei welchen Twins fehlen wichtige Informationen?
>
> Beispiel:
> ```
> Carbon Farming
> Twin 84 % komplett
> 6 Actions diesen Monat
> Vertraglich zugesichert pro Quartal: 10, durchgeführt: 12
> 3 aktuelle Opportunities
> ```

## Stand

**Gebaut** (PR #24). Fünf Kennzahlen, Mandantenüberblick mit Twin-Prozent,
Actions im Monat, Vertrag gegen Lieferung, Opportunities und Status; darunter
„Braucht Entscheidung", „In Arbeit" und „Twins mit Wissenslücken".

Drei Festlegungen, die dabei getroffen wurden:

- Eine Action ist eine **Auslieferung** (`released_at`), kein Entwurf.
- Dazu ein Nachtragsbuch für Arbeit außerhalb des Werkzeugs — sonst ist die
  Zahl gegen eine Vertragszusage systematisch zu niedrig.
- Kein hinterlegter Vertragswert heißt **unvermessen**, nicht „auf Kurs".

## Offen

Die Spalte „RAUTE Intelligence" aus dem Mockup — strategische Hinweise für
heute. Braucht Modellaufrufe und eine eigene Festlegung, was ein Hinweis ist
und wann er entsteht.
