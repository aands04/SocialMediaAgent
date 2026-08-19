# Umsetzungsplan FuPa-Spielberichte

1. Optionale FuPa-Referenzen an Mannschaften und Spielen ergänzen.
2. Mandantengebundene Snapshots, Rückfragen, Berichte, Versionen und
   Veröffentlichungsversuche als neue Tabellen anlegen.
3. Einen sicheren `FupaReader` mit normalisierten Ergebnis- und Tickerdaten
   implementieren.
4. Quellen in einem unveränderlichen `MatchContentContext` zusammenführen und
   Konflikte deterministisch erkennen.
5. WhatsApp-Rückfragen über die bestehende Meta-Verbindung versenden und
   Antworten vor der Live-Ereignisinterpretation eindeutig zuordnen.
6. Berichtsgenerator mit strukturiertem Ergebnis, Faktenvalidierung,
   Vereinsbeispielen und gelernter Tonalität implementieren.
7. Worker-Zyklus für fällige Abrufe, Fristen und Generierungen ergänzen.
8. Spielbericht-Seite mit Quellencheck, Notizen, Versionen, Freigabe und
   kontrollierter manueller FuPa-Übergabe ergänzen.
9. Mandanten-, Rollen-, Parser-, Konflikt-, Versions-, Scheduler- und
   Idempotenztests ergänzen.
10. Migration Upgrade/Downgrade, Ruff, Compileall und relevante Test-Suiten
    ausführen.
