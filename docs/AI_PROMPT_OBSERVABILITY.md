# KI-Promptverwaltung und Provider-Protokoll

## Spielstätten und Branding

Bei einem Heimspiel verwendet die Prompt- und Textaufbereitung zuerst die im
Vereinsbranding gepflegte **Kurzbezeichnung der Heimspielstätte**, danach die
ausgewählte Standard-Heimspielstätte und erst danach den Spielort des
Spieldatenproviders. Damit kann eine für Social Media geeignete Kurzbezeichnung
einen technischen Provider-Namen ersetzen. Bei Auswärtsspielen bleibt der
verifizierte Spielort des konkreten Spiels maßgeblich.

Die eingebauten Standardschriften sind kontrollierte Schlüssel und keine frei
eingebbaren CSS-Werte. Das Produktionsimage installiert DejaVu und Liberation;
zusätzlich steht die Systemschrift zur Verfügung. Eigene Vereinsschriften
bleiben tenantgebundene `FontAsset`-Referenzen.

## Versionierte Plattformvorlagen

Der effektive Provider-Prompt besteht aus Fakten- und Sicherheitsregeln, der
versionierten Plattformvorlage sowie einem serverseitig semantisch kompilierten
Brandingblock. Vereinsbenutzer sehen weder die Vorlage noch den finalen Prompt.
Der Compiler verwendet ausschließlich validierte Auswahlwerte und strukturierte
Listen; er überträgt keinen rohen Branding-JSON-Block. Die Brandingwerte haben
bei rein stilistischen Konflikten Vorrang vor allgemeineren Vorlagenangaben.

Für Bildvarianten werden nachvollziehbar unterschiedliche
Kompositionsrichtungen ergänzt. Ein gemeinsamer Spieltagsbeitrag erhält einen
einzigen Textauftrag mit den vollständigen Fakten aller enthaltenen Spiele und
ohne Bevorzugung einer Mannschaft.

Nur ein `PlatformAdmin` kann `/prompts` aufrufen. Dort stehen sowohl die
eingebauten Kombinationen aus Ankündigung, Erinnerung oder Ergebnis und
Text, Feed oder Story als auch gespeicherte Versionen zur Auswahl.

- Eine eingebaute Vorlage wird nie überschrieben.
- Eine gespeicherte Version wird nie nachträglich verändert.
- **Anzeigen / bearbeiten** erzeugt beim Speichern eine neue Entwurfsversion.
- Erst die gesonderte Aktion **Aktivieren** macht die neue Version wirksam.
- Aktivierung und Archivierung werden im Plattform-Audit ohne Prompttext
  protokolliert.

Fixture-Vorschauen und Versionsvergleiche rufen keinen KI-Anbieter auf.

Die Bild-Policy `verified-media-ai-references-v10-person-preservation` verlangt
für neue Feed- und Storybilder die unveränderte Übernahme jeder Person aus der
Fotoreferenz, einschließlich Gesicht, Kleidung, Körperhaltung, Beinen und genauer
Fußstellung. Neue Bälle, Requisiten, verdeckte Körperteile oder neue Posen sind
ausgeschlossen. Nur die gesamte Person darf proportional skaliert und verschoben
werden; beim Ergebnis-Umbau bleiben auch Größe und Position erhalten. Hintergrund
und Layout müssen sich der Person anpassen. Diese serverseitigen Regeln gelten
auch für gespeicherte Vorlagen und haben Vorrang vor Branding und Variantenstil.
Bestehende Bilder werden dadurch nicht korrigiert. Die generative Umsetzung ist
keine Garantie für pixelgenaue Erhaltung und bleibt visuell zu prüfen; für lokale
Übernahme der Fotopixel existiert der Testmodus [photo_montage](PHOTO_MONTAGE_TEST.md).

## Exakt versandte Prompts

