# FuPa-Spielberichte

Stand der Bestandsanalyse: 19. August 2026

## Bestehende Architektur

Die Erweiterung verwendet die vorhandenen, mandantenfähigen Bausteine und führt
keinen parallelen Vereins- oder Berechtigungsweg ein:

- `Game` und `Team` sind direkt einem `Club` zugeordnet. `MatchEvent` bildet
  bestätigte Live-Ereignisse append-only und mit Idempotenzschlüssel ab.
- WhatsApp bleibt über den bestehenden Meta-Kanal angebunden. Telegram ergänzt
  denselben fachlichen Rückfrageprozess über eine kleine providerneutrale
  Schnittstelle; beide Provider besitzen getrennte Webhook-, Verbindungs- und
  Sicherheitsprüfungen.
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
   eindeutig zugeordnete Messenger-Rückmeldungen. Widersprüche werden nicht still aufgelöst, sondern
   blockieren Freigabe und automatische Veröffentlichung.
3. **Freitext kann falsche Tatsachen enthalten.** Das Sprachmodell erhält einen
   strukturierten Faktenblock und muss ein streng validiertes Ergebnis liefern.
   Namen, Spielstände und Ereignisse werden gegen den Kontext geprüft.
4. **Messenger-Antworten können verspätet oder mehrdeutig sein.** Jede Anfrage
   hat Provider, Chat, Nachricht, Frist und Status. Antworten werden bevorzugt
   über die beantwortete Nachricht zugeordnet. Eine fehlende Antwort blockiert
   die Berichtserstellung nicht; unzuordenbare Antworten werden nicht geraten.
5. **FuPa-Schreibzugriff.** Es ist keine stabile, dokumentierte Schreib-API Teil
   der Anwendung. Der optionale Browser-Publisher benötigt deshalb zwingend ein
   vorhandenes berechtigtes FuPa-Konto und eine interaktiv erzeugte Anmeldung.
   Er meldet sich nie selbst an, speichert keine Zugangsdaten und umgeht weder
   CAPTCHA noch Zwei-Faktor-Prüfungen. Details stehen in
   [`FUPA_BROWSER_PUBLISHING.md`](FUPA_BROWSER_PUBLISHING.md).

## Datenfluss

1. Ein Verein hinterlegt für ein Spiel optional eine FuPa-Spiel-URL.
2. Der Worker liest nach Spielende eine FuPa-Momentaufnahme und wiederholt
   fehlende Ergebnisse mit begrenztem Backoff.
3. Bestätigte Live-Center-Ereignisse, manuelle Notizen, Branding, Textbeispiele
   und fristgerecht eingegangene WhatsApp- oder Telegram-Rückmeldungen werden in einem
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
an Mannschaft und Spiel. Migration `0033` ergänzt die providerneutralen
Messenger-Felder, Telegram-Endpunkte, einmalige Verknüpfungstokens und das
Webhook-Idempotenzledger. Bestehende WhatsApp-Kontakte werden als WhatsApp-
Endpunkte übernommen und behalten ihre bisherige Auswahl. Bestehende Spiele und
Live-Ereignisse bleiben unverändert. Ohne FuPa-URL ist die neue Automatik
inaktiv. Ein Downgrade von `0033` verweigert sich, sobald Telegram- oder andere
nicht mehr darstellbare providerneutrale Daten vorhanden sind.
Migration `0034` ergänzt ausschließlich den verschlüsselten, vereinsgebundenen
FuPa-Browserzustand. Bestehende Spielberichte bleiben unverändert.

## Betrieb

Die Funktion ist standardmäßig deaktiviert:

```env
FUPA_REPORTS_ENABLED=false
FUPA_REPORT_AUTOMATIC_GENERATION_ENABLED=false
FUPA_REPORT_AUTOMATIC_PUBLISH_ENABLED=false
FUPA_REPORT_FEEDBACK_WAIT_MINUTES=30
FUPA_BROWSER_PUBLISH_ENABLED=false
TELEGRAM_WEBHOOK_BASE_URL=https://meta.example.org
```

Vor einer Aktivierung müssen Datenschutz, FuPa-Nutzungsbedingungen, Abrufrate,
Vereinsrollen und der gewünschte redaktionelle Freigabeprozess geprüft werden.
`FUPA_REPORT_AUTOMATIC_PUBLISH_ENABLED` bleibt ohne ausdrücklich installierten
und geprüften Publisher wirkungslos.

Telegram wird zusätzlich zweistufig freigegeben: Der PlatformAdmin aktiviert
den Provider für den Verein; danach richtet ein Vereinsadministrator den
vereinseigenen Bot ein. Ist kein Messenger aktiv, läuft die Berichtserstellung
ohne Rückfrage weiter. Einrichtung, Bot-Kommandos, Datenschutz, Rollout und
Rollback beschreibt [`TELEGRAM_MATCH_FEEDBACK.md`](TELEGRAM_MATCH_FEEDBACK.md).

## Verbleibende Grenze

Das Lesen öffentlich erreichbarer FuPa-Spielseiten wird defensiv unterstützt.
Aktuelle FuPa-Seiten liefern die normalisierten Spieldaten unter anderem in
einem JSON-Bootstrap unter `window.REDUX_DATA`. Dieser Datenblock wird als JSON
dekodiert, ohne JavaScript auszuführen; JSON-LD und Next-Daten bleiben als
Fallback erhalten. Unvollständige Seiten werden weiterhin als `incomplete`
gekennzeichnet und nicht mit geratenen Fakten aufgefüllt.
Ein stabiler offizieller Schreibzugriff auf FuPa wird nicht behauptet. Der
Standard-Publisher kennzeichnet Berichte deshalb als manuell zu übertragen.
Optional kann ein Administrator die ausdrücklich bestätigte, browsergestützte
Übergabe aktivieren. Änderungen an der FuPa-Oberfläche führen dabei sicher zum
Abbruch statt zu einer geratenen Bedienaktion.
