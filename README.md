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

**uv** (ohne Homebrew) mit dem offiziellen Installer von Astral; landet in `~/.local/bin`.
uv lädt Python 3.12 selbst herunter und legt die Projekt-venv `.venv` an:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
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

## Pipeline ohne Bot

```bash
uv run reels glossary import demo begriffe.csv     # term,variants,category,note,highlight,status
uv run reels client set demo subtitle.karaoke=true # Kundeneinstellungen ändern (validiert)
uv run reels process video.mp4 --client demo       # alle Schritte sofort, hier im Terminal
uv run reels process video.mp4 --client demo --project 7   # neu rendern, Audio/Transkript wiederverwenden
uv run reels bench-transcribe video.mp4 --runs 2 --out docs/benchmark.md
```

- `--engine whisperx|mlx` wählt die Transkription (Standard: `TRANSCRIBE_ENGINE`).
  `mlx` braucht `uv sync --extra mac`. Die Modelle (`WHISPERX_MODEL`, `MLX_WHISPER_MODEL`) werden
  beim ersten Lauf von Hugging Face geladen (mehrere GB).
- Hervorhebungen kommen von OpenAI, wenn `OPENAI_API_KEY` und `OPENAI_MODEL_TEXT` gesetzt sind,
  sonst gibt es keine. `--no-llm` schaltet sie ab.
- Schnellere Testrenders: `RENDER_PRESET=veryfast` in `.env`.
- Glossar-CSV: Varianten mit `|` trennen. Ohne `status` gelten Einträge als bestätigt.
- Ergebnisse: `data/clients/<client>/projects/<project>/` (Original, Audio, Transkript) und
  `v<n>/` (Schnittplan, Untertitel, `export.mp4`).
- Qualitätsprüfung: [docs/quality-test.md](docs/quality-test.md).

Schrift: Montserrat (OFL), siehe [assets/fonts/README.md](assets/fonts/README.md).

## Queue und Worker

Jeder Pipeline-Schritt ist ein Job in Postgres (Tabelle `jobs`):
`probe → audio → transcribe → glossary → vocab → vad → cutplan → segments → highlights → subtitles → render`.
`reels process` führt dieselben Jobs direkt aus; im Betrieb übernimmt sie der Worker.

```bash
uv run reels enqueue video.mp4 --client demo   # Projekt + Version 1 anlegen, ersten Schritt einreihen
uv run python -m reels.worker                  # Worker starten (mehrere parallel möglich)
uv run python -m reels.worker --until-idle     # nur abarbeiten, was gerade ansteht
uv run reels jobs list [--status failed]       # letzte Jobs mit Laufzeit und Fehler
uv run reels jobs retry <job_id>               # fehlgeschlagenen Job neu einreihen
uv run reels jobs rerun <version_id> --from cutplan   # ab einem Schritt neu (Rest wird wiederverwendet)
```

- Ein Schritt, sein Abschluss und das Einreihen des nächsten Schritts werden in **einer**
  Transaktion gespeichert. Ein Absturz hinterlässt keine halben Ergebnisse.
- Jeder Job läuft in einem eigenen Prozess mit Zeitlimit (`WORKER_STEP_TIMEOUT_S`).
  Höchstens `WORKER_MAX_ATTEMPTS` Versuche, danach `failed` und Admin-Meldung
  (bis Etappe 3 als Logzeile `admin_notify`).
- Stirbt ein Worker mitten im Job, setzt der nächste Worker den Job beim Start zurück: Ein Job gilt
  als verwaist, wenn seine Zeile nicht mehr von einem laufenden Schritt gesperrt ist.
- `Ctrl+C` / SIGTERM: aktuellen Job fertig machen, dann beenden. Zweites Signal: Job abbrechen und
  ohne Zählung des Versuchs zurück in die Queue.
- Aufräumen: einmal pro `WORKER_CLEANUP_INTERVAL_H` löscht ein `cleanup`-Job bei freigegebenen
  Projekten nach `retention_days` Original und Zwischenstände. Exporte und Cover bleiben.
