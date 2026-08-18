# FuPa-Spielberichte

Stand der Bestandsanalyse: 18. August 2026

## Bestehende Architektur

Die Erweiterung verwendet die vorhandenen, mandantenfähigen Bausteine und führt
keinen parallelen Vereins- oder Berechtigungsweg ein:

- `Game` und `Team` sind direkt einem `Club` zugeordnet. `MatchEvent` bildet
  bestätigte Live-Ereignisse append-only und mit Idempotenzschlüssel ab.
- Der Live-Center-Webhook ordnet WhatsApp-Nachrichten anhand der unveränderlichen
  Meta-Konto- und Telefonnummer-IDs einem Verein zu und prüft die Webhook-Signatur.
- Rollen und Mannschaftsrechte werden serverseitig über `require(...)` geprüft.
- Hintergrundarbeit läuft im vorhandenen Worker; automatische Funktionen sind
  über Einstellungen und den globalen Not-Aus begrenzt.
- Kreative Beispiele und gelernte Präferenzen sind bereits versioniert und
  mandantengebunden. Für Spielberichte wird zusätzlich ein textbezogenes,
  nachvollziehbares Vereinsprofil geführt.
- Generierte Beiträge, Freigaben und Veröffentlichungsaufträge besitzen bereits
  Versions-, Audit- und Idempotenzmechanismen. Spielberichte bleiben fachlich
  davon getrennt, da sie längere redaktionelle Texte und eine andere Zielplattform
  besitzen.

## Risiken und Schutzmaßnahmen

1. **FuPa ist eine externe HTML-Quelle.** Seitenstruktur und dynamisch geladene
   Inhalte können sich ändern. Der Reader verwendet eine Host-Allowlist,
   begrenzte Timeouts, Größenlimits, defensive Parser und persistiert nur
   normalisierte Daten samt Prüfsumme und Herkunft.
2. **Quellen können widersprechen.** Strukturierte FuPa-Daten haben Vorrang vor
   dem FuPa-Ticker, anschließend folgen manuelle Vereinsangaben und zuletzt
   WhatsApp-Rückmeldungen. Widersprüche werden nicht still aufgelöst, sondern
   blockieren Freigabe und automatische Veröffentlichung.
3. **Freitext kann falsche Tatsachen enthalten.** Das Sprachmodell erhält einen
   strukturierten Faktenblock und muss ein streng validiertes Ergebnis liefern.
   Namen, Spielstände und Ereignisse werden gegen den Kontext geprüft.
4. **WhatsApp-Antworten können verspätet oder mehrdeutig sein.** Jede Anfrage hat
   eine eindeutige Zuordnung, Frist und Status. Keine Antwort blockiert die
   Berichtserstellung nicht; unzuordenbare Antworten werden nicht geraten.
5. **FuPa-Schreibzugriff.** Es ist keine stabile, dokumentierte Schreib-API Teil
   der Anwendung. Daher existiert eine austauschbare Publisher-Schnittstelle,
   aber automatische FuPa-Veröffentlichung bleibt standardmäßig und produktiv
   deaktiviert. Es gibt keine CAPTCHA-Umgehung und keine versteckte
   Browser-Automatisierung.

## Datenfluss

1. Ein Verein hinterlegt für ein Spiel optional eine FuPa-Spiel-URL.
2. Der Worker liest nach Spielende eine FuPa-Momentaufnahme und wiederholt
   fehlende Ergebnisse mit begrenztem Backoff.
3. Bestätigte Live-Center-Ereignisse, manuelle Notizen, Branding, Textbeispiele
   und fristgerecht eingegangene WhatsApp-Rückmeldungen werden in einem
   `MatchContentContext` zusammengeführt.
4. Der Konfliktprüfer erzeugt eine Quellen- und Konfliktübersicht.
5. Der Generator erstellt Überschrift, Anreißer und Bericht. Jede Überarbeitung
   erzeugt eine unveränderliche neue Version.
6. Freigeber oder Administratoren prüfen Quellen und Konflikte und geben genau
   eine Version frei. Automatische Freigabe und FuPa-Veröffentlichung sind aus.

## Statusmodell

- Bericht: `collecting`, `waiting_feedback`, `ready`, `generating`, `review`,
  `conflict`, `approved`, `publishing`, `published`, `failed`, `cancelled`
- FuPa-Abruf: `pending`, `available`, `retry`, `exhausted`, `failed`
- Rückfrage: `pending`, `sent`, `answered`, `expired`, `failed`, `cancelled`
- Veröffentlichung: `draft`, `scheduled`, `publishing`, `published`, `failed`,
  `cancelled`

## Migration und Rückwärtskompatibilität

Migration `0032` ergänzt ausschließlich neue Tabellen und optionale FuPa-Felder
an Mannschaft und Spiel. Bestehende Spiele und Live-Ereignisse bleiben
unverändert. Ohne FuPa-URL ist die neue Automatik inaktiv. Beim Downgrade werden
nur die neuen Tabellen und Spalten entfernt.

## Betrieb

Die Funktion ist standardmäßig deaktiviert:

```env
FUPA_REPORTS_ENABLED=false
FUPA_REPORT_AUTOMATIC_GENERATION_ENABLED=false
FUPA_REPORT_AUTOMATIC_PUBLISH_ENABLED=false
```

Vor einer Aktivierung müssen Datenschutz, FuPa-Nutzungsbedingungen, Abrufrate,
Vereinsrollen und der gewünschte redaktionelle Freigabeprozess geprüft werden.
`FUPA_REPORT_AUTOMATIC_PUBLISH_ENABLED` bleibt ohne ausdrücklich installierten
und geprüften Publisher wirkungslos.

## Verbleibende Grenze

Das Lesen öffentlich erreichbarer FuPa-Spielseiten wird defensiv unterstützt.
Ein stabiler automatischer Schreibzugriff auf FuPa wird nicht behauptet. Der
Standard-Publisher kennzeichnet Berichte deshalb als manuell zu übertragen und
stellt Text sowie Quellenübersicht für die redaktionelle Arbeit bereit.
