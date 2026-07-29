# SocialMediaAgent

Sicherheitsorientiertes, selbst gehostetes MVP für automatisch erzeugte, **immer manuell freizugebende** Fußball-Instagram-Beiträge. UI und Betriebsdokumentation sind deutsch; externe FUSSBALL.DE-, OpenAI-, SMB- und Meta-Zugriffe bleiben standardmäßig Fixture/Mock/Dry-Run.

## Enthalten
- FastAPI/Jinja2/HTMX-Dashboard, Session-Login, Argon2, CSRF, RBAC und Mannschafts-Scope
- SQLAlchemy-2-Modell, Alembic, PostgreSQL/SQLite, Optimistic Locking und Auditmodell
- austauschbarer FUSSBALL.DE-HTML-Provider ohne erfundene API
- sicherer lokaler bzw. host-gemounteter SMB-Speicher und einmalige Bildreservierung
- automatische Feed-/Multi-Story-Erzeugung (1080×1350/1080×1920), Fakten-only Textgenerator
- versionierte Freigaben, einzelne Publishing-Aufträge, Not-Aus, Idempotenz und unklare Plattformzustände
- offizieller Graph-API-Publisher plus Mock/Dry-Run; Live-Modus ist mehrfach opt-in
- Docker Compose mit Web, Worker, PostgreSQL, Nginx, Healthchecks, Volumes, Backup/Restore

## Lokal starten
Python 3.12 vorausgesetzt:
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env                 # lokal DATABASE_URL auf sqlite:///./data/app.db setzen
alembic upgrade head
python scripts/seed.py
uvicorn app.main:app --reload
```
Dann `http://localhost:8000`, initial `admin@example.invalid` / `ChangeMe-Immediately!`; sofort ändern. Tests: `pytest`; Lint: `ruff check .`.

Docker:
```bash
mkdir -p secrets && openssl rand -base64 32 > secrets/db_password.txt
cp .env.example .env                 # DB-Passwort in DATABASE_URL passend setzen
PUBLISHER_MODE=dry-run GLOBAL_PUBLISH_ENABLED=false docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

## Administration
1. **Instagram-Seite:** internen Namen, Username und offizielle Konto-ID anlegen; Token ausschließlich als Docker Secret/Environment. Nach offizieller Verbindungsprüfung aktivieren. Mehrere Teams dürfen dieselbe Seite nutzen; der Beitrag snapshotet sein Ziel.
2. **Mannschaft:** Namen/Slug, plausible `https://www.fussball.de/...`-URL, aktive Seite, relativen Medienunterordner, vorhandene Vorlagen/Fonts, Farben und Zeitzone anlegen. Löschen ist Soft-Delete/Archivierung.
3. **Rechte:** Rolle und Mannschaftszuordnung sind getrennt. `all_teams=false` verlangt explizite `UserTeam`-Zeilen; direkte URLs und Services prüfen serverseitig.
4. **Zeitregeln:** Feed als Minuten vor Anpfiff; Story-Regeln referenzieren Anpfiff, geplantes Ende, Ergebniserkennung, Freigabe oder Folgetag, mit Offset/fester Uhrzeit. Jede Regel erzeugt einen Job; Kollisionen werden nicht unbemerkt dupliziert.
5. **Medien:** SMB-Share auf dem Host mounten (siehe unten), Team-Unterordner scannen. Ein Bild wird atomar einem Spiel reserviert und darf in dessen Feed/Storys wiederverwendet werden. Ohne Bild entsteht eine neutrale Grafik mit Prüfhinweis.
6. **Workflow:** Worker synchronisiert Spiele, erzeugt Beiträge automatisch, rendert alle Dateien und Text. Freigeber prüft Version, Ziel und abgelaufene Zeiten. Jede relevante Änderung setzt offene Jobs auf erneute Freigabe.
7. **Fehler:** Transiente Fehler werden begrenzt wiederholt. Token-/Rechtefehler stoppen. Timeout/unklare Antwort wird `uncertain`; Status muss bei Meta geprüft werden, bevor jemand erneut startet.

