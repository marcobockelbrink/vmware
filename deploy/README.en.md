# Deployment variants

> 🇩🇪 [Deutsche Fassung: README.md](README.md)

The application is a single Python script (`aria_kapa.py`, standard library
only, Python 3.8+). The same artifact can be delivered in several ways —
choose by environment:

| Variant | When | Directory |
|---|---|---|
| **RPM** | RHEL/Alma/Rocky 9, one or more hosts, native package management with `dnf` upgrades | [`rpm/`](rpm/) |
| **Ansible / AAP** | roll out and configure across a whole fleet, secrets from the vault | [`ansible/`](ansible/) |
| **Docker / Podman** | test/dev environments or hosts with a container runtime (UBI9 image) | [`docker/`](docker/) |

The three paths complement each other: the **RPM** does the host-local
installation (files, user, systemd unit), **Ansible/AAP** orchestrates the
RPM across many hosts and manages the configuration, and the **container
image** is meant for environments with Docker/Podman.

## Common foundations

- **Configuration**: one INI file, `config/kapa.ini.example` (all non-secret
  options; English twin: `config/kapa.ini.en.example`). Secrets (Aria/SMTP/
  backup passwords) come via file (`--password-file`), environment variable
  (`ARIA_PASSWORD` …) or — in the container — as a mounted secret.
- **Data** lives under `data/` (or `/opt/kapa/data`) and must survive
  updates. Restore guide: `config/RESTORE.en.md`.
- **Reverse proxy**: the dashboard listens locally on `127.0.0.1:8080` and is
  published via nginx at `/capa` (`config/nginx-kapa.conf`).

## RHEL 9 note (SELinux)

If nginx runs as a reverse proxy in front of the service, SELinux must allow
outgoing connections from nginx — otherwise you get `502`:

```bash
sudo setsebool -P httpd_can_network_connect 1
```

The Ansible role sets this automatically.
