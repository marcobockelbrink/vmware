# Ansible / AAP-Rollout

Rollt das [RPM](../rpm/) über eine ganze RHEL-9-Flotte aus, pflegt die
Konfiguration, legt das Aria-Passwort ab, setzt den nötigen SELinux-Schalter
und startet den Dienst – idempotent. Secrets kommen aus dem Ansible-Vault.

## Struktur

```
deploy/ansible/
├── playbook.yml                     # spielt die Role auf [kapa_servers]
├── inventory.example.ini            # Beispiel-Inventar
├── requirements.yml                 # Collection ansible.posix
├── group_vars/
│   └── kapa_servers.yml.example     # Variablen inkl. Vault-Platzhalter
└── roles/kapa/                      # die eigentliche Role
```

## Voraussetzungen

- RPM gebaut und erreichbar (GitHub-Release-Asset, internes `dnf`-Repo oder
  lokaler Pfad) – siehe [`../rpm/`](../rpm/). Pfad/URL in `kapa_rpm`.
- Collection installieren:

  ```bash
  ansible-galaxy collection install -r deploy/ansible/requirements.yml
  ```

## Verwendung (CLI)

```bash
cp deploy/ansible/group_vars/kapa_servers.yml.example \
   deploy/ansible/group_vars/kapa_servers.yml
# Werte eintragen; Passwort mit ansible-vault verschlüsseln:
ansible-vault encrypt_string 'DAS-ARIA-PASSWORT' --name kapa_aria_password

ansible-playbook -i deploy/ansible/inventory.example.ini \
    deploy/ansible/playbook.yml --ask-vault-pass
```

## Verwendung in der AAP

1. Dieses Repository als **Projekt** einbinden.
2. **Inventar** in der AAP pflegen (Gruppe `kapa_servers`).
3. **Vault-Credential** für das verschlüsselte Passwort hinterlegen.
4. **Job-Template** auf `deploy/ansible/playbook.yml` zeigen lassen; die
   nicht-geheimen Variablen als Extra-Vars oder in `group_vars`, die Secrets
   über das Vault-Credential.

Die Collection `ansible.posix` muss in der Execution-Environment verfügbar sein
(bei den mitgelieferten EEs der Fall; sonst per `requirements.yml` ins EE
aufnehmen).

## Was die Role tut

| Schritt | Modul |
|---|---|
| RPM installieren | `ansible.builtin.dnf` |
| `/etc/kapa/kapa.ini` aus Vorlage schreiben | `ansible.builtin.template` |
| `/etc/kapa/*.pass` (0640 root:kapa, `no_log`) ablegen | `ansible.builtin.copy` |
| `httpd_can_network_connect` aktivieren | `ansible.posix.seboolean` |
| nginx-Snippet einbinden (optional) | `ansible.builtin.copy` |
| Dienst aktivieren/starten | `ansible.builtin.systemd` |

Änderungen an Konfiguration oder Passwort lösen automatisch einen Neustart des
Dienstes aus (Handler). Wichtige Variablen stehen in
[`roles/kapa/defaults/main.yml`](roles/kapa/defaults/main.yml).

> Hinweis zu `disable_gpg_check`: Die Role installiert das RPM ohne
> Signaturprüfung, damit ein unsigniertes internes Paket funktioniert. Für den
> produktiven Einsatz empfiehlt sich, das RPM zu **signieren** und die Prüfung
> zu aktivieren.
