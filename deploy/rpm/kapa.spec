Name:           kapa-dashboard
Version:        %{?_kapa_version}%{!?_kapa_version:0.9}
Release:        1%{?dist}
Summary:        VMware Aria Operations Kapazitätsplanung – Dashboard

# Kein LICENSE-File im Repo -> vorerst "Proprietary" (alle Rechte vorbehalten).
# Wenn ihr das Projekt unter eine Open-Source-Lizenz stellt (z. B. MIT), hier
# anpassen und eine LICENSE-Datei ergänzen (dann auch %license LICENSE bei %files).
License:        Proprietary
URL:            https://github.com/marcobockelbrink/vmware
Source0:        kapa-%{version}.tar.gz

BuildArch:      noarch
Requires:       python3 >= 3.8
Requires(pre):  shadow-utils
%{?systemd_requires}
BuildRequires:  systemd-rpm-macros

%description
Kapazitätsauswertung pro Cluster aus VMware Aria Operations mit
browserbasiertem Dashboard, Reservierungs- und Genehmigungs-Workflow,
AD-Anmeldung, Rollen, Audit-Log, SFTP-Backup und lesender v1-API.

Ein einzelnes Python-Skript ohne externe Abhängigkeiten (nur
Standardbibliothek). Der Dienst lauscht lokal auf 127.0.0.1:8080 und wird
üblicherweise per nginx unter /capa veröffentlicht.

%prep
%setup -q -n kapa-%{version}

%build
# nichts zu bauen – reines Python-Skript

%install
rm -rf %{buildroot}

# Anwendung (root:root, nicht durch den Dienst-Benutzer beschreibbar)
install -D -m 0755 aria_kapa.py            %{buildroot}/opt/kapa/aria_kapa.py

# Daten-Verzeichnis (dem Dienst-Benutzer gehörend – siehe %files)
install -d -m 0750                          %{buildroot}/opt/kapa/data

# systemd-Unit
install -D -m 0644 config/kapa-dashboard.service \
    %{buildroot}%{_unitdir}/kapa-dashboard.service

# Konfigurationsvorlagen nach /etc/kapa (werden bei Updates NICHT überschrieben)
install -D -m 0640 config/kapa.env.example  %{buildroot}%{_sysconfdir}/kapa/kapa.env
install -D -m 0640 config/kapa.ini.example  %{buildroot}%{_sysconfdir}/kapa/kapa.ini
# nginx-Snippet als Beispiel (manuell in den 443-Server einbinden)
install -D -m 0644 config/nginx-kapa.conf   %{buildroot}%{_sysconfdir}/kapa/nginx-kapa.conf.sample

# Dokumentation kommt über %doc in %files (kein manuelles Install nötig)

%pre
# Dienst-Benutzer/-Gruppe anlegen (idempotent)
getent group kapa >/dev/null || groupadd -r kapa
getent passwd kapa >/dev/null || \
    useradd -r -g kapa -d /opt/kapa -s /sbin/nologin \
            -c "VMware Kapazitätsplanung" kapa
exit 0

%post
%systemd_post kapa-dashboard.service
if [ $1 -eq 1 ]; then
    # Nur bei Erstinstallation: Hinweise ausgeben
    cat <<'EOF'

kapa-dashboard installiert. Nächste Schritte:

  1) Konfiguration eintragen:
       sudoedit /etc/kapa/kapa.env        # Aria-URL, Benutzer, AD, SMTP …
       sudoedit /etc/kapa/kapa.ini        # optional: alle weiteren Optionen
     Danach: sudo chown root:kapa /etc/kapa/kapa.env && sudo chmod 640 /etc/kapa/kapa.env

  2) Aria-Passwort ablegen (wird per systemd LoadCredential übergeben):
       echo 'DAS-ARIA-PASSWORT' | sudo tee /etc/kapa/aria.pass >/dev/null
       sudo chmod 600 /etc/kapa/aria.pass

  3) SELinux erlauben, dass nginx zum Dienst proxied:
       sudo setsebool -P httpd_can_network_connect 1

  4) nginx-Snippet einbinden (Beispiel):
       /etc/kapa/nginx-kapa.conf.sample  ->  in den bestehenden 443-Server

  5) Dienst starten:
       sudo systemctl enable --now kapa-dashboard
       journalctl -u kapa-dashboard -f

EOF
fi

%preun
%systemd_preun kapa-dashboard.service

%postun
%systemd_postun_with_restart kapa-dashboard.service

%files
%doc README.md config/API.md config/RESTORE.md
%dir /opt/kapa
/opt/kapa/aria_kapa.py
%attr(0750, kapa, kapa) %dir /opt/kapa/data
%{_unitdir}/kapa-dashboard.service
%dir %{_sysconfdir}/kapa
%config(noreplace) %attr(0640, root, kapa) %{_sysconfdir}/kapa/kapa.env
%config(noreplace) %attr(0640, root, kapa) %{_sysconfdir}/kapa/kapa.ini
%config(noreplace) %{_sysconfdir}/kapa/nginx-kapa.conf.sample

%changelog
* Tue Jul 15 2026 Marco Bockelbrink <company@bockelbrink.net> - 0.9-1
- Erstes RPM-Paket; Security-Härtung (XSS-Escaping, Login-Bremse, CSP,
  Secure-Cookie, Body-Limit), sichtbare eindeutige IDs.
