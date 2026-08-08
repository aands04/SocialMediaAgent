# Meta-Kanäle: Instagram, Facebook und WhatsApp

Stand der fachlichen und technischen Prüfung: **8. August 2026**.

Die Implementierung verwendet ausschließlich offizielle Meta-Schnittstellen. Es gibt keine
Browserautomatisierung, keine privaten APIs, keine WhatsApp-Web-Bots und kein automatisches
WhatsApp-Status-Publishing. Die Graph-Version wird zentral mit `META_GRAPH_VERSION`
konfiguriert und nicht in einzelnen Services fest codiert. Ein Versionswechsel erfolgt erst
nach App-Kompatibilitätstest in Meta-Test.

## Offizielle Quellen

- [Instagram API – offizieller Meta-Workspace](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api)
- [Facebook API – offizieller Meta-Workspace](https://www.postman.com/meta/facebook/documentation/r56bjfd/facebook-api)
- [WhatsApp Business Platform – offizieller Meta-Workspace](https://www.postman.com/meta/whatsapp-business-platform/overview)
- [WhatsApp Embedded Signup – offizieller Meta-Workspace](https://www.postman.com/meta/whatsapp-business-platform/documentation/du6gzjv/embedded-signup)
- [WhatsApp Business Messaging Policy](https://business.whatsapp.com/policy)

Die Meta-Entwicklerdokumentation ist dynamisch und kann je App, App-Modus, Graph-Version,
Business-Verifizierung und App-Review unterschiedliche Optionen anzeigen. Vor jeder
Produktivaktivierung sind die im konkreten Meta-App-Dashboard sichtbaren Anforderungen erneut
zu prüfen.

## Instagram

Die vorhandene Integration bleibt auf dem offiziellen **Instagram Login** aufgebaut. Für die
Inhaltsveröffentlichung werden mindestens folgende Scopes geprüft:

- `instagram_business_basic`
- `instagram_business_content_publish`

Unterstützt werden in der Vereinszentrale:

- einzelnes Feed-Bild,
- Karussell,
- Begleittext,
- Story, wenn Kontotyp und aktuelle API diese Funktion erlauben.

Die Anwendung verlangt für Publishing ein professionelles Business-Konto. Meta dokumentiert
zusätzliche Einschränkungen und Publishing-Limits; diese dürfen nicht durch parallele
inoffizielle Logins umgangen werden. Die Verbindung wird standardmäßig alle zwölf Stunden
lesend geprüft. Ein unklarer Schreibaufruf wird nicht automatisch wiederholt.

## Facebook-Seiten

Die Kontoauswahl erfolgt über den offiziellen Meta-OAuth-Dialog. Die Anwendung liest die vom
angemeldeten Benutzer verwaltbaren Seiten über `/me/accounts` und übernimmt ausschließlich
eine ausdrücklich ausgewählte Seite. Page Access Tokens werden verschlüsselt serverseitig
gespeichert und nie an den Browser zurückgegeben.

Technisch werden für den derzeit implementierten Funktionsumfang benötigt:

- `pages_show_list`,
- `pages_read_engagement`,
- `pages_manage_posts`.

Implementiert sind Text-/Bildbeiträge und Mehrbildbeiträge auf einer Facebook-Seite. Der
Verbindungstest ist rein lesend und erzeugt keinen Testbeitrag. Neue Verbindungen aktivieren
keine historischen Beiträge rückwirkend.

## WhatsApp Business Platform / Cloud API

WhatsApp ist ein Nachrichtenkanal und kein Feed- oder Story-Kanal. Die Einrichtung verwendet
den offiziellen Embedded-Signup-/Cloud-API-Prozess mit:

- Meta Business und WhatsApp Business Account (WABA),
- registrierter Telefonnummer und Phone Number ID,
- `business_management`, `whatsapp_business_management` und
  `whatsapp_business_messaging`, soweit für den konkreten App-Review erforderlich,
- abonnierten Webhooks,
- genehmigten Nachrichtenvorlagen für proaktive Vereinsnachrichten.

Die aktuelle erste Ausbaustufe versendet ausschließlich genehmigte Vorlagennachrichten.
Freiform-, Bild- und Linknachrichten werden in der UI erst angeboten, wenn der zulässige
Kontext (beispielsweise das Servicefenster) und die entsprechenden Komponenten vollständig
modelliert und getestet sind.

### Einwilligung und Abmeldung

Ein Empfänger wird nur aktiviert, wenn eine konkrete Einwilligungsquelle erfasst und die
Einwilligung ausdrücklich bestätigt wurde. Proaktive Nachrichten werden ohne bestätigten
Opt-in blockiert. `STOP`, `STOPP`, `ABMELDEN` und `UNSUBSCRIBE` über einen signierten
WhatsApp-Webhook deaktivieren den Empfänger für künftige Jobs. Opt-in, Präferenzänderung und
Opt-out werden mandantenbezogen auditiert.

### Vorlagen und Kosten

Vorlagen werden aus Meta synchronisiert. Nur der Status `approved` ist versandfähig. Preise
werden nicht fest programmiert, weil Meta das Preismodell ändern kann. Die Delivery-Ledger-
Einträge sind auf Kategorie und eine optionale tatsächlich gelieferte Kosteninformation
vorbereitet.

### Kein WhatsApp-Status-Publishing

Bei der Prüfung wurde keine offizielle Cloud-API-Funktion zum automatischen Veröffentlichen
eines WhatsApp-Status gefunden. Die Vereinszentrale bietet diese Funktion deshalb nicht an.

## Sicherheits- und Mandantenmodell

- Jede Verbindung, Auswahl, Mannschaftszuordnung, jeder Empfänger, jede Vorlage, jeder
  Webhook und jeder Auslieferungsversuch besitzt eine `club_id`.
- Composite Foreign Keys verhindern vereinsübergreifende Zuordnungen zusätzlich zur zentralen
  `TenantSession`.
- OAuth-State ist zufällig, gehasht, kurzlebig und einmalig. Temporäre Page Tokens liegen nur
  verschlüsselt in der Datenbank und werden nach der Auswahl entfernt.
- Tokens stehen weder in HTML noch in JSON-Antworten, Logs oder Auditdetails.
- Der globale Not-Aus sowie Kanal-, Konto-, Mannschafts-, Beitrags- und Freigabegates gelten
  vor jedem externen Schreibaufruf.
- Ein Timeout nach einem möglicherweise angenommenen Schreibaufruf führt zu `uncertain` und
  niemals zu einer automatischen Wiederholung.
- Webhooks werden mit `X-Hub-Signature-256` und dem Meta App Secret geprüft. Die GET-
  Verifizierung verwendet ein getrenntes Secret aus `META_WEBHOOK_VERIFY_TOKEN`.

## Konfiguration

Mindestens relevant:

```env
META_GRAPH_VERSION=v23.0
META_FACEBOOK_OAUTH_REDIRECT_URI=https://meta.example.org/public/meta/channels/oauth/callback
META_WHATSAPP_CONFIGURATION_ID=
FACEBOOK_CHANNEL_ENABLED=false
WHATSAPP_CHANNEL_ENABLED=false
```

`META_APP_ID`, `META_APP_SECRET`, `META_TOKEN_ENCRYPTION_KEY` und
`META_WEBHOOK_VERIFY_TOKEN` werden ausschließlich als Secrets injiziert. Eigene optionale
Facebook-App-Werte (`META_FACEBOOK_APP_ID`, `META_FACEBOOK_APP_SECRET`) sind möglich, wenn eine
separate Meta-App verwendet wird.

Öffentlich dürfen ausschließlich diese Pfade erreichbar sein:

- `GET /public/instagram/oauth/callback`
- `GET /public/meta/channels/oauth/callback`
- `POST /public/meta/channels/oauth/select`
- `GET|POST /public/meta/webhook`
- `GET /public/meta-media/*`

Der normale Dashboard-Proxy bleibt authentifiziert. Caddy beziehungsweise ein anderer äußerer
TLS-Proxy muss dieselbe Allowlist verwenden.

## Manuelle Meta-App-Schritte

1. App-Typ und Business-Verknüpfung im Meta-App-Dashboard prüfen.
2. Instagram Login und bestehende Redirect-URI beibehalten.
3. Facebook Login und die neue OAuth-Redirect-URI exakt hinterlegen.
4. Benötigte Pages-Berechtigungen durch App-Review/Advanced Access freigeben lassen.
5. WhatsApp Embedded Signup konfigurieren und die Configuration ID als Secret/Umgebungswert
   bereitstellen.
6. Webhook-URL und Verify Token bei Meta eintragen; `whatsapp_business_account` abonnieren.
7. WhatsApp-Vorlagen für Spielankündigung und Ergebnis in Meta anlegen und genehmigen lassen.
8. Zunächst in Meta-Test testen. Feature Flags einzeln aktivieren und erst danach die
   Automatik mit ausdrücklicher Bestätigung freigeben.

## Bekannte Grenzen dieser Ausbaustufe

- Kein WhatsApp-Status-Publishing.
- WhatsApp sendet proaktiv nur genehmigte Templates; noch keine freien Servicefenstertexte.
- WhatsApp-Medienheader und Links werden noch nicht angeboten.
- Kanalspezifische Promptparameter sind vorbereitet. Bestehende Beiträge behalten ihren
  freigegebenen Textsnapshot; ein neuer Kanal erzeugt nicht rückwirkend einen neuen KI-Text.
- Meta liefert nicht für jeden Tokentyp ein verlässliches Ablaufdatum. In diesem Fall zeigt die
  UI „nicht von Meta gemeldet“ und stützt sich zusätzlich auf die regelmäßige Verbindungsprüfung.
