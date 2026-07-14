# VMware Kapazitätsplanung (Aria Operations)

Kapazitätsauswertung pro Cluster aus VMware Aria Operations mit browserbasiertem
Dashboard und Reservierungsfunktion für künftige Kapazitätsanfragen.

## Dashboard

- **Kompakte Tabellenansicht**: pro Cluster die freien vCPU-/RAM-Kapazitäten
  (nach Abzug genehmigter Reservierungen) mit Auslastungsbalken; Klick auf den
  Clusternamen zeigt Details (Hosts, VMs, Reservierungen, Antrags-Formular)
- **Filterfeld** für Cluster bzw. Reservierungen
- **Eigene Reservierungsseite** (Tab „Reservierungen" bzw. `/reservierungen`)
  mit allen Kapazitätsanfragen, Status und Summenzeile
- **Genehmigungs-Dashboard** (Tab „Genehmigungen" bzw. `/genehmigungen`):
  offene Anträge genehmigen oder ablehnen
- **Auto-Aktualisierung** im Serve-Modus (Standard: alle 30 Minuten, sichtbarer
  Countdown) plus Knopf „⟳ Jetzt aktualisieren"

## Berechnung

- **CPU-Kapazität** = Summe physischer Cores aller ESXi-Hosts im Cluster × Überprovisionierungsfaktor (Standard: 6)
- **RAM-Kapazität** = Summe physischer RAM aller Hosts (1:1)
- **Ausfallreserve (N+1)**: pro Cluster wird der größte Host (Cores und RAM)
  von der Gesamtkapazität abgezogen (`--failover-hosts`, Standard: 1, `0` = aus)
- **Belegt** = provisionierte vCPUs / RAM aller VMs im Cluster (inkl. powered-off)
- **Frei** = Kapazität − belegt − genehmigte Reservierungen

## Verwendung

Nur Python 3.8+ nötig, keine Zusatzpakete — läuft damit direkt auf jedem Linux-Host.

**Server-Modus** (empfohlen): Seite lädt sofort aus dem Datei-Cache
(`kapa_cache.json`); beim allerersten Start ohne Cache werden die Daten
automatisch abgerufen. Danach Aktualisierung alle 30 Minuten oder per Knopf:

```bash
python3 aria_kapa.py --url https://aria-ops.firma.de --user admin --insecure --serve
# Dashboard: http://localhost:8080  ·  Reservierungen: http://localhost:8080/reservierungen
```

**Einmaliger Snapshot** (statisches HTML, Reservierungen dann nur im Browser):

```bash
python3 aria_kapa.py --url https://aria-ops.firma.de --user admin --insecure
```

**Demo ohne Aria-Verbindung:**

```bash
python3 aria_kapa.py --sample                # statisch
python3 aria_kapa.py --sample --serve        # Server-Modus
```

## Reservierungen (Kapazitätsanfragen)

Anlegen per Dialog („+ Neue Kapazitätsanfrage") oder direkt in der
Detailkarte eines Clusters; Export/Import als JSON.

- **Gültigkeit**: Reservierungen gelten automatisch ab dem Anlagetag für
  30 Tage; das „gültig bis"-Datum wird in jeder Reservierung angezeigt.
- **Genehmigung**: Neue Anträge haben den Status „beantragt" und zählen erst
  nach Genehmigung (Tab „Genehmigungen") gegen die freie Kapazität.
- **Serve-Modus**: Reservierungen liegen zentral auf dem Server in
  `kapa_reservierungen.json` — alle Nutzer sehen denselben Stand.
- **Statisches HTML**: Speicherung lokal im Browser (localStorage).
- **Automatischer Ablauf**: Reservierungen werden `--res-ttl-days` Tage nach
  Anlage automatisch gelöscht (Standard: 31, `0` = nie löschen); die angezeigte
  Gültigkeit endet einen Tag davor (30 Tage).

## Optionen

| Option | Beschreibung |
|---|---|
| `--cpu-factor 6` | CPU-Überprovisionierungsfaktor |
| `--failover-hosts 1` | Ausfall-Hosts pro Cluster (N+1), `0` = aus |
| `--auth-source local` | Auth-Quelle (z. B. AD-Quelle) |
| `--insecure` | TLS-Zertifikat nicht prüfen (Self-Signed) |
| `--serve --port 8080` | Webserver-Modus |
| `--bind 0.0.0.0` | Bind-Adresse für `--serve` |
| `--refresh-interval 1800` | Auto-Aktualisierung in Sekunden (`0` = aus) |
| `--cache kapa_cache.json` | Datei-Cache der letzten Abfrage |
| `--res-file kapa_reservierungen.json` | Reservierungsdatei (Serve-Modus) |
| `--res-ttl-days 31` | Reservierungen nach N Tagen löschen (`0` = nie) |
| `--output datei.html` | Ausgabedatei (statischer Modus) |
| `--json datei.json` | Rohdaten zusätzlich als JSON |

Cache- und Reservierungsdatei sind lokale Laufzeitdaten und per `.gitignore`
vom Repository ausgeschlossen.

## Hinweis zum Betrieb

Der eingebaute Webserver hat keine Authentifizierung. Für den Betrieb über
`localhost` hinaus empfiehlt sich `--bind 127.0.0.1` hinter einem Reverse-Proxy
(z. B. nginx mit Basic-Auth/TLS) oder der Einsatz nur im vertrauenswürdigen
Verwaltungsnetz.
