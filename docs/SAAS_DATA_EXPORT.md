# Datenschutz, Clubexport und spätere Löschung

Ein Clubexport muss alle clubgebundenen Daten anhand der unveränderlichen UUID
ausgeben: Benutzer, Teams, Spiele, Medienmetadaten und -objekte, Posts,
Veröffentlichungen, Jobs, Provider-Snapshots, Branding, Audit sowie Usage- und
Storage-Ledger. Plattformweite Prompts, Secrets, andere Clubs und interne
Plattformkosten sind ausgeschlossen.

Archivierung ist reversibel und löscht nichts. Sie invalidiert Sitzungen und
blockiert Uploads, Generierung, Publishing und Scheduler. Die endgültige
Löschung bleibt deaktiviert, bis folgender Workflow implementiert und rechtlich
abgenommen ist:

1. PlatformAdmin-Anforderung mit starker Bestätigung;
2. definierte Wartefrist;
3. Hinweis und optionaler Export;
4. Prüfung offener Veröffentlichungen und Aufbewahrungspflichten;
5. idempotenter Löschjob für Datenbank und Club-UUID-Objektnamespace;
6. Abschlussbericht und nicht sensible Auditspur.

Backups unterliegen separaten Lösch- und Aufbewahrungsfristen. Signierte URLs
und Uploadreservierungen werden unabhängig davon kurzlebig gehalten.
