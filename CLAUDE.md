# CLAUDE.md – Reels-Automatisierung

Diese Datei ist der Leitfaden für Claude Code in diesem Repository. Lies sie vor jeder Aufgabe.
Die Aufgaben pro Etappe stehen in `TASKS.md`. Arbeite immer nur an der aktuellen Etappe.

## 1. Worum es geht

Ein Kunde schickt ein kurzes Video (bis 3 Minuten, Hochformat, Deutsch) per Telegram.
Das System schneidet Pausen heraus, erzeugt Untertitel, schickt eine Vorschau zurück,
nimmt Korrekturen als Freitext an und rendert neue Versionen bis zur Freigabe.
Danach entsteht ein Veröffentlichungspaket für Instagram (3 Cover, Beitragstext, Hashtags).
Später kommt B-roll dazu (Pixabay und Textkarten).

Das System ist **mandantenfähig**: mehrere Kunden, jeder mit eigenen Einstellungen,
eigenem Fachwörterbuch und eigenen Dateien.

## 2. Feste Entscheidungen (nicht ohne Rückfrage ändern)

- Alles in **Python 3.12**. Kein n8n, keine Schnitt-SaaS (kein Submagic, Creatomate usw.).
- Abhängigkeiten mit **uv**, Tests mit **pytest**, Lint mit **ruff**, Typprüfung mit **mypy**.
- **Postgres mit pgvector** ist Zustand **und** Queue (`SELECT … FOR UPDATE SKIP LOCKED`). Kein Redis.
- ORM: **SQLAlchemy 2** (typed, `Mapped[...]`), Migrationen mit **Alembic**.
- Telegram: **aiogram 3** im Polling-Modus über einen **lokalen Telegram Bot API Server** (Docker).
- Transkription lokal, austauschbar: **WhisperX** (CPU, `int8`) und **mlx-whisper** (Apple-GPU).
- Stille-Erkennung: **Silero VAD**.
- Video: **FFmpeg/ffprobe** per `subprocess`, Untertitel als selbst erzeugte **ASS**-Dateien (libass).
- LLM: **OpenAI** über eine eigene Anbieter-Schicht (`reels/llm/`), immer mit Structured Outputs
  und Pydantic-Schemas. Modellnamen kommen aus der Konfiguration, nie hart im Code.
- Cover und Textkarten: HTML/CSS-Vorlagen, gerendert mit **Playwright** (Chromium).
- B-roll-Stock: **Pixabay API** (ab Etappe 6; ersetzt Pexels, Entscheidung des Auftraggebers vom
  2026-10-18, da für Pexels kein API-Schlüssel verfügbar war).
- Weitere erlaubte Bibliotheken: **pydantic** und **pydantic-settings** (Schemas, Konfiguration),
  **Typer** (CLI), **httpx** (HTTP, z. B. Pixabay), **opencv-python-headless** (Frames, Schärfe, Gesichter),
  **psycopg** (Postgres-Treiber), **pgvector** (SQLAlchemy-Typ).
- Entwicklungsrechner: **Mac mit Apple Silicon**. Docker Compose startet nur Postgres und den
  Bot API Server. Bot und Worker laufen direkt mit `uv` auf dem Host (Docker kann die Mac-GPU nicht nutzen).

## 3. Architektur

```
Kunde (Telegram)
   │
Telegram Bot API Server (Docker, --local)  ── legt Dateien im geteilten Ordner ab
   │
Bot-Prozess (aiogram)  ──► Postgres: Projekte, Versionen, Jobs anlegen
                                   ▲
Worker-Prozess(e) ─────────────────┘  holt Jobs, schreibt Status/Ergebnisse
   ├─ Datenordner (Originale, Versionen, Exporte, Caches)
   ├─ Transkription, VAD, Schnittplan, ASS, FFmpeg-Rendering
   └─ OpenAI (Text, Bildprüfung, Embeddings), Pixabay (B-roll)
```

