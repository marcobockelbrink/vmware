#!/usr/bin/env bash
# Baut das RPM aus dem aktuellen Repository-Stand.
# Voraussetzung (auf einem RHEL/Alma/Rocky-9-Host oder in einer UBI9-Umgebung):
#   sudo dnf install -y rpm-build rpmdevtools systemd-rpm-macros
#
# Aufruf:  deploy/rpm/build.sh
# Ergebnis: ~/rpmbuild/RPMS/noarch/kapa-dashboard-<version>-1.*.noarch.rpm
set -euo pipefail

# Projektwurzel (zwei Ebenen über diesem Skript)
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
spec="$here/kapa.spec"

# Version aus aria_kapa.py ziehen, damit Paket und Code nie auseinanderlaufen
version="$(grep -E '^VERSION = ' "$root/aria_kapa.py" | head -1 | cut -d'"' -f2)"
if [ -z "$version" ]; then
    echo "FEHLER: VERSION konnte nicht aus aria_kapa.py gelesen werden." >&2
    exit 1
fi
echo "Baue kapa-dashboard $version"

# rpmbuild-Baum vorbereiten
topdir="${HOME}/rpmbuild"
mkdir -p "$topdir"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}

# Quell-Tarball erzeugen (nur die für das Paket nötigen Dateien)
stage="$(mktemp -d)"
pkg="kapa-$version"
mkdir -p "$stage/$pkg/config"
cp "$root/aria_kapa.py"                 "$stage/$pkg/"
cp "$root/README.md"                    "$stage/$pkg/"
cp "$root/config/kapa-dashboard.service" "$stage/$pkg/config/"
cp "$root/config/kapa.env.example"      "$stage/$pkg/config/"
cp "$root/config/kapa.ini.example"      "$stage/$pkg/config/"
cp "$root/config/nginx-kapa.conf"       "$stage/$pkg/config/"
cp "$root/config/API.md"                "$stage/$pkg/config/"
cp "$root/config/RESTORE.md"            "$stage/$pkg/config/"

tar -C "$stage" -czf "$topdir/SOURCES/$pkg.tar.gz" "$pkg"
rm -rf "$stage"

# RPM bauen
rpmbuild --define "_topdir $topdir" \
         --define "_kapa_version $version" \
         -bb "$spec"

echo
echo "Fertig. Paket(e):"
find "$topdir/RPMS" -name "kapa-dashboard-$version-*.rpm" -print
