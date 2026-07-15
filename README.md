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
- **Sortierbare Tabellen**: Klick auf eine Spaltenüberschrift sortiert auf-/
  absteigend (numerisch, nach Datum oder Text) – in allen Datentabellen
  (Kapazität, Reservierungen, Genehmigungen, Log, Benutzer/Rollen, Tokens). Die
  Genehmigungs-Teams behalten ihre manuelle Prüfreihenfolge.
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
- **Storage-Kapazität** = Summe der Kapazität aller an den Cluster-Hosts
  angedockten Datastores (vSAN **und** externe FC-LUNs). Die Zuordnung läuft
  über die Host-Beziehungen in Aria (Datastore → angedockte Hosts → Cluster);
  jeder Datastore zählt **je Cluster genau einmal**, auch wenn ihn alle Hosts
  sehen (kein Doppeln geteilter LUNs). Wird keine Kapazität geliefert, zeigt die
  Spalte „–". Der Abruf protokolliert im Log, wie viele Datastores zugeordnet
  wurden und die Summe je Cluster – hilfreich zur Kontrolle.
- **Ausfallreserve (N+1)**: pro Cluster wird der größte Host (Cores und RAM)
  von der Gesamtkapazität abgezogen (`--failover-hosts`, Standard: 1, `0` = aus);
  Storage bleibt davon unberührt.
- **Belegt** = provisionierte vCPUs / RAM aller VMs bzw. belegter Datastore-Platz (inkl. powered-off)
- **Frei** = Kapazität − belegt − genehmigte Reservierungen (für vCPU, RAM und Storage)

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

- **Ressourcen**: Je Anfrage werden **vCPU**, **RAM (GB)** und **Storage (GB)**
  als **Ganzzahlen** erfasst (keine Kommazahlen). vCPU und RAM zählen gegen die
  berechnete Cluster-Kapazität; die Storage-Größe wird zur Anfrage geführt und
  überall mit angezeigt.
- **Gültigkeit**: Reservierungen gelten automatisch ab dem Anlagetag für
  30 Tage; das „gültig bis"-Datum wird in jeder Reservierung angezeigt.
