# Instagram-Meta-Test

> **META-TEST – ECHTE INSTAGRAM-VERÖFFENTLICHUNGEN MÖGLICH**
>
> Diese Umgebung ist vom Proxmox-Staging getrennt. Das Staging bleibt
> `PUBLISHER_MODE=dry-run`. Der Meta-Test erlaubt ausschließlich bewusst und
> zweistufig manuell bestätigte Veröffentlichungen auf einer ausdrücklich
> markierten Business-Testseite. Produktion und Scheduler-Live-Publishing sind
> nicht aktiviert.

## Offiziell geprüfte Grundlagen

Abrufstand: **31.07.2026**. Vor jedem ersten echten Test müssen die Angaben
erneut mit den aktuellen offiziellen Meta-Quellen abgeglichen werden:

- [Instagram API – offizielle Meta-Postman-Sammlung](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api)
- [Instagram API with Instagram Login](https://www.postman.com/meta/instagram/folder/23987686-98bfade9-3736-4738-8b4a-f56d6534f6de)
- [Mediencontainer erstellen](https://www.postman.com/meta/instagram/request/23987686-f4b5a72d-a125-4080-8968-93de1a549e68)
- [Container veröffentlichen (`media_publish`)](https://www.postman.com/meta/instagram/request/23987686-299b176b-90aa-4d8a-b6cf-e6028fc69de5)
- [Meta App Review](https://developers.facebook.com/docs/app-review/)

Der implementierte Flow verwendet **Instagram API with Instagram Login** und
`graph.instagram.com`. Unterstützt werden professionelle Konten. Diese
Anwendung verlangt nur:

- `instagram_business_basic`
- `instagram_business_content_publish`

Die vorhandene globale Tokenannahme über `graph.facebook.com` wurde für den
Meta-Test ersetzt. Jede Instagram-Seite besitzt eine eigene verschlüsselte
Verbindung. Der Standard `META_GRAPH_VERSION=v23.0` entspricht der in der
geprüften offiziellen Sammlung gezeigten Version und ist ausdrücklich
konfigurierbar. Vor einem realen Test ist zu prüfen, ob Meta inzwischen eine
andere unterstützte Version verlangt.

Der Instagram-Login-Flow stellt keinen Facebook-Graph-Endpunkt
`/me/permissions` bereit. Die Anwendung fordert deshalb im OAuth-Dialog exakt
die beiden oben genannten Scopes an und speichert diesen OAuth-Grant nach einem
erfolgreichen Codeaustausch. Verbindungstests validieren Token, Konto-ID,
Benutzername und Kontoart über den unterstützten Instagram-Endpunkt `/me` und
prüfen zusätzlich den gespeicherten Scope-Satz. Ein fehlender gespeicherter
Scope sperrt die Verbindung; der ungültige `/me/permissions`-Aufruf wird nicht
verwendet. Effektiv entzogene Rechte werden außerdem von jedem konkreten
Instagram-Endpunkt abgewiesen und niemals durch einen automatischen Retry
umgangen.

Feed und Story verwenden eine öffentlich erreichbare `image_url`. Für eine
Bild-Story sendet die Anwendung `media_type=STORIES` und keine erfundene
Caption. Die Container-ID wird vor jedem weiteren Schritt persistent
gespeichert. Der Containerstatus muss `FINISHED` sein, bevor
`media_publish` bewusst ausgelöst wird. Tokenlaufzeiten und Ablaufzeitpunkte
werden aus den API-Antworten gespeichert; sie werden nicht hartcodiert.

App-Modus, Rollen, Testnutzer, App Review, Publishing-Limits und aktuell
zulässige Medienanforderungen müssen in der Meta-App unmittelbar vor dem
ersten echten Test erneut geprüft werden.

## Sicherheitsmodell

Ein Meta-Aufruf ist nur möglich, wenn gleichzeitig gilt:

- `ENVIRONMENT=meta-test`
- `PUBLISHER_MODE=instagram`
- `META_TEST_ENABLED=true`
- `META_TEST_PUBLISH_ENABLED=true`
- `META_SCHEDULER_ENABLED=false`
- Seite ist ausdrücklich als Testseite markiert
- Seite und Verbindung sind aktiv und geprüft
- Kontoart ist `BUSINESS`
- beide benötigten Berechtigungen sind vorhanden
- Token ist nicht abgelaufen
- globaler Not-Aus ist deaktiviert
- Beitrag, Version, Zielseite und Auftrag sind vollständig freigegeben
- Spiel ist nicht abgesagt, verschoben oder anderweitig gesperrt
- PNG, Auflösung, Pfad und Prüfsumme sind gültig
- eine gültige HTTPS-Medienfreigabe existiert
- der Administrator bestätigt Container und Publishing jeweils mit einem
  einmaligen Code.

Tokens werden mit einem Fernet-Schlüssel authentifiziert verschlüsselt.
OAuth-States und Bestätigungscodes sind kurzlebig und einmalig. Öffentliche
Medientokens werden ausschließlich gehasht gespeichert. Weder Token,
App-Secret, OAuth-Code noch vollständiger Bestätigungscode erscheinen in
Logs, Audit-Einträgen oder URLs.

Meta darf das freigegebene Medium bis zum Ablauf mehrfach abrufen. Der
Endpunkt prüft bei jedem Abruf Dateipfad, Symlink-Grenze, PNG-Typ, Größe und
Prüfsumme. Er kann weder Uploads noch SMB-Dateien ausliefern.

## Meta-App vorbereiten

1. In Meta for Developers eine separate Test-App anlegen.
2. Das Produkt **Instagram** beziehungsweise **Instagram API with Instagram
   Login** gemäß der aktuellen Meta-Oberfläche aktivieren.
3. Das Business-Konto `svehlen1901` ausschließlich als Tester/App-Rolle
   verbinden. Kein Produktionskonto verwenden.
4. Nur die beiden oben genannten Berechtigungen konfigurieren.
5. Die exakte Callback-URL eintragen:

   `https://meta.example.org/public/instagram/oauth/callback`

6. App-ID und App-Secret sicher notieren. Das Instagram-Passwort wird der
   Anwendung niemals mitgeteilt oder dort gespeichert.
7. Vor einem externen Test Kontoart, gewährte Scopes, App-Modus und etwaige
   Review-Vorgaben in der aktuellen Meta-Dokumentation nochmals prüfen.

## Secret-Dateien

Auf der Proxmox-VM:

```bash
sudo install -d -m 0700 -o root -g root /etc/social-media-agent/meta-test-secrets
sudo sh -c 'umask 077; openssl rand -base64 48 > /etc/social-media-agent/meta-test-secrets/db_password'
sudo sh -c 'umask 077; openssl rand -base64 64 > /etc/social-media-agent/meta-test-secrets/session_secret'
sudo sh -c 'umask 077; printf "%s" "OPENAI_API_KEY_HIER" > /etc/social-media-agent/meta-test-secrets/openai_api_key'
sudo sh -c 'umask 077; printf "%s" "META_APP_ID_HIER" > /etc/social-media-agent/meta-test-secrets/meta_app_id'
sudo sh -c 'umask 077; printf "%s" "META_APP_SECRET_HIER" > /etc/social-media-agent/meta-test-secrets/meta_app_secret'
sudo sh -c 'umask 077; openssl rand -base64 32 | tr "+/" "-_" > /etc/social-media-agent/meta-test-secrets/meta_token_encryption_key'
sudo chmod 600 /etc/social-media-agent/meta-test-secrets/*
```

Der Verschlüsselungsschlüssel muss in das verschlüsselte externe
Secret-Backup aufgenommen werden. Ein Verlust macht gespeicherte Verbindungen
unlesbar. Bei Rotation `META_TOKEN_KEY_VERSION` erhöhen und Verbindungen
kontrolliert neu herstellen.

## Konfiguration und Start

```bash
cd /opt/socialmediaagent
cp .env.meta-test.example .env.meta-test
nano .env.meta-test

sudo docker compose \
  --env-file .env.meta-test \
  -f docker-compose.yml \
  -f docker-compose.meta-test.yml \
  config -q

sudo docker compose \
  --env-file .env.meta-test \
  -f docker-compose.yml \
  -f docker-compose.meta-test.yml \
  up -d --build

sudo bash -lc '
  cd /opt/socialmediaagent
  set -a
  . ./.env.meta-test
  set +a
  ./scripts/meta-test-check.sh
'
```

`META_PUBLIC_BASE_URL` und `META_OAUTH_REDIRECT_URI` müssen echte HTTPS-URLs
der öffentlichen Subdomain sein. Das Dashboard bleibt lokal auf
`127.0.0.1:8080`.

## Öffentliche Subdomain

Der Container `public_proxy` stellt lokal nur Port `8081` bereit. Öffentlich
dürfen ausschließlich diese Pfade erreichbar sein:

- `/public/instagram/oauth/callback`
- `/public/meta-media/<zufälliges-token>`

Alle anderen Pfade müssen 404 liefern. Beispiel für Caddy:

```caddy
meta.example.org {
    @allowed path /public/instagram/oauth/callback /public/meta-media/*
    handle @allowed {
        reverse_proxy 127.0.0.1:8081
    }
    respond 404
}
```

Alternativ kann ein Cloudflare Tunnel ausschließlich auf
`http://127.0.0.1:8081` zeigen. Die Nginx-Konfiguration im Repository
verweigert anschließend erneut alle nicht erlaubten Pfade. Access-Logs sind
für die öffentlichen Token-/OAuth-Pfade deaktiviert.

## Verbindung herstellen

1. Zunächst mit `META_TEST_PUBLISH_ENABLED=false` Compose, Secrets,
   öffentlichen Proxy und Dashboard prüfen. Es erfolgt noch kein Meta-Aufruf.
2. Unmittelbar vor dem bewussten OAuth-Test
   `META_TEST_PUBLISH_ENABLED=true` setzen und Web/Worker kontrolliert neu
   erstellen. Ohne dieses Gate werden OAuth-Codeaustausch und
   Verbindungsprüfung serverseitig blockiert.
3. Dashboard ausschließlich lokal per SSH-Tunnel/VPN öffnen.
4. Unter **Instagram-Seiten** die vorgesehene Seite auswählen.
5. **Mit Instagram verbinden** wählen.
6. Meta-Anmeldung durchführen und exakt `svehlen1901` bestätigen.
7. Nach dem Callback Konto-ID, Benutzername, Business-Kontoart,
   Berechtigungen und Tokenablauf kontrollieren.
8. **Verbindung prüfen** ausführen.
9. Seite ausdrücklich als **Meta-Testseite** markieren und Publishing für
   diese Seite aktivieren.

Bei einem falschen Benutzernamen, fehlendem Scope, Nicht-Business-Konto,
abgelaufenem Token oder fehlgeschlagenem Verbindungstest bleibt Publishing
gesperrt.

## Teststufen

### Validate-only

1. Einen vollständig freigegebenen Feed- oder Story-Auftrag wählen.
2. `validate-only` starten.
3. Lokale Datei, Prüfsumme, PNG-Eigenschaften und HTTPS-Freigabe prüfen.
4. Es wird kein Meta-Container erzeugt.

### Container-only

1. Erst nach erfolgreichem Validate-only `container-only` wählen.
2. Einmaligen Container-Code erzeugen und erneut eingeben.
3. Container bewusst erstellen.
4. Status kontrolliert abfragen.
5. Nicht veröffentlichen.

### Erster Feed-Test

1. Isolierten, ausdrücklich freigegebenen Feed-Auftrag auswählen.
2. Stufe `publish` wählen und Vorschau einschließlich Caption prüfen.
3. Medien-URL extern testen.
4. Container-Code erzeugen/eingeben und Container erstellen.
5. Bis zum offiziellen Bereitschaftsstatus warten.
6. Separaten Publishing-Code erzeugen/eingeben.
7. `media_publish` einmalig ausführen.
8. Media-ID, optionalen Permalink und den Instagram-Testaccount prüfen.

### Erster Story-Test

Der Ablauf ist identisch, jedoch mit einem Story-Auftrag. Die Anfrage muss
`media_type=STORIES` enthalten und darf keine Caption senden. Feed-Erfolg
garantiert keinen Story-Erfolg; beide werden getrennt geprüft.

## Unklare Antworten und Wiederanlauf

Bei Timeout nach möglicher Meta-Annahme wird der Versuch `uncertain`.
Container- oder Media-ID bleiben gespeichert. Die Anwendung erstellt niemals
blind einen zweiten Container und ruft `media_publish` niemals blind erneut
auf. Ein Administrator prüft Meta/Instagram und dokumentiert anschließend im
Assistenten entweder:

- **als veröffentlicht bestätigen** mit bekannter Media-ID, oder
- **als nicht veröffentlicht bestätigen**.

Ein Neustart von Container oder VM ändert diese Regel nicht.
Bleibt ein Versuch nach einem Prozess- oder VM-Neustart in
`validating_public_media`, `creating_container` oder `publishing` stehen, kann
derselbe manuelle Abgleich nach einer Sicherheitsfrist verwendet werden. Vor
Ablauf dieser Frist blockiert die Anwendung den Abgleich, damit ein
möglicherweise noch laufender externer Aufruf nicht mit einer Bedienaktion
kollidiert.

## Tokenpflege, Trennung und Not-Aus

- Dashboard zeigt Ablaufzeit und warnt vor bald ablaufenden Tokens.
- **Erneuern** verwendet ausschließlich den offiziell implementierten
  Token-Refresh.
- **Trennen** löscht kein Instagram-Konto, sondern sperrt die lokale
  Verbindung und widerruft aktive Medienfreigaben.
- Der globale Not-Aus wird unmittelbar vor jedem externen Schritt erneut
  geprüft.
- In einem Vorfall zuerst Not-Aus aktivieren, dann offene/unklare Vorgänge
  fachlich bei Meta abgleichen.

## Bewusste Grenzen

- Keine automatische Scheduler-Veröffentlichung.
- Keine Produktionsumgebung.
- Keine Instagram-Browserautomatisierung.
- Keine Instagram-Passwörter.
- Keine dauerhaften öffentlichen Medien-URLs.
- Kein Anspruch, dass eine heute gültige Meta-Version oder App-Review-Regel
  dauerhaft unverändert bleibt.
- Die Implementierung und Tests verwenden ausschließlich gemockte
  Meta-Antworten; Codex führt keine echte Veröffentlichung durch.
