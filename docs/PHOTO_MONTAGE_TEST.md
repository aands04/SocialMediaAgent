# Experimentelle Fotomontage

Zusätzlicher, manuell gewählter Modus für eine einzelne vorhandene Feed- oder
Story-Ausgabe. Die reguläre Generierung und die Automatik behalten ihren bisherigen
Modus. Keine Datenbankmigration oder zusätzliche Abhängigkeit erforderlich.

## Ausprobieren

1. Einen spielbezogenen, noch bearbeitbaren Beitrag öffnen.
2. Bei der gewünschten Medienausgabe **Fotomontage testen · Originalfoto erhalten**
   aufklappen. Bisheriges oder anderes verfügbares Originalfoto auswählen.
3. **Fotomontage erstellen** starten. Der vorhandene Worker verarbeitet den Auftrag;
   ein kostenpflichtiger OpenAI-Bildaufruf erzeugt ausschließlich den Hintergrund.
4. Nach Abschluss die neue Version mit den bisherigen Versionen vergleichen,
   die gewünschte Version auswählen und erneut freigeben. Die Montage ist im
   Versionsvergleich gekennzeichnet.

Voraussetzung ist die vorhandene OpenAI-Bildkonfiguration einschließlich Secret,
ein Originalfoto und das verifizierte Mannschaftslogo. Produktionsbereitstellung
bleibt ein separat beauftragter Vorgang. Lokale Tests verwenden einen Mock-Provider.

## Erhalt des Fotos

OpenAI erhält ausschließlich einen eigenen Hintergrundprompt mit validierten
Vereinsfarben. Spielerfoto, Wappen, Sponsorenlogos, Spielinformationen und der
normale Bildprompt werden nicht übermittelt. Die Protokollierung zeigt deshalb
keine Bildreferenzen und den tatsächlich versandten Hintergrundprompt.

Der lokale Renderer setzt verifizierte Logos und Spielinformationen. Danach wird
das Originalfoto EXIF-orientiert, proportional mit Lanczos skaliert und lokal in
die reservierte Bildfläche eingesetzt. Es gibt keinen Beschnitt, keine generative
Schärfung, keine Gesichtsrekonstruktion und keine generative Nachbearbeitung des
fertigen Bildes. Vollständig deckende Fotopixel entsprechen exakt der lokalen
Skalierung. Transparente PNGs werden per Alphakanal zusammengesetzt. Die
Originaldatei bleibt unverändert; ihre Prüfsumme und die Methode werden gespeichert.

Version 1 bietet ein festes Layout mit vollständig sichtbarem Foto und optional
bereits freigestellten PNGs. Sie enthält keine automatische Freistellung. Verdeckte
Körperteile werden nicht ergänzt. Stark abweichende Seitenverhältnisse lassen mehr
Hintergrund sichtbar; lange Beschriftungen werden verkleinert oder sicher abgelehnt.
Die Hintergrundgenerierung kann trotz Prompt unerwünschte Elemente enthalten;
die visuelle Prüfung bleibt erforderlich.

**Dieses Bild gezielt ändern** und **komplett neu erstellen** verwenden weiterhin
generative Bearbeitung. Der Fotoerhalt gilt ausschließlich für den Fotomontagemodus;
ein entsprechender Hinweis steht bei der gezielten Bearbeitung.

## Versionen und Sicherheit

Die Montage verwendet den bestehenden tenantgebundenen Generierungsauftrag,
Kosten-/Lease-Verwaltung, Medienauswahl und Versionsablauf. Die Aktion wird als
`photo_montage` gespeichert. Alte Dateien bleiben erhalten; neue Versionen
entziehen die bisherige Freigabe. Es erfolgt keine automatische Veröffentlichung.
Die Auswahl gilt nur für diesen Auftrag und wird kein neuer globaler Standard.

Ein vollständig gespeichertes Ergebnis desselben Jobs kann wiederverwendet werden.
Fehler nach möglicherweise angenommenem Anbieteraufruf durchlaufen die bestehende
Behandlung; es gibt keine zusätzliche automatische Wiederholung.

Prüfungen:

```sh
python -m pytest -q tests/test_photo_montage.py tests/test_photo_montage_routes.py tests/test_post_management.py tests/test_ai_generation.py tests/test_imagegen_openai.py tests/test_generation_jobs.py tests/test_render_validation.py
```

Lokale Verifikation am 12.09.2026: 136 gezielte Tests erfolgreich (einschließlich
Karussell-, Tenant- und Storage-/Prompt-Tests). Fotopixelvergleich für Feed/Story
und Alpha-PNG, Job-Wiederverwendung, originalgetreue Referenzprotokollierung,
CSRF/Versions-/Ausgabeprüfung und Freigabeentzug wurden geprüft. Feed- und
Story-Renderings wurden visuell kontrolliert. Ruff-Lint der betroffenen Python-Dateien
und `git diff --check` erfolgreich. Neue Dateien sind formatiert; bestehende
Formatabweichungen in fünf bereits auf HEAD unformatierten Dateien wurden nicht
projektweit bereinigt. Keine vollständige Testsuite, keine echten Provideraufrufe,
keine Produktionsänderung.