- **Mehrstufiger Genehmigungsprozess**: Sind Teams konfiguriert, durchläuft
  jeder Antrag sie **nacheinander** in der festgelegten Reihenfolge. Der Status
  wandert von „beantragt" → „in Prüfung" (sobald das erste Team freigegeben hat)
  → „genehmigt" (erst wenn **alle** Teams freigegeben haben). Erst dann zählt
  der Antrag gegen die Kapazität. Beim Status **„in Prüfung"** zeigt ein
  Mouseover, welche Teams (mit Person und Datum) bereits freigegeben haben und
  welches Team als Nächstes dran ist. Ein Team kann erst freigeben, wenn es an
  der Reihe ist; jedes Team kann in seiner Stufe auch ablehnen. Ohne Teams
  bleibt es einstufig (Admin genehmigt direkt).
  - **Teams pflegen**: im Tab „Verwaltung" (Abschnitt „Genehmigungs-Teams")
    – hinzufügen, per ↑/↓ in die richtige Prüfreihenfolge bringen, **umbenennen**
    (✎, die Reihenfolge bleibt erhalten und zugewiesene Reviewer werden
    automatisch übernommen) und entfernen. Gespeichert in `data/kapa_teams.json`.
    Der Parameter `--approval-teams` dient nur noch zur **Erstbefüllung**, falls
    diese Datei noch nicht existiert.
  - **Reviewer einem Team zuordnen**: Bei der Rollenzuweisung (Abschnitt
    „Benutzer und Rollen") wird für die Rolle *Reviewer* das Team über eine
    **Auswahlliste** der vorhandenen Teams gesetzt. Nur so zugeordnete Benutzer
    dürfen in der jeweiligen Stufe freigeben (serverseitig erzwungen).
- **Genehmigungsübersicht** (Tab „Genehmigungen"): zeigt je Antrag die freie
  Kapazität des Ziel-Clusters (⚠ wenn er nicht mehr hineinpasst), den
  Fortschritt und – für das gerade zuständige Team bzw. Admins – die
  Freigabe-/Ablehnen-Schaltflächen.
- **Ablehnungen** bleiben 31 Tage (ab Ablehnung) als Historie sichtbar
  (Status „abgelehnt"; im Mouseover steht, in welcher Stufe abgelehnt wurde).
- **Storno**: Anfragen lassen sich nicht löschen, sondern **stornieren**. Das
  darf ein Admin, der Anforderer selbst oder **jemand aus derselben Abteilung**
  (Button „⦸ Storno" in der Reservierungsliste). Eine stornierte Anfrage bekommt
  den Status „storniert", bleibt als Historie erhalten und zählt nicht mehr
  gegen die Kapazität.
- **Kommentar**: Beim Freigeben/Ablehnen/Stornieren kann ein Kommentar
  (z. B. Begründung, **max. 64 Zeichen**) über einen schlanken Dialog erfasst
  werden; er erscheint in der Reservierungsübersicht und in der Report-Mail.
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
| **Anforderer** | Kapazitätsanfragen stellen; eigene, noch offene Anträge zurückziehen; sieht nur Anfragen der **eigenen Abteilung**, nicht wer entschieden hat |
| **Reviewer** | Mitglied eines Genehmigungsteams; gibt Anträge frei bzw. lehnt sie ab, **wenn das eigene Team an der Reihe ist** (Tab „Genehmigungen"); sieht alle Anträge, aber keine Verwaltung/Log |
| **Administrator** | Anträge in jeder Stufe genehmigen/ablehnen (mit Kommentar), Daten aus Aria aktualisieren, alle Reservierungen verwalten, Import, Rollen/Teams pflegen (Tab „Verwaltung"); sieht alles |
| **Technische Prüfung** | Alle Daten und Seiten einsehen — keinerlei Änderungen möglich |

- **Rollen zuweisen**: Tab „Verwaltung" (`/verwaltung`) — AD-Benutzernamen
  eintragen, Rolle wählen und im Feld „Abteilung / Team" bei **Anforderern** die
  Abteilung, bei **Reviewern** das Team (eines der im selben Tab gepflegten
  Genehmigungs-Teams, per Auswahlliste) angeben; gespeichert in
  `data/kapa_rollen.json`. Bestehende Zuweisungen lassen sich per Klick
  bearbeiten (Rolle und Team/Abteilung) oder entfernen.
- **Standardrolle**: Jeder erfolgreich am AD angemeldete Benutzer **ohne**
  explizite Zuweisung gilt automatisch als **Anforderer** — er kann Anfragen
  stellen, aber nichts freigeben. Reviewer-, Admin- und Auditor-Rechte gibt es
  nur über eine ausdrückliche Zuweisung.
- **Rollen-Bezeichnungen umbenennen**: Die angezeigten Namen der vier Rollen
  sind im Tab „Verwaltung" (Abschnitt „Rollen-Bezeichnungen") **frei wählbar**
  (z. B. „Anforderer" → „Antragsteller"), gespeichert in
  `data/kapa_rollennamen.json`. Die internen Rollen-Schlüssel und damit die
  **Rechte bleiben unverändert** — nur die Anzeige ändert sich.
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

### Härtung

- **Session-Cookie** mit `HttpOnly`, `SameSite=Lax` und `Secure`. Da das
  Dashboard hinter dem HTTPS-nginx läuft, ist `Secure` Standard; nur für
  einen lokalen HTTP-Test ohne Proxy lässt es sich mit `--cookie-insecure`
  abschalten.
- **Sicherheits-Header** auf jeder Antwort: `Content-Security-Policy`,
  `X-Frame-Options: DENY` (kein Clickjacking), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: same-origin`.
- **Ausgabe-Escaping**: aus Aria stammende Namen (Cluster, Hosts, VMs) werden
  script-tag-sicher eingebettet, sodass sie kein JavaScript einschleusen können.
- **Login-Bremse**: nach 5 Fehlversuchen je Benutzer/IP wird die Anmeldung für
  einige Minuten mit `429` gesperrt (Schutz vor Password-Spraying). Eine
  einheitliche Fehlermeldung verrät nicht, welche Konten berechtigt sind.
  AD-Ausfälle zählen dabei bewusst nicht als Fehlversuch.
- **Request-Größe** ist begrenzt (2 MiB), damit ein großer Body den Dienst
  nicht überlasten kann.

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
| `--data-dir /var/lib/kapa` | Basisordner aller Laufzeitdaten (Standard `data/`); bei CI/CD außerhalb des Deploy-Verzeichnisses wählen |
| `--cache kapa_cache.json` | Datei-Cache der letzten Abfrage |
| `--res-file data/kapa_reservierungen.json` | Reservierungsdatei (Serve-Modus) |
| `--res-ttl-days 31` | Reservierungen nach N Tagen löschen (`0` = nie) |
| `--approval-teams "A,B,C"` | **Erstbefüllung** der Genehmigungs-Teams (nur wenn `--teams-file` noch fehlt); danach Pflege im Tab „Verwaltung" |
| `--teams-file data/kapa_teams.json` | Datei mit den Genehmigungs-Teams (Pflege über die Verwaltungsseite) |
| `--rolenames-file data/kapa_rollennamen.json` | Datei mit den frei wählbaren Rollen-Bezeichnungen (Pflege über die Verwaltungsseite) |
| `--ad-url ldaps://dc01…` | AD-Anmeldung aktivieren |
| `--ad-domain firma.local` | Domäne für Benutzernamen ohne `@` |
| `--ad-insecure` | LDAPS-Zertifikat nicht prüfen |
| `--cookie-insecure` | Session-Cookie ohne `Secure` (nur lokaler HTTP-Test) |
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

Alle JSON-Datendateien (Cache, Reservierungen, Rollen, Teams, Log, Tokens,
`--json`-Export) liegen standardmäßig im Ordner `data/`, der komplett per
`.gitignore` vom Repository ausgeschlossen ist. Der Basisordner ist über
`--data-dir` frei wählbar; explizite Pfade (z. B. `--cache /pfad/cache.json`)
werden respektiert.

> **Wichtig bei CI/CD (GitLab-Pipeline o. Ä.):** Legt die Laufzeitdaten mit
> `--data-dir` **außerhalb** des Deploy-Verzeichnisses ab (z. B.
> `/var/lib/kapa`). `data/` ist gitignored, also im Repository/Artefakt nicht
> enthalten. Deployt die Pipeline den Code über das Zielverzeichnis (per
> `git clean -fdx`, `rsync --delete` oder „Verzeichnis leeren und neu
> befüllen"), löscht sie damit den mitliegenden `data/`-Ordner bei **jedem**
> Deploy. Liegen die Daten unter `/var/lib/kapa`, bleiben sie unberührt. Die
> mitgelieferte systemd-Unit ist bereits so konfiguriert.

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

### Auslieferung: RPM, Ansible/AAP, Container

Neben der manuellen Installation aus `config/` gibt es fertige
Deployment-Varianten unter [`deploy/`](deploy/) – dasselbe Skript, drei
Verpackungen:

- **[`deploy/rpm/`](deploy/rpm/)** — natives RPM für RHEL/Alma/Rocky 9
  (`dnf install`/`upgrade`, Dienst-Benutzer, systemd-Unit, Konfiguration unter
  `/etc/kapa` mit `noreplace`). `deploy/rpm/build.sh` baut das Paket, die
  Version kommt automatisch aus `aria_kapa.py`.
- **[`deploy/ansible/`](deploy/ansible/)** — Role + Playbook für den Rollout
  über eine Flotte bzw. die Ansible Automation Platform; installiert das RPM,
  pflegt die Konfiguration aus dem Vault und setzt den SELinux-Schalter
  `httpd_can_network_connect`.
- **[`deploy/docker/`](deploy/docker/)** — Container-Image auf Basis von Red Hat
  UBI 9 (läuft als nicht-root, auch mit Podman) samt `docker-compose.yml`.

Details und die Auswahlhilfe stehen in [`deploy/README.md`](deploy/README.md).
