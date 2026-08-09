# WhatsApp im Live Center

Stand der offiziellen Prüfung: 9. August 2026

## Grundsatz

Jeder Verein verbindet sein eigenes WhatsApp Business Account (WABA) und seine
eigene Telefonnummer. Es gibt weder eine zentrale Plattformnummer noch ein
vereinsübergreifendes Zugangstoken. Die Verbindung wird als
`SocialChannelConnection` mit `club_id`, unveränderlicher WABA-ID und
`phone_number_id` gespeichert; das Zugangstoken liegt ausschließlich
authentifiziert verschlüsselt in der Datenbank.

Die Integration verwendet nur die offizielle WhatsApp Business Platform / Cloud
API. WhatsApp Web, QR-Code-Bots, Browser-Automatisierung und private APIs werden
nicht unterstützt.

Offizielle technische Quellen:

- [WhatsApp Business Platform – offizielle Meta-Collection](https://www.postman.com/meta/whatsapp-business-platform/overview)
- [WhatsApp Webhooks](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview/)
- [Webhook-Endpunkt einrichten](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint/)
- [WhatsApp Groups API](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups)

## Einrichtung je Verein

1. Der Vereinsadministrator verbindet in **Social-Media-Kanäle** das eigene WABA.
2. Die Anwendung speichert WABA-ID und `phone_number_id` und abonniert den WABA
   für Webhooks.
3. Für proaktive Live-Nachrichten wird eine von Meta genehmigte Utility-Vorlage
   des Typs **Live-Ereignis** benötigt. Die aktuelle Integration akzeptiert
   genau einen BODY-Platzhalter `{{1}}`; darin steht die geprüfte Live-Meldung.
4. Unter **Live Center** werden Reporter, Mannschaftsrechte und ein aktives Spiel
   zugeordnet.
5. Der Verein legt eine Zielgruppe an:
   - eine Empfängerliste mit dokumentiertem Opt-in oder
   - eine offizielle WhatsApp-Gruppe, aber nur wenn Meta für die konkrete
     Verbindung die Gruppen-Capability bestätigt.
6. Anschließend werden Regeln je Mannschaft und Ereignisart zunächst manuell und
   erst nach erfolgreichem Test optional automatisch aktiviert.

## Eingang und Mandantenzuordnung

Der öffentliche Meta-Webhook prüft zuerst `X-Hub-Signature-256`. Danach wird der
Mandant ausschließlich aus `entry.id` (WABA) und
`metadata.phone_number_id` bestimmt. Der Nachrichtentext wird erst innerhalb des
eindeutig ermittelten Tenant-Kontexts gelesen. Null, mehrere oder widersprüchliche
Treffer werden abgewiesen; ein Standardverein wird niemals angenommen.

Provider-Nachrichten-IDs werden idempotent verarbeitet. Unbekannte Absender,
abgelaufene Spielzuordnungen und fehlende Mannschaftsrechte erzeugen keine
bestätigten Ereignisse. Rohe Webhook-Nutzdaten und vollständige Nachrichtentexte
werden nicht in allgemeinen Logs oder Audit-Einträgen gespeichert.

## Versand und Einwilligung

Empfängerlisten enthalten ausschließlich aktive Empfänger mit bestätigtem
Opt-in. `STOP`, `STOPP`, `ABMELDEN` und `UNSUBSCRIBE` im verifizierten Webhook
widerrufen den Opt-in und blockieren künftige Nachrichten. Der Verein ist für
einen nachweisbaren, rechtmäßigen Opt-in und passende Aufbewahrungsfristen
verantwortlich.

Ein Live-Versand läuft nur, wenn alle Produktions-, Scheduler-, Vereins-, Kanal-,
Regel-, Freigabe-, Opt-in- und Not-Aus-Gates aktiv sind. Pro Empfänger entsteht
ein eigener idempotenter `LiveDeliveryAttempt`. Die Zustände `sent`, `delivered`
und `read` werden anhand der Meta-Statuswebhooks nachgeführt.

Nach einem Worker-Abbruch wird niemals blind erneut gesendet. Ein Auftrag ohne
begonnenen externen Versuch wird erneut eingeplant. War der externe Aufruf bereits
gestartet, erhält der Versuch den Zustand `uncertain` und muss vor einer
Wiederholung manuell abgeglichen werden.

## Offizielle Gruppen als optionale Fähigkeit

Die Gruppenfunktion ist kein allgemeiner Ersatz für bestehende WhatsApp-Gruppen.
Sie wird nur angeboten, wenn die offizielle Meta Groups API für das konkrete WABA
verfügbar ist und die Anwendung die Capability serverseitig bestätigt hat. Bei
fehlender oder unklarer Eignung bleibt die Funktion gesperrt; es erfolgt kein
Fallback auf WhatsApp Web oder eine inoffizielle API.

## Datenschutz, Kosten und Betrieb

- Zugangstokens werden nie im Browser, Audit oder Log ausgegeben.
- Telefonnummern sind nur für berechtigte Vereinsadministratoren sichtbar.
- Nachrichtenstatus, Fehlerkategorie, Empfängerreferenz und Meta-Nachrichten-ID
  werden zur Nachvollziehbarkeit gespeichert; Secrets und rohe Provider-Payloads
  nicht.
- Meta-Preise werden nicht fest im Programm hinterlegt. Tatsächlich verfügbare
  Kosteninformationen müssen später providerbezogen im bestehenden Usage-Ledger
  erfasst werden.
- Der Systemstatus und das Worker-Heartbeat-Feld `automatic_live_delivery`
  zeigen den Zustand der automatischen Live-Auslieferung.

## Bekannte Grenze

Instagram-Live-Storys dürfen den bestehenden Generierungs-, Medienprüfungs-,
Quoten- und Freigabeworkflow nicht umgehen. Das Live Center legt dafür eine
nachvollziehbare Entscheidung an, veröffentlicht aber kein ungeprüftes Rohereignis
direkt bei Instagram.
