# API für externe Anwendungen (v1, nur lesend)

Das Dashboard stellt unter `/api/v1/` eine stabile, lesende REST-API bereit.
Externe Anwendungen authentifizieren sich mit einem **Bearer-Token**, das
Admins im Tab „Verwaltung" erzeugen (Abschnitt „API-Tokens"). Das Token wird
nur einmal angezeigt; gespeichert wird ausschließlich ein SHA-256-Hash.
Tokens sind einzeln widerrufbar; jede Erstellung/Widerruf und ungültige
Zugriffe landen im Audit-Log.

```bash
TOKEN="kapa_..."
BASE="https://host/capa"     # bzw. http://localhost:8080 ohne Proxy

curl -H "Authorization: Bearer $TOKEN" $BASE/api/v1/reservations
```

## Endpunkte

### GET /api/v1/reservations

Alle Reservierungen (ungefiltert, wie die Admin-Sicht). Felder je Eintrag:

| Feld | Bedeutung |
|---|---|
| `id` | Eindeutige ID |
| `name` | Bezeichnung / Projekt |
| `change` | Change-Nummer (CHB…/CHI…) |
| `cluster` | Ziel-Cluster |
| `vcpu`, `ram_gb` | Angefragte Kapazität |
| `von`, `abteilung` | Anforderer und Abteilung |
| `created` | Gilt ab (Anlagetag, ISO-Datum) |
| `approved`, `approved_on`, `approved_by` | Genehmigungsstatus |
| `rejected`, `rejected_on`, `rejected_by` | Ablehnungsstatus |
| `comment` | Kommentar des Admins |

**Filter** (kombinierbar):

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/reservations?cluster=Cluster-01&status=genehmigt&abteilung=IT"
```

- `cluster=<Name>` — nur ein Cluster
- `status=beantragt|genehmigt|abgelehnt`
- `abteilung=<Name>`

**CSV-Export** (Semikolon-getrennt, für Excel):

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/reservations?format=csv" -o reservierungen.csv
```

CSV-Spalten: `name;change;cluster;vcpu;ram_gb;von;abteilung;gilt_ab;`
`gueltig_bis;status;entschieden_von;kommentar`

### GET /api/v1/data

Cluster-Kapazitäten aus dem letzten Aria-Abruf:

```json
{"updated": "14.07.2026 19:00",
 "clusters": [{"name": "Cluster-01", "hostCount": 3, "cores": 64,
               "spareCores": 48, "spareRamGb": 1024.0,
               "vcpuCap": 384, "vcpuUsed": 370, "vcpuFree": 14,
               "ramCap": 1024.0, "ramUsed": 1504.0, "ramFree": -480.0,
               "vmCount": 93, "vmOff": 8, "hosts": [...], "vms": [...]}]}
```

Hinweis: `vcpuFree`/`ramFree` sind **vor** Abzug genehmigter Reservierungen;
die Reservierungen liefert `/api/v1/reservations` (Status `genehmigt`).

### GET /api/v1/status

```json
{"version": "0.6", "updated": "14.07.2026 19:00",
 "refreshing": false, "next": 1234}
```

`next` = Sekunden bis zur nächsten automatischen Aktualisierung.

## Fehlercodes

| Code | Bedeutung |
|---|---|
| 401 | Kein oder ungültiges/widerrufenes Token |
| 404 | Unbekannter Endpunkt |

## Hinweise

- Die v1-Pfade bleiben stabil; Erweiterungen kommen als neue Felder oder
  Parameter, bestehende Felder ändern sich nicht.
- Tokens sind rein lesend — Schreibzugriffe (Anträge stellen) sind für eine
  spätere Version mit eigenem `write`-Scope vorgesehen.
- Browser-Sessions (angemeldete Admins) können die v1-Endpunkte ebenfalls
  aufrufen, z. B. zum Testen.