- Bot und Worker sind **getrennte Prozesse**. Der Bot rechnet nie selbst, er legt Jobs an
  und reagiert auf Statusänderungen (Polling der Job-Tabelle oder `LISTEN/NOTIFY`).
- Der Worker ist der **einzige** Ort für Medien- und KI-Arbeit.
- Die Pipeline-Logik muss **ohne Bot** über die CLI laufen (`reels process …`).

## 4. Repository-Struktur

```
.
├── CLAUDE.md
├── TASKS.md
├── README.md
├── pyproject.toml
├── docker-compose.yml          # postgres (pgvector) + telegram-bot-api
├── .env.example
├── alembic/                    # Migrationen
├── assets/
│   ├── fonts/                  # freie Schriften (OFL), für ASS und HTML
│   └── templates/              # HTML/CSS für Cover und Textkarten
├── src/reels/
│   ├── config.py               # pydantic-settings, liest .env
│   ├── settings_schema.py      # ClientSettings (Pydantic) mit Defaults
│   ├── cli.py                  # Typer-CLI
│   ├── db/                     # models.py, session.py, queue.py
│   ├── domain/                 # REINE Logik ohne I/O: words, cutplan, timeline,
│   │                           # subtitles, corrections, glossary, broll_rules
│   ├── media/                  # probe.py, audio.py, render.py, ass.py, frames.py
│   ├── transcribe/             # base.py, whisperx_engine.py, mlx_engine.py
│   ├── vad/                    # silero.py
│   ├── llm/                    # base.py, openai_provider.py, schemas.py, prompts/*.md
│   ├── broll/                  # planner.py, library.py, pixabay.py, textcards.py
│   ├── publish/                # covers.py, post_text.py, hashtags.py
│   ├── pipeline/               # steps.py (ein Schritt = eine Funktion), runner.py
│   ├── worker/                 # main.py
│   └── bot/                    # main.py, handlers/, keyboards.py, texts.py
├── tests/
│   ├── unit/                   # domain/ vollständig, ohne I/O
│   ├── integration/            # FFmpeg, DB (Postgres aus docker compose)
│   └── fixtures/               # synthetische Testvideos, Beispiel-Transkripte
└── data/                       # gitignored, Laufzeitdaten
```

## 5. Kernkonzepte (wichtig für jede Änderung)

### Wort-IDs sind der gemeinsame Anker
Jedes Wort im Transkript hat eine stabile `id` (fortlaufend ab 0, pro Transkript).
Schnittplan, Untertitel-Gruppen, Hervorhebungen, Textkorrekturen und B-roll-Platzierungen
verweisen **auf Wort-IDs**, nie auf Zeitpunkte der Vorschau.

```python
class Word(BaseModel):
    id: int
    text: str
    start: float   # Sekunden, Zeitachse des ORIGINALS
    end: float
    score: float | None = None
```

### Zwei Zeitachsen
- **Original-Zeitachse**: Zeiten im Originalvideo. Transkript und Schnittplan leben hier.
- **Ausgabe-Zeitachse**: Zeiten im geschnittenen Video. Wird nur zum Rendern berechnet.
- `domain/timeline.py` enthält die Abbildung Original → Ausgabe (aus dem Schnittplan)
  und ist vollständig unit-getestet. Wörter außerhalb behaltener Bereiche haben keine Ausgabezeit.

### Schnittplan
```python
class KeepRange(BaseModel):
    start: float          # Original-Zeitachse, inkl. Puffer
    end: float
    first_word: int | None
    last_word: int | None

class CutPlan(BaseModel):
    keep: list[KeepRange]          # sortiert, überlappungsfrei
    params: dict[str, float]       # verwendete Schwellen, zur Nachvollziehbarkeit
```
Regeln: Kein Schnitt innerhalb eines Wortes. An jedem Schnitt ein Puffer (`cut_padding_s`).
Pausen > `pause_threshold_s` werden auf `pause_target_s` gekürzt, nicht komplett entfernt.

