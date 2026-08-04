# Automatische FUSSBALL.DE-Synchronisation

Die Produktionsumgebung kann den öffentlichen Mannschaftsspielplan regelmäßig
lesen, Spiele idempotent aktualisieren, Ergebnisse kontrolliert bestätigen und
freigabepflichtige Beitragsentwürfe einreihen. Sie veröffentlicht dadurch
nichts ungeprüft: Jeder neue oder geänderte Beitrag durchläuft weiterhin die
vorhandene manuelle Freigabe und die versionsgebundenen Publishing-Gates.

## Sicherheitsmodell

Die Funktion besitzt zwei globale und zwei mannschaftsbezogene Opt-ins:

- `FUSSBALL_AUTOMATIC_SYNC_ENABLED=true` erlaubt dem Produktionsworker Abrufe.
- `AUTOMATIC_POST_GENERATION_ENABLED=true` erlaubt das Einreihen von Entwürfen.
- **Regeln & Storys → FUSSBALL.DE-Spielplan automatisch abrufen** aktiviert
  eine einzelne Mannschaft.
- **Freigabepflichtige Entwürfe automatisch erzeugen** aktiviert die
  Entwurfsplanung dieser Mannschaft.

Die globale Entwurfserzeugung darf nicht ohne den globalen Abruf aktiviert
werden. Staging und Meta-Test verweigern beide Funktionen technisch. Absagen,
Verlegungen, gelöschte/importgesperrte Spiele und nicht zugelassene vorläufige
Spielpläne werden nicht verarbeitet.

## Ablauf

1. Der vorhandene Worker beansprucht genau eine fällige Mannschaft über eine
   persistente Lease. PostgreSQL verwendet `FOR UPDATE SKIP LOCKED`.
2. Der Mannschaftsspielplan wird mit Größenlimit, Timeout und begrenzten
   Versuchen gelesen. Neue oder noch unvollständige Spiele werden über ihre
   öffentliche Detailseite um Platz, Platzart und Anschrift ergänzt.
3. Das unveränderte HTML und das Parsergebnis werden als Provider-Snapshot
   archiviert.
4. Der bestehende Import aktualisiert Spiele anhand
   `(team_id, provider, external_id)`. Doppelte Spiele entstehen nicht.
5. Relevante Änderungen entziehen weiterhin offene Freigaben. Manuelle
   Overrides, Löschsperren und veröffentlichte Aufträge bleiben erhalten.
6. Fällige Ankündigungen, optionale Erinnerungen und bestätigte Ergebnisse
   werden als persistente Generierungsjobs eingereiht. Der Browser und der
   Abrufworker führen keine OpenAI-Anfrage synchron aus.

Das normale Intervall ist `FUSSBALL_SYNC_INTERVAL_SECONDS` (Standard 1800).
Von fünf Stunden nach bis zwei Stunden vor einem Spiel wird zur
Ergebnisbeobachtung `FUSSBALL_RESULT_POLL_INTERVAL_SECONDS` (Standard 300)
verwendet. Fehler führen zu begrenztem exponentiellem Backoff.

## Ergebniserkennung

Normale ASCII-Ergebnisse werden direkt gelesen. Für die von FUSSBALL.DE
verwendete dynamische Symbolschrift wird ausschließlich deren offizieller,
zum HTML gehörender TrueType-Font geladen. Der Parser validiert Header,
Tabellenbereiche, Glyphenzahl und vollständige Ziffernbelegung und liest die
Unicode-Zuordnung deterministisch. Es findet weder OCR noch visuelles Raten
statt. Eine strukturell abweichende Schrift wird abgelehnt und führt nach
Spielende zu einem manuellen Prüfhinweis.

Ein Ergebnis gilt erst als bestätigt, wenn:

- das Spiel mindestens `FUSSBALL_RESULT_MIN_AGE_MINUTES` alt ist,
- dieselbe Torfolge in mindestens zwei verschiedenen Snapshots vorkommt und
- sie mindestens `FUSSBALL_RESULT_STABILITY_SECONDS` unverändert blieb.