- Metriken pro Job in `jobs.metrics`: Laufzeit, Versuch, Wiederverwendung, LLM-Tokens.

## Telegram-Bot

Voraussetzungen: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` und `ADMIN_CHAT_ID`
in `.env`, `docker compose up -d` (Bot API Server läuft im `--local`-Modus).

```bash
uv run python -m reels.bot --logout-cloud      # einmalig: Bot vom Telegram-Cloud-Server abmelden
uv run python -m reels.bot                     # Bot (Terminal 1)
uv run python -m reels.worker                  # Worker (Terminal 2)
```

- Nur Telegram-Nutzer aus `client_users` dürfen schreiben:
  `uv run reels client add "Name" --telegram-user <id>` oder im Admin-Chat `/kunde_user <kunde> <id>`.
  Unbekannte bekommen eine kurze Absage, der Admin-Chat einen Hinweis mit der ID.
- Ablauf: Video senden (am besten als Datei) → Statusnachricht wird aktualisiert
  (empfangen → in Bearbeitung → Vorschau fertig) → Vorschauvideo + nummerierte Sätze +
  Buttons „Freigeben“ / „Ändern“ → bei „Freigeben“ kommt der Export als Datei.
- Korrekturen: Der Kunde schreibt einfach, was anders sein soll (z. B. „In Satz 2 heißt es
  Kubernetes, Satz 3 raus“), oder tippt erst auf „Ändern“. Der Worker übersetzt den Text per LLM
  in erlaubte Operationen (`replace_text`, `delete_segment`, `restore_segment`, `delete_words`,
  `keep_pause`, `highlight`, `unhighlight`, `move_subtitles`, `split_segment`, `note`) und prüft
  sie gegen die Version. Der Bot zeigt, was verstanden wurde: „Ausführen“ erzeugt die nächste
  Version, „Nochmal“ verwirft. Unklares wird zurückgefragt, nie geraten. Ohne Klick auf
  „Ausführen“ ändert sich nichts.
- Neu gerendert wird nur ab dem ersten betroffenen Schritt: Textkorrekturen schneiden nicht neu,
  gelöschte Sätze schon. Die Transkription läuft nie erneut. `note` geht an den Admin-Chat.
- Antwortet der Kunde auf eine ältere Vorschau, wird die Korrektur mit Hinweis abgelehnt.
- Nach einer Schreibweisen-Korrektur fragt der Bot: „Soll das künftig immer so korrigiert
  werden?“ Bei „Ja, immer“ entsteht ein bestätigter Wörterbucheintrag, der ab dem nächsten Video
  automatisch angewendet wird.
- Vokabelprüfung (Schritt `vocab`): Das LLM markiert bis zu 5 Wörter, die vermutlich falsch
  erkannt wurden (Fachbegriffe, Namen). Der Bot fragt pro Wort („Passt so“ / Vorschlag / Antwort
  mit der richtigen Schreibweise); bis alles beantwortet ist, wartet die Pipeline vor dem Schnitt.
  Antworten landen als *Vorschlag* im Wörterbuch (wirken erst nach `/begriff_ok`). Ohne
  Telegram-Chat (`reels process`) wird nur protokolliert, nicht gewartet.
- Wörterbuch im Bot: `/woerterbuch`, `/begriff_neu Begriff = falsch1, falsch2`,
  `/begriff_aendern …`, `/begriff_loeschen …`, `/begriff_ok …`, `/begriffe` (mehrere Zeilen),
  `/begriffe_export` (CSV).
- Fehler: Der Kunde bekommt eine verständliche Nachricht (z. B. „Video zu lang“), der Admin-Chat
  Job-ID und Ursache.
- Admin-Chat (`ADMIN_CHAT_ID`): `/kunde_neu <Name> [| Telegram-ID …]`, `/kunde_user <kunde> <id>`,
  `/status`, `/admin`.
- Alle Kundentexte stehen in `src/reels/bot/texts.py`.
- Der Bot rechnet nichts selbst: Er legt Projekte und Jobs an und meldet zurück, was der Worker
  erledigt hat (Abfrage der Datenbank alle 2 s).

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
