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
  nach Genehmigung (Tab „Genehmigungen") gegen die freie Kapazität. Die
  Genehmigungsübersicht zeigt je Antrag die freie Kapazität des Ziel-Clusters
  (⚠ wenn der Antrag nicht mehr hineinpasst).
- **Ablehnungen** bleiben 31 Tage (ab Ablehnung) als Historie sichtbar
  (Status „abgelehnt"), inkl. wer abgelehnt hat; genehmigte Einträge zeigen
  den genehmigenden Admin (Tooltip auf dem Status).
- **Mail-Reports**: Mit `--smtp-server` verschickt das Dashboard bei jeder
  Genehmigung/Ablehnung eine Mail mit den Reservierungsdaten und dem
  ausführenden Admin an `--smtp-to` sowie automatisch an den Anforderer.
- **Serve-Modus**: Reservierungen liegen zentral auf dem Server in
  `kapa_reservierungen.json` — alle Nutzer sehen denselben Stand.
- **Statisches HTML**: Speicherung lokal im Browser (localStorage).
- **Automatischer Ablauf**: Reservierungen werden `--res-ttl-days` Tage nach
  Anlage automatisch gelöscht (Standard: 31, `0` = nie löschen); die angezeigte
  Gültigkeit endet einen Tag davor (30 Tage).

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
| **Anforderer** | Kapazitätsanfragen stellen; eigene, noch offene Anträge zurückziehen; sieht nur Anfragen der **eigenen Abteilung** |
| **Administrator** | Anträge genehmigen/ablehnen, alle Reservierungen verwalten, Import, Rollen und Abteilungen pflegen (Tab „Verwaltung"); sieht alles |
| **Technische Prüfung** | Alle Daten und Seiten einsehen — keinerlei Änderungen möglich |

- **Rollen zuweisen**: Tab „Verwaltung" (`/verwaltung`) — AD-Benutzernamen
  eintragen, Rolle wählen und (für Anforderer) die Abteilung angeben;
  gespeichert in `kapa_rollen.json`. Benutzer ohne zugewiesene Rolle können
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
| `--ad-url ldaps://dc01…` | AD-Anmeldung aktivieren |
| `--ad-domain firma.local` | Domäne für Benutzernamen ohne `@` |
| `--ad-insecure` | LDAPS-Zertifikat nicht prüfen |
| `--admin-user a@…,b@…` | Immer-Admins (Bootstrap) |
| `--roles-file kapa_rollen.json` | Rollendatei |
| `--smtp-server mail.firma.local:25` | Mailserver für Reports |
| `--smtp-from`, `--smtp-to` | Absender / Report-Empfänger (kommagetrennt) |
| `--smtp-user`, `--smtp-password`, `--smtp-tls` | SMTP-Anmeldung / STARTTLS |
| `--output datei.html` | Ausgabedatei (statischer Modus) |
| `--json datei.json` | Rohdaten zusätzlich als JSON |

Cache- und Reservierungsdatei sind lokale Laufzeitdaten und per `.gitignore`
vom Repository ausgeschlossen.

## Hinweis zum Betrieb

Ohne `--ad-url` hat der eingebaute Webserver keine Authentifizierung — dann
nur im vertrauenswürdigen Verwaltungsnetz betreiben. Der Server spricht
selbst kein HTTPS; für den Betrieb über `localhost` hinaus empfiehlt sich
`--bind 127.0.0.1` hinter einem Reverse-Proxy mit TLS (z. B. nginx), damit
Anmeldedaten und Session-Cookies verschlüsselt übertragen werden.
