# Kontrollierte automatische Instagram-Veröffentlichung

## Sicherheitsmodell

Die automatische Veröffentlichung läuft ausschließlich in einer eigenen
Produktionsumgebung. Die vorhandenen Umgebungen behalten ihre bisherigen
Grenzen:

- **Staging:** ausschließlich `DryRunPublisher`, keine Meta-Verbindung.
- **Meta-Test:** echte Aufrufe nur über den manuellen Testassistenten; der
  Scheduler darf keine Instagram-Aufträge verarbeiten.
- **Produktion:** automatische Verarbeitung ist möglich, beginnt aber mit
  vollständig deaktivierten Automatik-Gates.

Der Worker startet die Instagram-Automatik nur, wenn diese drei Schalter
gleichzeitig aktiv sind:

```text
GLOBAL_PUBLISH_ENABLED=true
META_SCHEDULER_ENABLED=true
META_AUTOMATIC_PUBLISH_ENABLED=true
```

Ein teilweise aktivierter Zustand wird beim Workerstart abgelehnt. Zusätzlich
müssen `ENVIRONMENT=production`, `PUBLISHER_MODE=instagram`,
`META_PRODUCTION_ENABLED=true` und `META_TEST_ENABLED=false` gelten.

Das Freischalten der Umgebung allein reicht nicht. Jede Instagram-Seite muss
im Dashboard separat für Publishing und anschließend mit der Bestätigung
`AUTOMATISCH VERÖFFENTLICHEN` für die Automatik aktiviert werden. Feed und
Story können je Seite einzeln zugelassen werden. Manuelle Bildkarussells gelten
als Feed-Typ und benötigen deshalb die Feed-Freigabe der Seite.

## Voraussetzungen pro Auftrag

Unmittelbar vor jedem externen Schritt prüft der Worker erneut:

- der globale Not-Aus existiert und ist ausdrücklich deaktiviert,
- Seite und automatische Veröffentlichung sind aktiv,
- die Seitenfreigabe wurde von einem Administrator bestätigt,
- die Instagram-Verbindung ist verbunden, ein Business-Konto und hinreichend
  frisch geprüft,
- die nötigen Berechtigungen sind vorhanden,
- der Token ist nicht abgelaufen,
- die Medienart ist für die Seite zugelassen,
- der Beitrag ist ausdrücklich freigegeben,
- freigegebene Beitragsversion und Auftragsversion stimmen überein,
- Spiel und Beitrag besitzen keine Automatisierungssperre oder kritische
  Warnung,
- der geplante Zeitpunkt ist erreicht und nicht als veraltet markiert,
- bei einem Karussell sind 2 bis 10 geordnete Dateien vollständig vorhanden;
  für jede Datei stimmen PNG-Validierung und Prüfsumme,
- bei Feed und Story stimmen Datei, PNG-Validierung und Prüfsumme,
- es gibt weder eine bestehende Plattform-ID noch einen konkurrierenden
  aktiven Versuch.

Der Benutzer, der die Beitragsversion freigegeben hat, muss weiterhin aktiv
sein. Eine inaktive oder entfernte Freigabeperson stoppt den Auftrag.

## Automatische Verbindungsprüfung

Bei aktiver Produktionsautomatik prüft der Worker jede aktive, verbundene
Instagram-Seite automatisch zweimal täglich über den offiziellen lesenden
Profilaufruf. Das Standardintervall beträgt 43.200 Sekunden und wird mit
`META_CONNECTION_CHECK_INTERVAL_SECONDS` konfiguriert. Es muss kleiner als
`META_CONNECTION_MAX_AGE_SECONDS` bleiben. Die Prüfung aktualisiert Kontoart,
Benutzername, Verbindungsstatus und Prüfzeitpunkt, wird mandantenbezogen
auditiert und löst weder Containererstellung noch Veröffentlichung aus.

Schlägt die Prüfung fehl, speichert die Anwendung ausschließlich die bereinigte
Fehlermeldung, markiert die Verbindung als fehlerhaft und blockiert weiterhin
sicher jede Veröffentlichung. Nach einem temporären Fehler erfolgt der nächste
Versuch im konfigurierten Intervall; eine erneute OAuth-Verbindung wird nicht
automatisch vorgenommen.

## Zustandsablauf und Idempotenz

Der PostgreSQL-Worker beansprucht fällige Aufträge transaktional mit
`SELECT … FOR UPDATE SKIP LOCKED`. Ein aktiver Datenbankschlüssel verhindert,
dass mehrere Worker für denselben Veröffentlichungsauftrag parallel einen
Meta-Versuch anlegen.

Jeder Durchlauf führt pro Versuch höchstens einen externen Schritt aus:

