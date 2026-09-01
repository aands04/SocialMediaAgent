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
            exec -T web /app/scripts/entrypoint.sh \
            python -m app.monitoring.health_details
        ;;
```

Zusätzlich wird exakt dieser Befehl passwortlos erlaubt:

```sudoers
andi ALL=(root) NOPASSWD: /usr/local/sbin/socialmedia-admin health-details
```

Danach lautet der ausschließlich lesende Aufruf:

```bash
sudo -n /usr/local/sbin/socialmedia-admin health-details
```

Die Ausgabe enthält nur `status`, die bekannten festen `critical`-Bezeichnungen
und folgende Checkfelder:

```text
checks.scheduler.ok
checks.automatic_scheduler.ok
checks.fussball_automatic.ok
checks.fussball_automatic.detail.global_sync_gate
checks.fussball_automatic.detail.enabled_teams
checks.fussball_automatic.detail.running
checks.fussball_automatic.detail.errors
checks.fussball_automatic.detail.stale
checks.fussball_automatic.detail.stale_reasons
checks.smb.ok
checks.publishing.ok
checks.social_media_channels.ok
checks.social_media_channels.detail.enabled_connections
checks.social_media_channels.detail.unhealthy_connections
checks.social_media_channels.detail.last_successful_check
```

Die Social-Media-Zahlen werden über die bekannten Kanaltypen Instagram,
Facebook und WhatsApp summiert; `last_successful_check` ist ausschließlich der
neueste aggregierte UTC-Zeitstempel. Bei neuen Critical- oder Stale-Gründen muss
die Allowlist bewusst per Review erweitert werden.
