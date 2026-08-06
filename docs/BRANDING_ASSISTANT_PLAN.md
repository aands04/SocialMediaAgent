# Technischer Plan: Branding-Assistent

## Bestandsanalyse

- Die Vereinsansicht wird über `GET /branding` und `POST /branding` in
  `app/admin_routes.py` bereitgestellt. Beide Routen sind auf angemeldete
  Vereinsadministratoren beschränkt. Der aktive Tenant-Scope filtert alle
  mandantenfähigen SQLAlchemy-Modelle zusätzlich in der Session.
- Die Konfiguration liegt bereits versioniert in
  `ClubBrandingConfiguration.image_settings` und `text_settings`. Schriftarten
  werden über `primary_font_id` und `secondary_font_id` referenziert. Das
  Club-Modell enthält zusätzlich ausschließlich für Altbestände
  `branding_settings`.
- Branding-Daten werden in `app/branding/service.py` validiert und nur als
  strukturierter Datenblock in geschützte Plattform-Prompts eingesetzt. Der
  Promptinhalt selbst wird nicht an Vereinsrouten oder Templates übertragen.
- Mannschaften, Spiele, Logos, Medien und Schriftarten sind mandantenfähig.
  Ein eigenes Spielstätten- oder Sponsorenmodell existiert derzeit nicht.
  Heimspielstätten können sicher aus den Spielen und Mannschaften des aktiven
  Vereins abgeleitet werden. Sponsoren werden deshalb in dieser Ausbaustufe als
  validierte strukturierte Liste in der vorhandenen Branding-Konfiguration
  gespeichert.
- Der HTML/CSS-Fallback-Renderer verwendet bereits Farben, Logos und
  Schriftarten. KI-Generierungen erhalten die validierte Branding-Konfiguration
  über den serverseitigen Prompt-Service. Eine Vorschau darf daher die
  strukturierten Regeln visualisieren, ohne Prompttexte zu kennen.
- Migration `0016` hat die Branding-Tabelle eingeführt. Da JSON bereits die
  benötigten strukturierten Werte tragen kann, ist keine Schemaänderung nötig.

## Umsetzung

1. Das bestehende Validierungsmodul erhält ein explizites Schema für Auswahlwerte,
   Farben, Listen, Mannschaftsschreibweisen, Sponsoren sowie Feed- und
   Story-Einstellungen.
2. Altwerte werden beim Lesen in eine Darstellungsstruktur überführt. Eindeutig
   überführbare Werte erscheinen in den neuen Bedienelementen. Nicht eindeutig
   überführbare Werte bleiben unter `legacy_values` erhalten und werden im
   Bereich „Erweiterte Vorgaben“ ausgewiesen.
3. Mannschaften, Logos, Schriftarten, Medien und aus Heimspielen abgeleitete
   Spielstätten werden ausschließlich für `current.club_id` geladen. Referenzen
   werden beim Speichern erneut serverseitig gegen denselben Verein geprüft.
4. Die Seite wird als fünfteiliger Assistent mit Abschnittsnavigation,
   Fortschrittsanzeige, strukturierten Auswahlkomponenten und fixierter Vorschau
   umgesetzt. Ohne JavaScript bleiben vorhandene Werte sichtbar und das Formular
   kann weiterhin gespeichert werden; Komfortfunktionen benötigen JavaScript.
5. Die Live-Vorschau arbeitet ausschließlich mit bereits an das Formular
   übergebenen, validierten Vereinsdaten. Es gibt keinen Prompt- oder
   KI-Vorschau-Endpunkt und keine externen Requests.
6. Speichern, empfohlene Werte und Zurücksetzen verwenden denselben
   transaktionalen, versionsgeprüften Schreibpfad. Zurücksetzen löscht weder Logo
   noch bestehende Altwerte stillschweigend.
7. Tests decken Validierung, Normalisierung, Altwerte, Tenant-Isolation,
   Berechtigungen, CSRF, dynamische Beispiele, Fortschritt und Rücksetzen ab.

## Bewusste Grenze

Eine eigenständige, medienübergreifende Sponsorenbibliothek wird nicht parallel
zur bestehenden Medienbibliothek eingeführt. Die strukturierte Sponsorenliste
referenziert vorhandene Medien und Mannschaften. Sie kann später ohne Änderung
der Bedienlogik auf ein eigenes Sponsorenmodell migriert werden.
