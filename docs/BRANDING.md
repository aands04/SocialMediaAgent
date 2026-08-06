# Branding-Assistent

Der Bereich **Vereinsbranding** führt Vereinsadministratoren durch fünf klar
getrennte Bereiche: Grunddesign, Bildgestaltung, Text und Sprache, Sponsoren
und Pflichtangaben sowie erweiterte Vorgaben. Die geschützten
Plattform-Promptvorlagen sind kein Bestandteil dieser Seite und werden weder
im HTML noch im JavaScript oder in API-Antworten ausgeliefert.

## Speicherung und Standardwerte

Die vorhandene, versionierte `ClubBrandingConfiguration` bleibt die einzige
Quelle der Branding-Einstellungen. Strukturierte Bildwerte werden weiterhin in
`image_settings`, Textwerte in `text_settings` gespeichert. Schriftarten
bleiben über `primary_font_id` und `secondary_font_id` referenziert. Deshalb ist
für den Assistenten keine neue Datenbankmigration erforderlich.

Sichere Standardwerte verwenden unter anderem Dunkelblau und Weiß als neutrale
Ausgangsfarben, einen modernen Grafikstil, normale Sicherheitsabstände, eine
ausgewogene Bilddynamik und eine sachliche mittellange Textgestaltung. Die
Aktionen **Empfohlene Einstellungen verwenden** und **Auf Standardwerte
zurücksetzen** laufen über denselben versionsgeprüften und auditierten
Speicherpfad wie das normale Speichern. Verifizierte Vereinsassets und nicht
eindeutig übertragbare Altwerte werden beim Zurücksetzen nicht stillschweigend
gelöscht.

## Datenübernahme

Eindeutig interpretierbare bisherige Freitextwerte werden beim Laden in die
neuen Auswahl- und Listenelemente überführt. Dazu gehören insbesondere Farben,
Hashtags, Erwähnungen sowie Feed- und Story-Zusatzvorgaben. Werte, die nicht
verlustfrei einer Auswahl zugeordnet werden können, verbleiben unter
`legacy_values` und werden im eingeklappten Bereich **Erweiterte Vorgaben**
angezeigt. Alte Einstellungen bleiben damit lesbar und werden bei erneutem
Speichern nicht kommentarlos verworfen.

Listen werden als JSON-Listen gespeichert. Mannschaftsschreibweisen und
Sponsoren sind strukturierte JSON-Objekte; unsichere Trennzeichenketten sind
nicht die persistente Quelle. Die serverseitige Validierung normalisiert
Hashtags und Instagram-Benutzernamen, entfernt Duplikate, prüft Hexfarben und
verwirft unbekannte Auswahlwerte.

## Dynamische Beispiele

Textbeispiele verwenden ausschließlich Daten des aktiven Vereins:

- Vereinsname und optionaler Kurzname aus `Club`,
- strukturierter Anzeigename einer aktiven Mannschaft,
- ausgewählte Heimspielstätte aus Heimspielen des Vereins.

Fehlen Daten, erscheinen ausschließlich die neutralen Bezeichnungen
**Dein Verein** und **eurer Heimspielstätte**. Reale Vereins- oder Ortsnamen
sind nicht als Rückfallwerte im Quellcode hinterlegt. Änderungen an
Mannschaftsschreibweise oder Spielstätte aktualisieren die Beispiele im Browser
ohne externen Dienstaufruf.

## Mandantenschutz und Berechtigungen

`GET /branding`, `POST /branding` und Schriftvorschauen verlangen eine aktive
Administratorsitzung. Der `TenantSession`-Scope filtert Verein, Mannschaften,
Spiele, Logos, Medien und Schriftarten auf `current.club_id`. Beim Speichern
werden sämtliche übermittelten Mannschafts-, Medien-, Logo- und Schrift-IDs
erneut serverseitig gegen denselben Tenant geprüft. Konfigurations- und
Vereinsversion verhindern unbemerkte parallele Überschreibungen. CSRF-Schutz
und Audit-Protokoll bleiben aktiv.

## Vorschau

Die fixierte Vorschau kann zwischen Feed und Story sowie Spielankündigung und
Ergebnisbeitrag wechseln. Sie zeigt Farben, lokale Schriftarten, Stil,
Hintergrund, Ausrichtung, Logo- und Spielerposition, Abstände, Textmenge und
Sponsorenbereich. Sie arbeitet ausschließlich lokal mit validierten
Formularwerten und privaten, berechtigungsgeprüften Asset-Endpunkten. Es gibt
keinen KI- oder Prompt-Vorschau-Endpunkt, keine Speicherung bei
Vorschauänderungen und keine externen Requests.

Die Vorschau verwendet dieselben strukturierten Brandingwerte wie Renderer und
KI-Generierungsablauf, ist aber keine pixelgenaue Simulation eines generativen
Bildmodells. Die tatsächliche Ausgabe bleibt deshalb vor der Freigabe visuell
zu prüfen.

## Sponsoren und Spielstätten

Das aktuelle Datenmodell besitzt noch keine eigenständige Sponsoren- oder
Spielstättenbibliothek. Spielstätten werden tenant-sicher aus vorhandenen
Heimspielen abgeleitet. Sponsoren werden als strukturierte, wiederholbare Liste
in der Branding-Konfiguration gespeichert und können vorhandene Medien und
Mannschaften des Vereins referenzieren. Diese Struktur kann später ohne
Änderung des Bedienkonzepts in eigene Entitäten überführt werden.

Ohne JavaScript bleiben Felder, Altwerte und Speichern verfügbar. Dynamische
Komfortfunktionen wie Tag-Eingabe, Sponsorenzeilen, Schriftvorschau,
Live-Vorschau und Warnung vor ungespeicherten Änderungen benötigen JavaScript.

