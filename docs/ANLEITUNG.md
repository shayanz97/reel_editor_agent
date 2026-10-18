# Kurzanleitung Reels-Bot

Details stehen im [README](../README.md), Regeln für die Entwicklung in [CLAUDE.md](../CLAUDE.md).

## So ist es aufgebaut

```
Kunde (Telegram) ─► Telegram Bot API Server (Docker, lokal)
                          │
                    Bot-Prozess ──► Postgres (Docker): Projekte, Versionen, Jobs (= Queue)
                                          ▲
                    Worker-Prozess ───────┘  holt Jobs ab, macht alle Medien- und KI-Arbeit
```

- **Docker** startet nur Postgres (mit pgvector) und den lokalen Telegram-Server.
- **Bot** (`reels.bot`) nimmt Videos und Nachrichten an, legt Jobs an und schickt Ergebnisse zurück.
  Er rechnet nichts selbst.
- **Worker** (`reels.worker`) arbeitet die Jobs ab, jeder Schritt in einem eigenen Prozess mit
  Zeitlimit und bis zu 3 Versuchen.
- **Schritte pro Video:** probe → audio → transcribe → glossary → vocab → vad → cutplan → segments
  → highlights → broll_plan → broll_fetch → subtitles → render.
  Nach „Freigeben“: frames → cover_plan → covers → post_text.
- **Daten** liegen in `data/clients/<kunde>/projects/<projekt>/` (Original, Audio, Transkript) und
  `v<n>/` (Schnittplan, Untertitel, `export.mp4`, `covers/`).
- **Externe Dienste:** OpenAI (Text, Bildprüfung, Embeddings; nie ganze Videos oder Audio),
  Pixabay (B-Roll). Transkription läuft lokal (mlx-whisper auf der Apple-GPU).
- **Konfiguration:** `.env` (Schlüssel, Modelle, Pfade). Werte **nicht** per `export`/`source` in die
  Shell laden, die App liest `.env` selbst.

## Starten

```bash
docker compose up -d                 # Postgres + Telegram-Server
uv run alembic upgrade head          # nach Updates: Datenbank aktualisieren
uv run python -m reels.worker        # Terminal 1
uv run python -m reels.bot           # Terminal 2
```

Beenden mit `Ctrl+C`: Der Worker macht den laufenden Job noch fertig, zweimal `Ctrl+C` bricht ab.

## Telegram: für Kunden

| Aktion | Was passiert |
| --- | --- |
| Video schicken (am besten als Datei) | Status „empfangen → in Bearbeitung → Vorschau fertig“ |
| Fragen zu Wörtern beantworten | „Passt so“, Vorschlag, oder auf die Frage mit der richtigen Schreibweise antworten |
| Vorschau: **Freigeben** | Video in voller Qualität, danach Cover und Beitragstext |
| Vorschau: Text schreiben (oder **Ändern**) | z. B. „In Satz 2 heißt es Kubernetes, Satz 3 raus, B2 weg“ → Zusammenfassung → **Ausführen** / **Nochmal** |
| „Soll das künftig immer so korrigiert werden?“ | **Ja, immer** speichert es im Wörterbuch |
| Cover-Auswahl / **Text passt** / Text-Antwort | Cover wählen, Text bestätigen oder per Antwort ändern lassen → finales Paket |
| Datei, Video oder Foto mit `#broll` | landet in der eigenen B-Roll-Bibliothek |

Was man in Änderungswünschen sagen kann: Text korrigieren, Satz raus/zurück (E1 …), Wörter raus,
Pause behalten, Wort hervorheben/nicht hervorheben, Untertitel oben/Mitte/unten, Satz teilen,
Zwischenbild B… entfernen/ersetzen/einfügen, weniger/mehr/keine Zwischenbilder, Hinweis ans Team.

