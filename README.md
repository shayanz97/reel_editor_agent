# Reels-Automatisierung

Ein Kunde schickt ein kurzes Hochformat-Video per Telegram. Das System schneidet Pausen heraus,
erzeugt Untertitel, schickt eine Vorschau zurück, nimmt Korrekturen als Freitext an und rendert
neue Versionen bis zur Freigabe. Architektur und Konventionen: [CLAUDE.md](CLAUDE.md),
Umsetzungsstand: [TASKS.md](TASKS.md).

## Einrichtung auf dem Mac (Apple Silicon)

### 1. Systemwerkzeuge

**FFmpeg** muss mit libass gebaut sein (für Untertitel) und auf dem `PATH` liegen:

```bash
ffmpeg -hide_banner -filters | grep subtitles   # muss eine Zeile "subtitles" zeigen
```

**uv** wird ohne Homebrew in einer kleinen Bootstrap-venv installiert (`.venv-uv`, gitignored).
uv lädt Python 3.12 selbst herunter und legt die Projekt-venv `.venv` an:

```bash
python3 -m venv .venv-uv
.venv-uv/bin/pip install uv
export PATH="$PWD/.venv-uv/bin:$PATH"   # danach funktionieren alle `uv …`-Befehle unten
```

Docker: [Docker Desktop für Mac](https://www.docker.com/products/docker-desktop/) installieren
und starten. Docker führt nur Postgres und den Telegram Bot API Server aus. Bot und Worker laufen
direkt auf dem Mac, weil Docker die Apple-GPU nicht nutzen kann.

### 2. Projekt

```bash
uv sync                       # Abhängigkeiten inkl. Entwicklungswerkzeuge
uv sync --extra mac           # zusätzlich mlx-whisper (Transkription auf der Apple-GPU)
cp .env.example .env          # danach Werte eintragen, siehe unten
```

Playwright (Cover und Textkarten, ab Etappe 5):

```bash
uv run playwright install chromium
```

### 3. Telegram-Zugangsdaten

- **Bot-Token:** In Telegram mit [@BotFather](https://t.me/BotFather) chatten, `/newbot` senden,
  Namen vergeben. Das Token als `TELEGRAM_BOT_TOKEN` in `.env` eintragen.
- **`api_id` / `api_hash`:** Der lokale Bot API Server braucht eigene API-Zugangsdaten.
  Auf [my.telegram.org](https://my.telegram.org) mit der Telefonnummer anmelden,
  „API development tools“ öffnen, eine App anlegen und die Werte als `TELEGRAM_API_ID` und
  `TELEGRAM_API_HASH` eintragen.
- **Admin-Chat:** Die numerische Chat-ID für Fehlermeldungen als `ADMIN_CHAT_ID`.

Der lokale Bot API Server (`--local`) hebt das 20-MB-Limit für Downloads auf und legt Dateien
im Ordner `data/telegram-bot-api` ab. Die Pfade, die der Server meldet, gelten im Container
(`TELEGRAM_FILES_CONTAINER_PATH`) und werden auf den Host abgebildet (`TELEGRAM_FILES_HOST_PATH`).

### 4. Dienste und Datenbank

```bash
docker compose up -d          # postgres (pgvector) + telegram-bot-api
uv run alembic upgrade head   # Schema anlegen
```

Postgres legt beim ersten Start zusätzlich die Datenbank `reels_test` für die Integrationstests an.
Bei einem bereits vorhandenen Volume fehlt sie eventuell; dann einmalig:

```bash
docker compose exec postgres createdb -U reels reels_test
```

### 5. Erster Kunde

```bash
uv run reels client add "Demo" --telegram-user 123456789
uv run reels client list
```

Der Kunde bekommt die Default-Einstellungen aus `ClientSettings` (siehe CLAUDE.md, Abschnitt 5).

## Pipeline ohne Bot (Etappe 1)

```bash
uv run reels glossary import demo begriffe.csv     # term,variants,category,note,highlight,status
uv run reels process video.mp4 --client demo       # schreibt data/clients/<id>/projects/<id>/v1/
uv run reels process video.mp4 --client demo --project 7 --karaoke   # neu rendern, Cache nutzen
uv run reels bench-transcribe video.mp4 --runs 2 --out docs/benchmark.md
```

- `--engine whisperx|mlx` wählt die Transkription (Standard: `TRANSCRIBE_ENGINE`).
  `mlx` braucht `uv sync --extra mac`. Die Modelle (`WHISPERX_MODEL`, `MLX_WHISPER_MODEL`) werden
  beim ersten Lauf von Hugging Face geladen (mehrere GB).
- Hervorhebungen kommen von OpenAI, wenn `OPENAI_API_KEY` und `OPENAI_MODEL_TEXT` gesetzt sind,
  sonst gibt es keine. `--no-llm` schaltet sie ab.
- Glossar-CSV: Varianten mit `|` trennen. Ohne `status` gelten Einträge als bestätigt.
- Qualitätsprüfung: [docs/quality-test.md](docs/quality-test.md).

Schrift: Montserrat (OFL), siehe [assets/fonts/README.md](assets/fonts/README.md).

## Entwicklung

```bash
uv run pytest                                      # alle Tests
uv run pytest -m "not integration"                 # nur Unit-Tests (ohne FFmpeg/Postgres)
uv run ruff check . && uv run ruff format --check .
uv run mypy src
```

Integrationstests brauchen FFmpeg und das Postgres aus `docker compose`. Ist die Testdatenbank
nicht erreichbar, werden die DB-Tests übersprungen. `TEST_DATABASE_URL` muss auf eine Datenbank
zeigen, deren Name auf `_test` endet – die Tests setzen das Schema dort zurück.

Synthetische Testvideos (Hoch- und Querformat, Sinuston mit Stille-Abschnitten an bekannten
Stellen, dazu eine JSON-Datei mit den Intervallen):

```bash
uv run python -m tests.fixtures.synthetic_video --out tests/fixtures/generated
```

Kundenvideos gehören nie ins Repository.

## Logging

Eine Zeile pro Ereignis, mit `client_id`, `project_id`, `version_id`, `job_id`, wo vorhanden.
`LOG_FORMAT=text` (lesbar, `key=value`) oder `LOG_FORMAT=json` (eine JSON-Zeile pro Ereignis).
