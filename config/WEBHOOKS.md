# Ausgehende Webhooks

> 🇬🇧 [English version: WEBHOOKS.en.md](WEBHOOKS.en.md)

Das Dashboard kann bei bestimmten Ereignissen einen **HTTP-POST mit JSON** an
externe Systeme schicken — z. B. um ITSM-Tickets (ServiceNow/Jira) anzulegen,
eine **Ansible-AWX**-Job-Vorlage zu starten, eine **GitLab-/GitHub-Pipeline**
auszulösen oder in **Slack/Teams** zu posten.

Konfiguration: **Verwaltung → Webhooks** (nur Admin). Je Ziel: URL, Secret,
Ereignisse (Häkchen), aktiv-Schalter, Beschreibung. Ein **Test**-Knopf schickt
einen Ping und zeigt die Antwort.

## Ereignisse

| Schlüssel | Wann |
|-----------|------|
| `created` | Kapazitätsanfrage angelegt |
| `approved` | Anfrage (vollständig) genehmigt |
| `rejected` | Anfrage abgelehnt |
| `team_turn` | nächstes Team ist mit der Freigabe dran |
| `reminder` | Erinnerung an eine liegengebliebene Stufe |
| `storage_requested` | Storage-Erweiterung/‑Anlage angefragt |
| `storage_done` | Storage-Anfrage als erledigt gemeldet |
| `imported` | Offline-Quelle (PowerCLI-JSON) importiert/ersetzt |

## Payload

Immer JSON, UTF-8. Gemeinsame Felder: `event`, `at` (ISO-Zeit), dazu je nach
Ereignis das Objekt. Beispiele:

```json
{ "event": "approved", "at": "2026-07-24T09:12:00",
  "actor": "anna.schmidt@firma.local", "team": "",
  "reservation": { "id": "KAPA-4f0…", "name": "SAP HANA Q3", "cluster": "Cluster-01",
                   "vcpu": 32, "ram_gb": 256, "storage_gb": 2000,
                   "status": "genehmigt", "change": "OPS-2087" } }
```

```json
{ "event": "storage_requested", "at": "2026-07-24T09:15:10",
  "actor": "lisa.brandt@firma.local",
  "request": { "id": "…", "cluster": "Cluster-02", "kind": "expand",
               "lun_name": "FC-LUN-201", "target_gb": 8000, "naa": "naa.60060160…" } }
```

```json
{ "event": "imported", "at": "2026-07-24T09:20:00", "actor": "admin@firma.local",
  "source": "RZ-Insel", "clusters": 3, "skipped": [], "replaced": true }
```

Die Reservierungs-Objekte kommen über dieselbe Aufbereitung wie die API
(`public_res`) — **ohne** interne Felder wie Empfänger-Mailadressen.

## Signatur prüfen (wichtig)

Jeder POST trägt zusätzlich zum Body diese Header:

```
X-Kapa-Event: approved
X-Kapa-Delivery: <eindeutige-id>
X-Kapa-Signature: sha256=<hmac-hex>      # nur wenn ein Secret gesetzt ist
```

`X-Kapa-Signature` ist ein **HMAC-SHA256** über den **rohen Request-Body** mit
dem hinterlegten Secret. Der Empfänger sollte die Signatur prüfen und den
Request sonst verwerfen.

**Python:**

```python
import hmac, hashlib
def gueltig(body_bytes, header_sig, secret):
    erwartet = "sha256=" + hmac.new(secret.encode(), body_bytes,
                                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(erwartet, header_sig or "")
```

**Node.js:**

```js
const crypto = require("crypto");
function gueltig(bodyBuf, headerSig, secret) {
  const erwartet = "sha256=" + crypto.createHmac("sha256", secret)
                                     .update(bodyBuf).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(erwartet), Buffer.from(headerSig || ""));
}
```

## Zustellung & Sicherheit

- **Nicht blockierend**: Der Versand läuft im Hintergrund; ein langsamer
  Empfänger bremst das Dashboard nicht. Timeout 8 s, **ein** Wiederholversuch.
- **Auditiert**: Jede Zustellung (Erfolg/HTTP-Code bzw. Fehler) steht im Log.
- **Secrets** werden nach dem Speichern **nie wieder angezeigt** und liegen
  bewusst **nicht im Backup** (wie die Sitzungsdaten).
- Nur Admins pflegen die Ziele; erlaubt sind ausschließlich `http`/`https`.
  Die Ziel-URLs sind vertrauenswürdig zu wählen (interne Adressen möglich).