| Befehl | Zweck |
| --- | --- |
| `/start` | Begrüßung |
| `/woerterbuch` | eigenes Wörterbuch anzeigen |
| `/begriff_neu Kubernetes = Kubanetis, Cuber Netties` | Begriff mit falschen Schreibweisen anlegen |
| `/begriff_aendern Kubernetes = Kubanetis` | falsche Schreibweisen ersetzen |
| `/begriff_loeschen Kubernetes` | Begriff löschen |
| `/begriff_ok Kubernetes` | Vorschlag bestätigen (wirkt ab dem nächsten Video) |
| `/begriffe` + Zeilen `Begriff = Variante, …` | mehrere Begriffe auf einmal |
| `/begriffe_export` | Wörterbuch als CSV |

## Telegram: Admin-Chat (`ADMIN_CHAT_ID`)

| Befehl | Zweck |
| --- | --- |
| `/kunde_neu Müller GmbH \| 12345678` | Kunde anlegen (Telegram-ID optional) |
| `/kunde_user <kunde> <Telegram-ID>` | weiteren Nutzer freischalten |
| `/status` | Queue und Projekte |
| `/admin` oder `/hilfe` | Befehlsübersicht |

Der Admin-Chat bekommt automatisch: Fehler mit Job-ID und Ursache, unbekannte Nutzer (mit ID zum
Freischalten) und Hinweise von Kunden (`note`).

## Terminal: wichtige Befehle

```bash
# Kunden
uv run reels client add "Name" --telegram-user <id>
uv run reels client list
uv run reels client set <kunde> broll.enabled=true            # B-Roll an
uv run reels client set <kunde> subtitle.karaoke=true subtitle.position=center
uv run reels client set <kunde> publish.cover_template=default publish.max_hashtags=5

# Wörterbuch
uv run reels glossary import <kunde> begriffe.csv             # term,variants (mit |),…
uv run reels glossary export <kunde> --out begriffe.csv

# Videos ohne Telegram
uv run reels process video.mp4 --client <kunde>               # alles sofort im Terminal
uv run reels process video.mp4 --client <kunde> --project <id> # neu rendern, Transkript wiederverwenden
uv run reels enqueue video.mp4 --client <kunde>               # nur einreihen (Worker macht's)

# Jobs
uv run reels jobs list [--status failed]
uv run reels jobs retry <job_id>                              # fehlgeschlagenen Job neu starten
uv run reels jobs rerun <version_id> --from <schritt>         # z. B. --from subtitles, --from covers

# Wartung / Entwicklung
uv sync --extra mac                                           # Abhängigkeiten (immer mit --extra mac)
uv run python -m reels.worker --until-idle                    # Queue abarbeiten und beenden
uv run reels bench-transcribe video.mp4 --runs 2              # Transkription vergleichen
uv run pytest && uv run ruff check . && uv run mypy src       # Tests, Lint, Typen
```

Alle Kundeneinstellungen stehen in `src/reels/settings_schema.py`; `reels client set` prüft sie.

## Wenn etwas hängt

- **Fehler im Admin-Chat:** Ursache lesen, beheben (z. B. Modellname in `.env`), dann
  `uv run reels jobs retry <job_id>`. Das Video muss nicht neu hochgeladen werden.
- **`.env` geändert, aber alter Wert wirkt:** neues Terminal öffnen (oder `unset <VARIABLE>`), dann
  Worker und Bot neu starten.
- **Telegram-Server startet nicht:** `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` in `.env` prüfen,
  `docker compose up -d`.
- **Datenbank nicht erreichbar:** Docker Desktop läuft? Dann `docker compose up -d`.
- **Vor dem ersten Start mit lokalem Server (einmalig):** `uv run python -m reels.bot --logout-cloud`.

## Offen vor echten Kunden

Qualitätstest mit 10 Videos ([quality-test.md](quality-test.md)), Pixabay-Lizenz für kommerzielle
Nutzung prüfen lassen, Datenschutz (Kundeninfo, Auftragsverarbeitung mit OpenAI), dauerhafter
Betrieb (Rechner, Autostart, Backups).