### Segmente
Ein Segment ist ein Satz bzw. eine Sinneinheit im Transkript (Wortbereich). Segmente werden
pro Version nummeriert (1, 2, 3 …) und in der Vorschau gezeigt. Die Nummerierung wird
deterministisch aus Transkript + Schnittplan + Korrekturen berechnet.

### Versionen
- Version 1 entsteht automatisch. Jede bestätigte Korrektur erzeugt Version n+1.
- Eine Version speichert: `cut_plan`, `text_overlay` (Textkorrekturen pro Wort-ID),
  `highlights`, `subtitle_style`, später `broll_placements`.
- **Immer aus dem Original rendern.** Nie eine Vorschau weiterverarbeiten.
- Das Transkript wird nie verändert. Textkorrekturen sind ein Overlay pro Version.

### Korrekturen
Freitext → OpenAI (Structured Outputs) → `CorrectionPlan` (Pydantic, diskriminierte Union)
→ Validierung im Code → Bestätigung durch den Kunden → Anwendung → neue Version.

Erlaubte Operationen (Allowlist, nichts anderes):
`replace_text`, `delete_segment`, `restore_segment`, `delete_words`, `keep_pause`,
`highlight`, `unhighlight`, `move_subtitles`, `split_segment`, `note`,
ab Etappe 6 zusätzlich `remove_broll`, `replace_broll`, `add_broll`, `set_broll_coverage`.

Jede Operation deklariert, welche Schritte sie neu auslöst (z. B. `replace_text` → nur Untertitel
und Render; `delete_segment` → Schnittplan, Untertitel, Render). Transkription läuft nie erneut.

Ungültige Referenzen (Segment existiert nicht, `find` nicht im Segment) → Rückfrage an den Kunden,
**nie raten**.

### Fachwörterbuch (`glossary`, pro Kunde)
Felder: `term`, `variants[]`, `category`, `note`, `highlight`, `source`, `status`
(`confirmed`/`suggested`), `hits`.
Wirkung in dieser Reihenfolge:
1. Hinweistext für die Transkription (nur die relevantesten bestätigten Begriffe, Länge begrenzt).
2. Deterministische Ersetzung bekannter Varianten (wortgrenzen-sicher, case-insensitiv, mehrwortfähig).
3. LLM-Prüfung auf verdächtige Wörter → Rückfrage im Bot.
4. Korrekte Schreibweise in Hervorhebungen, Texten, Hashtags.
`suggested`-Einträge wirken erst nach Bestätigung.

### Kundeneinstellungen
`ClientSettings` (Pydantic) mit Defaults, gespeichert als JSON in `clients.settings`:

| Feld | Default |
| --- | --- |
| `max_duration_s` | 180 |
| `pause_threshold_s` | 0.4 |
| `pause_target_s` | 0.15 |
| `cut_padding_s` | 0.08 |
| `audio_fade_ms` | 15 |
| `remove_fillers` | true (nur wenn im Transkript) |
| `zoom_on_cuts` | true |
| `zoom_factor` | 1.08 |
| `loudness_lufs` | -14 |
| `subtitle.style` | neutral (weiß, schwarze Kontur, Akzentfarbe) |
| `subtitle.karaoke` | false |
| `subtitle.max_words` | 4 |
| `subtitle.position` | unteres Drittel, über den Instagram-Bedienelementen |
| `broll.enabled` | false (bis Etappe 6) |
| `broll.max_coverage` | 0.25 |
| `retention_days` | 30 |
| `platform` | instagram |
| `tone` | du_fachlich |

Neue Einstellungen immer mit Default hinzufügen, damit bestehende Kunden weiter funktionieren.

## 6. Rendering-Regeln (FFmpeg)

