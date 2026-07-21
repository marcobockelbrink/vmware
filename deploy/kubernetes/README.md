# Kubernetes-Deployment

> 🇬🇧 [English version: README.en.md](README.en.md)

Zwei Wege, dasselbe Ergebnis: **einfache Manifeste** (`kubectl apply`) oder
das **Helm-Chart**. Beide nutzen das fertige GHCR-Image
(`ghcr.io/marcobockelbrink/kapa-dashboard`, bei jedem Release automatisch
gebaut, amd64 + arm64).

> **Wichtig:** Die Anwendung hält ihren Zustand in Dateien (PVC,
> ReadWriteOnce) — **`replicas` muss 1 bleiben**, Update-Strategie ist
> `Recreate`. Für Verfügbarkeit sorgt Kubernetes durch Neustart
> (Liveness-Probe auf `/healthz`), nicht durch Skalierung.

## Variante A: Manifeste

```bash
cd deploy/kubernetes/manifests

# 1) Secret anlegen (NICHT die Beispieldatei committen/ausrollen):
kubectl create secret generic kapa-dashboard-secrets \
  --from-literal=ARIA_PASSWORD='DAS-ARIA-PASSWORT'

# 2) ConfigMap aus dem Beispiel ableiten und Werte eintragen
#    (Vorlage aller Optionen: ../../config/kapa.ini.example)
cp configmap.example.yaml configmap.yaml && $EDITOR configmap.yaml

# 3) Ausrollen
kubectl apply -f pvc.yaml -f configmap.yaml -f deployment.yaml -f service.yaml

# 4) Optional: Ingress (Beispiel anpassen — Host, TLS-Secret)
cp ingress.example.yaml ingress.yaml && $EDITOR ingress.yaml
kubectl apply -f ingress.yaml
```

## Variante B: Helm

```bash
helm install kapa deploy/kubernetes/helm/kapa-dashboard \
  --set-file kapaIni=meine-kapa.ini \
  --set secrets.existingSecret=kapa-dashboard-secrets \
  --set ingress.enabled=true --set ingress.host=kapa.firma.local

# Update auf ein neues Release:
helm upgrade kapa deploy/kubernetes/helm/kapa-dashboard --reuse-values \
  --set image.tag=2.11
```

Wichtige Werte (`values.yaml`): `image.tag` (leer = App-Version des Charts),
`persistence.size`/`storageClassName`/`existingClaim`, `ingress.*`,
`kapaIni` (kompletter INI-Inhalt), `secrets.existingSecret`.

## Konfiguration & Secrets

- **INI als ConfigMap** (`/etc/kapa/kapa.ini`, read-only eingehängt):
  alle nicht-geheimen Optionen — Vorlage `config/kapa.ini.example`.
  `data-dir = /opt/kapa/data` zeigt ins PVC.
- **Passwörter als Kubernetes-Secret** (`ARIA_PASSWORD`, optional
  `SMTP_PASSWORD`/`BACKUP_PASSWORD`) — die App liest sie als
  Umgebungsvariablen; `password-file` wird im Container nicht gebraucht.
- **AD-Anmeldung aktivieren** (`ad-url` in der INI) — ohne sie ist die UI
  eine offene Vollzugriffs-Oberfläche.
- **TLS** terminiert am Ingress. Spricht der Ingress kein TLS (nur intern),
  in der INI `cookie-insecure = true` setzen, sonst kommt das
  Session-Cookie (Secure-Flag) nicht durch.

## Betrieb

- **Probes**: Liveness + Readiness auf `/healthz` (ohne Auth, liefert auch
  Datenalter und letzten Abruf-Fehler — praktisch fürs Monitoring).
- **Updates**: neues Image-Tag setzen (`kubectl set image` bzw.
  `helm upgrade --set image.tag=…`); `Recreate` stoppt sauber, das PVC
  bleibt. Sitzungen überleben den Neustart (persistente Sessions).
- **Backup**: entweder das eingebaute SFTP-Backup (INI-Abschnitt) oder
  Volume-Snapshots des PVC — Restore-Anleitung:
  [`../../config/RESTORE.md`](../../config/RESTORE.md).
- **OpenShift-kompatibel**: Image läuft als non-root (UID 1001), das
  Datenverzeichnis gehört Gruppe 0 mit `g=u`.
