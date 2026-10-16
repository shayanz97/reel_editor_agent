# TASKS.md – Umsetzung in Etappen

Regeln: Immer nur die aktuelle Etappe bearbeiten. Aufgaben in Reihenfolge abarbeiten und abhaken.
Eine Etappe gilt erst als fertig, wenn alle Abnahmekriterien erfüllt sind.
Architektur, Konventionen und Schemas stehen in `CLAUDE.md`.

Status: **Etappe 1** (Etappe 0 fertig bis auf den Start des Bot-API-Servers: wartet auf Telegram-Zugangsdaten)

---

## Etappe 0 – Projektgerüst

**Ziel:** Ein lauffähiges, leeres Projekt mit Datenbank, Konfiguration und Tests.

- [x] 0.1 `pyproject.toml` mit uv anlegen (Python 3.12), Paket `reels` unter `src/`, Extra `mac` für mlx-whisper
- [x] 0.2 ruff, mypy (strict für `domain/`), pytest konfigurieren
- [x] 0.3 `docker-compose.yml`: Postgres mit pgvector (Volume), Telegram Bot API Server mit `--local` (Volume `data/telegram-bot-api`)
- [x] 0.4 `.env.example` mit allen Variablen: DB-URL, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_API_URL`, Pfadabbildung Container→Host, `ADMIN_CHAT_ID`, `OPENAI_API_KEY`, `OPENAI_MODEL_TEXT`, `OPENAI_MODEL_VISION`, `OPENAI_MODEL_EMBED`, `PEXELS_API_KEY`, `DATA_DIR`, `TRANSCRIBE_ENGINE`
- [x] 0.5 `config.py` mit pydantic-settings, `settings_schema.py` mit `ClientSettings` und Defaults aus `CLAUDE.md`
- [x] 0.6 SQLAlchemy-Modelle und erste Alembic-Migration: `clients`, `client_users`, `projects`, `assets`, `transcripts`, `versions`, `corrections`, `glossary`, `jobs`
- [x] 0.7 Typer-CLI-Gerüst `reels` mit `client add` und `client list`
- [x] 0.8 Strukturiertes Logging (eine Zeile pro Ereignis, mit `client_id`, `project_id`, `job_id`, wo vorhanden)
- [x] 0.9 Skript für synthetisches Testvideo in `tests/fixtures/` (Bild + Ton mit eingebauten Stille-Abschnitten, Hoch- und Querformat)
- [x] 0.10 README: Einrichtung auf dem Mac (Homebrew-FFmpeg mit libass, uv, Docker, Playwright), Bot-Token über @BotFather, `api_id`/`api_hash` über my.telegram.org

**Abnahme**
- `docker compose up -d` startet beide Dienste, `uv run alembic upgrade head` läuft durch
- `uv run pytest`, `ruff` und `mypy` sind grün
- `uv run reels client add "Demo" --telegram-user 123` legt einen Kunden mit Default-Einstellungen an

---

## Etappe 1 – Pipeline als Kommandozeile

**Ziel:** `uv run reels process video.mp4 --client demo` erzeugt ein fertiges Reel. Kein Bot, keine Queue.

- [x] 1.1 `media/probe.py`: Dauer, Auflösung, fps, Rotation, Ausrichtung. Ablehnung über `max_duration_s`
- [x] 1.2 `media/audio.py`: Mono-WAV 16 kHz extrahieren
- [ ] 1.3 `transcribe/base.py` (Protokoll, liefert `list[Word]` mit stabilen IDs), `whisperx_engine.py` (CPU, int8, mit Alignment), `mlx_engine.py` (Wort-Zeitstempel), Fake-Engine für Tests — *Code + Fake fertig, Echtlauf der Modelle steht aus*
- [x] 1.4 Hinweistext aus dem Wörterbuch für die Transkription (bestätigte Begriffe, längenbegrenzt)
- [x] 1.5 `domain/glossary.py`: deterministische Variantenersetzung (wortgrenzen-sicher, mehrwortfähig, Zähler `hits`) + `reels glossary import/export` (CSV)
- [x] 1.6 `vad/silero.py`: Sprachbereiche
- [x] 1.7 `domain/cutplan.py`: Schnittplan aus VAD + Wortgrenzen, Pausen kürzen, Puffer, Füllwörter (nur wenn im Transkript), nie innerhalb eines Wortes schneiden
- [x] 1.8 `domain/timeline.py`: Abbildung Original → Ausgabe, inkl. Tests für Randfälle
- [x] 1.9 `domain/segments.py`: Segmentierung in Sätze/Sinneinheiten, deterministische Nummerierung
- [x] 1.10 `domain/subtitles.py` + `media/ass.py`: Gruppen (max. `subtitle.max_words`, möglichst einzeilig), Hervorhebungen, Karaoke-Option (`\k`-Tags), Position im unteren Drittel, Schrift aus `assets/fonts/`, Golden-File-Tests
- [x] 1.11 Hervorhebungen über die LLM-Schicht: `llm/base.py`, `openai_provider.py`, Schema `HighlightPlan`, Prompt in `llm/prompts/highlights.md`. Fallback ohne LLM: keine Hervorhebungen
- [x] 1.12 `media/render.py`: Schnitt mit `trim`/`atrim`/`concat`, Audio-Fades, abwechselnder Zoom, Mitten-Crop bei Querformat, `loudnorm`, Untertitel zuletzt, Export nach Spezifikation
- [x] 1.13 `cli.py`: `reels process` schreibt alle Zwischenstände in `data/clients/<id>/projects/<id>/v1/` und gibt eine Zusammenfassung aus (Originaldauer, neue Dauer, Anzahl Schnitte, Laufzeit pro Schritt)
- [ ] 1.14 `reels bench-transcribe`: beide Engines auf dasselbe Video, Ausgabe von Laufzeit, Wortanzahl, Abweichung der Wortgrenzen zwischen den Engines und eine Textgegenüberstellung — *Code fertig, Lauf mit echtem Video steht aus*
- [x] 1.15 Qualitätstest-Protokoll `docs/quality-test.md` vorbereiten (Checkliste pro Video, siehe Abnahme)

**Abnahme**
- Synthetisches Testvideo: erwartete Stille-Abschnitte werden gekürzt, keine Schnittkante liegt innerhalb eines Wortes (automatisierter Test)
- Hoch- und Querformat erzeugen jeweils 1080 × 1920
- Untertitel erscheinen im Export, Karaoke-Option schaltbar
- Benchmark läuft mit beiden Engines auf dem Mac, Ergebnisse werden dokumentiert (keine geschätzten Zahlen)
- **Qualitätstest mit 10 echten Kundenvideos** (nicht im Repo) nach Checkliste:
  - keine abgeschnittenen Silben, keine Klicks an Schnitten
  - Pausen wirken natürlich, nicht gehetzt
  - Untertitel synchron, richtig geschrieben, gut lesbar, nicht verdeckt
  - Zoom wirkt dezent
  - Lautstärke gleichmäßig
- Entscheidung für eine Standard-Engine ist getroffen und in `.env.example` eingetragen
- **Erst nach erfolgreichem Qualitätstest mit Etappe 2 beginnen.**

---

## Etappe 2 – Datenbank, Queue, Worker

**Ziel:** Die Pipeline läuft als Jobs mit Versionen, zuverlässig und wiederholbar.

- [ ] 2.1 `db/queue.py`: Job anlegen, Job holen mit `FOR UPDATE SKIP LOCKED`, Statuswechsel, Retry-Zähler
- [ ] 2.2 `pipeline/steps.py`: jeder Schritt als eigene Funktion mit klaren Ein- und Ausgaben (probe, audio, transcribe, glossary, cutplan, segments, highlights, subtitles, render)
- [ ] 2.3 `pipeline/runner.py`: Abhängigkeiten zwischen Schritten, Wiederverwendung vorhandener Ergebnisse, gezieltes Neuausführen ab einem Schritt
- [ ] 2.4 `worker/main.py`: Schleife, Timeout pro Schritt, max. 3 Versuche, Zurücksetzen hängender Jobs beim Start, sauberes Beenden (SIGTERM)
- [ ] 2.5 Versionen: Version 1 aus Pipeline, Speicherung von `cut_plan`, `highlights`, `subtitle_style`, `export_path`
- [ ] 2.6 Metriken pro Job in `jobs.metrics` (Laufzeit, LLM-Tokens)
- [ ] 2.7 Aufräum-Job: Originale und Zwischenstände nach `retention_days` ab Freigabe löschen
- [ ] 2.8 CLI: `reels enqueue <video> --client …`, `reels jobs list`, `reels jobs retry <id>`

**Abnahme**
- Zwei Worker parallel verarbeiten nie denselben Job (Test)
- Ein absichtlich fehlschlagender Schritt wird 3-mal versucht und dann als `failed` markiert
- Erneutes Ausführen eines Projekts verwendet Transkript und Audio wieder (Test über Metriken/Logs)
- Worker-Neustart während eines Jobs führt nicht zu doppelter oder verlorener Arbeit

---

## Etappe 3 – Telegram-Bot

**Ziel:** Kunde schickt Video, bekommt Vorschau mit Segmentliste und kann freigeben.

- [ ] 3.1 `bot/main.py`: aiogram mit lokalem Bot API Server, Polling
- [ ] 3.2 Zugriffsprüfung über `client_users`, Absage für Unbekannte + Hinweis an Admin-Chat
- [ ] 3.3 Empfang von Video als Datei und als komprimiertes Video (Hinweis „bitte als Datei senden“), Pfadabbildung Container→Host, Projekt + Job anlegen, Duplikaterkennung über `message_id`
- [ ] 3.4 Optionaler Begleittext wird gespeichert (für spätere Nutzung als Referenz)
- [ ] 3.5 Statusmeldungen: empfangen, in Bearbeitung, Vorschau bereit (Nachricht bearbeiten statt neue senden)
- [ ] 3.6 Vorschau: Video per Upload + nummerierte Segmentliste + Buttons „Freigeben“ und „Ändern“
- [ ] 3.7 Freigabe setzt `approved_at`, sendet finalen Export
- [ ] 3.8 Admin-Chat: Fehler mit Job-ID und Ursache, Admin-Befehle `/kunde_neu`, `/kunde_user`, `/status`
- [ ] 3.9 Alle Kundentexte zentral in `bot/texts.py` (Deutsch, Du, fachlich, knapp)

**Abnahme**
- Ende-zu-Ende lokal: Video in Telegram senden → Vorschau kommt zurück → Freigeben → Export kommt
- Videos über 20 MB funktionieren (lokaler Bot API Server)
- Unbekannter Telegram-User wird abgewiesen, Admin wird informiert
- Ein Fehler im Worker führt zu einer verständlichen Kundennachricht und einer Admin-Meldung

---

## Etappe 4 – Korrekturen und Fachwörterbuch

**Ziel:** Kunde korrigiert per Freitext, bekommt eine neue Version. Das Wörterbuch lernt mit.

- [ ] 4.1 `domain/corrections.py`: Pydantic-Schemas aller Operationen (diskriminierte Union), Validierung gegen Version, Angabe der neu auszulösenden Schritte pro Operation
- [ ] 4.2 Anwendung der Operationen auf Schnittplan, Text-Overlay, Hervorhebungen, Untertitelposition → neue Version
- [ ] 4.3 LLM: Freitext → `CorrectionPlan` (Prompt `llm/prompts/corrections.md`, Kundennachricht klar als Daten markiert)
- [ ] 4.4 Bot-Ablauf: „Ändern“ → Freitext → Zusammenfassung des Verstandenen → Buttons „Ausführen“ / „Nochmal“ → neue Version → neue Vorschau
- [ ] 4.5 Rückfragen bei ungültigen oder unklaren Operationen statt Raten
- [ ] 4.6 Korrekturen zu veralteten Versionen ablehnen mit Hinweis
- [ ] 4.7 `add_to_glossary`: Nachfrage „Soll das künftig immer so korrigiert werden?“ → Eintrag mit Variante
- [ ] 4.8 Vokabularprüfung nach der Transkription: LLM markiert verdächtige Wörter → gezielte Rückfrage im Bot vor dem Rendern (mit Option „passt so“)
- [ ] 4.9 Wörterbuch-Befehle: `/begriffe` (Liste importieren), `/woerterbuch`, `/begriff_neu`, `/begriff_aendern`, `/begriff_loeschen`, CSV-Export
- [ ] 4.10 `note`-Operation leitet Wünsche an den Admin-Chat weiter

**Abnahme**
- Testfälle für jede Operation auf `domain`-Ebene (ohne LLM)
- Beispiel „In 2 heißt es Kubernetes, Satz 3 raus“ führt zu genau zwei korrekten Operationen und einer neuen Version
- Textkorrektur löst kein neues Schneiden aus, Segment-Löschung schon (über Metriken prüfbar)
- Eine bestätigte Wörterbuch-Korrektur wird im nächsten Video automatisch angewendet
- Keine Korrektur wird ohne Bestätigung des Kunden ausgeführt

---

## Etappe 5 – Veröffentlichungspaket (Instagram)

**Ziel:** Nach Freigabe bekommt der Kunde drei Cover, einen Beitragstext und Hashtags zur Auswahl.

- [ ] 5.1 `media/frames.py`: Kandidaten-Frames extrahieren, nach Schärfe filtern, Gesichtserkennung (OpenCV) zur Vorauswahl
- [ ] 5.2 LLM wählt 3 Frames (Bildeingabe) und schlägt 3 unterschiedliche Überschriften vor
- [ ] 5.3 Neutrale HTML/CSS-Standardvorlage in `assets/templates/cover/`, Platzhalter für Frame, Überschrift, Farben, Schrift
- [ ] 5.4 `publish/covers.py`: Rendern mit Playwright als PNG 1080 × 1920
- [ ] 5.5 `publish/post_text.py`: Beitragstext (Einstieg, Mehrwert, Handlungsaufforderung), Ton aus `ClientSettings.tone`, Schreibweisen aus dem Wörterbuch
- [ ] 5.6 `publish/hashtags.py`: Vorschläge, ohne Reichweitenversprechen
- [ ] 5.7 Bot: Cover-Auswahl per Button, Textkorrektur per Freitext, Ausgabe von MP4, Cover und Text

**Abnahme**
- Nach Freigabe kommen automatisch 3 verschiedene Cover und ein Text
- Ausgewähltes Cover und korrigierter Text werden als finales Paket gesendet
- Cover-Vorlage ist pro Kunde austauschbar, ohne Codeänderung

---

## Etappe 6 – B-roll

**Ziel:** Passende B-roll aus Pexels oder als Textkarte, an Wort-IDs verankert, per Korrektur steuerbar.

- [ ] 6.1 Migration: `broll_assets` (mit pgvector-Embedding), `broll_placements`
- [ ] 6.2 `domain/broll_rules.py`: keine B-roll in den ersten 2–3 s, 1,5–4 s pro Stelle, Gesamtanteil ≤ `broll.max_coverage`, Mindestabstand
- [ ] 6.3 `broll/planner.py`: LLM schlägt Stellen vor (Wortbereich, Bildidee, englische Suchanfrage, Typ, Priorität), Code wendet die Regeln an
- [ ] 6.4 `broll/library.py`: Kundenbibliothek, Indexierung (Frames → Beschreibung per Bildeingabe → Embedding), Ähnlichkeitssuche
- [ ] 6.5 `broll/pexels.py`: Suche (Hochformat bevorzugt, Mindestlänge), Download in den Cache, Quell-URL speichern
- [ ] 6.6 `broll/textcards.py`: Textkarten aus HTML-Vorlage per Playwright, als kurzes Video
- [ ] 6.7 Prüfung: Vorschaubild gegen Bildidee bewerten, unter Schwellwert verwerfen
- [ ] 6.8 Rendering: Vollbild-Overlay ohne Ton, kurze Überblendung, Untertitel darüber
- [ ] 6.9 Vorschau zeigt B-roll-Liste (`[B1] Satz 4 · Serverraum (Pexels)`)
- [ ] 6.10 Korrektur-Operationen `remove_broll`, `replace_broll`, `add_broll`, `set_broll_coverage`
- [ ] 6.11 `#broll`: Kunde schickt Clips/Fotos, sie landen indexiert in seiner Bibliothek

**Abnahme**
- B-roll-Regeln sind unit-getestet
- Ein gelöschtes Segment entfernt die zugehörige B-roll automatisch
- Ohne passenden Treffer wird die Stelle übersprungen, statt Unpassendes einzusetzen
- Eigene Clips aus der Bibliothek werden vor Pexels bevorzugt
- Pexels-Lizenzbedingungen sind vor dem Echtbetrieb geprüft und im README vermerkt

---

## Später (nicht jetzt umsetzen)

- Sprachnachrichten als Korrekturen
- Automatisches Veröffentlichen auf Instagram
- Weitere Plattformen (TikTok, YouTube Shorts)
- Versprecher- und Wiederholungserkennung
- Bot und Worker in Containern für VPS oder Kunden-PC, Backups, Monitoring
