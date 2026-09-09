# 08 Quality / Evidence Light

> Die komplette Quality & Verification Engine müssen wir am Anfang nicht bauen,
> aber ein Minimum brauchen wir schon. Bevor wir ein Asset verwenden, sollte das
> OS prüfen:
>
> - Welche Aussagen sind harte Fakten?
> - Welche sind nur Client Claims?
> - Welche Aussagen haben Quellen?
> - Welche Quellen sind veraltet?
> - Welche Aussagen sind unverified?
> - Gibt es Widersprüche zum Twin?
> - Verstoßen wir gegen ein No-Go?
>
> Beispiel mit CFG:
> > Claim: 400 % THG-Minderung
> > Warning: Client Statement
> > Independent verification not available
> > Use with caution
>
> Das reicht für Version 1 vollkommen.
>
> Ergebnisse und große Performance-Messung würde ich in der ersten Version
> bewusst noch nicht bauen. Aber wir müssen tracken, was wir gemacht haben.
> Sobald ich eine Opportunity aktiviere, entsteht eine Action:
>
> ```
> Carbon Farming
> Rapid Response FuelEU
> 08.09.2026
> Kundenmail erstellt
> Pitch erstellt
> 5 Journalisten ausgewählt
> ```
>
> Das ist wichtig, damit wir nicht jede Woche dieselbe Idee produzieren oder
> denselben Journalisten mit derselben Geschichte angehen.

## Was heute schon steht

- Zweitmodell-Review je Anschreiben (`review`, `review_ok`) und Guide-Prüfung
- `overclaim`-Feld am Impuls
- Nachtragsbuch für Aktionen (`ClientAction`), gezählt gegen die Vertragszusage
- Die Empfängerliste datiert, an wen zu diesem Impuls schon geschrieben wurde

## Was fehlt

- Klassifikation **Aussage für Aussage** über einem fertigen Text — setzt die
  Herkunftsklassen aus 01 voraus
- Twin-Abgleich und No-Go-Prüfung als eigene Schritte
- Die Action-Historie **an der Opportunity** (heute ist `ClientAction` flach am
  Mandanten, nicht unter einer Kampagne gruppiert)
