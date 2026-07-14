# VMware Kapazitätsplanung (Aria Operations)

Kapazitätsauswertung pro Cluster aus VMware Aria Operations mit browserbasiertem Dashboard und Reservierungsfunktion für künftige Kapazitätsanfragen.

## Berechnung

- **CPU-Kapazität** = Summe physischer Cores aller ESXi-Hosts im Cluster × Überprovisionierungsfaktor (Standard: 6)
- **RAM-Kapazität** = Summe physischer RAM aller Hosts (1:1)
- **Belegt** = provisionierte vCPUs / RAM aller VMs im Cluster (inkl. powered-off)
- **Frei** = Kapazität − belegt − Reservierungen

## Verwendung

Nur Python 3.8+ nötig, keine Zusatzpakete.

**Einmaliger Snapshot** (statisches HTML):

```bash
python3 aria_kapa.py --url https://aria-ops.firma.de --user admin --insecure
```

**Server-Modus** (empfohlen bei großen Umgebungen): Seite lädt sofort aus dem
Zwischencache, Knopf „⟳ Daten aus Aria abrufen" holt frische Daten im Hintergrund:

```bash
python3 aria_kapa.py --url https://aria-ops.firma.de --user admin --insecure --serve
# Dashboard: http://localhost:8080
```

**Demo ohne Aria-Verbindung:**

```bash
python3 aria_kapa.py --sample                # statisch
python3 aria_kapa.py --sample --serve        # Server-Modus
```

## Optionen

| Option | Beschreibung |
|---|---|
| `--cpu-factor 6` | CPU-Überprovisionierungsfaktor |
| `--auth-source local` | Auth-Quelle (z. B. AD-Quelle) |
| `--insecure` | TLS-Zertifikat nicht prüfen (Self-Signed) |
| `--serve --port 8080` | Webserver-Modus |
| `--cache kapa_cache.json` | Cache-Datei der letzten Abfrage |
| `--output datei.html` | Ausgabedatei (statischer Modus) |
| `--json datei.json` | Rohdaten zusätzlich als JSON |

## Reservierungen

Kapazitätsanfragen lassen sich im Dashboard per Dialog („+ Neue Kapazitätsanfrage")
oder direkt in der Cluster-Karte anlegen. Sie werden im Browser (localStorage)
gespeichert, in der freien Kapazität verrechnet und können als JSON
exportiert/importiert werden.