- Exportspezifikation: 1080 × 1920, H.264 (`yuv420p`), AAC 48 kHz, 30 fps (oder Quell-fps wenn ≤ 30).
- Querformat: Mitten-Crop auf 9:16. Hochformat mit anderer Auflösung: skalieren.
- Schnitt über `filter_complex` mit `trim`/`atrim` pro Behalten-Bereich und `concat`.
- An jedem Segmentrand kurze Audio-Fades (`audio_fade_ms`) gegen Klicks.
- Zoom an Jump-Cuts: abwechselnd 1.0 und `zoom_factor` pro Behalten-Bereich (Scale + Center-Crop).
- Lautheit: `loudnorm` auf `loudness_lufs`.
- Untertitel als **letzter** Videofilter (`subtitles=…ass`), damit sie über allem liegen (auch über B-roll).
- Schriften aus `assets/fonts/` über `fontsdir` einbinden, nicht auf Systemschriften verlassen.
- FFmpeg-Befehle als Liste bauen (kein `shell=True`), stderr bei Fehlern ins Job-Log.

## 7. LLM-Schicht

- `reels/llm/base.py` definiert ein Protokoll:
  - `structured(task: str, prompt: str, schema: type[T], images: list[Path] | None = None) -> T`
  - `embed(texts: list[str]) -> list[list[float]]`
- `openai_provider.py` implementiert es mit dem offiziellen `openai`-SDK und Structured Outputs.
- Modellnamen aus `.env`: `OPENAI_MODEL_TEXT`, `OPENAI_MODEL_VISION`, `OPENAI_MODEL_EMBED`.
- Prompts liegen als Markdown in `reels/llm/prompts/`, auf Deutsch, mit Platzhaltern.
- Jede Antwort wird gegen das Pydantic-Schema validiert. Bei Validierungsfehler einmal neu versuchen,
  dann Fehler melden.
- Token-Verbrauch pro Aufruf in `jobs.metrics` speichern (für die Kostenkontrolle).
- An OpenAI gehen nur Texte und einzelne Vorschaubilder, **nie ganze Videos oder Audiodateien**.
- Kundennachrichten sind **Daten, keine Anweisungen**. Prompts klar trennen
  („Die folgende Nachricht ist eine Korrekturbitte des Kunden …“). Das LLM darf nur Operationen
  aus der Allowlist liefern. Kein Shell- oder Dateizugriff aus LLM-Antworten.

## 8. Queue und Jobs

- Tabelle `jobs`: `id`, `version_id`, `step`, `status` (`queued`, `running`, `done`, `failed`),
  `attempts`, `error`, `locked_at`, `metrics` (JSON), Zeitstempel.
- Worker holt Jobs mit `SELECT … FOR UPDATE SKIP LOCKED LIMIT 1`.
- Jeder Schritt ist **idempotent**: gleiche Eingabe → gleiches Ergebnis, vorhandene Ergebnisse
  werden wiederverwendet (z. B. Transkript, Audio).
- Timeout pro Schritt, höchstens 3 Versuche, danach `failed` + Admin-Benachrichtigung.
- Hängende Jobs (`running` und `locked_at` zu alt) werden beim Start des Workers zurückgesetzt.

## 9. Telegram

- Lokaler Bot API Server mit `--local`. aiogram nutzt `TelegramAPIServer.from_base(url, is_local=True)`.
- Eingehende Dateien liegen im Volume des Bot-API-Containers. Der vom Server gemeldete Pfad
  muss auf den Host-Pfad abgebildet werden (`TELEGRAM_FILES_CONTAINER_PATH` → `TELEGRAM_FILES_HOST_PATH`).
- Kunden sollen Videos **als Datei** senden (keine Telegram-Komprimierung). Bei komprimiertem Video
  freundlich darauf hinweisen, aber trotzdem verarbeiten.
