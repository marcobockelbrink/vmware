# Ansible / AAP rollout

> 🇩🇪 [Deutsche Fassung: README.md](README.md)

Rolls out the [RPM](../rpm/) across an entire RHEL 9 fleet, manages the
configuration, stores the Aria password, sets the required SELinux boolean
and starts the service — idempotently. Secrets come from the Ansible vault.

## Structure

```
deploy/ansible/
├── playbook.yml                     # plays the role on [kapa_servers]
├── inventory.example.ini            # example inventory
├── requirements.yml                 # collection ansible.posix
├── group_vars/
│   └── kapa_servers.yml.example     # variables incl. vault placeholders
└── roles/kapa/                      # the actual role
```

## Prerequisites

- RPM built and reachable (GitHub release asset, internal `dnf` repo or a
  local path) — see [`../rpm/`](../rpm/). Path/URL goes into `kapa_rpm`.
- Install the collection:

  ```bash
  ansible-galaxy collection install -r deploy/ansible/requirements.yml
  ```

## Usage (CLI)

```bash
cp deploy/ansible/group_vars/kapa_servers.yml.example \
   deploy/ansible/group_vars/kapa_servers.yml
# fill in the values; encrypt the password with ansible-vault:
ansible-vault encrypt_string 'THE-ARIA-PASSWORD' --name kapa_aria_password

ansible-playbook -i deploy/ansible/inventory.example.ini \
    deploy/ansible/playbook.yml --ask-vault-pass
```

## Usage in AAP

1. Add this repository as a **project**.
2. Maintain the **inventory** in AAP (group `kapa_servers`).
3. Store a **vault credential** for the encrypted password.
4. Point a **job template** at `deploy/ansible/playbook.yml`; non-secret
   variables as extra vars or in `group_vars`, secrets via the vault
   credential.

The collection `ansible.posix` must be available in the execution
environment (true for the shipped EEs; otherwise add it to the EE via
`requirements.yml`).

## What the role does

| Step | Module |
|---|---|
| install the RPM | `ansible.builtin.dnf` |
| write `/etc/kapa/kapa.ini` from the template | `ansible.builtin.template` |
| place `/etc/kapa/*.pass` (0640 root:kapa, `no_log`) | `ansible.builtin.copy` |
| enable `httpd_can_network_connect` | `ansible.posix.seboolean` |
| include the nginx snippet (optional) | `ansible.builtin.copy` |
| enable/start the service | `ansible.builtin.systemd` |

Changes to the configuration or password automatically trigger a service
restart (handler). The important variables live in
[`roles/kapa/defaults/main.yml`](roles/kapa/defaults/main.yml).

> Note on `disable_gpg_check`: the role installs the RPM without signature
> verification so an unsigned internal package works. For production use it
> is recommended to **sign** the RPM and enable verification.
