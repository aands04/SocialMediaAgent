# SaaS-Deployment und Objektspeicher

## Rollout-Reihenfolge

1. Datenbankbackup und Wiederherstellungstest durchführen.
2. `scripts/tenant_migration_preflight.py` gegen eine Datenbankkopie ausführen.
3. `INITIAL_CLUB_ID`, `INITIAL_CLUB_NAME`, `INITIAL_CLUB_SHORT_NAME` und
   `INITIAL_CLUB_SLUG` eindeutig setzen.
4. Migrationen in einer Wartungsphase ausführen und den persistierten
   Migrationsbericht prüfen.
5. ersten PlatformAdmin mit `scripts/platform_admin.py` anlegen.
6. zunächst `MULTI_TENANT_ENABLED=false`, Selbstregistrierung und Billing
   deaktiviert lassen; Isolationstests und Smoke-Test ausführen.
7. S3-Provider konfigurieren, direkten Testupload ausführen und anschließend
   den geschützten Speicherabgleich in der Plattformübersicht ohne Abweichung
   abschließen.
8. erst danach Mandantenbetrieb aktivieren.

## Cloudflare R2 (bevorzugter Start)

Einen privaten Bucket mit EU-Datenresidenz anlegen und einen auf diesen Bucket
begrenzten API-Token verwenden:

```env
OBJECT_STORAGE_PROVIDER=cloudflare-r2
S3_ENDPOINT_URL=https://ACCOUNT_ID.r2.cloudflarestorage.com
S3_REGION=auto
S3_BUCKET=vereinszentrale-private
```

`S3_ACCESS_KEY_ID` und `S3_SECRET_ACCESS_KEY` werden als Container-Secrets
injiziert, nie committed. CORS erlaubt nur die Dashboard-Origin, `PUT`, die
benötigten Content-Type-Header und kurze Ablaufzeiten. Der Bucket bleibt privat.
Lifecycle-Regeln löschen `publishing/` nach der festgelegten Frist.

## Hetzner Object Storage

```env
OBJECT_STORAGE_PROVIDER=hetzner-object-storage
S3_ENDPOINT_URL=https://fsn1.your-objectstorage.com
S3_REGION=fsn1
S3_BUCKET=vereinszentrale-private
```

Auch hier gelten private ACLs, eingeschränkte Credentials, CORS nur für die
Anwendung und Lifecycle-Regeln für Publishing-Objekte.

## Lokal und SMB

`OBJECT_STORAGE_PROVIDER=local` ist für Entwicklung. Der eigenständige
`SmbImportProvider` bindet vorhandene Mounts ausschließlich lesend ein und
verweigert Upload, Löschen und öffentliche Signierung. SMB ist damit eine
Importquelle für Altinstallationen und ausdrücklich kein primärer SaaS-Speicher.

## Bekannte Übergangsgrenze

Neue direkte SaaS-Uploads und Ledger sind objektbasiert. Historische lokale
Medien- und Rendererpfade bleiben kompatibel lesbar und werden nicht ungeprüft
automatisch verschoben. Eine vollständige Bestandsobjektmigration muss vor dem
Abschalten des Legacypfads separat geplant, geprüft und reconciled werden.
