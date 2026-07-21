# Kubernetes deployment

> 🇩🇪 [Deutsche Fassung: README.md](README.md)

Two paths, same result: **plain manifests** (`kubectl apply`) or the
**Helm chart**. Both use the ready-made GHCR image
(`ghcr.io/marcobockelbrink/kapa-dashboard`, built automatically on every
release, amd64 + arm64).

> **Important:** the application keeps its state in files (PVC,
> ReadWriteOnce) — **`replicas` must stay 1**, the update strategy is
> `Recreate`. Availability comes from Kubernetes restarting the pod
> (liveness probe on `/healthz`), not from scaling out.

## Option A: manifests

```bash
cd deploy/kubernetes/manifests

# 1) Create the secret (do NOT commit/roll out the example file):
kubectl create secret generic kapa-dashboard-secrets \
  --from-literal=ARIA_PASSWORD='THE-ARIA-PASSWORD'

# 2) Derive the ConfigMap from the example and fill in your values
#    (template with all options: ../../config/kapa.ini.en.example)
cp configmap.example.yaml configmap.yaml && $EDITOR configmap.yaml

# 3) Roll out
kubectl apply -f pvc.yaml -f configmap.yaml -f deployment.yaml -f service.yaml

# 4) Optional: ingress (adapt the example — host, TLS secret)
cp ingress.example.yaml ingress.yaml && $EDITOR ingress.yaml
kubectl apply -f ingress.yaml
```

## Option B: Helm

```bash
helm install kapa deploy/kubernetes/helm/kapa-dashboard \
  --set-file kapaIni=my-kapa.ini \
  --set secrets.existingSecret=kapa-dashboard-secrets \
  --set ingress.enabled=true --set ingress.host=kapa.example.com

# Upgrade to a new release:
helm upgrade kapa deploy/kubernetes/helm/kapa-dashboard --reuse-values \
  --set image.tag=2.11
```

Key values (`values.yaml`): `image.tag` (empty = chart app version),
`persistence.size`/`storageClassName`/`existingClaim`, `ingress.*`,
`kapaIni` (full INI content), `secrets.existingSecret`.

## Configuration & secrets

- **INI as a ConfigMap** (`/etc/kapa/kapa.ini`, mounted read-only): all
  non-secret options — template `config/kapa.ini.en.example`.
  `data-dir = /opt/kapa/data` points into the PVC.
- **Passwords as a Kubernetes secret** (`ARIA_PASSWORD`, optionally
  `SMTP_PASSWORD`/`BACKUP_PASSWORD`) — the app reads them as environment
  variables; `password-file` is not needed inside the container.
- **Enable AD sign-in** (`ad-url` in the INI) — without it the UI is an
  open full-access interface.
- **TLS** terminates at the ingress. If the ingress does not speak TLS
  (internal only), set `cookie-insecure = true` in the INI, otherwise the
  session cookie (Secure flag) will not make it through.

## Operations

- **Probes**: liveness + readiness on `/healthz` (no auth; also reports
  data age and the last fetch error — handy for monitoring).
- **Updates**: set a new image tag (`kubectl set image` or
  `helm upgrade --set image.tag=…`); `Recreate` stops cleanly, the PVC
  stays. Sessions survive the restart (persistent sessions).
- **Backup**: either the built-in SFTP backup (INI section) or volume
  snapshots of the PVC — restore guide:
  [`../../config/RESTORE.en.md`](../../config/RESTORE.en.md).
- **OpenShift-compatible**: the image runs as non-root (UID 1001), the data
  directory belongs to group 0 with `g=u`.