Bei einem Karussell werden zunächst die Child-Container in der eingefrorenen
Reihenfolge erzeugt und ihre IDs einzeln gespeichert. Erst danach wird ein
Parent-Container erstellt. Bereits gespeicherte Child-IDs werden nach einem
Neustart wiederverwendet; ein Timeout mit unklarer Annahme wird nicht durch
einen kosten- beziehungsweise duplikatenträchtigen Neuversuch übergangen.
Positionsbezogene Instagram-Kontomarkierungen manueller Feed-Beiträge werden
am Feed-Container, bei Karussells am jeweiligen Child-Container als
`user_tags` übertragen. Sie sind Bestandteil des eingefrorenen
Design-Snapshots und werden vor jedem externen Aufruf erneut validiert.

```text
scheduled/retry
  → creating_media_grant
  → creating_container
  → waiting_for_container
  → ready_to_publish
  → publishing
  → completed
```

Container-ID und Media-ID werden sofort persistent gespeichert. Ist eine
Container-ID vorhanden, wird kein zweiter Container erstellt. Ist eine
Media-ID vorhanden, wird `media_publish` nicht erneut aufgerufen.

Statusabfragen dürfen begrenzt wiederholt werden. Ein Timeout oder Abbruch
während eines möglicherweise angenommenen schreibenden Meta-Aufrufs wird als
`uncertain` gespeichert und niemals automatisch wiederholt. Der Vorgang muss
anschließend im Dashboard und direkt bei Instagram abgeglichen werden.

## Erstinstallation der Produktionsumgebung

1. Meta-Test vollständig abnehmen: Verbindung, `validate-only`,
   `container-only`, ein Feed und eine Story.
2. Aktuelles Backup von Datenbank, Uploads und generierten Medien anlegen.
3. `.env.production.example` nach `.env.production` kopieren.
4. eigene Produktionsverzeichnisse und eigene Secret-Dateien unter dem in
   `PRODUCTION_SECRETS_ROOT` angegebenen Pfad anlegen.
5. Öffentliche HTTPS-Adressen für OAuth-Callback und Kurzzeitmedien eintragen.
6. Alle drei Automatik-Gates zunächst auf `false` belassen.
7. Konfiguration prüfen und die getrennte Umgebung starten:

   ```bash
   docker compose --env-file .env.production \
     -f docker-compose.yml \
     -f docker-compose.production.yml config -q

   docker compose --env-file .env.production \
     -f docker-compose.yml \
     -f docker-compose.production.yml up -d --build

   ./scripts/production-check.sh
   ```

8. Über das Produktionsdashboard die Instagram-Seite neu verbinden und die
   Verbindung prüfen. Tokens werden nicht aus Meta-Test kopiert.
9. Feed und Story als erlaubte Medienarten festlegen, Seiten-Publishing
   aktivieren und die Automatik ausdrücklich bestätigen.
10. In der Datenbank beziehungsweise über die geschützte Dashboard-Aktion den
    globalen Not-Aus bewusst deaktivieren.
11. Einen freigegebenen Testbeitrag mit einem künftigen Zeitpunkt verwenden.
12. Erst jetzt alle drei Automatik-Gates gemeinsam auf `true` setzen und Web
    sowie Worker neu erstellen.
13. `scripts/production-check.sh` erneut ausführen und den ersten Feed bis zur
    Plattform-ID beobachten. Erst danach eine Story testen.

## Pausieren und Not-Aus

Für eine geplante Pause werden alle drei Automatik-Gates gemeinsam auf
`false` gesetzt und der Worker neu erstellt. Die persistenten Aufträge bleiben
erhalten und können nach erneuter Prüfung fortgesetzt werden.

Für einen sofortigen Stopp ist der globale Not-Aus im Dashboard zu aktivieren.
Er verhindert jeden noch nicht begonnenen externen Schritt. Ein bereits von
Meta angenommener Aufruf lässt sich dadurch nicht zurückholen; solche Vorgänge
müssen anhand der gespeicherten Container- oder Media-ID abgeglichen werden.

## Betrieb und Diagnose

Der Systemstatus zeigt unter anderem:

- erwarteten und tatsächlichen Automatik-Scheduler,
- automatisch freigegebene Seiten,
- fällige automatische Aufträge,
- aktive automatische Meta-Versuche,
- offene Container,
- unklare und fehlgeschlagene Vorgänge,
- Token- und Verbindungszustände.

Bei einem festhängenden Auftrag zuerst Worker-Heartbeat, Not-Aus,
Seitenfreigabe, Verbindung, Tokenablauf, Beitragsversion und Meta-Versuchsphase
prüfen. Bei `uncertain` niemals den Auftrag zurücksetzen oder erneut senden,
bevor der Instagram-Account und die gespeicherten Plattform-IDs abgeglichen
wurden.

Mehrere Worker sind zulässig. Die Datenbanksperren verhindern parallele
Verarbeitung. Die Batchgröße wird mit `META_SCHEDULER_BATCH_SIZE` begrenzt;
Containerstatus und maximale Wartezeit mit
`META_CONTAINER_POLL_INTERVAL_SECONDS` und
`META_CONTAINER_MAX_WAIT_SECONDS`.
