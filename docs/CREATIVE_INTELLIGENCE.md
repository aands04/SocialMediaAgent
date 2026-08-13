# Creative Intelligence und Branding-Onboarding

Stand der Architekturpruefung: 12. August 2026

## 1. Bestehende Architektur

Die Vereinszentrale besitzt bereits die benoetigten stabilen Anker, die fuer
Creative Intelligence erweitert und nicht ersetzt werden:

- `Club` und der serverseitige `TenantContext` bilden die verbindliche
  Mandantengrenze. ORM-Lese- und Schreibzugriffe werden zusaetzlich durch
  `TenantSession` eingeschraenkt.
- `ClubBrandingConfiguration` speichert die ausdruecklichen Bild- und
  Texteinstellungen des Vereins. Diese Angaben bleiben immer hoeher priorisiert
  als gelernte Vorlieben.
- `GeneratedMediaVersion` und `PostTextVersion` sind unveraenderliche Versionen.
  Sie eignen sich deshalb als stabile Referenzen fuer Feedback und Beispiele.
- `GenerationJob`, `AiPromptDispatch` und `UsageLedgerEntry` liefern technische
  Nachvollziehbarkeit, ohne geschuetzte Prompts in Vereinsansichten offenzulegen.
- `PromptTemplate`, `ClubPromptOverride` und der Branding-Compiler erzeugen den
  finalen Provider-Prompt ausschliesslich serverseitig.
- Rollen-, Freigabe-, Audit-, Not-Aus- und Publikationsmechanismen bleiben
  unveraendert verbindlich.

## 2. Ziel und Domaenengrenze

Creative Intelligence lernt ausschliesslich gestalterische und sprachliche
Vorlieben eines einzelnen Vereins. Es entscheidet weder ueber Fakten noch ueber
Freigaben, Rechte, Logos, Sicherheitsregeln oder Veroeffentlichungsziele.

Die Domaene besteht aus:

1. einem unveraenderlichen Feedback-Ledger,
2. versionierten Praeferenzprofilen,
3. positiven und negativen Beispielreferenzen,
4. einem deterministischen Creative Director fuer Laufzeitanweisungen,
5. optionaler, zwischengespeicherter visueller Merkmalsanalyse,
6. einem fortsetzbaren Branding-Onboarding mit Kalibrierung.

## 3. Dateneigentum und Mandantenschutz

Jedes Feedback, Profil, Beispiel, Analyseergebnis und jede Onboarding-Sitzung
enthaelt eine verpflichtende `club_id`. Referenzen auf Mannschaften, Beitraege,
Medien- und Textversionen werden im Service zusaetzlich gegen denselben Verein
geprueft. Bei fehlendem oder widerspruechlichem Tenant-Kontext wird die Aktion
abgelehnt; ein Standardverein wird nie angenommen.

Plattformweite Creative-Rezepte enthalten keine Vereinsdaten. PlatformAdmins
koennen sie verwalten und Vereinsprofile einsehen, muessen einen
vereinsbezogenen Zugriff aber ausdruecklich auswaehlen. Die ORM-Plattformscope
ist von normalen Vereinssitzungen getrennt.

## 4. Feedback-Ereignisfluss

```text
Auswahl / Freigabe / Ablehnung / Veroeffentlichung / Regeneration / Bearbeitung
                                |
                                v
                    CreativeFeedbackEvent (append-only)
                                |
                       Schwellwert oder Rebuild
                                v
                   CreativePreferenceLearner
                                |
                 neue versionierte Profilversion
                                |
              Creative Director bei kuenftiger Generierung
```

Feedback-Ereignisse werden niemals aktualisiert oder geloescht. Eine Korrektur
ist ein neues Ereignis mit `correction_of_id`. Idempotency Keys verhindern
Doppelverbuchungen bei erneut zugestellten Requests und Worker-Neustarts.

