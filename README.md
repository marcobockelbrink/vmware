# VMware Kapazitätsplanung (Aria Operations)

Kapazitätsauswertung pro Cluster aus VMware Aria Operations mit browserbasiertem
Dashboard und Reservierungsfunktion für künftige Kapazitätsanfragen.

![Dashboard mit Demo-Daten](docs/screenshot.png)

*Kapazitätsübersicht mit Demo-Daten: freie vCPU-, RAM- und Storage-Kapazität je
Cluster mit Auslastungsbalken (`python3 aria_kapa.py --sample --serve`).*

## Dashboard

- **Kompakte Tabellenansicht**: pro Cluster die freien **vCPU-, RAM- und
  Storage**-Kapazitäten (nach Abzug genehmigter Reservierungen) mit
  Auslastungsbalken. Die Erläuterungen zur Berechnung stehen hinter den Knöpfen
  „ℹ Info Kapa-Berechnung" und „? Hilfe".
- **Detailkarte in Reitern**: Klick auf den Clusternamen öffnet die Details,
  aufgeteilt in **CPU & RAM** (Auslastung, Kennzahlen, Reservierungen inkl.
  Antrags-Formular und darunter die **vSphere-Tags** des Clusters), **Storage**
  (Auslastung und jede LUN, sortierbar nach Größe/Belegung), **Netzwerk** (die
  Portgruppen des Clusters mit ihren VLAN-Nummern), **Hosts** und
  **VMs**. Ein Klick auf den Storage-Wert springt direkt in den Storage-Reiter.
  Die Karte ist breit angelegt und lässt sich unten rechts frei in der Größe
  ziehen.
- **VLAN-Suche** (Tab „VLAN-Suche" bzw. `/vlan-suche`, zwischen Kapazität und
  Reservierungen): durchsucht die Portgruppen **aller** Cluster. Weil die
  Portgruppen-Namen die IP-Netze enthalten, findet man über eine Teil-Eingabe
  (z. B. `10.2.30` oder `VLAN205`) sofort, **an welchem Cluster** ein Netz hängt.
  Ergebnis als sortierbare Tabelle Portgruppe / VLAN / Cluster.
- **Filterfeld** für Cluster bzw. Reservierungen (findet auch Change-Nummer,
  Anforderer, Team, Status und ID)
