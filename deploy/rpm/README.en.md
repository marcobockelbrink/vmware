# RPM package (RHEL/Alma/Rocky 9)

> 🇩🇪 [Deutsche Fassung: README.md](README.md)

Native installation as a `dnf` package — including the service user, systemd
unit, configuration templates under `/etc/kapa` and a clean upgrade path (the
configuration is **not** overwritten on updates thanks to
`%config(noreplace)`).

## Building

On a RHEL/Alma/Rocky 9 host (or inside a UBI9 container):

```bash
sudo dnf install -y rpm-build rpmdevtools systemd-rpm-macros
deploy/rpm/build.sh
```

Result: `~/rpmbuild/RPMS/noarch/kapa-dashboard-<version>-1.el9.noarch.rpm`.
The version is taken automatically from `aria_kapa.py` (`VERSION = …`).

## Installing

```bash
sudo dnf install ./kapa-dashboard-2.3-1.el9.noarch.rpm
```

The `%post` script prints the next steps. In short:

```bash
# 1) Fill in the configuration (the ONE file, non-secret)
sudoedit /etc/kapa/kapa.ini            # Aria URL, user, AD, SMTP, backup …

# 2) Store the Aria password as its own .pass file (path is set in the INI)
echo 'THE-ARIA-PASSWORD' | sudo tee /etc/kapa/aria.pass >/dev/null
sudo chown root:kapa /etc/kapa/aria.pass && sudo chmod 640 /etc/kapa/aria.pass

# 3) SELinux: allow nginx to proxy to the service
sudo setsebool -P httpd_can_network_connect 1

# 4) Include the nginx snippet
#    /etc/kapa/nginx-kapa.conf.sample -> into the existing 443 server

# 5) Start the service
sudo systemctl enable --now kapa-dashboard
journalctl -u kapa-dashboard -f
```

## Updating

```bash
sudo dnf upgrade ./kapa-dashboard-<new>-1.el9.noarch.rpm
```

The service is restarted automatically (`%systemd_postun_with_restart`);
`/etc/kapa/*` and the data under `/opt/kapa/data` are preserved.

## Distributing to multiple hosts

- **Simple**: attach the `.rpm` to the GitHub release, then per host
  `sudo dnf install https://…/kapa-dashboard-2.3-1.el9.noarch.rpm`.
- **Clean with many hosts**: provide an internal `dnf` repo
  (`createrepo_c` on a web server), register it once as a `.repo` and then
  `sudo dnf install kapa-dashboard` / `dnf upgrade` everywhere.
- **Automated**: see [`../ansible/`](../ansible/) — the role installs exactly
  this RPM and manages the configuration from the vault.

## Included files

| Path | Contents |
|---|---|
| `/opt/kapa/aria_kapa.py` | application (root:root, 0755 — the service cannot modify it) |
| `/opt/kapa/data/` | runtime data (kapa:kapa, 0750) |
| `/usr/lib/systemd/system/kapa-dashboard.service` | systemd unit |
| `/etc/kapa/kapa.ini` | configuration (noreplace) |
| `/etc/kapa/nginx-kapa.conf.sample` | nginx snippet to include |
| `/usr/share/doc/kapa-dashboard/` | README, API.md, RESTORE.md |