Die zentral konfigurierten Gewichte unterscheiden etwa Veroeffentlichung,
Auswahl, Ablehnung und Regeneration. Ein einzelnes Ereignis erzeugt keine harte
Regel: der Learner verwendet Mindeststichproben, Konfidenz und zeitlichen
Gewichtsverfall.

## 5. Praeferenzprofile

Profile sind je Verein, Modalitaet und Inhaltstyp versioniert. Unterstuetzt
werden:

- Bild: Ankuendigung, Ergebnis, Erinnerung und Tor,
- Text: Ankuendigung, Ergebnis und Erinnerung.

Eine neue Berechnung ueberschreibt keine alte Version. Die bisher aktive
Version wird als `superseded` markiert. Praeferenzen, zu vermeidende Merkmale,
Konfidenz, Stichprobenzahl, Quellzusammenfassung und die verwendete
Learner-Version bleiben nachvollziehbar.

## 6. Creative Director und Promptzusammensetzung

Der Creative Director liefert ein strukturiertes, begrenztes
Laufzeit-Supplement. Er veraendert weder zentrale Promptvorlagen noch
Vereinsbranding. Die Reihenfolge bleibt verbindlich:

1. Sicherheits- und Faktenregeln,
2. ausdrueckliches Vereinsbranding,
3. geschuetzte PlatformAdmin-Vereinsanpassung,
4. PlatformAdmin Creative Override,
5. ausreichend sichere gelernte Praeferenzen,
6. Plattformstandard.

Gelernte Praeferenzen werden nur oberhalb der konfigurierten Konfidenzschwelle
angewendet. Bei jedem Fehler liefert der Director ein leeres Supplement; die
normale Generierung wird dadurch nicht blockiert. Im Verein werden weder der
zentrale Prompt noch das zusammengesetzte Supplement angezeigt.

Die OpenAI Responses API kann optional fuer eine strukturierte Director- oder
Merkmalsausgabe genutzt werden. Die offizielle Structured-Outputs-Schnittstelle
mit Pydantic-Schema wird verwendet; der deterministische lokale Director bleibt
der sichere Standard und Fallback.

## 7. Beispiele und visuelle Analyse

Beispiele referenzieren bestehende unveraenderliche Medien- oder Textversionen
des gleichen Vereins. Version 1 verwendet PostgreSQL-Ranking und begrenzt die
Auswahl standardmaessig auf maximal fuenf positive und drei negative Beispiele.
Ein Vector Store ist nicht erforderlich.

Die optionale visuelle Analyse verarbeitet nur Gestaltungsmerkmale wie
Farbwirkung, Kompositionsdichte, Typografiegewicht oder Motivdynamik. Sie fuehrt
keine Personenidentifikation und keine biometrische Analyse aus. Ergebnisse
werden pro Verein, Pruefsumme und Analyzer-Version gecacht.

## 8. Onboarding-Zustandsmaschine

Eine `ClubOnboardingSession` besitzt genau einen Verein und die Zustaende
`not_started`, `in_progress`, `calibration_pending`, `completed` oder `skipped`.
Die elf Schritte erfassen vorhandene Grunddaten, Branding, Bild- und
Textvorlieben, Content-Prioritaeten, Beispiele, Kalibrierung und Abschluss.

Die Sitzung ist fortsetzbar und verwendet eine explizite Versionsnummer fuer
optimistische Sperren. Bereits vorhandene Vereinsdaten werden als Vorschlag
angezeigt, aber niemals ungefragt ueberschrieben. Kalibrierungsinhalte sind
Fixture-Inhalte, koennen nicht veroeffentlicht werden und werden als interne,
nicht auf das normale Vereinskontingent angerechnete Nutzung verbucht.

## 9. Nutzung und Kosten

Zusaetzliche technische Nutzungstypen sind:

- `creative_director`,
- `preference_learning`,
- `visual_trait_analysis`,
- `onboarding_calibration`.

