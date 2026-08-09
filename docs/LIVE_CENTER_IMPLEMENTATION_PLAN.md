# Live Center – Implementierungs- und Migrationsplan

## Risiken

1. **Mandantenverwechslung im Webhook:** Inhalte dürfen erst nach eindeutiger Zuordnung der
   technischen Telefonnummer-ID verarbeitet werden.
2. **Doppelte Webhooks:** Meta wiederholt Zustellungen. Alle Eingänge und Folgeaktionen
   benötigen Idempotenzschlüssel.
3. **Falsche Live-Daten:** Ein Parser darf unplausible Ergebnisse nicht automatisch
   bestätigen. Korrekturen bleiben historisch sichtbar.
4. **Unzulässiger Versand:** WhatsApp-Ausgänge benötigen weiterhin Opt-in und gegebenenfalls
   genehmigte Templates. Gruppen sind nur bei nachgewiesener OBA-Capability möglich.
5. **Rollen- und Mannschaftsrechte:** Vereinsrolle allein reicht nicht. Spiel, Reporter und
   Mannschaft müssen im selben Tenant liegen.
6. **Kosten:** Eine optionale KI-Interpretation wird als eigener Usage-Typ
   `live_event_parsing` erfasst und nicht mit Beitragsgenerierungen vermischt.

## Umsetzungsschritte

1. Neue, ausschließlich additive Alembic-Migration für Reporter, Ereignisse, Live-Status,
   Regeln und Auslieferungen.
2. Deterministischer Ereignisparser und streng typisierte Provider-Schnittstelle.
3. Tenant- und teamgesicherter Live-Service mit Idempotenz, Plausibilität, Bestätigung und
   Korrekturhistorie.
4. Einbindung in den vorhandenen signaturgeprüften WhatsApp-Webhook.
5. Live-Center-Dashboard, manueller Spielleiter, Reporter- und Regelverwaltung.
6. Regelbasierte Auslieferungsentscheidungen unter Beibehaltung aller Not-Aus-,
   Kanal-, Freigabe- und Opt-in-Prüfungen.
7. Simulator mit markiertem Testmodus; keine echten Meta-Aufrufe.
8. Tests für Parser, Rollen, Cross-Tenant-Zugriff, Idempotenz, Plausibilität, Korrekturen,
   Webhook-Signatur und falsche Telefonnummer-ID.
9. Upgrade-/Downgrade-Prüfung, vollständige Testreihe, Ruff, Compileall,
   Compose-Konfiguration und `git diff --check`.

## Rollout

Die Migration ist additiv und verändert keine bestehenden Spiele, Beiträge oder
Veröffentlichungen. Nach dem Deployment bleibt Live-Publishing standardmäßig aus. Ein
Vereinsadministrator richtet zuerst Reporter und Mannschaften ein, testet den Simulator und
aktiviert anschließend einzelne Regeln. Bestehende Instagram-/Facebook-/WhatsApp-Automatik
wird nicht rückwirkend verändert.
