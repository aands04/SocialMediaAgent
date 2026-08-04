# Kontenanmeldung und Passwortverwaltung

Die Vereinszentrale authentifiziert Benutzer selbst. Ein zusätzliches
Caddy-`basic_auth` vor dem geschützten Dashboard ist nach der Einrichtung der
Anwendungskonten nicht mehr erforderlich. OAuth-Callback und Meta-Medienpfade
bleiben weiterhin getrennt über den öffentlichen Proxy erreichbar.

## Vorgeschaltetes Caddy-Kennwort entfernen

Im Serverblock des produktiven Dashboards den kompletten `basic_auth`-Block
entfernen. Der Block soll anschließend beispielsweise so aussehen:

```caddyfile
socialmedia.svehlen.de {
    import common_headers
    reverse_proxy 127.0.0.1:8083
}
```

Danach prüfen und ohne Unterbrechung neu laden:

```bash
sudo caddy fmt --overwrite /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl -I https://socialmedia.svehlen.de/login
```

Die Antwort darf keinen `WWW-Authenticate: Basic`-Header mehr enthalten. Das
Anwendungsformular unter `/login` bleibt weiterhin erforderlich.

## Passwort ändern

Jeder angemeldete Benutzer findet im Menü `Passwort ändern`. Er muss sein
aktuelles Passwort angeben. Nach erfolgreicher Änderung werden durch die
erhöhte Authentifizierungsversion alle bestehenden Sitzungen des Kontos
ungültig und eine erneute Anmeldung ist erforderlich.

Passwörter müssen mindestens acht und dürfen höchstens 256 Zeichen lang sein.

## Registrierung und administrative Freigabe

Auf der Anmeldeseite können neue Benutzer über `Registrierung beantragen` ein
Konto anfordern. Solche Konten erhalten zunächst ausschließlich die Rolle
`Betrachter`, besitzen keine Mannschaftsrechte und sind technisch inaktiv. Eine
Anmeldung ist erst möglich, nachdem ein Administrator den Antrag unter
`Benutzer & Rechte` ausdrücklich freigegeben hat. Ablehnung und Freigabe werden
auditiert. Rollen und Mannschaftsrechte werden anschließend getrennt vergeben.

## E-Mail-Adresse ändern

Jeder angemeldete Benutzer kann unter `E-Mail-Adresse ändern` nach Eingabe des
aktuellen Passworts eine neue Adresse anfordern. Die Adresse wird nicht sofort
geändert: Die Anwendung sendet einen einmal verwendbaren Bestätigungslink an
die **bisherige** E-Mail-Adresse. Erst die zusätzliche Bestätigung übernimmt die
neue Adresse, widerruft offene Passwort-Reset-Links und beendet alle bestehenden
Sitzungen des Kontos. Der Token wird nur als SHA-256-Hash gespeichert.

## Passwort vergessen per E-Mail aktivieren

1. SMTP-Passwort als eingeschränkte Secret-Datei anlegen:

   ```bash
   sudo install -m 640 -o root -g 996 /dev/null \
     /etc/social-media-agent/production-secrets/smtp_password
   read -rsp "SMTP-Passwort: " SMTP_PASSWORD
   echo
   printf '%s' "$SMTP_PASSWORD" | sudo tee \
     /etc/social-media-agent/production-secrets/smtp_password >/dev/null
   unset SMTP_PASSWORD
   ```

2. In `.env.production` die SMTP-Daten des verwendeten Mailanbieters eintragen:

   ```dotenv
   APP_PUBLIC_BASE_URL=https://socialmedia.svehlen.de
   PASSWORD_RESET_ENABLED=true
   PASSWORD_RESET_TOKEN_TTL_SECONDS=1800
   PASSWORD_RESET_REQUEST_COOLDOWN_SECONDS=60
   EMAIL_CHANGE_TOKEN_TTL_SECONDS=1800
   SMTP_HOST=smtp.example.org
   SMTP_PORT=587
   SMTP_STARTTLS=true
   SMTP_USE_SSL=false
   SMTP_USERNAME=admin@svehlen.de
   SMTP_FROM_EMAIL=admin@svehlen.de
   SMTP_FROM_NAME=Vereinszentrale
   SMTP_PASSWORD_FILE_HOST=/etc/social-media-agent/production-secrets/smtp_password
   ```

3. Webcontainer neu erstellen und prüfen:

   ```bash
   sudo docker compose --env-file .env.production \
     -f docker-compose.yml -f docker-compose.production.yml \
     up -d --force-recreate web proxy
   sudo ./scripts/production-check.sh
   ```

4. In einem privaten Browserfenster `/password/forgot` testen. Die Seite zeigt
   für bekannte und unbekannte E-Mail-Adressen absichtlich dieselbe Antwort.

Reset-Tokens werden ausschließlich als SHA-256-Hash gespeichert, sind 30
Minuten gültig, nur einmal verwendbar und werden bei einer neuen Anfrage
widerrufen. Ein erfolgreicher Reset hebt Kontosperren auf und beendet alle
bestehenden Sitzungen. Token, SMTP-Passwort und neues Passwort werden nicht in
Audit-Daten gespeichert.