- **Cluster-Selektor**: Über der Kapazitätsliste blenden sich bis zu drei
  **kaskadierende** Auswahllisten ein, mit denen man sich anhand der
  vSphere-Tags zu den Clustern durchfiltert (z. B. Umgebung → Standort →
  Betreuung). Stufe 2 zeigt nur Werte, die zur Wahl in Stufe 1 passen. Welche
  Tag-Kategorien die Stufen bilden, konfigurierst du frei im Tab „Verwaltung"
  (Abschnitt „Cluster-Selektor"); pro Stufe lässt sich ein eigener
  **Anzeigename** vergeben (z. B. Kategorie „Standort" → Beschriftung
  „Rechenzentrum"). Gespeichert wird per Knopf „✓ Selektor speichern". Die
  Werte kommen live aus den Tags.
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
  Stornos, Importe, Rollenänderungen und Backups nach `data/kapa_log.jsonl`.
  Die Datei **rotiert automatisch** ab 10 MB (`.1` … `.3`), und die Ansicht
  liest nur das Dateiende – das Log kann also nicht unbegrenzt wachsen oder
  den Aufruf ausbremsen.
- **Export**: Reservierungen als **CSV** (Semikolon, direkt Excel-tauglich)
  oder als JSON über die Knöpfe in der Kopfleiste.
- **Auto-Aktualisierung** im Serve-Modus (Standard: alle 30 Minuten, sichtbarer
  Countdown) plus Knopf „⟳ Jetzt aktualisieren"

## Screenshots

Alle Aufnahmen mit Demo-Daten (`python3 aria_kapa.py --sample --serve`).

**Reservierungen** — alle Anfragen mit ID, Change-Nummer, Team, vCPU/RAM/Storage
und Status: `beantragt`, `in Prüfung (n/3)`, `genehmigt`, `abgelehnt` und
`storniert`. Jede Spalte ist per Klick sortierbar, „⦸ Storno" zieht eine Anfrage
zurück:

![Reservierungen](docs/screenshot-reservierungen.png)

**Genehmigungen** — offene Anträge mit der freien Kapazität des Ziel-Clusters
(⚠ markiert Anträge, die nicht mehr hineinpassen), dem Fortschritt der
mehrstufigen Freigabe und der Schaltfläche für das jeweils zuständige Team:

![Genehmigungen](docs/screenshot-genehmigungen.png)

**Verwaltung** (nur Admins) — Benutzer **und AD-Gruppen** mit Rolle und Team,
frei wählbare Rollen-Bezeichnungen, die Genehmigungs-Teams in ihrer
Prüfreihenfolge sowie API-Tokens für externe Anwendungen:

![Verwaltung](docs/screenshot-verwaltung.png)

**Log** (nur Admins) — Audit-Log mit Anmeldungen, Anträgen, Freigaben,
Ablehnungen, Stornos und Backups:

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
  Spalte „–". Der Abruf protokolliert im Log die zugeordneten Datastores, die
  Summe je Cluster und die erkannten Storage-Typen – hilfreich zur Kontrolle.
- **vSAN wird als nutzbare Kapazität gerechnet**: Weil vSAN spiegelt (RAID-1),
  zählt die Bruttokapazität nur anteilig. Der Faktor ist über `--vsan-factor`
  einstellbar (Standard `0.5`; `1` = brutto). Er wirkt auf **Kapazität und
  Belegung**, damit die Auslastung stimmt — vROps meldet beide Werte brutto.
  Die LUN-Liste zeigt den **Storage-Typ** (vSAN/VMFS/NFS) und bei vSAN die
  Bruttokapazität im Tooltip. Der Typ kommt aus den Datastore-Eigenschaften;
  wird keiner geliefert, greift die Erkennung über den Datastore-Namen.
  - **LUN-Detail**: Ein Klick auf den Storage-Wert (oder auf den Clusternamen)
    öffnet die Detailkarte mit **jedem einzelnen Datastore/LUN** – wahlweise
    sortiert nach **Größe** oder nach **Belegung**, mit Größe, belegtem Platz,
    Belegung in % und freiem Platz.
- **Ausfallreserve (N+1)**: pro Cluster wird der größte Host (Cores und RAM)
  von der Gesamtkapazität abgezogen (`--failover-hosts`, Standard: 1, `0` = aus);
  Storage bleibt davon unberührt.
- **Belegt** = provisionierte vCPUs / RAM aller VMs bzw. belegter Datastore-Platz (inkl. powered-off)
- **Frei** = Kapazität − belegt − genehmigte Reservierungen (für vCPU, RAM und Storage)
- **Ausschluss per Tag**: Mit `--exclude-tag Kapa_Filter:Ja` werden VMs mit dem
  angegebenen vROps-Tag (Kategorie:Wert) aus der Belegung herausgerechnet.
- **vSphere-Tags**: Die Tags des Clusters kommen aus den **Eigenschaften** der
  Ressource (`/resources/{id}/properties`) und werden in der Detailkarte
  (Reiter „CPU & RAM") als Chips angezeigt. Ohne weitere Angabe werden alle
  Eigenschaften übernommen, deren Schlüssel `tag` enthält; mit `--tag-property`
  lässt sich das auf ein Präfix eingrenzen (z. B. `summary|tag`).
  Enthält eine Eigenschaft **JSON** (z. B. `TagJson`), wird es aufgeschlüsselt
  und nur die Tags werden gelistet — rohes JSON erscheint nie in der Anzeige.
  Das Log nennt nach jedem Abruf die erkannten Schlüssel und einen Auszug des
  Rohwerts — praktisch zum Feinjustieren.
- **dvSwitches / Portgruppen**: Aria liefert die verteilten Switches
  (`VmwareDistributedVirtualSwitch`) und Portgruppen
  (`DistributedVirtualPortgroup`) als eigene Ressourcen. Die Zuordnung zum
  Cluster läuft — wie beim Storage — über die angedockten Hosts
  (dvSwitch → HostSystem → `summary|parentCluster`); die VLAN-Nummer wird best
  effort aus den Portgruppen-Eigenschaften gelesen. Schlägt der Abruf fehl,
  bleibt der Netzwerk-Reiter leer und der Rest läuft weiter. Das Log meldet
  `dvSwitches: N, Portgruppen: M · zugeordnet: …` — dort nach dem nächsten
  Abruf gegenprüfen.
- Die Erläuterungen zur Berechnung und die Hilfe stehen im Dashboard hinter den
  Buttons **„ℹ Info Kapa-Berechnung"** und **„? Hilfe"** (aufgeräumte Kopfzeile).

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

- **Change / Jira-Ticket (optional)**: Jede Anfrage kann eine Change-Nummer oder
  ein Jira-Ticket tragen – frei wählbar, ohne festes Format und **kein
  Pflichtfeld**. Der Wert erscheint in den Übersichten und in der Report-Mail;
  fehlt er, steht dort „–".

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
- **Mail-Benachrichtigungen** (SMTP-Server über `--smtp-server` vorausgesetzt):
  In der **Verwaltung** legst du je interner Rolle fest, bei welchem Ereignis
  eine Mail rausgeht — **Anlage**, **Ablehnung**, **Freigabe** (endgültige
  Genehmigung) und **„Team ist dran"** (ein Team ist im Freigabe-Workflow an der
  Reihe). Empfänger:
  - **Anforderer** → der jeweilige Antragsteller (automatisch),
  - **Admin/Auditor** → je eine frei eingetragene Verteiler-Adresse
    (Admin fällt auf `--smtp-to` zurück, falls das Feld leer bleibt),
  - **„Team ist dran"** → die pro Genehmigungs-Team hinterlegte Adresse.

  Die Mail enthält die Reservierungsdaten; der Versand läuft asynchron und
  best-effort (Fehler nur im Log). Beim **Anlegen** eines Antrags wird zusätzlich
  automatisch das erste Team benachrichtigt, nach jeder Freigabe das nächste.
- **Serve-Modus**: Reservierungen liegen zentral auf dem Server in
  `data/kapa_reservierungen.json` — alle Nutzer sehen denselben Stand.
- **Statisches HTML**: Speicherung lokal im Browser (localStorage).
- **Automatischer Ablauf**: Reservierungen werden `--res-ttl-days` Tage nach
  Anlage automatisch gelöscht (Standard: 31, `0` = nie löschen); die angezeigte
  Gültigkeit endet einen Tag davor (30 Tage).

## Datenspeicher

Alle Schreibvorgänge erfolgen **atomar** (erst in eine Temp-Datei, dann
umbenennen), sodass ein Absturz mitten im Speichern keine halb geschriebene,
beschädigte Datei hinterlassen kann. Über `--storage` (bzw. `storage =` in der
INI) wird die Ablageform gewählt:

- **`json`** (Standard): Je Sammlung eine gut lesbare, notfalls von Hand
  editierbare `.json`-Datei im `--data-dir` (`kapa_reservierungen.json`,
  `kapa_rollen.json`, `kapa_teams.json` …). Für den üblichen Betrieb völlig
  ausreichend.
- **`sqlite`**: Eine einzelne `data/kapa.db` (SQLite steckt in der
  Python-Standardbibliothek — **kein** zusätzliches Modul, kein Server, kein
  Port). Reservierungen werden **inkrementell** geschrieben (nur die geänderte
  Zeile statt der ganzen Liste); die kleinen Sammlungen liegen als
  Schlüssel-Wert-Einträge in derselben Datei. Sinnvoll erst bei sehr vielen
  (mehrere Tausend) aktiven Reservierungen.

Beim **erstmaligen Umstellen** auf `sqlite` übernimmt das Dashboard vorhandene
JSON-Daten **einmalig automatisch** in die neue `kapa.db` (Rollen, Teams,
Selektor, Rollennamen, Tokens und alle Reservierungen). Die JSON-Dateien bleiben
als Sicherung liegen — ein Rückwechsel auf `json` ist damit jederzeit möglich.
Das Audit-Log (`kapa_log.jsonl`) und der Aria-Cache bleiben in beiden Modi
eigene Dateien.

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
  das **Team** (eines der im selben Tab gepflegten Genehmigungs-Teams, per
  Auswahlliste) angeben – für **Anforderer und Reviewer** gleichermaßen; Admin
  und Auditor brauchen kein Team. Gespeichert in `data/kapa_rollen.json`.
  Bestehende Zuweisungen lassen sich per Klick bearbeiten oder entfernen.
- **Team-Sicht (nur Anforderer)**: Ein **Anforderer** sieht in der
  Reservierungsliste nur die Anfragen des **eigenen Teams** (fremde genehmigte
  bleiben anonymisiert als „(anderes Team)" enthalten, damit die freie
  Kapazität stimmt). **Reviewer, Admin und Auditor sehen alle** Anfragen – der
  mehrstufige Genehmigungsprozess bleibt dadurch unberührt.
- **Standardrolle**: Jeder erfolgreich am AD angemeldete Benutzer **ohne**
  explizite Zuweisung gilt automatisch als **Anforderer** — er kann Anfragen
  stellen, aber nichts freigeben. Reviewer-, Admin- und Auditor-Rechte gibt es
  nur über eine ausdrückliche Zuweisung.
- **AD-Gruppen berechtigen**: In der Verwaltung lässt sich (Typ „AD-Gruppe")
  auch einer ganzen **AD-Gruppe** eine Rolle (und ein Team) zuweisen — genau wie
  einem Benutzer. Jedes Mitglied der Gruppe erhält dann diese Rolle. Dafür ist
  ein **Service-Konto** nötig (`--ad-bind-dn`/`--ad-bind-password`/`--ad-base-dn`),
  mit dem das System nach der Anmeldung die AD-Gruppen (`memberOf`) des Benutzers
  sucht. Direkt zugewiesene Benutzerrollen haben Vorrang; bei mehreren Gruppen
  gewinnt die höchste Berechtigung.
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
mit installiertem `sshpass`. Admins können ein Backup jederzeit **manuell
auslösen** – im Tab „Verwaltung" (Abschnitt „Backup") per Knopf oder direkt über
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
| `--exclude-tag Kapa_Filter:Ja` | VMs mit diesem vROps-Tag (Kategorie:Wert) aus der Auswertung ausschließen |
| `--contact-info "…"` | Kontakt-/Impressumszeile (Footer + Login) für Rückfragen |
| `--ad-bind-dn`, `--ad-bind-password`, `--ad-base-dn` | Service-Konto für die AD-Gruppen-Berechtigung (memberOf-Suche) |
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
