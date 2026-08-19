# Telegram-Rückfragen für FuPa-Spielberichte

Stand der Dokumentationsprüfung: 19. August 2026

## Zweck und Abgrenzung

Telegram ergänzt den bestehenden WhatsApp-Rückfrageweg für Spielberichte. Die
fachliche Berichtserstellung bleibt providerneutral: Kontakte, Mannschaften und
Vereine können einen bevorzugten Messenger und höchstens einen ausdrücklich
gewählten Ersatzkanal festlegen. Telegram ersetzt WhatsApp nicht und verändert
keine vorhandenen WhatsApp-Verbindungen.

Unterstützt werden ausschließlich private Chats mit einem offiziellen Telegram-
Bot. Gruppen, Kanäle, Polling, n8n und Telegram-Login sind nicht Bestandteil.
Sprachnachrichten werden nach ausdrücklicher Aktivierung vorübergehend geladen,
in ein einheitliches Audioformat umgewandelt und über die bestehende OpenAI-
Transkriptionsanbindung als Text in den Spielbericht übernommen. Andere
Medienantworten werden weiterhin nur datensparsam als Metadaten protokolliert.

## Offizielle Schnittstelle

Die Integration verwendet ausschließlich die offizielle Telegram Bot API:

- Bot API und Webhooks: <https://core.telegram.org/bots/api>
- Bot-Kommandos und Deep Links: <https://core.telegram.org/bots/features>

Der Server registriert einen HTTPS-Webhook mit zufälligem, nicht erratbarem Pfad
und `secret_token`. Telegram muss diesen Wert bei jedem Update im Header
`X-Telegram-Bot-Api-Secret-Token` mitsenden. Der Bot antwortet nur auf private
Chats und unterstützt `/start`, `/help`, optional `/status` sowie die Inline-
Aktion **Keine Ergänzungen**.

## Architektur und Provider-Auswahl

`FeedbackProvider` kapselt den Versand einer Rückfrage. WhatsApp und Telegram
implementieren diese Schnittstelle getrennt; der übrige FuPa-Ablauf kennt nur
providerneutrale Requests, Responses und Endpunkte.

Die Auswahl erfolgt nachvollziehbar in dieser Reihenfolge:

1. ausdrückliche Einstellung am Kontakt,
2. Einstellung der Mannschaft,
3. Einstellung des Vereins,
4. bestehender kompatibler Standard WhatsApp.

Ein Ersatzkanal wird nur verwendet, wenn er ausdrücklich konfiguriert wurde.
Ungültige oder widersprüchliche Einstellungen werden nicht still auf einen
anderen Provider umgebogen. Ist kein erlaubter und verbundener Provider
verfügbar, wird keine Nachricht versendet; Berichtserstellung und Freigabe
bleiben davon unabhängig möglich.

## Einrichtung

### 1. Bot bei BotFather anlegen

1. In Telegram den verifizierten Account `@BotFather` öffnen.
2. Mit `/newbot` einen eigenen Bot für genau einen Verein anlegen.
3. Anzeigenamen und eindeutigen Bot-Benutzernamen festlegen.
4. Das Bot-Token nur einmal in der Vereinszentrale eintragen. Es darf nicht in
   Tickets, Logs, `.env`-Dateien oder Screenshots übernommen werden.

Ein Bot darf nur einem Verein zugeordnet sein. Die Datenbank verhindert sowohl
eine globale Mehrfachzuordnung derselben Bot-ID als auch mehrere gleichzeitig
aktive Telegram-Bots in einem Verein.

### 2. Server konfigurieren

```env
TELEGRAM_BOT_API_BASE_URL=https://api.telegram.org
TELEGRAM_WEBHOOK_BASE_URL=https://meta.example.org
TELEGRAM_LINK_TTL_MINUTES=30
TELEGRAM_HTTP_TIMEOUT_SECONDS=20
TELEGRAM_VOICE_TRANSCRIPTION_ENABLED=true
TELEGRAM_VOICE_TRANSCRIPTION_MODEL=gpt-4o-transcribe
TELEGRAM_VOICE_MAX_BYTES=20000000
TELEGRAM_VOICE_MAX_DURATION_SECONDS=300
TELEGRAM_VOICE_TRANSCRIPTION_TIMEOUT_SECONDS=45
FUPA_REPORT_FEEDBACK_WAIT_MINUTES=30
```

`TELEGRAM_WEBHOOK_BASE_URL` muss ohne abschließenden Pfad öffentlich per HTTPS
erreichbar sein und auf den öffentlichen Proxy der Anwendung zeigen. Die
Anwendung erzeugt den vollständigen zufälligen Webhook-Pfad selbst. Der Bot-
Zugriff und das Webhook-Secret werden mit der bestehenden Tokenverschlüsselung
gespeichert und nie an den Browser zurückgegeben.

