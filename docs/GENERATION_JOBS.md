# Persistente Generierungsaufträge

Text- und Bildgenerierung läuft ausschließlich im vorhandenen Worker. Die
FastAPI-Routen prüfen CSRF, Rolle und Mannschaftszugriff, legen einen
`generation_jobs`-Datensatz an und leiten sofort auf dessen Statusseite um.

## Zustände und Übergänge

- `queued`: bereit zur Beanspruchung
- `running`: von genau einem Worker mit Lease beansprucht
- `retry_wait`: eindeutig sicher wiederholbarer technischer Fehler mit
  begrenztem exponentiellem Backoff
- `succeeded`: Beitrag beziehungsweise neue Medienversionen gespeichert
- `failed`: fachlicher oder dauerhaft technischer Fehler
- `cancelled`: vor Start abgebrochen oder während der Verarbeitung zum Abbruch
  vorgemerkt
- `manual_review_required`: Worker-Ausfall oder Timeout während einer
  möglicherweise kostenpflichtigen Anfrage; keine automatische Wiederholung

Die Phasen `preparing`, `generating_text`, `generating_feed`,
`generating_story`, `validating`, `saving` und `completed` machen den Fortschritt
im Dashboard sichtbar.

## Beanspruchung, Leases und Neustarts

PostgreSQL-Worker beanspruchen fällige Aufträge in einer kurzen Transaktion mit
`SELECT ... FOR UPDATE SKIP LOCKED`. SQLite-Tests verwenden dieselbe
Zustandsmaschine ohne die PostgreSQL-spezifische Klausel. Eine Lease verhindert,
dass zwei Worker denselben Auftrag verarbeiten.

Läuft eine Lease außerhalb einer kostenpflichtigen Phase ab, wird der Auftrag
sicher erneut eingereiht. Endet sie während `generating_*`, wechselt der Auftrag
zur manuellen Prüfung. Damit können Container- oder VM-Neustarts keine
unkontrollierten OpenAI-Wiederholungen auslösen.

## Idempotenz und Kostenschutz

Ein stabiler aktiver Schlüssel verhindert parallele Ersterstellungen für
dasselbe Spiel und denselben Beitragstyp sowie parallele Rerender-Aufträge für
denselben Beitrag. Zusätzlich bleibt der vollständige Idempotency Key eindeutig.
Ein vorhandener aktiver Hauptbeitrag wird direkt geöffnet.

Eindeutig transiente Datenbankfehler werden höchstens bis zur konfigurierten
Versuchsgrenze mit Backoff wiederholt. Timeouts oder Verbindungsabbrüche nach
möglicher Annahme einer OpenAI-Anfrage werden nie automatisch wiederholt.

## Abbruch, Retry und Diagnose

Wartende Jobs können sofort abgebrochen werden. Bei laufenden Jobs setzt das
Dashboard einen Kündigungswunsch; der Worker prüft ihn zwischen den
Generierungsphasen. Fehlgeschlagene und unklare Jobs können nach fachlicher
Prüfung bewusst erneut eingereiht werden. Jede Queue-, Claim-, Abbruch-, Retry-
und Abschlussaktion wird auditiert.

Der Systemstatus zeigt wartende, laufende, fehlgeschlagene und manuell zu
prüfende Generierungen, den ältesten wartenden sowie den letzten erfolgreichen
Auftrag. Ein Lauf über 15 Minuten wird als auffällig markiert. Für die Diagnose
zusätzlich Worker-Logs und die Felder `phase`, `locked_by`, `locked_at`,
`lease_expires_at`, `attempts`, `error_category` und `error_message` prüfen.

Mehrere Worker dürfen mit derselben Datenbank betrieben werden; ein zusätzlicher
Broker oder ein weiterer Microservice ist nicht erforderlich.
