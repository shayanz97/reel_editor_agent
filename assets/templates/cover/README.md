# Cover-Vorlagen

Jede Vorlage ist ein Ordner mit einer `cover.html` (1080 × 1920). Welche Vorlage ein Kunde
bekommt, steht in seinen Einstellungen:

```bash
uv run reels client set <kunde> publish.cover_template=<ordnername>
uv run reels client set <kunde> publish.cover_accent_color=#FF3366 publish.cover_text_color=#FFFFFF
```

Platzhalter (werden beim Rendern ersetzt):

| Platzhalter | Inhalt |
| --- | --- |
| `$frame_url` | Standbild aus dem Video (file-URL, 1080 × 1920) |
| `$headline` | Überschrift (HTML-sicher) |
| `$font_url` | Montserrat aus `assets/fonts/` (variable Schrift, 100–900) |
| `$text_color`, `$accent_color` | Farben aus den Kundeneinstellungen |

Ein Dollarzeichen im CSS als `$$` schreiben. Wenn die Seite fertig ist (z. B. nach Schriftanpassung),
`document.body.dataset.ready = "1"` setzen; der Renderer wartet darauf (höchstens 5 s).
Den mittleren 3:4-Bereich (y 240–1680) für wichtige Inhalte nutzen: Den zeigt Instagram im Profilraster.
