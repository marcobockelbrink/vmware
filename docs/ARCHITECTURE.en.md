# Architecture — VMware Capacity Planning

> 🇩🇪 [Deutsche Fassung: ARCHITEKTUR.md](ARCHITEKTUR.md)
>
> As of v2.9. The diagrams are Mermaid — GitHub renders them right in the
> browser.

## Guiding idea

A **single Python script** (`aria_kapa.py`, standard library only,
Python 3.8+) is data collector, web server, calculation core and UI server in
one. No pip, no build, no database server — a deliberate choice so the
dashboard runs on any RHEL host without a package zoo and an update means
swapping **one file**.

```mermaid
flowchart LR
    subgraph Users
        B["Browser<br/>(admin / reviewer /<br/>requester / auditor)"]
        EXT["External apps<br/>(Grafana, CMDB, …)<br/>bearer token"]
        MON["Monitoring<br/>(uptime check)"]
    end

    subgraph Host["Linux host"]
        NG["nginx :443<br/>TLS, path /capa/"]
        APP["aria_kapa.py --serve<br/>127.0.0.1:8080<br/>systemd, user kapa"]
        DATA[("Data store<br/>/var/lib/kapa<br/>JSON or SQLite")]
    end

    subgraph External["Surrounding systems"]
        V1["vROps source 1<br/>(DC north)"]
        V2["vROps source n<br/>(DC south)"]
        AD["Active Directory<br/>ldaps://"]
        SMTP["SMTP server"]
        BK["SFTP backup target"]
    end

    B -->|HTTPS| NG
    EXT -->|HTTPS /api/v1| NG
    MON -->|/healthz| NG
    NG -->|local HTTP| APP
    APP <-->|Suite API, OpsToken| V1
    APP <-->|Suite API, OpsToken| V2
    APP -->|simple bind + memberOf| AD
    APP -->|mails, template| SMTP
    APP -->|tar.gz twice a day| BK
    APP <--> DATA
```

**Trust boundaries:** TLS terminates at nginx; the dashboard itself speaks
local HTTP only. vROps access is strictly **read-only** (dedicated read-only
service account), optionally through a per-source proxy. Secrets never live
in the INI but in `.pass` files (root:kapa, 0640).

## Inside the process

```mermaid
flowchart TB
    subgraph HTTP["ThreadingHTTPServer (one thread per request)"]
        R1["Page routes<br/>/ /reservierungen /genehmigungen<br/>/archiv /verwaltung /log"]
        R2["Session API<br/>/api/* (cookie)"]
        R3["v1 API<br/>/api/v1/* (bearer/session)<br/>read + per-token write permissions"]
        R4["/healthz (no auth)"]
    end

    subgraph BG["Background threads (daemon)"]
        T1["scheduler<br/>Aria refresh every 30 min"]
        T2["maintenance<br/>TTL expiry, log rotation"]
        T3["backup_loop<br/>SFTP twice a day + rotation"]
        T4["reminder_loop<br/>hourly: nudge stalled<br/>requests"]
    end

    subgraph CORE["Shared core"]
        ST["state (cluster data<br/>from last refresh)"]
        RES["reservations + res_lock"]
        DEC["res_apply_approve/reject/cancel<br/>ONE decision logic<br/>for UI and API"]
        MAIL["mail_event → template<br/>{{placeholders}}, localized subject"]
        STORE["JsonStore / SqliteStore<br/>atomic writes (mkstemp+rename)"]
    end

    R1 & R2 & R3 --> CORE
    T1 --> ST
    T2 & T3 --> STORE
    T4 --> DEC
    DEC --> MAIL
    RES <--> STORE
    ST <--> STORE
```

Core rule since v2.8.1: **state transitions exist exactly once.** Session UI
and write API call the same `res_apply_*` functions — behavior cannot drift
(the refactor promptly surfaced a divergence bug: cancelled requests could
still be approved via the UI).

## Data flow: Aria refresh

```mermaid
sequenceDiagram
    participant S as scheduler
    participant C as collect() per source
    participant V as vROps Suite API
    participant B as build_summary
    participant ST as state + cache

    S->>C: do_refresh (all sources in turn)
    C->>V: clusters, hosts, VMs (+ metrics/properties, bulk)
    C->>V: datastores (storage, vSAN factor)
    C->>V: tags, workload badge
    C->>V: dvSwitches → port groups (VLAN cache,<br/>full re-read once a day)
    C->>V: Tanzu namespaces (reservations,<br/>candidate keys, best effort)
    C->>B: raw data per cluster
    B->>B: N+1 deduction, CPU factor,<br/>Tanzu MHz→vCPU (rounded up)
    B->>ST: cluster list (+ source badge)
    Note over ST: partial-failure tolerant: if one source fails,<br/>the others keep delivering
```