Nur Spiele innerhalb von `FUSSBALL_RESULT_MAX_AGE_HOURS` (Standard 48) werden
für neue automatische Ergebnisbeiträge betrachtet. Dadurch erzeugt der erste
Produktionsabruf keine Entwürfe für historische Partien.

Eine geänderte Torfolge beginnt die Stabilitätsprüfung neu. Erst danach wird
das Spiel auf `finished` gesetzt. Im Zeitmodell **sofort nach bestätigter
Erkennung** werden Feed und Ergebnis-Story ohne weitere Wartezeit eingeplant.
Die mannschaftsbezogene **Wartezeit nach Ergebnis** gilt nur für geplante
relative oder feste Ergebniszeiten. Eine spätere
Ergebniskorrektur entzieht offene Freigaben und erfordert eine neue Prüfung.

## Beitragsplanung

Der **Generierungsvorlauf** bestimmt, wie lange vor dem frühesten Feed- oder
Story-Zeitpunkt der kostenpflichtige Hintergrundjob beginnen darf. Ein stabiler
Idempotency Key verhindert mehrfache aktive Aufträge für dasselbe Spiel und
denselben Beitragstyp.

- `announcement`: nach den vorhandenen Ankündigungs- und Story-Regeln.
- `reminder`: nur bei aktiviertem separatem Erinnerungsbeitrag.
- `result`: erst nach bestätigtem Ergebnis und der Ergebniszeitregel.

Die erzeugten Beiträge starten grundsätzlich im Zustand `pending_approval`.
Nur wenn die getrennte mannschaftsbezogene Option für automatische Freigaben
aktiv ist, durchlaufen sie anschließend sofort den normalen Freigabeservice.
Gemeinsame Vereins-Karussells werden nur automatisch freigegeben, wenn alle
beteiligten Mannschaften diese Option aktiviert haben.

## Erstes Aktivieren in Produktion

1. Backup erstellen und `main` aktualisieren.
2. Images mit `docker-compose.production.yml` neu bauen und starten.
3. Prüfen, dass Alembic `0009 (head)` meldet.
4. In `.env.production` beide neuen globalen Gates zunächst `false` lassen.
5. Unter **Regeln & Storys** nur für eine Mannschaft Abruf, vorläufige Spiele
   (falls fachlich gewünscht), Entwurfsarten, Zeiten und Generierungsvorlauf
   konfigurieren.
6. Erst `FUSSBALL_AUTOMATIC_SYNC_ENABLED=true` setzen, Web und Worker neu
   erstellen und einen vollständigen Abruf im Systemstatus prüfen.
7. Spiele, Spielorte, Status und Ergebniswarnungen kontrollieren. Noch darf
   kein automatisch erzeugter Beitrag entstehen.
8. Danach `AUTOMATIC_POST_GENERATION_ENABLED=true` setzen und nur den Worker
   neu erstellen.
9. Prüfen, dass genau ein Generierungsauftrag entsteht und der fertige Beitrag
   weiterhin manuell freigegeben werden muss.
10. `scripts/production-check.sh` erneut ausführen und mindestens ein ganzes
    Spielwochenende beobachten.

Zum sofortigen Pausieren zuerst
`AUTOMATIC_POST_GENERATION_ENABLED=false`, anschließend bei Bedarf
`FUSSBALL_AUTOMATIC_SYNC_ENABLED=false` setzen und den Worker neu erstellen.
Der globale Instagram-Not-Aus bleibt davon unabhängig verfügbar.

## Diagnose

Der Systemstatus zeigt globale Gates, aktivierte Mannschaften, laufende und
fehlerhafte Zustände, veraltete Abrufe und den letzten Erfolg. Der
Worker-Heartbeat enthält außerdem das Ergebnis des letzten Abrufzyklus.

Bei Problemen:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.production.yml \
  logs --since=30m --no-color worker web

docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.production.yml \
  exec -T web /app/scripts/entrypoint.sh alembic current

./scripts/production-check.sh
```

Providerfehler lösen keine automatische Löschung vorhandener Spiele aus. Ein
manuell gelöschtes importiertes Spiel bleibt durch `import_suppressed`
unterdrückt und wird bei späteren Abrufen nicht wieder aktiviert.