Für Sprachnachrichten verwendet die Anwendung den bereits sicher hinterlegten
`OPENAI_API_KEY`; Vereinsadministratoren benötigen keinen eigenen Schlüssel.
Das Container-Image enthält `ffmpeg`, um Telegrams OGG/Opus-Dateien vor der
Transkription in ein kompatibles Mono-WAV umzuwandeln. Modell, Größen-, Dauer-
und Zeitlimit sind zentral konfigurierbar. Audiodaten und Telegram-Datei-IDs
werden nur im Arbeitsspeicher verarbeitet und nicht dauerhaft gespeichert.

### 3. Provider freigeben

1. PlatformAdmin öffnet den Verein im Plattformbereich und aktiviert Telegram
   ausdrücklich für diesen Mandanten.
2. Vereinsadministrator öffnet **Social-Media-Kanäle → Telegram einrichten**,
   trägt das Bot-Token ein und bestätigt die Verbindung.
3. Die Anwendung prüft `getMe`, registriert den Webhook und verifiziert die
   zurückgemeldete Webhook-URL. Es wird keine öffentliche Nachricht versendet.

### 4. Kontakte verknüpfen

Ein Vereinsadministrator erzeugt am Spielberichtskontakt einen Link. Der Link:

- enthält ausschließlich ein zufälliges opakes Token,
- ist kurzlebig und nur einmal verwendbar,
- ist an Verein, Bot und Kontakt gebunden,
- enthält weder Vereins-, Kontakt- noch Benutzer-ID.

Der Kontakt öffnet `https://t.me/<bot>?start=<token>` und bestätigt damit den
privaten Chat. Abgelaufene, manipulierte oder fremdmandantige Tokens werden
sicher abgewiesen. Ein Chat kann nicht unbemerkt einem zweiten Kontakt
zugeordnet werden.

## Verarbeitung und Datenschutz

Jedes Telegram-Update wird vor fachlicher Verarbeitung anhand des Webhook-
Secrets geprüft. Danach ordnet der Server den zufälligen Webhook-Pfad genau
einer aktiven Vereinsverbindung zu und aktiviert erst dann den Tenant-Kontext.
`update_id` wird je Verbindung eindeutig gespeichert; doppelte Zustellungen
werden idempotent quittiert.

Antworten werden zuerst über die beantwortete Telegram-Nachricht zugeordnet.
Nur wenn genau eine offene Rückfrage eindeutig passt, darf die Rückmeldung
übernommen werden. Mehrdeutige oder fremde Nachrichten bleiben ohne fachliche
Wirkung. In `MatchContentContext` erscheinen ausschließlich providerneutrale,
bestätigte Messenger-Fakten mit sicherer Quellenrolle.

Gespeichert werden die technisch notwendigen Telegram-IDs, Zeitpunkte und
Statusdaten. Bot-Token und Webhook-Secret bleiben verschlüsselt. Weder Secrets
noch vollständige Providerantworten erscheinen in HTML, Audit oder normalen
Fehlermeldungen.

Bei erfolgreicher Spracherkennung wird ausschließlich das erkannte Transkript
samt Modell- und Dauer-Metadaten gespeichert. Schlägt Download, Konvertierung
oder Transkription fehl, bleibt die Rückfrage offen und der Bot bittet um eine
erneute Textantwort. Dadurch wird eine Rückfrage nicht durch eine leere
Medienantwort fälschlich als beantwortet markiert.

## Fehler, Wiederholung und Status

- HTTP `429`: `retry_after` wird als sichere Betriebsinformation übernommen;
  der bestehende Worker darf den Versuch begrenzt wiederholen.
- HTTP `401`: Verbindung wird als gestört markiert und muss erneuert werden.
- HTTP `403`/`404`: Kontakt beziehungsweise Chat wird als nicht erreichbar
  markiert; es erfolgt kein stiller Versand über einen anderen Provider.
- Netzwerk- und 5xx-Fehler bleiben als technische Zustellfehler sichtbar, ohne
  Token oder Telegram-Payload zu protokollieren.

Der Systemstatus zeigt Verbindung und Webhook-Zustand. Ein deaktivierter
Provider quittiert valide Updates lediglich idempotent; er verknüpft keine
Kontakte, speichert keine fachliche Rückmeldung und sendet keine Bot-Antwort.

## Sicherer Rollout und Rollback

1. Migration `0033` zunächst mit deaktiviertem Telegram-Feature ausrollen.
2. HTTPS-Webhookbasis und Tokenverschlüsselung prüfen.
3. Einen Testverein durch den PlatformAdmin freigeben.
4. Bot verbinden, Kontaktlink testen und eine private Testantwort eindeutig
   einem Testspiel zuordnen.
5. Audit, Webhook-Idempotenz und Berichtskontext prüfen.
6. Erst danach weitere Vereine einzeln aktivieren.

Für einen sofortigen Rollback Telegram im PlatformAdmin-Bereich deaktivieren.
Der FuPa-Bericht läuft dann ohne Telegram-Rückfragen weiter; WhatsApp bleibt
unverändert. Beim Trennen eines Bots wird der Webhook bei Telegram entfernt,
lokale Endpunkte werden deaktiviert und das verschlüsselte Token verworfen.
Historische, bereits zugeordnete Rückmeldungen und Auditdaten bleiben erhalten.