Every step is **best effort**: if storage/network/Tanzu data is missing in an
environment, that part stays empty and the rest keeps working. The log states
per step what was detected (keys, mappings, cache hits) — version-dependent
vROps stat keys can be verified without code changes.

## Approval workflow

```mermaid
stateDiagram-v2
    [*] --> requested: request (UI or API)
    requested --> inReview: 1st team approves
    inReview --> inReview: next stage approves<br/>(order = teams table)
    inReview --> approved: last stage approves
    requested --> rejected: team/admin rejects
    inReview --> rejected: team/admin rejects
    requested --> cancelled: requester/team/admin
    inReview --> cancelled: requester/team/admin
    approved --> cancelled: cancel (no longer counts)
    rejected --> [*]: archive (permanent)
    cancelled --> [*]: archive (permanent)
    approved --> [*]: expiry after res-ttl-days

    note right of inReview
        reminder_loop mails the team
        when a stage waits longer
        than reminder_days
    end note
```

Optionally an **auto-approval** approves per-team-checked stages
automatically when the target cluster meets configured thresholds
(vCPU/RAM/largest LUN/workload) after subtracting the request — evaluated on
creation and stage changes, conservative (missing data blocks), fully
audited. Only the **approved** status counts against free capacity — together with the
automatically read **Tanzu namespace reservations**. Mails fire per event
according to the matrix in the administration (created/rejected/approved/
"team's turn"/reminder), rendered through the **editable HTML template**.

## Security at a glance

| Layer | Mechanics |
|---|---|
| Sign-in | LDAP simple bind (BER-encoded → no filter injection), empty password rejected, login throttle 5/5 min, password detector in the username field |
| Authorization | roles enforced server-side (requesters: team visibility, no workload, no "decided by"); reviewers only when their team is up |
| Sessions | `secrets.token_urlsafe(32)`, cookie `HttpOnly; Secure; SameSite=Lax` (CSRF protection), pruning on login |
| API | tokens stored as SHA-256 hash only, `hmac.compare_digest`, write permissions per token individually, everything audited |
| Output | strict CSP, `json_for_html` against `</script>` breakout, escaping of all foreign data, template preview in a sandboxed iframe |
| Operations | systemd sandbox (ProtectSystem=strict), files 0600 via mkstemp, request limit 2 MiB, gzip for text types only |

## Frontend

A single HTML page (embedded in the script as a template, data injected
server-side via `__PLACEHOLDERS__`), views via a `render()` dispatch (path or
hash). Cross-cutting engines live at the end of the script:

- **i18n**: German is the source; browser ≠ German → dictionary (~400
  entries) + regex patterns, a MutationObserver continuously translates text
  nodes **and** attributes. API values/status logic stay German (v1 contract).
- **Theme**: CSS variables, `data-theme="light"` on `<html>`, head snippet
  against flashing, choice stored in the per-user server prefs.
- **Prefs**: columns, "announcement seen", theme — one PUT replaces
  everything, hence `prefsBody()` always builds the full state.
- **Deep links**: `#cluster=Name` opens the detail card, the hash is set on
  opening.

## Data storage

All collections (reservations, roles, teams, selector, role labels, tokens,
mail rules, prefs, announcement) go through a store abstraction:
**JSON files** (default, one file per collection) or **SQLite** (a single
`kapa.db`, incremental reservation writes, automatic one-time migration).
Writes are always atomic. Details and restore:
[`../config/RESTORE.en.md`](../config/RESTORE.en.md).

## Deployment

```mermaid
flowchart LR
    GH["GitHub repo<br/>+ release tag v*"]
    GH -->|GitHub Actions| IMG["GHCR image<br/>kapa-dashboard:latest + :x.y<br/>amd64 + arm64"]
    IMG --> DOCK["Docker/Podman<br/>compose, UBI9, non-root"]
    IMG --> K8S["Kubernetes<br/>manifests or Helm chart<br/>1 replica + PVC, /healthz probes"]
    GH --> HOSTS["Classic: systemd + nginx<br/>(templates under config/)"]
```

Same artifact, container-first — decision guide in
[`../deploy/README.en.md`](../deploy/README.en.md).

## Deliberate decisions (mini ADRs)

1. **Standard library only, one file** — operations without package
   management, update = file swap; paid for with embedded templates.
2. **German as source language + translation engine** instead of duplicated
   templates — one source to maintain, EN follows automatically; the API
   stays stable German (v1 contract).
3. **Best-effort data collection with candidate keys** — vROps versions
   differ; better partially empty + well logged than failing hard.
4. **Tanzu counted conservatively** — namespace reservation on top of VM
   usage; possible double counting accepted in favor of safe planning.
5. **Cluster name as the key across sources** (variant A) — requires unique
   names; in return reservations survive source restructuring.
6. **Fail-fast configuration** — unknown INI keys and misplaced `[quelle:*]`
   entries abort startup with a hint instead of silently running on wrong
   defaults.