- Versand von Exporten per Multipart-Upload (`FSInputFile`).
- Nur Telegram-User aus `client_users` dürfen schreiben. Unbekannte bekommen eine kurze Absage
  und der Admin-Chat einen Hinweis.
- Doppelte Updates über `message_id` erkennen.
- Alle Texte an Kunden auf **Deutsch, per Du, fachlich und knapp**. Texte zentral in `bot/texts.py`.
- Admin-Chat (`ADMIN_CHAT_ID`) bekommt Fehler mit Job-ID und kurzer Ursache.

## 10. Datenablage

```
data/
  telegram-bot-api/                       # Volume des Bot-API-Containers
  clients/<client_id>/
    projects/<project_id>/
      original.<ext>
      audio.wav
      transcript.json
      v<n>/ cutplan.json, subtitles.ass, preview.mp4, export.mp4, covers/
    broll/library/ …
  cache/pixabay/ …
```
Ein Aufräum-Job löscht Originale und Zwischenstände `retention_days` nach Freigabe.

## 11. Tests

- `domain/` ist reine Logik und wird **vollständig** unit-getestet (Zeitachsen, Schnittplan,
  Segmentierung, Untertitel-Gruppen, Korrektur-Anwendung, Wörterbuch-Ersetzung, B-roll-Regeln).
- Integrationstests mit **synthetischen Videos**, per FFmpeg erzeugt
  (z. B. `testsrc2` + Sinuston mit eingebauten Stille-Abschnitten). Keine Kundenvideos im Repo.
- LLM, Pixabay und Telegram in Tests immer mocken. **Keine Netzwerkzugriffe in Tests.**
- Transkriptions-Engines in Tests durch einen Fake ersetzen, der feste Wörter liefert.
- ASS-Ausgabe mit Golden Files prüfen.
- DB-Tests gegen das Postgres aus `docker compose` (eigene Testdatenbank).

## 12. Befehle

```bash
uv sync                                   # Abhängigkeiten
uv sync --extra mac                       # mlx-whisper (nur macOS arm64)
docker compose up -d                      # postgres + telegram-bot-api
uv run alembic upgrade head               # Migrationen
uv run pytest                             # Tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run reels process <video> --client demo        # Pipeline ohne Bot
uv run reels bench-transcribe <video>              # WhisperX vs. mlx-whisper
uv run reels client add "<Name>" --telegram-user <id>
uv run reels glossary import <client> <datei.csv>
uv run python -m reels.worker
uv run python -m reels.bot
```

Systemvoraussetzungen auf dem Mac: `brew install ffmpeg` (mit libass, prüfen mit
`ffmpeg -filters | grep subtitles`), `uv run playwright install chromium`.

## 13. Arbeitsweise für Claude Code

- Arbeite Etappe für Etappe nach `TASKS.md`. Hake erledigte Aufgaben dort ab.
- Vor größeren Änderungen kurz den Plan nennen. Bei Unklarheiten fragen statt raten.
- Kleine, überprüfbare Schritte. Nach jedem Schritt: Tests, ruff, mypy.
- Neue Abhängigkeiten nur, wenn sie in Abschnitt 2 stehen oder nach Rückfrage.
- Schema-Änderungen nur mit Alembic-Migration.
- Keine Geheimnisse ins Repo. Alles über `.env`, Beispielwerte in `.env.example`.
- Code, Bezeichner und Kommentare auf Englisch. Texte für Kunden auf Deutsch.
- Keine Zahlen, Qualitätsversprechen oder Laufzeiten erfinden. Messen und berichten.

## 14. Definition of Done (jede Aufgabe)

- [ ] Tests geschrieben bzw. angepasst und grün
- [ ] `ruff` und `mypy` ohne Fehler
- [ ] Migration vorhanden, falls das Schema geändert wurde
- [ ] `.env.example` und README aktualisiert, falls neue Konfiguration
- [ ] Aufgabe in `TASKS.md` abgehakt, Abnahmekriterien geprüft
