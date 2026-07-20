# Deployment-Varianten

> 🇬🇧 [English version: README.en.md](README.en.md)

Die Anwendung ist ein einzelnes Python-Skript (`aria_kapa.py`, nur
Standardbibliothek, Python 3.8+). Dasselbe Artefakt lässt sich auf mehrere
Arten ausliefern – wähle nach Umgebung:

| Variante | Wann | Verzeichnis |
|---|---|---|
| **RPM** | RHEL/Alma/Rocky 9, ein oder mehrere Hosts, native Paketverwaltung mit `dnf`-Upgrades | [`rpm/`](rpm/) |
| **Ansible / AAP** | Ausrollen und Konfigurieren über eine ganze Flotte, Secrets aus dem Vault | [`ansible/`](ansible/) |
| **Docker / Podman** | Test-/Dev-Umgebungen oder Hosts mit Container-Runtime (UBI9-Image) | [`docker/`](docker/) |

Die drei Wege ergänzen sich: Das **RPM** macht die Host-lokale Installation
(Dateien, Benutzer, systemd-Unit), **Ansible/AAP** orchestriert das RPM über
viele Hosts und verwaltet die Konfiguration, das **Container-Image** ist für
Umgebungen mit Docker/Podman gedacht.

## Gemeinsame Grundlagen

- **Konfiguration**: eine INI-Datei `config/kapa.ini.example` (alle
  nicht-geheimen Optionen). Secrets (Aria-/SMTP-/
  Backup-Passwort) kommen per Datei (`--password-file`), Umgebungsvariable
  (`ARIA_PASSWORD` …) oder – im Container – als gemountetes Secret.
- **Daten** liegen unter `data/` (bzw. `/opt/kapa/data`) und müssen bei
  Updates erhalten bleiben. Restore-Anleitung: `config/RESTORE.md`.
- **Reverse Proxy**: Das Dashboard lauscht lokal auf `127.0.0.1:8080` und wird
  per nginx unter `/capa` veröffentlicht (`config/nginx-kapa.conf`).

## RHEL-9-Hinweis (SELinux)

Läuft nginx als Reverse Proxy vor dem Dienst, muss SELinux ausgehende
Verbindungen von nginx erlauben – sonst gibt es `502`:

```bash
sudo setsebool -P httpd_can_network_connect 1
```

Die Ansible-Role setzt das automatisch.