Ergebnisbilder erhalten in Feed und Story eine große Hauptüberschrift:
**HEIMSIEG!**, **AUSWÄRTSSIEG!**, **HEIMNIEDERLAGE**,
**AUSWÄRTSNIEDERLAGE** oder **UNENTSCHIEDEN** aus Sicht der eigenen
Mannschaft. Sie ersetzt „ERGEBNIS“ und einen nachgeordneten Ergebnis-Badge.
Direkt darunter steht der überdimensionale Spielstand. Nur die eigene Torzahl
erhält einen Ergebnisakzent: Grün bei Sieg, Rot bei Niederlage und Bernstein
bei Remis. Wappen, Trikots und die übrige Vereinsfarbwelt bleiben unverändert.
Die Überschrift vermittelt den Ausgang auch unabhängig von der Farbe.
Mannschaftsnamen und weitere ausgewählte Angaben sind kleiner angeordnet;
die bisherige Texthierarchie einer Ankündigung darf dafür neu gestaltet werden.
Der Server leitet die Kennzeichnung aus der eindeutigen Mannschaftszuordnung und
dem bestätigten Heim:Gast-Spielstand ab; fehlende oder unlesbare Ergebnisse
blockieren den Bildprompt. Tore bleiben eindeutig Heim- und Gastmannschaft
zugeordnet. Diese Faktenregel gilt auch bei gespeicherten Plattformvorlagen,
reduzierter Feldauswahl und dem Umbau einer Ankündigungsgrafik. Bei Niederlagen
sind zusätzliche Jubeltexte und Siegesinszenierungen ausgeschlossen. Reale
Personen im Referenzbild bleiben erhalten; ein vorhandenes Jubelfoto wird durch
die deutliche Ergebniskennzeichnung eingeordnet. Bereits erzeugte Bilder und
gespeicherte Promptversionen bleiben unverändert; die Regel gilt für neue
Generierungen. Die visuelle Umsetzung durch das Modell bleibt manuell zu prüfen.

Migration `0022` führt `ai_prompt_dispatches` ein. Unmittelbar vor einem echten
Text- oder Bildaufruf wird dort der vollständige, final zusammengesetzte
Provider-Input gespeichert. Der Eintrag enthält unter anderem Verein,
Generierungsauftrag, Spiel/Mannschaft, Prompt-Art, Medium, Modell,
Vorlagenreferenz, Prüfsumme, Versuch und Versandstatus. Ein tenantgebundener
Idempotency Key verhindert eine doppelte Protokollierung bei Wiederaufnahme.

Die Ansicht **Plattform → KI-Generierungen** kann nach Verein, Bild-/Textprompt
und Status filtern. Alle Treffer bleiben über eine Seitennavigation erreichbar;
die Seitengröße ist begrenzt einstellbar. Die Ansicht ist ausschließlich für
`PlatformAdmin` erreichbar.
Vereinsansichten, Beitrags-Snapshots, Exporte, Club-Audit und allgemeine
Fehlermeldungen erhalten weiterhin keinen Prompttext.

Die Tabelle enthält geschützte Plattformlogik und gegebenenfalls
vereinsbezogene Laufzeitdaten. Zugriff und Datenbank-Backups sind entsprechend
zu beschränken. Eine automatische Löschfrist ist noch nicht konfiguriert; vor
einem breiteren SaaS-Betrieb ist eine zur Support- und Datenschutzfrist
passende Retention festzulegen.

## Fehler- und Wiederaufnahmeregeln

Verifizierte Sponsorenmedien erscheinen im finalen Bildprompt als nummerierte
Referenzbilder nach Spieler-, Mannschafts- und optionalem Gegnerlogo. Die
Promptmetadaten speichern Rollen, Medien-ID und Prüfsumme, nicht jedoch einen
lokalen Dateipfad. Es gibt keine festen Logo-Koordinaten und keinen lokalen
Compositor-Schritt für neue KI-Ausgaben.

Ein Prompt wird vor dem externen Aufruf mit Status `dispatched` gespeichert.
Nach eindeutiger Antwort folgt `completed`, bei einem technischen Fehler
`failed`. Der gespeicherte Fehler enthält nur die Fehlerklasse, keine Tokens,
Secrets oder unbereinigte Anbieterantwort. Usage-Ledger und Promptprotokoll
verwenden getrennte, idempotente Einträge.
