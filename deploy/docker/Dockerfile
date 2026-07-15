# VMware Kapazitätsplanung – Container-Image auf Basis von Red Hat UBI 9.
# Nur Python-Standardbibliothek, daher genügt python3 im Minimal-Image.
#
# Bauen (aus der Projektwurzel!):
#   docker build -f deploy/docker/Dockerfile -t kapa-dashboard:latest .
# Läuft unbändert auch mit Podman:
#   podman build -f deploy/docker/Dockerfile -t kapa-dashboard:latest .
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

LABEL org.opencontainers.image.title="VMware Kapazitätsplanung" \
      org.opencontainers.image.source="https://github.com/marcobockelbrink/vmware" \
      org.opencontainers.image.description="Aria Operations Kapazitäts-Dashboard (stdlib-only)"

RUN microdnf install -y python3 shadow-utils \
    && microdnf clean all \
    && useradd -r -u 1001 -d /opt/kapa -s /sbin/nologin kapa

WORKDIR /opt/kapa
COPY aria_kapa.py /opt/kapa/aria_kapa.py

# Daten-Volume. Gruppe 0 + g=u macht das Verzeichnis auch dann beschreibbar,
# wenn die Runtime eine beliebige UID vergibt (OpenShift-kompatibel).
RUN mkdir -p /opt/kapa/data \
    && chown -R 1001:0 /opt/kapa \
    && chmod -R g=u /opt/kapa

USER 1001
EXPOSE 8080
VOLUME ["/opt/kapa/data"]

# Im Container an alle Interfaces binden (der Port wird vom Host/Proxy gemappt).
# Zusätzliche Optionen kommen über CMD und lassen sich beim Start überschreiben,
# z. B.:  docker run ... kapa-dashboard:latest --config /etc/kapa/kapa.ini
ENTRYPOINT ["python3", "/opt/kapa/aria_kapa.py", "--serve", "--bind", "0.0.0.0", "--port", "8080"]
CMD ["--config", "/etc/kapa/kapa.ini"]
