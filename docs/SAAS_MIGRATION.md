# Migration einer bestehenden Installation

Die vorhandenen Migrationen `0001` bis `0015` bleiben unverändert. Die
SaaS-Migration beginnt mit `0016` und ordnet Bestandsdaten genau einem explizit
konfigurierten initialen Club zu.

## Vorprüfung

```powershell
python scripts/tenant_migration_preflight.py
```

Der Bericht listet Vereinsnamen, Mengen, verwaiste Beziehungen und den
erwarteten initialen Club. Mehrdeutige Vereinsdaten oder fehlende
`INITIAL_CLUB_*`-Werte bei Bestandsdaten führen zum sicheren Abbruch. Die
Anwendung nimmt später nie automatisch diesen Club an.

## Empfohlener Ablauf

1. Produktionsbackup und Restoretest;
2. Preflight gegen eine Datenbankkopie;
3. UUID und Slug festlegen und unverändert dokumentieren;
4. `alembic upgrade head` in der Kopie;
5. `tenant_migration_reports` und Counts prüfen;
6. Isolationstests mit mindestens zwei Testclubs durchführen;
7. Downgrade nur in der Kopie testen;
8. Wartungsfenster für Produktion und erneutes Backup;
9. Migration, Smoke-Tests und PlatformAdmin-Anlage;
10. Worker erst nach erfolgreicher Prüfung starten.

Ein Downgrade entfernt SaaS-Zuordnungen und ist deshalb nur als getesteter
technischer Rollback auf einer gesicherten Kopie vorgesehen. Produktive Daten
dürfen nicht ohne vollständigen Restore zurückgestuft werden.