Sie werden im bestehenden Usage-Ledger mit eigener Kategorie erfasst. Interne
Onboarding-Kalibrierung und Platformtests belasten nicht das normale Text- oder
Bildkontingent. Kosten koennen dennoch fuer PlatformAdmins sichtbar bleiben.

## 10. Migration und Rollout

Die neue Migration legt ausschliesslich neue Tabellen und Indizes an und
erweitert die Laenge des vorhandenen Usage-Typfeldes. Bestehende Beitraege,
Medien, Texte, Promptversionen und Brandingwerte werden nicht veraendert.

Die Feature Flags `creative_intelligence_enabled` und
`onboarding_calibration_enabled` sind standardmaessig deaktiviert. Dadurch ist
ein schrittweiser Rollout pro Verein moeglich. Das Sammeln explizit bestaetigter
Feedbackdaten und die Anwendung gelernter Profile sind getrennt schaltbar.

Die periodische Profilverdichtung läuft im vorhandenen Worker. `hourly` prüft
stündlich, `nightly` ausschließlich im vorgesehenen UTC-Zeitfenster und
`disabled` deaktiviert den Lauf. Ein Lauf erzeugt nur dann eine neue Version,
wenn seit dem letzten aktiven Profil mindestens die konfigurierte Zahl neuer,
verwertbarer Feedbackereignisse hinzugekommen ist. Fehler eines einzelnen
Vereins werden isoliert protokolliert und blockieren weder andere Vereine noch
die normale Generierung.

## 11. Risiken und Gegenmassnahmen

- **Zu wenig Daten:** Mindeststichprobe und Konfidenzschwelle verhindern
  Ueberanpassung.
- **Widerspruechliches Feedback:** Ereignisse bleiben erhalten; Recency und
  Gewichtung werden transparent im Profil zusammengefasst.
- **Prompt-Injection:** Freitext wird begrenzt, normalisiert und nie als
  Systemanweisung behandelt.
- **Cross-Tenant-Leak:** Jede Referenz wird in Service und ORM gegen `club_id`
  geprueft; Cache-/Idempotency-Schluessel enthalten die Vereins-ID.
- **Kosten oder Stoerung:** optionale KI-Analyse ist fail-open fuer die normale
  Generierung, wird separat verbucht und kann zentral deaktiviert werden.
- **Rueckwirkung:** bestehende Beitraege bleiben unveraendert; Profile wirken
  nur auf neue Generierungen oder bewusst gestartete Neugenerierungen.

## 12. Offizielle OpenAI-Grundlage

Geprueft am 12. August 2026: Die offizielle OpenAI-Dokumentation beschreibt fuer
strukturierte Ausgaben der Responses API `responses.parse` zusammen mit einem
Pydantic-Schema. Dieses Verfahren wird fuer optionale strukturierte Analysen
verwendet; interne Prompttexte werden weder an Browser noch Vereins-APIs
uebertragen.

Quelle: <https://developers.openai.com/api/docs/guides/structured-outputs>

## 13. Bewusste Grenzen dieser Ausbaustufe

- Die Stil-Kalibrierung erzeugt derzeit sichere, reproduzierbare
  Fixture-Beispiele. Modelschnittstellen, getrennte Nutzungsarten und das
  Kostenlimit sind vorbereitet; kostenpflichtige Live-KI-Samples werden aber
  nicht automatisch erzeugt.
- Die visuelle Merkmalsanalyse besitzt Modell, Cache, Feature Flag und
  Nutzungstyp, ist aber ohne ausdrücklich konfiguriertes Analysemodell nicht
  aktiv. Es findet keine Personen- oder Identitätsanalyse statt.
- Ein externer Vector Store ist bewusst nicht erforderlich. Positive und
  negative Referenzen werden versioniert in PostgreSQL gerankt.
- Die Erweiterung verändert keine historischen Beiträge oder bereits
  veröffentlichten Medien.
