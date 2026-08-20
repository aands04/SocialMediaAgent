# Kontrollierte FuPa-Browserübergabe

Stand der Prüfung: 20. August 2026

## Voraussetzungen und Entscheidung

FuPa beschreibt das Anlegen eines Spielberichts als Funktion für angemeldete
Vereinsverwalter oder Ligaleiter in der Vereinsverwaltung. Eine öffentlich
dokumentierte Schreib-API für diesen Vorgang wurde nicht gefunden. Deshalb ist
die Browserübergabe ein standardmäßig deaktivierter, ausdrücklich zu
bestätigender Zusatzweg und keine offiziell zugesicherte API-Integration.

Offizielle FuPa-Quellen:

- [Spielbericht zu einem Match anlegen](https://support.fupa.net/support/solutions/articles/75000020122-spielbericht-zu-einem-match-anlegen)
- [FuPaner/Vereinsverwalter werden](https://support.fupa.net/support/solutions/articles/75000014337-fupaner-vereinsverwalter-werden)
- [FuPa-Nutzungsbedingungen](https://www.fupa.net/about/terms-of-use)

Vor einem kommerziellen SaaS-Betrieb sollte FuPa die Nutzung dieses
browsergestützten Ablaufs ausdrücklich bestätigen. Änderungen an FuPa können
den Ablauf jederzeit unterbrechen.

## Sicherheitsmodell

- Ein vorhandenes, für den Verein berechtigtes FuPa-Konto ist zwingend.
- Der Administrator meldet sich selbst im echten FuPa-Browserfenster an.
- Die Vereinszentrale fragt weder Benutzername noch Passwort ab.
- Es wird nur ein auf `fupa.net` begrenzter Playwright-Sitzungszustand
  übernommen, serverseitig verschlüsselt und einem einzelnen Verein zugeordnet.
- Fremde Cookies und Origins werden vor dem Speichern entfernt.
- CAPTCHA, Zwei-Faktor-Prüfung, Login-Seiten und unbekannte Oberflächen führen
  zum sicheren Abbruch. Es gibt keine Umgehung.
- Jede Übertragung benötigt einen freigegebenen Bericht, einen Administrator
  und eine zusätzliche ausdrückliche Bestätigung.
- Ohne eindeutige FuPa-Speicherbestätigung wird der Bericht nicht als
  übertragen markiert.

## Einrichtung

1. Auf einem vertrauenswürdigen Rechner im Repository ausführen:

   ```bash
   python scripts/capture_fupa_session.py --output fupa-session.json
   ```

2. Im geöffneten FuPa-Fenster manuell anmelden und eine bearbeitbare
   Vereinsverwaltungsseite öffnen.
3. Im Terminal Enter drücken.
4. Die erzeugte Datei im Spielbericht unter „FuPa-Anmeldung verwalten“
   hochladen. Sie wird vor dem Speichern auf FuPa-Daten reduziert und
   verschlüsselt.
5. Die lokale JSON-Datei sicher löschen.
6. Einen freigegebenen Bericht und das Zielspiel prüfen, die Bestätigung setzen
   und „Jetzt bei FuPa speichern“ wählen.

## Aktivierung

```env
FUPA_BROWSER_PUBLISH_ENABLED=true
FUPA_BROWSER_HEADLESS=true
FUPA_BROWSER_TIMEOUT_SECONDS=30
FUPA_BROWSER_SESSION_MAX_BYTES=524288
```

Die vorhandene `META_TOKEN_ENCRYPTION_KEY` wird für die authentifizierte
Verschlüsselung der FuPa-Sitzung wiederverwendet. Es darf kein FuPa-Passwort in
Umgebungsvariablen, Datenbank, Logs oder Auditdaten eingetragen werden.

## Grenzen und Betrieb

Die Selektoren orientieren sich an der sichtbaren deutschsprachigen
FuPa-Oberfläche. Bei einer Änderung wird nicht geraten: Der Vorgang endet mit
einer verständlichen Fehlermeldung, ohne den Bericht als übertragen zu
kennzeichnen. Eine abgelaufene Anmeldung muss interaktiv neu erzeugt werden.
Automatische Hintergrundveröffentlichungen bei FuPa bleiben deaktiviert.
