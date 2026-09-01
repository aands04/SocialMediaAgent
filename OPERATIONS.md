# Betriebsanleitung
- Alle Zeiten intern UTC, Anzeige Europe/Berlin. Vor Sommer-/Winterzeitwechsel Testplanung kontrollieren.
- Täglich Datenbank/Dateien sichern; monatlich Restore in separater Umgebung testen.
- `/health` überwachen; Worker-JSON-Logs auf ausbleibenden Heartbeat, `uncertain`, Token- und SMB-Fehler alarmieren.
- Bei Störung zuerst globalen Not-Aus setzen. Unklare Meta-Aufträge niemals blind erneut senden.
- Proxmox→Cloud: Backup, frische Hosts/Volumes/SMB, neue Secrets, Migration, Restore, Dry-Run-Abnahme, dann DNS/TLS und Publishing einzeln aktivieren.

## Sanitizierte Health-Diagnose

`python -m app.monitoring.health_details` liest den internen `system_status()`
und gibt ausschließlich eine feste Positivliste aggregierter Betriebsdaten als
JSON aus. Der Befehl schreibt nicht in die Datenbank, startet keine Jobs oder
Providerabfragen und gibt insbesondere keine IDs, Kontodaten, Pfade,
Fehlertexte, Tokens oder Secrets aus. Neue Felder aus `system_status()` werden
nicht automatisch übernommen.

Der root-eigene VPS-Wrapper `/usr/local/sbin/socialmedia-admin` wird derzeit
nicht aus diesem Repository installiert oder gepflegt. Nach Deployment des
repositoryseitigen Diagnoseeinstiegs muss ein Root-Operator einmalig folgenden
Case in den bestehenden Wrapper aufnehmen:

```sh
    health-details)
        [ "$#" -eq 1 ] || exit 64
        cd "$PROJECT"
        exec /usr/bin/docker compose \
            --env-file "$ENV_FILE" \
            -f docker-compose.yml \
            -f docker-compose.production.yml \
            exec -T web sh /app/scripts/health-details.sh
        ;;
```

Zusätzlich wird exakt dieser Befehl passwortlos erlaubt:

```sudoers
andi ALL=(root) NOPASSWD: /usr/local/sbin/socialmedia-admin health-details
```

Ist dieser Case bereits installiert, benötigen spätere Erweiterungen des
repositoryseitigen Diagnose-Schemas keine weitere Änderung am Wrapper oder an
sudoers.

Danach lautet der ausschließlich lesende Aufruf:

```bash
sudo -n /usr/local/sbin/socialmedia-admin health-details
```

Die Ausgabe enthält nur `status`, die bekannten festen `critical`-Bezeichnungen,
`unknown_critical_count` als Anzahl nicht freigegebener Critical-Strings und
folgende Checkfelder:

```text
checks.postgresql.ok
checks.worker.ok
checks.scheduler.ok
checks.automatic_scheduler.ok
checks.automatic_fussball_sync.ok
checks.fussball_automatic.ok
checks.fussball_automatic.detail.global_sync_gate
checks.fussball_automatic.detail.enabled_teams
checks.fussball_automatic.detail.running
checks.fussball_automatic.detail.errors
checks.fussball_automatic.detail.stale
checks.fussball_automatic.detail.stale_reasons
checks.fussball_automatic.detail.unhealthy_teams[]:
  display_name
  short_name
  status
  stale_reason
  sync_interval_hours
  consecutive_failures
  last_success_at
  last_completed_at
  next_poll_at
  retry_scheduled
  error_category
checks.smb.ok
checks.publishing.ok
checks.social_media_channels.ok
checks.social_media_channels.detail.{instagram,facebook,whatsapp}:
  enabled_connections
  unhealthy_connections
  non_connected_connections
  missing_last_success
  stale_last_success
  last_check_at
  last_successful_check
  status_counts
```

Die Social-Media-Zahlen werden getrennt für die fest erlaubten Kanaltypen
Instagram, Facebook und WhatsApp ausgegeben. Statuswerte haben ebenfalls eine
feste Positivliste; alle anderen Werte werden nur unter `unknown` gezählt.
Mehrere Gesundheitsgründe derselben Verbindung können gleichzeitig zählen.

`unhealthy_teams` enthält ausschließlich aktivierte, tatsächlich ungesunde
Mannschaften mit Anzeige-/Kurzname und aggregierten Betriebsdaten. Interne IDs,
Quell-URLs und rohe Providerfehler werden nicht übernommen. Die Fehlerkategorie
wird nur aus fest bekannten, anwendungseigenen Fehlertypen bestimmt; unbekannte
Texte ergeben `unknown`. Unbekannte Critical-Bezeichnungen werden nur gezählt
und niemals ausgegeben. Bei neuen Critical-, Stale-, Status- oder
Fehlerkategorien muss die Allowlist bewusst per Review erweitert werden.
