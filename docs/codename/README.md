# Codename-Vorschläge (die lustige Runde) 🎉

Vorschläge für einen Projekt-Codenamen — mit Virtualisierungs- & Kapazitäts-Bezug,
jeweils inklusive **Sticker-Logo**.

**[Codename-Vorschlaege.pptx](Codename-Vorschlaege.pptx)** — 9 Folien: Titel,
7 Namen (je Logo + Gag + Begründung) und ein Sticker-Sheet.

## Die Kandidaten

| Logo | Name | Der Gag |
|------|------|---------|
| 🛎️ | **vAcancy** | „Vacancy" (freie Zimmer) im VMware-`v`-Look — zeigt, wo Platz frei ist |
| 🦌 | **Platzhirsch** | „Platz" = Kapazität; der Platzhirsch sichert sich das beste Revier |
| 🧩 | **ClusterTetris** | Kapazitätsplanung ist Tetris — passt der Block noch rein? |
| 💪 | **RAMbo** | Der Muskel hinter deinem Speicher |
| ✈️ | **Overbooked** | Wie die Airline — VMware nennt es „Overcommit" |
| 😰 | **Clusterphobie** | Die Angst, dass der Cluster volläuft — die App ist die Therapie |
| 🎈 | **99 Luftballons** | „Ballooning" ist ein echter VMware-Speichertrick (Nena gratis dazu) |

Die fertigen Sticker-PNGs (512×512, transparenter Hintergrund, Die-Cut-Look)
liegen unter [`logos/`](logos/) — direkt druck-/aufkleberfertig.

## Neu erzeugen

```bash
# 1) Logos rendern (Chrome headless, transparent)
python3 logos.py
for f in html/*.html; do
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
    --force-device-scale-factor=2 --default-background-color=00000000 \
    --window-size=512,512 --screenshot="logos/$(basename "$f" .html).png" "file://$PWD/$f"
done

# 2) Deck bauen (braucht python-pptx – reines Build-Werkzeug, nicht Teil des Produkts)
python3 -m venv venv && ./venv/bin/pip install python-pptx
./venv/bin/python build_names.py logos Codename-Vorschlaege.pptx
```

Namen, Gags und Logo-Designs stehen direkt in `build_names.py` bzw. `logos.py`
und lassen sich dort leicht anpassen.