## SMB
Auf Linux z. B. `/etc/fstab` mit einer nur für root lesbaren Credentials-Datei verwenden:
```text
//server/share /mnt/social-media-assets cifs credentials=/root/.smb-social,ro,nosuid,nodev,noexec,uid=10001,gid=10001,file_mode=0440,dir_mode=0550 0 0
```
`MEDIA_HOST_ROOT=/mnt/social-media-assets`, Containerziel `/app/external-media`. Die Anwendung speichert weder SMB-Benutzer noch Passwort. Test: `findmnt /mnt/social-media-assets` und `/health`.

## Meta-Verbindung
Nur offizielle Meta-/Instagram-Schnittstellen verwenden. Konto-ID, passende professionelle Kontoart, App Review/Berechtigungen, öffentlich abrufbare Medien-URL und gültige Tokens gemäß **aktuell offizieller** Dokumentation konfigurieren. Niemals Instagram-Passwörter speichern. Erst nach Staging-Test `PUBLISHER_MODE=live` und separat `GLOBAL_PUBLISH_ENABLED=true`; zusätzlich müssen Seite, Team, Beitrag und Job aktiv/freigegeben sein. Der Containerstatus wird vor `media_publish` geprüft; Plattformbestätigung ist zwingend.

## Produktion, Betrieb und Umzug
Proxmox: private VM, Compose Production Override und Zugriff vorzugsweise Tailscale/VPN; Proxy nur an Loopback. Cloud: Firewall nur 80/443, Nginx hinter Caddy/Traefik mit automatischem TLS oder diesen Proxy um TLS ergänzen. Niemals Uvicorn-Reload öffentlich exponieren. Updates: Backup, Image bauen, `alembic upgrade head`, Compose rolling restart, Healthcheck prüfen.

Backup: `scripts/backup.sh`; Restore in Wartungsmodus: `scripts/restore.sh BACKUP`. Enthalten sind DB, Uploads, Vorschauen/Generiertes, Vorlagen und Compose-Konfiguration – keine `.env`/Secrets. SMB-Originale separat am Fileserver sichern. Bei Umzug: Backup übertragen, SMB neu mounten, Secrets neu setzen, Restore, Domain/TLS umstellen und alle Verbindungen im Dry-Run testen.

Not-Aus: `system_settings.key='emergency_stop'`, `value={"enabled":true}` stoppt noch nicht begonnene Jobs. Laufende/unklare Vorgänge zuerst bei Meta abgleichen. Weitere Schalter existieren global, je Seite, Team, Beitrag und Job.

## KI-Prompts, Bildgenerierung und Offline-Fallback
Unter **KI-Promptvorlagen** werden Bild- und Textprompts mit geprüften Jinja-Platzhaltern versioniert verwaltet. Mannschaftsregeln ordnen getrennte Feed-, Story- und Textprompts zu; einzelne Story-Regeln dürfen einen eigenen Story-Prompt wählen. Die Vorschau ersetzt Platzhalter mit Beispieldaten, ruft aber keine externe API auf. Am erzeugten Beitrag werden Name, Version, Modell, Qualität, Prompttext und vollständig gerenderter Prompt eingefroren. Damit bleibt jede Ausgabe nachvollziehbar und spätere Promptänderungen verändern bestehende Beiträge nicht.

Mit `IMAGE_GENERATOR_MODE=openai` und `TEXT_GENERATOR_MODE=openai` werden Grafiken beziehungsweise Begleittexte über die OpenAI API erzeugt. Standardmodell für Bilder ist `gpt-image-2`. Spielerbild und vorhandene Original-Logos werden als Referenzbilder mit hoher Eingangstreue übergeben. Die zunächst API-kompatibel erzeugte Ausgabe wird lokal verlustarm auf exakt 1080 × 1350 (Feed) beziehungsweise 1080 × 1920 (Story) zugeschnitten und technisch als PNG validiert. Das Modell kann trotz strenger Prompts Text, Logos, Gesichter oder Trikots fehlerhaft wiedergeben; deshalb bleibt die vorhandene manuelle visuelle Freigabe zwingend.

