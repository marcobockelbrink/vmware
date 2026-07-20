# RPM-Paket (RHEL/Alma/Rocky 9)

> 🇬🇧 [English version: README.en.md](README.en.md)

Native Installation als `dnf`-Paket – inklusive Dienst-Benutzer, systemd-Unit,
Konfigurationsvorlagen unter `/etc/kapa` und sauberer Upgrade-Pfad (die
Konfiguration wird bei Updates dank `%config(noreplace)` **nicht** überschrieben).

## Bauen

Auf einem RHEL/Alma/Rocky-9-Host (oder in einem UBI9-Container):

```bash
sudo dnf install -y rpm-build rpmdevtools systemd-rpm-macros
deploy/rpm/build.sh
```

Ergebnis: `~/rpmbuild/RPMS/noarch/kapa-dashboard-<version>-1.el9.noarch.rpm`.
Die Version wird automatisch aus `aria_kapa.py` (`VERSION = …`) übernommen.

## Installieren

```bash
sudo dnf install ./kapa-dashboard-0.9-1.el9.noarch.rpm
```

Das `%post`-Skript gibt die nächsten Schritte aus. Kurzfassung:

```bash
# 1) Konfiguration eintragen (die EINE Datei, nicht-geheim)
sudoedit /etc/kapa/kapa.ini            # Aria-URL, Benutzer, AD, SMTP, Backup …

# 2) Aria-Passwort als eigene .pass-Datei ablegen (Pfad steht in der INI)
echo 'DAS-ARIA-PASSWORT' | sudo tee /etc/kapa/aria.pass >/dev/null
sudo chown root:kapa /etc/kapa/aria.pass && sudo chmod 640 /etc/kapa/aria.pass

# 3) SELinux: nginx darf zum Dienst proxien
sudo setsebool -P httpd_can_network_connect 1

# 4) nginx-Snippet einbinden
#    /etc/kapa/nginx-kapa.conf.sample -> in den bestehenden 443-Server

# 5) Dienst starten
sudo systemctl enable --now kapa-dashboard
journalctl -u kapa-dashboard -f
```

## Aktualisieren

```bash
sudo dnf upgrade ./kapa-dashboard-<neu>-1.el9.noarch.rpm
```

Der Dienst wird automatisch neu gestartet (`%systemd_postun_with_restart`);
`/etc/kapa/*` und die Daten unter `/opt/kapa/data` bleiben erhalten.

## Verteilen an mehrere Hosts

- **Einfach**: das `.rpm` an den GitHub-Release hängen, dann je Host
  `sudo dnf install https://…/kapa-dashboard-0.9-1.el9.noarch.rpm`.
- **Sauber bei vielen Hosts**: internes `dnf`-Repo bereitstellen
  (`createrepo_c` auf einem Webserver), einmal als `.repo` eintragen und dann
  überall `sudo dnf install kapa-dashboard` bzw. `dnf upgrade`.
- **Automatisiert**: siehe [`../ansible/`](../ansible/) – die Role installiert
  genau dieses RPM und pflegt die Konfiguration aus dem Vault.

## Enthaltene Dateien

| Pfad | Inhalt |
|---|---|
| `/opt/kapa/aria_kapa.py` | Anwendung (root:root, 0755 – Dienst kann sie nicht ändern) |
| `/opt/kapa/data/` | Laufzeitdaten (kapa:kapa, 0750) |
| `/usr/lib/systemd/system/kapa-dashboard.service` | systemd-Unit |
| `/etc/kapa/kapa.ini` | Konfiguration (noreplace) |
| `/etc/kapa/nginx-kapa.conf.sample` | nginx-Snippet zum Einbinden |
| `/usr/share/doc/kapa-dashboard/` | README, API.md, RESTORE.md |
