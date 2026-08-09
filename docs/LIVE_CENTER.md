# Live Center – Architektur, Sicherheit und Betrieb

Stand der Prüfung: 9. August 2026

## Bestandsanalyse

Die Vereinszentrale besitzt bereits eine mandantenfähige Grundlage, auf der das Live Center
aufbaut:

- `Game` und `Team` sind unmittelbar einem `Club` zugeordnet.
- `TenantSession`, `TenantContext` und der Request-Scope verweigern unklare oder
  widersprüchliche Vereinskontexte.
- `SocialChannelConnection` speichert Facebook-, Instagram- und WhatsApp-Verbindungen je
  Verein. WhatsApp-Verbindungen enthalten insbesondere die technische
  `phone_number_id`; Zugangstokens werden verschlüsselt gespeichert.
- Der Meta-Webhook prüft `X-Hub-Signature-256` und ordnet eine WhatsApp-Nachricht anhand von
  WABA- und Telefonnummer-ID einem einzigen Verein zu, bevor Inhalte verarbeitet werden.
- `MetaWebhookEvent` stellt eine idempotente Eingangsspur bereit. Es speichert nur Schlüssel
  und Prüfsummen, nicht die vollständigen Nutzdaten.
- Rollen, Mannschaftsrechte, Audit, Not-Aus und kanalbezogene Schalter bleiben verbindlich.
- Veröffentlichungsaufträge sind bereits kanalbezogen. WhatsApp ist als Versand und nicht
  als öffentliche Veröffentlichung modelliert.

Es gab vor dieser Erweiterung kein neutrales Modell für einzelne Spielereignisse, keine
Reporter-Berechtigungen und keine Live-Ansicht. Eingehende WhatsApp-Nachrichten behandelten
nur Abmeldungen.

## Offizielle Meta-Voraussetzungen

Geprüft wurden ausschließlich offizielle Meta-Quellen:

- [WhatsApp Webhooks – Übersicht](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview/)
- [WhatsApp Webhook-Endpunkt](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint/)
- [WhatsApp Groups API](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups)
- [WhatsApp Groups API – Einstieg](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups/get-started/)

Wesentliche Konsequenzen:

- Der öffentliche Webhook benötigt HTTPS mit gültigem Zertifikat.
- Jede POST-Nachricht wird über `X-Hub-Signature-256` (HMAC-SHA256 mit dem App-Secret)
  geprüft.
- Meta kann Webhooks wiederholen. Provider-Nachrichten-IDs sind deshalb
  Idempotenzschlüssel.
- Der eingehende Payload enthält `metadata.phone_number_id`. Diese unveränderliche,
  technische ID ist der primäre Mandantenschlüssel. Telefonnummer oder Anzeigename sind
  dafür ungeeignet.
- Die Groups API ist nur für Official Business Accounts verfügbar, unterstützt höchstens
  acht Teilnehmer pro Gruppe und ist nicht für WhatsApp-Business-App- oder
  Multi-Solution-Telefonnummern verfügbar. Gruppen sind deshalb optional und niemals
  Voraussetzung für das Live Center.
- Ohne nachgewiesene Gruppen-Eignung verwendet die Anwendung ausschließlich zulässige
  Opt-in-Empfängerlisten und genehmigte Nachrichtenvorlagen.

## Domänenmodell

`MatchEvent` ist die neutrale Ereignisquelle. Dashboard, WhatsApp und spätere Provider
arbeiten mit demselben Modell. Unterstützt werden Spielphasen, Tore, Karten, Wechsel,
Unterbrechungen, Kommentare und Korrekturen. Jedes Ereignis besitzt eine `club_id`,
`game_id`, `team_id`, Herkunft, Idempotenzschlüssel, Plausibilitätsstatus und optional einen
Reporter.

`LiveGameState` ist der aus bestätigten Ereignissen abgeleitete aktuelle Spielstand. Er ist
kein Ersatz für die Ereignishistorie. Korrekturen erzeugen neue Ereignisse; bestätigte
Originalereignisse werden nicht still überschrieben.

