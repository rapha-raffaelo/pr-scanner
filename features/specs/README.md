# Spezifikationen für RauteOS v1

Lucas' Spezifikationen 01–08, wörtlich, plus die Mockups dazu. Sie sind die
Vorlage für den Umbau auf React und stehen hier, damit sie durchsuchbar,
versioniert und für Remy lesbar sind — nicht nur in einem Chatverlauf.

## Der Kernworkflow

Alles darunter dient dieser einen Kette:

```
Client anlegen
  → Digital Twin aufbauen
  → Positionierung und Kommunikationskontext kennen
  → Außenwelt überwachen
  → Opportunity automatisch erkennen ODER aktiv entwickeln lassen
  → erklären, warum sie für den Kunden relevant ist
  → Medium auswählen
  → Journalisten aktuell recherchieren
  → erklären, warum Medium und Journalist passen
  → Maßnahme auswählen
  → Kundenmail / Pitch / Statement erstellen
  → Facts und Claims prüfen
  → als Action speichern
  → Action durchführen
  → neues Wissen zurück in Twin und Journalist Intelligence
```

## Die Spezifikationen

| Nr. | Titel | Datei |
|-----|-------|-------|
| 01 | Client Twin | [01-client-twin.md](01-client-twin.md) |
| 02 | Desk | [02-desk.md](02-desk.md) |
| 03 | Ask RAUTE | [03-ask-raute.md](03-ask-raute.md) |
| 04 | Opportunity Intelligence | [04-opportunity-intelligence.md](04-opportunity-intelligence.md) |
| 05 | Opportunity Workspace | [05-opportunity-workspace.md](05-opportunity-workspace.md) |
| 06 | Generate Asset / Activation | [06-asset-activation.md](06-asset-activation.md) |
| 07 | Media & Journalist Intelligence | [07-journalist-intelligence.md](07-journalist-intelligence.md) |
| 08 | Quality / Evidence Light | [08-quality-evidence.md](08-quality-evidence.md) |

## Navigation für Version 1

Bewusst klein gehalten. Die Engines darunter bleiben unsichtbar — sie sind
Logik, keine Bildschirme.

**Global:** Desk · Clients · Assets · Ask RAUTE
**Im Mandanten:** Overview · Opportunities · Twin · Actions · Ask RAUTE · Develop Opportunity

## Gestaltung

Weiß, nicht grün. Das Grün aus dem Client-Overview-Mockup ist die Farbe des
Beispielmandanten (Carbon Farming), nicht die des Werkzeugs.

## Mockups

Unter `mocks/`. Die Nummern folgen den Spezifikationen; `00-` sind allgemeine
Referenzen.
