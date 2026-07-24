# Outbound webhooks

> 🇩🇪 [Deutsche Fassung: WEBHOOKS.md](WEBHOOKS.md) — the German document is
> primary; this translation follows.

On certain events the dashboard can send an **HTTP POST with JSON** to external
systems — e.g. to open an ITSM ticket (ServiceNow/Jira), launch an **Ansible
AWX** job template, trigger a **GitLab/GitHub pipeline** or post to
**Slack/Teams**.

Configuration: **Administration → Webhooks** (admin only). Per target: URL,
secret, events (checkboxes), active toggle, description. A **Test** button sends
a ping and shows the response.

## Events

| Key | When |
|-----|------|
| `created` | capacity request created |
| `approved` | request (fully) approved |
| `rejected` | request rejected |
| `team_turn` | the next team is up for approval |
| `reminder` | reminder for a stalled stage |
| `storage_requested` | storage expansion/new LUN requested |
| `storage_done` | storage request marked done |
| `imported` | offline source (PowerCLI JSON) imported/replaced |

## Payload

Always JSON, UTF-8. Common fields: `event`, `at` (ISO time), plus the object
depending on the event. Examples:

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

Reservation objects use the same shaping as the API (`public_res`) — **without**
internal fields such as recipient mail addresses. Status values stay German
(part of the stable v1 contract).

## Verify the signature (important)

In addition to the body, every POST carries these headers:

```
X-Kapa-Event: approved
X-Kapa-Delivery: <unique-id>
X-Kapa-Signature: sha256=<hmac-hex>      # only when a secret is set
```

`X-Kapa-Signature` is an **HMAC-SHA256** over the **raw request body** using the
configured secret. The receiver should verify the signature and otherwise
discard the request.

**Python:**

```python
import hmac, hashlib
def valid(body_bytes, header_sig, secret):
    expected = "sha256=" + hmac.new(secret.encode(), body_bytes,
                                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig or "")
```

**Node.js:**

```js
const crypto = require("crypto");
function valid(bodyBuf, headerSig, secret) {
  const expected = "sha256=" + crypto.createHmac("sha256", secret)
                                     .update(bodyBuf).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(headerSig || ""));
}
```

## Delivery & security

- **Non-blocking**: delivery runs in the background; a slow receiver does not
  slow the dashboard. 8 s timeout, **one** retry.
- **Audited**: every delivery (success/HTTP code or error) is logged.
- **Secrets** are **never shown again** after saving and are deliberately
  **not in the backup** (like session data).
- Only admins manage targets; only `http`/`https` are allowed. Choose target
  URLs you trust (internal addresses are possible).
