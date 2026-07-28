# Betriebsanleitung
- Alle Zeiten intern UTC, Anzeige Europe/Berlin. Vor Sommer-/Winterzeitwechsel Testplanung kontrollieren.
- Täglich Datenbank/Dateien sichern; monatlich Restore in separater Umgebung testen.
- `/health` überwachen; Worker-JSON-Logs auf ausbleibenden Heartbeat, `uncertain`, Token- und SMB-Fehler alarmieren.
- Bei Störung zuerst globalen Not-Aus setzen. Unklare Meta-Aufträge niemals blind erneut senden.
- Proxmox→Cloud: Backup, frische Hosts/Volumes/SMB, neue Secrets, Migration, Restore, Dry-Run-Abnahme, dann DNS/TLS und Publishing einzeln aktivieren.