`LiveReporter` ordnet einen Benutzer oder eine WhatsApp-Absendernummer genau einem Verein
zu. Mannschaftsrechte liegen in `live_reporter_teams`. Für WhatsApp muss zusätzlich die
konkrete Vereinsverbindung übereinstimmen.

`LiveEventRule` beschreibt je Mannschaft und Ereignisart den Modus `aus`, `manuell` oder
`automatisch`, Zielkanäle und Zielgruppe. `LiveEventDelivery` dokumentiert die daraus
entstehenden Auslieferungsentscheidungen getrennt vom Ereignis.

## Verarbeitung eingehender WhatsApp-Nachrichten

1. Signatur des unveränderten Request-Bodys prüfen.
2. `phone_number_id` und WABA-ID extrahieren.
3. Im expliziten Systemkontext genau eine Vereinsverbindung bestimmen; bei null oder mehr
   als einem Treffer sicher abbrechen.
4. Erst danach den Tenant-Scope aktivieren.
5. Provider-Nachrichten-ID idempotent registrieren.
6. Aktiven Reporter und dessen Mannschaftsrecht prüfen.
7. Deterministisch parsen. Unklare Texte bleiben ungeklärt; die optionale KI-Stufe darf nur
   ein streng validiertes Ereignisschema liefern.
8. Plausibilität gegen `LiveGameState` prüfen.
9. Ereignis je nach Vertrauen automatisch bestätigen oder zur Prüfung markieren.
10. Regeln auswerten und sichere Auslieferungsentscheidungen anlegen.

Unbekannte Absender, unklare Spielzuordnung und unplausible Ergebnisse erzeugen niemals
automatisch ein bestätigtes Ereignis.

## Datenschutz

- Vollständige Webhook-Payloads und Tokens werden nicht gespeichert.
- Rohe Nachrichtentexte werden nicht in Audit-Einträgen oder allgemeinen Logs abgelegt.
- MatchEvent speichert nur den für den Spielbetrieb nötigen, gekürzten Inhalt und eine
  Prüfsumme.
- Reporternummern sind nur für Vereinsadministratoren sichtbar.
- Aufbewahrungs- und Löschfristen für Reporter- und Ereignisdaten müssen betrieblich
  festgelegt werden. Audit- und Korrekturhistorien werden nicht unbemerkt gelöscht.

## Offizielle Gruppenfunktion

Das Datenmodell enthält keine Annahme, dass jeder Verein WhatsApp-Gruppen verwenden kann.
Die UI zeigt Gruppen nur dann als geeignet an, wenn die konkrete Verbindung die serverseitig
geprüfte Capability `groups` besitzt. Es gibt keine WhatsApp-Web-Automatisierung und keine
inoffizielle Gruppenintegration.

## Bekannte Grenzen dieser Ausbaustufe

- Live-Ereignisse, Reporter, manuelle Erfassung, WhatsApp-Eingang und -Ausgang,
  Plausibilitätsprüfung, Regeln, Mandantentrennung, Status-Webhooks und die
  restart-sichere Auslieferung sind implementiert.
- Externe FuPa- und FUSSBALL.DE-Livequellen sind nur über die neutrale Provider-Schnittstelle
  vorbereitet. Es wird kein Scraping eingeführt.
- Instagram-Live-Storys benötigen weiterhin den bestehenden, kosten- und
  freigabegesteuerten Bildgenerierungsweg. Das Live Center legt hierfür eine nachvollziehbare
  Auslieferungsentscheidung an; es umgeht keine Freigabe, Quote oder Sicherheitsprüfung.
- Eine Meta-Gruppenaktivierung ist nur nach tatsächlicher OBA-Eignungsprüfung zulässig.

Die verbindliche WhatsApp-Einrichtung, Opt-in-Regeln, Webhook-Zuordnung und
restart-sichere Auslieferung sind ergänzend in [`WHATSAPP.md`](WHATSAPP.md)
dokumentiert.
