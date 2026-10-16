# Qualitätstest Etappe 1

Ziel: Mit **10 echten Kundenvideos** prüfen, ob die automatisch erzeugte Version 1 gut genug ist,
bevor Etappe 2 beginnt. Kundenvideos liegen **nicht** im Repository; hier nur eine anonyme ID
eintragen (z. B. `K03-V2`).

## Vorbereitung

```bash
uv run reels client add "Qualitätstest" --slug qt
uv run reels glossary import qt begriffe.csv        # optional
uv run reels process /pfad/video.mp4 --client qt     # Ausgabe: data/clients/<id>/projects/<id>/v1/
```

Nach einer Einstellungsänderung dasselbe Projekt neu rendern (Audio, Transkript und VAD werden
wiederverwendet):

```bash
uv run reels process /pfad/video.mp4 --client qt --project <project_id>
```

## Startwerte, die der Test bestätigen oder korrigieren soll

Diese Werte sind gesetzt, aber noch nicht an echten Videos geprüft:

| Wert | Ort | aktuell |
| --- | --- | --- |
| Pause nach Schnitt (2 × Randpuffer) | `ClientSettings.cut_padding_s`, `pause_target_s` | 0,16 s (Puffer 0,08 s geht vor Ziel 0,15 s) |
| Pausen-Schwelle | `pause_threshold_s` | 0,4 s |
| Zoom an Schnitten | `zoom_factor` | 1,08 |
| Untertitel-Position (unteres Drittel) | `media/ass.py`, `_POSITIONS` | Unterkante bei y = 1420 von 1920 |
| Schriftgröße / Kontur | `media/ass.py`, `style_from_settings` | 72 px / 6 px, Montserrat ExtraBold |
| Zeichen pro Untertitel | `subtitle.max_chars` | 22 |
| Wörter pro Untertitel | `subtitle.max_words` | 4 |
| Nachlauf eines Untertitels | `domain/subtitles.py`, `linger_s` | 0,25 s |
| Lautheit | `loudness_lufs` | −14 LUFS (loudnorm, ein Durchgang) |

## Checkliste pro Video

Kopieren und pro Video ausfüllen. Bewertung: ✅ passt · ⚠️ kleine Mängel · ❌ nicht akzeptabel.

```markdown
### Video <ID>

- Länge vorher → nachher: … s → … s, Schnitte: …
- Format (Hoch/Quer, Auflösung, fps): …
- Engine / Modell: …
- Laufzeit gesamt (aus der Ausgabe von `reels process`): … s

| Prüfpunkt | Bewertung | Notiz (Zeitstempel im Export) |
| --- | --- | --- |
| Keine abgeschnittenen Silben am Satzanfang/-ende | | |
| Keine Klicks oder Knackser an Schnitten | | |
| Pausen wirken natürlich, nicht gehetzt | | |
| Keine Pause zu lang stehen geblieben | | |
| Füllwörter (ähm …) entfernt, ohne Wortreste | | |
| Untertitel synchron zum Ton | | |
| Untertitel richtig geschrieben (Fachbegriffe!) | | |
| Untertitel gut lesbar (Größe, Kontrast, Zeilen) | | |
| Untertitel nicht von Instagram-Bedienelementen verdeckt | | |
| Hervorhebungen sinnvoll (falls LLM aktiv) | | |
| Zoom an Schnitten wirkt dezent | | |
| Lautstärke gleichmäßig, nicht übersteuert | | |
| Bildausschnitt (Querformat: Mitten-Crop) passt | | |

Gesamturteil: ✅ / ⚠️ / ❌ – Bemerkung: …
```

Zur Prüfung „nicht verdeckt“ den Export auf dem Handy in der Instagram-App als Reel-Entwurf
öffnen (nicht veröffentlichen) oder ein Screenshot-Overlay der Instagram-Oberfläche verwenden.

## Zusammenfassung

| Video | Schnitt | Untertitel | Ton | Zoom | Gesamt |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

Abnahme Etappe 1: alle 10 Videos mindestens ⚠️, keines ❌ in „abgeschnittene Silben“,
„Klicks“ oder „Untertitel synchron“. Geänderte Startwerte oben eintragen.

## Benchmark der Transkription

```bash
uv sync --extra mac
uv run reels bench-transcribe /pfad/video.mp4 --runs 2 --out docs/benchmark.md
```

Lauf 1 enthält das Herunterladen bzw. Laden der Modelle, Lauf 2 zeigt die reine Laufzeit.
Ergebnisse unverändert übernehmen (keine geschätzten Zahlen). Danach Standard-Engine in
`.env.example` (`TRANSCRIBE_ENGINE`) festlegen und die Entscheidung hier begründen:

- Entscheidung: **mlx** (`mlx-community/whisper-large-v3-turbo`), Stand 2026-10-16.
- Begründung (Laufzeit, Textqualität, Wortgrenzen):
  - Laufzeit: 5,6 s statt 21,3 s für 32 s Audio (zweiter Lauf, Messung in
    [benchmark.md](benchmark.md)), also etwa 4× schneller.
  - Text: mlx erkennt „Wege zur ersten Million“ richtig, WhisperX schreibt „zu ersten Millionen“.
    Die einzige mlx-Schwäche (abgetrennte Bindestriche wie „S -Tier“) wird beim Bereinigen der
    Wörter automatisch korrigiert.
  - Sichtvergleich beider Exporte desselben Videos: mlx wirkt bei Untertitel-Sync und Schnitten
    spürbar besser.
  - Einschränkung: bisher ein einziges Video (32 s). Im Qualitätstest mit 10 Videos weiter prüfen;
    WhisperX bleibt per `--engine whisperx` verfügbar.
