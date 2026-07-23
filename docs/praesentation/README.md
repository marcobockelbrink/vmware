# Management-Präsentation

**[VMware-Kapazitaetsplanung.pptx](VMware-Kapazitaetsplanung.pptx)** — eine kompakte
10-Folien-Show (16:9) zum Vorzeigen bei Entscheidern.

## Inhalt

| Folie | Thema |
|-------|-------|
| 1 | Titel — „Kapazität sehen. Planen. Freigeben." |
| 2 | Highlight 1 · **Überblick** — freie Kapazität auf einen Blick |
| 3 | Highlight 2 · **Tiefe** — vom Cluster bis zur VM per Klick |
| 4 | Highlight 3 · **Governance** — mehrstufiger Freigabe-Workflow |
| 5 | Highlight 4 · **Prognose** — Trends & Wachstum aus der Historie |
| 6 | Highlight 5 · **Reichweite** — alle vROps-Quellen, auch isolierte |
| 7 | Mehr Funktionen — VLAN-Suche, Rollen/AD, eingebaute REST-API |
| 8 | Das Projekt in Zahlen — Releases, Commits, Code, Tests |
| 9 | Fazit — Betrieb, Sicherheit, Reichweite, Reife |
| 10 | Feature-Highlights — die komplette Sammlung auf einen Blick |

Die Screenshots stammen aus einer `--sample`-Instanz (Demo-Daten, keine echten
Systeme) und liegen unter [`screenshots/`](screenshots/).

## Neu erzeugen / anpassen

Die `.pptx` wird aus [`build_deck.py`](build_deck.py) erzeugt. Das Skript ist ein
**reines Build-Werkzeug für dieses Dokument** und gehört nicht zum Produkt
(das Dashboard selbst bleibt abhängigkeitsfrei / nur Standardbibliothek).

```bash
python3 -m venv venv && ./venv/bin/pip install python-pptx
cd docs/praesentation
../../venv/bin/python build_deck.py            # nutzt screenshots/, schreibt die .pptx
```

Texte (Titel, Nutzen-Bullets) stehen direkt im Skript und lassen sich dort
bequem ändern. Neue Screenshots (dunkles Theme) entstehen z. B. so:

```bash
python3 aria_kapa.py --serve --sample --port 9010 --data-dir /tmp/demo &
python3 tools/demo_seed.py http://127.0.0.1:9010     # realistische Anträge/Teams
# dann je Ansicht ein Headless-Screenshot, z. B.:
#   "…/Google Chrome" --headless --screenshot=screenshots/01_kapazitaet.png \
#     --window-size=1500,900 --force-device-scale-factor=2 http://127.0.0.1:9010/
# Deep-Links: /#genehmigungen  /#statistik  /#storage  /#cluster=Cluster-01
```
