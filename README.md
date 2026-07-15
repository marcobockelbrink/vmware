# VMware Kapazitätsplanung (Aria Operations)

Kapazitätsauswertung pro Cluster aus VMware Aria Operations mit browserbasiertem
Dashboard und Reservierungsfunktion für künftige Kapazitätsanfragen.

![Dashboard mit Demo-Daten](docs/screenshot.png)

*Screenshot mit Demo-Daten (`python3 aria_kapa.py --sample --serve`).*

## Dashboard

- **Kompakte Tabellenansicht**: pro Cluster die freien vCPU-/RAM-Kapazitäten
  (nach Abzug genehmigter Reservierungen) mit Auslastungsbalken; Klick auf den
  Clusternamen zeigt Details (Hosts, VMs, Reservierungen, Antrags-Formular)
- **Filterfeld** für Cluster bzw. Reservierungen
- **Eigene Reservierungsseite** (Tab „Reservierungen" bzw. `/reservierungen`)
  mit allen Kapazitätsanfragen, Status und Summenzeile
- **Genehmigungs-Dashboard** (Tab „Genehmigungen" bzw. `/genehmigungen`):
  offene Anträge genehmigen oder ablehnen
- **Audit-Log** (Tab „Log" bzw. `/log`, nur Admins): protokolliert
  Anmeldungen (auch fehlgeschlagene), Anträge, Genehmigungen/Ablehnungen,
  Löschungen, Importe, Rollenänderungen und Backups nach
  `data/kapa_log.jsonl`
- **Auto-Aktualisierung** im Serve-Modus (Standard: alle 30 Minuten, sichtbarer
  Countdown) plus Knopf „⟳ Jetzt aktualisieren"

## Screenshots

Alle Aufnahmen mit Demo-Daten (`python3 aria_kapa.py --sample --serve`).

**Reservierungen** — alle Anfragen mit ID, Change-Nummer, Status,
Entscheider und Kommentar:

![Reservierungen](docs/screenshot-reservierungen.png)

**Genehmigungen** — offene Anträge mit freier Cluster-Kapazität;
⚠ markiert Anträge, die nicht mehr passen:

![Genehmigungen](docs/screenshot-genehmigungen.png)

**Verwaltung** (nur Admins) — Rollen/Abteilungen aus dem Active Directory
und API-Tokens für externe Anwendungen:

![Verwaltung](docs/screenshot-verwaltung.png)

**Log** (nur Admins) — Audit-Log mit Anmeldungen, Anträgen,
Entscheidungen und Backups:

![Audit-Log](docs/screenshot-log.png)

**Anmeldung** mit Active-Directory-Konto:

![Login](docs/screenshot-login.png)

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
(`data/kapa_cache.json`); beim allerersten Start ohne Cache werden die Daten
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

- **Eindeutige ID**: Jede Anfrage erhält beim Anlegen automatisch eine
  eindeutige ID (12 Zeichen). Sie wird in den Tabellen „Reservierungen" und
  „Genehmigungen" als erste Spalte angezeigt und steht auch in der
  Report-Mail, im CSV-Export (`/api/v1/reservations?format=csv`) und im
  Audit-Log — so lässt sich jede Anfrage zweifelsfrei referenzieren.

- **Change-Nummer (Pflichtfeld)**: Jede Anfrage benötigt eine Change-Nummer,
  beginnend mit `CHB` oder `CHI` (z. B. `CHB0012345`); Eingaben werden
  normalisiert (Großschreibung, ohne Leerzeichen) und client- wie
  serverseitig validiert. Die Nummer erscheint in den Übersichten und in
  der Report-Mail.

- **Gültigkeit**: Reservierungen gelten automatisch ab dem Anlagetag für
  30 Tage; das „gültig bis"-Datum wird in jeder Reservierung angezeigt.
- **Genehmigung**: Neue Anträge haben den Status „beantragt" und zählen erst
  nach Genehmigung (Tab „Genehmigungen") gegen die freie Kapazität. Die
  Genehmigungsübersicht zeigt je Antrag die freie Kapazität des Ziel-Clusters
  (⚠ wenn der Antrag nicht mehr hineinpasst).
- **Ablehnungen** bleiben 31 Tage (ab Ablehnung) als Historie sichtbar
  (Status „abgelehnt").
- **Kommentar**: Beim Genehmigen/Ablehnen kann der Admin einen Kommentar
  (z. B. Begründung) erfassen; er erscheint in der Reservierungsübersicht
  und in der Report-Mail.
- **Entschieden von**: Die Übersicht zeigt, welcher Admin genehmigt bzw.
  abgelehnt hat — für Anforderer ist diese Information verborgen (Spalte und
  Datenfeld werden serverseitig entfernt); Admins und technische Prüfung
  sehen sie.
- **Mail-Reports**: Mit `--smtp-server` verschickt das Dashboard bei jeder
  Genehmigung/Ablehnung eine Mail mit den Reservierungsdaten und dem
  ausführenden Admin an `--smtp-to` sowie automatisch an den Anforderer.
- **Serve-Modus**: Reservierungen liegen zentral auf dem Server in
  `data/kapa_reservierungen.json` — alle Nutzer sehen denselben Stand.
- **Statisches HTML**: Speicherung lokal im Browser (localStorage).
- **Automatischer Ablauf**: Reservierungen werden `--res-ttl-days` Tage nach
  Anlage automatisch gelöscht (Standard: 31, `0` = nie löschen); die angezeigte
  Gültigkeit endet einen Tag davor (30 Tage).

## API für externe Anwendungen

Unter `/api/v1/` gibt es eine stabile, **lesende** REST-API für externe
Anwendungen (Grafana, CMDB, Reporting …). Admins erzeugen dafür im Tab
„Verwaltung" benannte Bearer-Tokens (werden nur einmal angezeigt, nur der
Hash wird gespeichert, einzeln widerrufbar, Nutzung im Audit-Log):

```bash
curl -H "Authorization: Bearer kapa_..." \
  "https://host/capa/api/v1/reservations?status=genehmigt&format=csv"
```

Endpunkte: `/api/v1/reservations` (Filter: `cluster`, `status`, `abteilung`;
`format=csv`), `/api/v1/data` (Cluster-Kapazitäten), `/api/v1/status`.
Details und Beispiele: [`config/API.md`](config/API.md).

## Rollenkonzept und AD-Anmeldung

Mit `--ad-url` verlangt der Serve-Modus eine Anmeldung mit dem
Active-Directory-Konto (LDAP Simple Bind, nur Standardbibliothek):

```bash
python3 aria_kapa.py --url https://aria-ops.firma.de --user svc-aria --serve \
  --ad-url ldaps://dc01.firma.local --ad-domain firma.local \
  --admin-user vorname.nachname@firma.local
```

| Rolle | Rechte |
|---|---|
| **Anforderer** | Kapazitätsanfragen stellen; eigene, noch offene Anträge zurückziehen; sieht nur Anfragen der **eigenen Abteilung**, nicht den entscheidenden Admin |
| **Administrator** | Anträge genehmigen/ablehnen (mit Kommentar), Daten aus Aria aktualisieren, alle Reservierungen verwalten, Import, Rollen und Abteilungen pflegen (Tab „Verwaltung"); sieht alles |
| **Technische Prüfung** | Alle Daten und Seiten einsehen — keinerlei Änderungen möglich |

- **Rollen zuweisen**: Tab „Verwaltung" (`/verwaltung`) — AD-Benutzernamen
  eintragen, Rolle wählen und (für Anforderer) die Abteilung angeben;
  gespeichert in `data/kapa_rollen.json`. Benutzer ohne zugewiesene Rolle können
  sich nicht anmelden.
- **Abteilungssicht**: Anforderer sehen nur Anfragen ihrer Abteilung.
  Fremde *genehmigte* Reservierungen bleiben anonymisiert als
  „(andere Abteilung)" sichtbar, damit die freie Kapazität stimmt;
  fremde offene/abgelehnte Anträge sind komplett ausgeblendet.
- **Bootstrap**: `--admin-user` (kommagetrennt) definiert Immer-Admins,
  damit der erste Admin die Verwaltung öffnen kann.
- Benutzernamen ohne `@` werden automatisch um `--ad-domain` ergänzt
  (`max` → `max@firma.local`).
- Alle Rechte werden **serverseitig** geprüft; die Oberfläche blendet
  nicht erlaubte Aktionen zusätzlich aus.
- `ldaps://` verwenden — bei `ldap://` gehen Passwörter unverschlüsselt
  über das Netz (`--ad-insecure` für Self-Signed-Zertifikate).
- Ohne `--ad-url` läuft alles wie bisher ohne Anmeldung (Vollzugriff).

## Konfigurationsdatei und SFTP-Backup

Statt vieler Parameter kann alles in einer INI-Datei stehen
(Vorlage: [`config/kapa.ini.example`](config/kapa.ini.example)):

```bash
python3 aria_kapa.py --config /etc/kapa/kapa.ini
```

Kommandozeilen-Argumente überschreiben Werte aus der Datei; unbekannte
Schlüssel werden mit Fehlermeldung abgewiesen.

**SFTP-Backup**: Mit `--backup-target backup@srv:/backup/kapa` werden die
Datendateien (Reservierungen, Rollen, Audit-Log, Cache) regelmäßig als
`tar.gz` per scp übertragen — Standard: **zweimal täglich**
(`--backup-interval 43200`). **Rotation**: Archive älter als 30 Tage werden
auf dem Ziel automatisch gelöscht (`--backup-keep-days`, per sftp, auch auf
sftp-only-Servern). Authentifizierung bevorzugt per SSH-Key (`--backup-key`);
ein Passwort (`--backup-password` bzw. `BACKUP_PASSWORD`) funktioniert nur
mit installiertem `sshpass`. Admins können ein Backup auch manuell auslösen:
`POST /api/backup`. Ergebnisse (auch Fehler) landen im Audit-Log.

**Restore**: Schritt-für-Schritt-Anleitung in
[`config/RESTORE.md`](config/RESTORE.md).

## Optionen

| Option | Beschreibung |
|---|---|
| `--config kapa.ini` | Alle Optionen aus INI-Datei laden |
| `--cpu-factor 6` | CPU-Überprovisionierungsfaktor |
| `--failover-hosts 1` | Ausfall-Hosts pro Cluster (N+1), `0` = aus |
| `--auth-source local` | Auth-Quelle (z. B. AD-Quelle) |
| `--insecure` | TLS-Zertifikat nicht prüfen (Self-Signed) |
| `--serve --port 8080` | Webserver-Modus |
| `--bind 0.0.0.0` | Bind-Adresse für `--serve` |
| `--refresh-interval 1800` | Auto-Aktualisierung in Sekunden (`0` = aus) |
| `--cache data/kapa_cache.json` | Datei-Cache der letzten Abfrage |
| `--res-file data/kapa_reservierungen.json` | Reservierungsdatei (Serve-Modus) |
| `--res-ttl-days 31` | Reservierungen nach N Tagen löschen (`0` = nie) |
| `--ad-url ldaps://dc01…` | AD-Anmeldung aktivieren |
| `--ad-domain firma.local` | Domäne für Benutzernamen ohne `@` |
| `--ad-insecure` | LDAPS-Zertifikat nicht prüfen |
| `--admin-user a@…,b@…` | Immer-Admins (Bootstrap) |
| `--roles-file data/kapa_rollen.json` | Rollendatei |
| `--smtp-server mail.firma.local:25` | Mailserver für Reports |
| `--smtp-from`, `--smtp-to` | Absender / Report-Empfänger (kommagetrennt) |
| `--smtp-user`, `--smtp-password`, `--smtp-tls` | SMTP-Anmeldung / STARTTLS |
| `--backup-target user@srv:/pfad` | SFTP/SCP-Backupziel |
| `--backup-key`, `--backup-password` | SSH-Key (empfohlen) bzw. Passwort (braucht sshpass) |
| `--backup-port 22`, `--backup-interval 43200` | SSH-Port / Backup-Intervall in s (2×/Tag) |
| `--backup-keep-days 30` | Rotation: ältere Archive auf dem Ziel löschen |
| `--password-file datei` | Aria-Passwort aus Datei (systemd LoadCredential) |
| `--log-file data/kapa_log.jsonl` | Audit-Log-Datei |
| `--tokens-file data/kapa_tokens.json` | API-Token-Datei |
| `--output datei.html` | Ausgabedatei (statischer Modus) |
| `--json datei.json` | Rohdaten zusätzlich als JSON |

Alle JSON-Datendateien (Cache, Reservierungen, Rollen, `--json`-Export)
liegen im Ordner `data/`, der komplett per `.gitignore` vom Repository
ausgeschlossen ist. Auch per Parameter angegebene Dateinamen ohne
Pfadangabe landen automatisch unter `data/`; explizite Pfade
(z. B. `/var/lib/kapa/cache.json`) werden respektiert.

## Betrieb auf einem Linux-Host (systemd + nginx)

Fertige Vorlagen liegen unter [`config/`](config/):

- **`config/kapa-dashboard.service`** — systemd-Unit: läuft als eigener
  Benutzer `kapa` unter `/opt/kapa`, bindet nur an `127.0.0.1:8080`,
  Neustart bei Fehlern, gehärtete Sandbox. Installationsschritte stehen
  als Kommentar in der Datei.
- **`config/kapa.env.example`** — Vorlage für `/etc/kapa/kapa.env`
  (Mode 640): Aria-URL/-Benutzer, AD, SMTP. **Das Aria-Passwort liegt als
  eigene Datei** `/etc/kapa/aria.pass` (root, Mode 600) und wird per
  systemd `LoadCredential` + `--password-file` an den Dienst gereicht —
  es taucht damit weder in `ps aux` noch in `systemctl show` auf.
  Alternativ gehen Umgebungsvariablen (`ARIA_PASSWORD`, `SMTP_PASSWORD`,
  `BACKUP_PASSWORD`) oder `--smtp-password-file`/`--backup-password-file`.
  Empfehlung: eigenes Nur-Lese-Servicekonto in Aria Operations verwenden,
  das Skript liest ausschließlich.
- **`config/nginx-kapa.conf`** — Snippet für den bestehenden 443er-Server:
  stellt das Dashboard unter `https://<host>/capa/` bereit (Redirect
  `/capa` → `/capa/`, Prefix-Stripping, Cookie-Pfad). Die Weboberfläche
  nutzt relative API-Pfade und funktioniert daher unverändert unter dem
  Unterpfad. Einbinden per `include`, dann `nginx -t && systemctl reload nginx`.

Ohne `--ad-url` hat der eingebaute Webserver keine Authentifizierung — dann
nur im vertrauenswürdigen Verwaltungsnetz betreiben. TLS übernimmt der
Reverse-Proxy; das Dashboard selbst spricht nur HTTP auf localhost.

Die laufende Version wird im Footer der Weboberfläche und per
`aria_kapa.py --version` angezeigt.