Vor Modellwechseln oder Produktivtests die aktuelle offizielle [OpenAI-Anleitung zur Bildgenerierung](https://developers.openai.com/api/docs/guides/image-generation) und die [Modellseite von GPT Image 2](https://developers.openai.com/api/docs/models/gpt-image-2) erneut prüfen.

`IMAGE_GENERATOR_MODE=playwright` bleibt der standardmäßige, reproduzierbare Offline-Fallback. Dabei werden Feed und Story aus HTML/CSS mit Playwright/Chromium gerendert. Die eingebauten Vorlagen `default-feed` und `default-story` unterstützen Ankündigung und Ergebnis; aktive Datenbankvorlagen werden in ihrer neuesten Version gewählt und vollständig im Beitragssnapshot eingefroren. Für lokale Entwicklung muss ein von Playwright nutzbares Chromium installiert sein; das Docker-Image installiert es automatisch.

Der API-Key wird ausschließlich über das Docker-Secret `openai_api_key` bereitgestellt. Für einen kontrollierten KI-Test in Staging:

```text
TEXT_GENERATOR_MODE=openai
IMAGE_GENERATOR_MODE=openai
OPENAI_MODEL=gpt-5-mini
OPENAI_IMAGE_MODEL=gpt-image-2
OPENAI_IMAGE_QUALITY=medium
```

Nach Änderung der `.env.staging` Web und Worker neu erstellen. Meta/Instagram bleibt davon unabhängig im Dry-Run.

Der FUSSBALL.DE-Parser ist fixture-getestet, muss aber bei HTML-Änderungen angepasst werden. Reale OpenAI-/Meta-Aufrufe wurden nicht durchgeführt. Details und Zustände: [ARCHITECTURE.md](ARCHITECTURE.md).

## Lokaler End-to-End-Test (ohne externe Dienste)
1. `.env.example` nach `.env` kopieren, `PUBLISHER_MODE=dry-run`, `GLOBAL_PUBLISH_ENABLED=false`, `FUSSBALL_LIVE_TEST_ENABLED=false` beibehalten und ein zufälliges Session-Secret setzen.
2. Einen lokalen Medienbaum `data/external-media/erste/` mit freigegebenen JPG/PNG-Dateien anlegen.
3. Migrationen und Seed ausführen, Anwendung starten und anmelden.
4. Im Dashboard eine Instagram-Seite anlegen und über **Mock-Verbindung prüfen** verbinden. Publishing bleibt dabei deaktiviert.
5. Mannschaft mit dem relativen Ordner `erste` anlegen, Medien neu einlesen, Ankündigungs-/Ergebnisregeln sowie mindestens zwei Story-Zeitpunkte konfigurieren.
6. Ein Fixture-Spiel und einen automatischen Beitrag über die vorhandenen Services/Worker erzeugen. Im Dashboard Feed, Text, Story-Aufträge und abgelaufene Zeitpunkte prüfen.
7. Beitrag ausdrücklich freigeben. Für die Verarbeitung `GLOBAL_PUBLISH_ENABLED=true` nur in dieser lokalen Dry-Run-Sitzung setzen. `DryRunPublisher` erzeugt ausschließlich `dry-run:*`-IDs und sendet nichts an Meta.
8. Not-Aus aktivieren und verifizieren, dass ein weiterer Auftrag ohne Versuch blockiert wird. Danach Testdatenbank und erzeugte Medien löschen.

Automatisierter Browser-/Integrationslauf: `pytest tests/test_dashboard.py`. Er deckt Anmeldung, CSRF, Instagram-Seite, Mock-Verbindung, Mannschaft, Story-Regel, Benutzeranlage, Teamrechte und verweigerte Administratorseiten ab.

## Kontrollierter FUSSBALL.DE-Live-Strukturtest
Der Modus ist standardmäßig aus und verändert weder Spiele noch Beiträge. Nur nach bewusster Aktivierung mit `FUSSBALL_LIVE_TEST_ENABLED=true` kann `python scripts/fussball_live_test.py TEAM_ID` öffentliches HTML lesen. Jeder Abruf wird unverändert und mit SHA-256 unter `data/provider-snapshots/` gespeichert; Parsergebnis oder ein klarer Strukturänderungsfehler landet zusätzlich in `provider_snapshots`. Der Modus plant und veröffentlicht nichts. Abrufintervall und rechtliche/robots-bezogene Vorgaben sind vor Einsatz zu prüfen.

## Erster Proxmox-Test
Benötigt werden eine Linux-VM mit Docker Engine/Compose v2, DNS oder Tailscale, ein als read-only eingebundenes SMB-Verzeichnis, ausreichend beschreib…39882 tokens truncated…   Image.new("RGBA", (200, 200), (255, 255, 255, 255)).save(logo)
    provider = FakeImageProvider()
    renderer = AIImageRenderer(tmp_path / "out", media, uploads, provider)
    prompt = builtin_prompt("image", "announcement", "feed", facts())
    output = renderer.render(
        "feed",
        "post/feed.png",
        {
            "player_image": str(player),
            "team_logo": str(logo),
            "opponent_logo": None,
            "image_prompt": prompt,
        },
    )
    assert Image.open(output).size == (1080, 1350)
    assert provider.calls[0]["size"] == "1088x1360"
    assert provider.calls[0]["model"] == "gpt-image-2"
    assert provider.calls[0]["references"] == [player.resolve(), logo.resolve()]


def test_ai_renderer_refuses_missing_player(tmp_path):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    media.mkdir()
    uploads.mkdir()
    renderer = AIImageRenderer(
        tmp_path / "out", media, uploads, FakeImageProvider()
    )
    with pytest.raises(ValueError, match="Spielerbild"):
        renderer.render(
            "story",
            "post/story.png",
            {
                "player_image": None,
                "image_prompt": builtin_prompt(
                    "image", "announcement", "story", facts()
                ),
            },
        )


def test_post_creation_freezes_image_prompt_versions(db, tmp_path, monkeypatch):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    media.mkdir()
    uploads.mkdir()
    player = media / "player.jpg"
    Image.new("RGB", (600, 900), "blue").save(player)
    monkeypatch.setattr(
        "app.posts.service.get_settings", lambda: Settings(media_root=media)
    )
    page = InstagramPage(
        internal_name="main",
        display_name="Hauptseite",
        username="sve",
        club="SV Ehlen",
        active=True,
        connection_status="connected",
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="erste",
        display_name="SV Ehlen",
        short_name="SVE",
        slug="sve",
        club="SV Ehlen",
        fussball_url="https://www.fussball.de/team",
        instagram_page_id=page.id,
        media_subdir="erste",
        rules={"feed_before_minutes": 1440},
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        external_id="ai-1",
        home_team="SV Ehlen",
        away_team="SG Beispiel",
        kickoff=datetime.now(timezone.utc) + timedelta(days=3),
        competition="Kreisliga A",
        venue="Ehlen",
        pitch="Rasenplatz",
        source_url=team.fussball_url,
    )
    db.add(game)
    db.flush()
    db.add_all(
        [
            MediaAsset(
                team_id=team.id,
                relative_path="player.jpg",
                filename="player.jpg",
                mime_type="image/jpeg",
                size=player.stat().st_size,
                checksum="x" * 64,
                mtime=datetime.now(timezone.utc),
            ),
            StoryRule(
                team_id=team.id,
                name="24h",
                post_type="announcement",
                reference="kickoff",
                direction="before",
                offset_minutes=360,
                template="default-story",
            ),
        ]
    )
    db.commit()
    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        AIImageRenderer(tmp_path / "out", media, uploads, FakeImageProvider()),
    )
    assert post.design_snapshot["mode"]["image"] == "openai"
    assert post.design_snapshot["prompts"]["feed"]["name"] == "default-image-feed"
    assert "SV Ehlen gegen SG Beispiel" in post.design_snapshot["prompts"]["feed"]["rendered"]
    assert post.design_snapshot["stories"][0]["prompt"]["name"] == "default-image-story"
    assert Image.open(post.feed_path).size == (1080, 1350)


def test_openai_text_generator_uses_resolved_prompt_without_live_request():
    prompt = builtin_prompt("text", "announcement", "none", facts())
    generator = OpenAITextGenerator("test-key", "unused")

    class Responses:
        def create(self, model, input):
            assert model == prompt.model
            assert input == prompt.rendered
            return type(
                "Response",
                (),
                {
                    "output_text": "Kopierbarer Testtext",
                    "usage": type("Usage", (), {"total_tokens": 42})(),
                },
            )()

    generator.client = type("Client", (), {"responses": Responses()})()
    result = generator.generate({"text_prompt": prompt})
    assert result.text == "Kopierbarer Testtext"
    assert result.prompt_version == "default-text-announcement:v1"
    assert result.tokens == 42
