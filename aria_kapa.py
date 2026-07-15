#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aria_kapa.py — Kapazitätsauswertung pro Cluster aus VMware Aria Operations (Suite API)

Berechnung:
  CPU-Kapazität  = Summe physischer Cores aller ESXi-Hosts im Cluster × Faktor (Standard 6)
  RAM-Kapazität  = Summe physischer RAM aller ESXi-Hosts im Cluster (1:1)
  Verbraucht     = Summe provisionierter vCPUs / RAM aller VMs im Cluster (inkl. powered-off)
  Frei           = Kapazität − Verbraucht

Aufruf:
  python3 aria_kapa.py --url https://aria-ops.firma.de --user admin [--password ...]
                       [--auth-source local] [--cpu-factor 6] [--insecure]
                       [--output kapa_dashboard.html]
  python3 aria_kapa.py --sample          # Demo mit Beispieldaten (ohne Verbindung)

Benötigt nur die Python-Standardbibliothek (Python 3.8+).
"""

VERSION = "1.3"

# Interne Rollen-Schlüssel (steuern die Rechte, unveränderlich) und ihre
# Standard-Bezeichnungen. Die Bezeichnungen lassen sich auf der Verwaltungsseite
# frei umbenennen (data/kapa_rollennamen.json) – die Schlüssel bleiben gleich.
ROLE_KEYS = ("admin", "anforderer", "reviewer", "auditor")
DEFAULT_ROLE_NAMES = {"admin": "Administrator", "anforderer": "Anforderer",
                      "reviewer": "Reviewer", "auditor": "Technische Prüfung"}

import argparse
import getpass
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

# ---------------------------------------------------------------- Suite API --

class AriaOps:
    def __init__(self, base_url, username, password, auth_source="local", verify_tls=True):
        self.base = base_url.rstrip("/") + "/suite-api/api"
        self.token = None
        self.ctx = None
        if not verify_tls:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self._login(username, password, auth_source)

    def _request(self, method, path, body=None, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "OpsToken " + self.token)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=120) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise RuntimeError(f"HTTP {e.code} bei {method} {path}: {detail}") from None

    def _login(self, username, password, auth_source):
        body = {"username": username, "password": password}
        if auth_source and auth_source != "local":
            body["authSource"] = auth_source
        resp = self._request("POST", "/auth/token/acquire", body)
        self.token = resp["token"]

    def resources(self, resource_kind, adapter_kind="VMWARE"):
        """Alle Ressourcen eines Typs (paginiert)."""
        out, page = [], 0
        while True:
            resp = self._request("GET", "/resources", params={
                "adapterKind": adapter_kind, "resourceKind": resource_kind,
                "pageSize": 1000, "page": page})
            items = resp.get("resourceList", [])
            out.extend(items)
            total = resp.get("pageInfo", {}).get("totalCount", len(out))
            page += 1
            if len(out) >= total or not items:
                return out

    def latest_stats(self, resource_ids, stat_keys, chunk=500, progress=None):
        """Neueste Metrikwerte: {resourceId: {statKey: wert}} — Bulk-POST, GET-Fallback."""
        result = {}
        post_ok = True
        for i in range(0, len(resource_ids), chunk):
            ids = resource_ids[i:i + chunk]
            resp = None
            if post_ok:
                try:
                    resp = self._request("POST", "/resources/stats/latest/query",
                                         {"resourceId": ids, "statKey": stat_keys})
                except RuntimeError:
                    post_ok = False  # ältere Version -> GET-Fallback
            if resp is None:
                resp = self._stats_get(ids, stat_keys)
            for entry in resp.get("values", []):
                rid = entry.get("resourceId")
                stats = {}
                for s in entry.get("stat-list", {}).get("stat", []):
                    vals = s.get("data", [])
                    if vals:
                        stats[s.get("statKey", {}).get("key")] = vals[-1]
                result[rid] = stats
            if progress:
                progress(f"{min(i + chunk, len(resource_ids))} / {len(resource_ids)}")
        return result

    def _stats_get(self, ids, stat_keys, chunk=25):
        merged = {"values": []}
        for i in range(0, len(ids), chunk):
            params = [("resourceId", rid) for rid in ids[i:i + chunk]] + \
                     [("statKey", k) for k in stat_keys]
            url = self.base + "/resources/stats/latest?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            req.add_header("Authorization", "OpsToken " + self.token)
            with urllib.request.urlopen(req, context=self.ctx, timeout=120) as r:
                merged["values"].extend(json.loads(r.read().decode()).get("values", []))
        return merged

    def properties(self, resource_ids, property_keys, chunk=500, progress=None):
        """Eigenschaften: {resourceId: {propertyKey: wert}} — bulk mit Fallback."""
        result = {}
        try:
            for i in range(0, len(resource_ids), chunk):
                resp = self._request("POST", "/resources/properties/latest/query",
                                     {"resourceIds": resource_ids[i:i + chunk],
                                      "propertyKeys": property_keys})
                for entry in resp.get("values", []):
                    rid = entry.get("resourceId")
                    props = {}
                    for p in entry.get("property-contents", {}).get("property-content", []):
                        vals = p.get("values") or p.get("data") or []
                        if vals:
                            props[p.get("statKey")] = vals[-1]
                    result[rid] = props
                if progress:
                    progress(f"{min(i + chunk, len(resource_ids))} / {len(resource_ids)}")
            return result
        except RuntimeError:
            pass  # ältere Version -> einzeln
        for rid in resource_ids:
            resp = self._request("GET", f"/resources/{rid}/properties")
            props = {}
            for p in resp.get("property", []):
                if p.get("name") in property_keys:
                    props[p["name"]] = p.get("value")
            result[rid] = props
        return result

    def related(self, resource_id, kinds=None, rel="ALL"):
        """Identifier der verwandten Ressourcen (optional gefiltert nach
        resourceKind, z. B. {'HostSystem'})."""
        resp = self._request("GET", f"/resources/{resource_id}/relationships",
                             params={"relationshipType": rel, "pageSize": 2000})
        out = []
        for r in resp.get("resourceList", []):
            kind = r.get("resourceKey", {}).get("resourceKindKey")
            if kinds is None or kind in kinds:
                out.append(r.get("identifier"))
        return out

# -------------------------------------------------- Active Directory (LDAP) --

def ldap_bind(url, username, password, timeout=10, insecure=False):
    """Minimaler LDAP Simple Bind (RFC 4511) nur mit der Standardbibliothek.

    Gibt True zurück, wenn sich der Benutzer (UPN, z. B. user@firma.local)
    mit dem Passwort am AD anmelden kann. ldaps:// wird empfohlen; bei
    ldap:// geht das Passwort unverschlüsselt über das Netz."""
    import socket
    if not password:
        return False  # leeres Passwort wäre ein anonymer Bind (immer "Erfolg")
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host = u.hostname
    port = u.port or (636 if u.scheme == "ldaps" else 389)

    def ber_len(n):
        if n < 0x80:
            return bytes([n])
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return bytes([0x80 | len(b)]) + b

    def tlv(tag, payload):
        return bytes([tag]) + ber_len(len(payload)) + payload

    # LDAPMessage { messageID=1, BindRequest { version=3, name, simple-Passwort } }
    bind_req = tlv(0x60, tlv(0x02, b"\x03")
                   + tlv(0x04, username.encode())
                   + tlv(0x80, password.encode()))
    msg = tlv(0x30, tlv(0x02, b"\x01") + bind_req)

    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.sendall(msg)
        data = b""
        while len(data) < 12:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()

    # BindResponse suchen (Tag 0x61), erstes Element darin ist der resultCode
    i = data.find(b"\x61")
    if i < 0 or i + 2 >= len(data):
        raise RuntimeError("Unerwartete LDAP-Antwort")
    j = i + 1
    if data[j] & 0x80:          # lange Längenform überspringen
        j += (data[j] & 0x7F)
    j += 1
    if data[j] != 0x0A:         # ENUMERATED resultCode
        raise RuntimeError("Unerwartete LDAP-Antwort")
    result_code = data[j + 2]
    return result_code == 0

# ----------------------------------------------------------- Datendateien ----

def ensure_dir(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def data_path(path, base="data"):
    """JSON-Datendateien ohne Verzeichnisangabe landen im Datenordner (Standard
    data/, per --data-dir änderbar, z. B. /var/lib/kapa außerhalb des
    Deploy-Verzeichnisses); explizite Pfade (relativ mit Ordner oder absolut)
    bleiben unverändert."""
    if path and not os.path.dirname(path):
        return os.path.join(base, path)
    return path


def migrate_data_files(*paths):
    """Alt-Dateien aus dem Arbeitsverzeichnis in den Datenordner verschieben
    (frühere Versionen legten kapa_*.json direkt neben dem Skript ab)."""
    for p in paths:
        ensure_dir(p)
        old = os.path.basename(p)
        if (not os.path.exists(p) and os.path.exists(old)
                and os.path.abspath(old) != os.path.abspath(p)):
            os.replace(old, p)
            print(f"Datendatei verschoben: {old} -> {p}", file=sys.stderr)

# ---------------------------------------------------------- SFTP-Backup -----

def _ssh_cmd(args, prog):
    """Basis-Kommando + Umgebung für scp/sftp mit Key- oder Passwort-Auth."""
    base = [prog, "-q", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15", "-o", f"Port={args.backup_port}"]
    env = os.environ.copy()
    if args.backup_key:
        base += ["-i", args.backup_key, "-o", "BatchMode=yes"]
    elif args.backup_password:
        env["SSHPASS"] = args.backup_password
        base = ["sshpass", "-e"] + base
    else:
        base += ["-o", "BatchMode=yes"]   # ssh-agent / Standard-Keys
    return base, env


def _ssh_run(cmd, env, timeout=180):
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise RuntimeError("sshpass nicht installiert – für Passwort-Backups "
                           "'sshpass' installieren oder besser --backup-key verwenden"
                           if "sshpass" in str(e) else str(e)) from None
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()
                           or f"{cmd[0]} beendet mit Exit-Code {r.returncode}")
    return r.stdout.decode(errors="replace")


def sftp_backup(args):
    """Datendateien als tar.gz per scp auf das Backupziel kopieren.

    Authentifizierung per SSH-Key (--backup-key, empfohlen) oder Passwort
    (--backup-password bzw. BACKUP_PASSWORD; erfordert installiertes sshpass).
    Gibt den Namen des übertragenen Archivs zurück."""
    import tarfile
    import tempfile
    if not args.backup_target:
        raise RuntimeError("kein --backup-target konfiguriert")
    files = [p for p in (args.cache, args.res_file, args.roles_file,
                         args.log_file, args.tokens_file, args.teams_file,
                         args.rolenames_file)
             if p and os.path.exists(p)]
    if not files:
        raise RuntimeError("keine Datendateien vorhanden")
    name = "kapa_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".tar.gz"
    tmp = os.path.join(tempfile.gettempdir(), name)
    with tarfile.open(tmp, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=os.path.basename(f))
    cmd, env = _ssh_cmd(args, "scp")
    try:
        _ssh_run(cmd + [tmp, args.backup_target.rstrip("/") + "/" + name], env)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return name


def backup_rotate(args):
    """Backups älter als --backup-keep-days auf dem Ziel löschen (per sftp,
    funktioniert auch auf sftp-only-Servern). Gibt die Anzahl gelöschter
    Archive zurück."""
    import tempfile
    if args.backup_keep_days <= 0:
        return 0
    host, _, rdir = args.backup_target.partition(":")
    rdir = (rdir or ".").rstrip("/") or "/"

    def sftp_batch(commands):
        cmd, env = _ssh_cmd(args, "sftp")
        with tempfile.NamedTemporaryFile("w", suffix=".batch",
                                         delete=False) as b:
            b.write("\n".join(commands) + "\n")
            batch = b.name
        try:
            return _ssh_run(cmd + ["-b", batch, host], env)
        finally:
            try:
                os.remove(batch)
            except OSError:
                pass

    listing = sftp_batch([f"ls -1 {rdir}"])
    cutoff = ((datetime.now() - timedelta(days=args.backup_keep_days))
              .strftime("%Y%m%d_%H%M%S"))
    olds = sorted({m.group(0)
                   for m in re.finditer(r"kapa_backup_(\d{8}_\d{6})\.tar\.gz",
                                        listing)
                   if m.group(1) < cutoff})
    if olds:
        # führendes '-' = Fehler einzelner rm-Befehle ignorieren
        sftp_batch([f"-rm {rdir}/{name}" for name in olds])
    return len(olds)

# ----------------------------------------------------------- Mail-Reports ----

def send_mail(args, subject, body, extra_to=()):
    """Report-Mail über den konfigurierten SMTP-Server (--smtp-server)."""
    import smtplib
    from email.message import EmailMessage
    if not args.smtp_server:
        return
    to = [t.strip() for t in (args.smtp_to or "").split(",") if t.strip()]
    to += [t for t in extra_to if t and "@" in t and t not in to]
    if not to:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = args.smtp_from or "kapa-dashboard@localhost"
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    host, _, port = args.smtp_server.partition(":")
    with smtplib.SMTP(host, int(port or 25), timeout=15) as smtp:
        if args.smtp_tls:
            smtp.starttls()
        if args.smtp_user:
            smtp.login(args.smtp_user, args.smtp_password or "")
        smtp.send_message(msg)


def reservation_mail_body(r, action, admin, res_ttl):
    valid = ""
    if r.get("created"):
        try:
            d = datetime.fromisoformat(r["created"]) + \
                timedelta(days=(res_ttl - 1 if res_ttl > 0 else 30))
            valid = d.strftime("%d.%m.%Y")
        except ValueError:
            pass
    approvals = r.get("approvals") or []
    if approvals:
        freigaben = "\n".join(
            f"             - {a.get('team') or '?'}: {a.get('by') or '?'} "
            f"am {a.get('on') or '?'}" for a in approvals)
        freigaben = "\n" + freigaben
    else:
        freigaben = " –"
    stor = r.get("storage_gb") or 0
    return (f"Kapazitätsreservierung {action}\n"
            f"\n"
            f"ID:          {r.get('id') or '–'}\n"
            f"Anfrage:     {r.get('name', '?')}\n"
            f"Change:      {r.get('change') or '–'}\n"
            f"Cluster:     {r.get('cluster', '?')}\n"
            f"vCPU:        {r.get('vcpu', 0)}\n"
            f"RAM:         {r.get('ram_gb', 0)} GB\n"
            f"Storage:     {stor} GB\n"
            f"Abteilung:   {r.get('abteilung') or '–'}\n"
            f"Beantragt:   von {r.get('von') or '–'} am {r.get('created') or '–'}\n"
            f"Gültig bis:  {valid or '–'}\n"
            f"Freigaben:  {freigaben}\n"
            f"Kommentar:   {r.get('comment') or '–'}\n"
            f"\n"
            f"{action} von {admin} am {datetime.now().strftime('%d.%m.%Y %H:%M')}\n")

# ------------------------------------------------------------- Datenabfrage --

def name_of(res):
    return res.get("resourceKey", {}).get("name", "?")


def collect(api, cpu_factor, progress=None, failover_hosts=1):
    log = progress or (lambda m: print(m, file=sys.stderr))

    log("Lese Cluster ...")
    clusters = api.resources("ClusterComputeResource")
    log(f"Lese ESXi-Hosts ... ({len(clusters)} Cluster gefunden)")
    hosts = api.resources("HostSystem")
    log(f"Lese VMs ... ({len(hosts)} Hosts gefunden)")
    vms = api.resources("VirtualMachine")
    log(f"{len(vms)} VMs gefunden")

    host_ids = [h["identifier"] for h in hosts]
    vm_ids = [v["identifier"] for v in vms]

    log("Lese Host-Metriken (Cores, RAM) ...")
    host_stats = api.latest_stats(host_ids,
        ["cpu|corecount_provisioned", "mem|host_provisioned"],
        progress=lambda m: log(f"Host-Metriken: {m}"))
    log("Lese Host-Zuordnung (Cluster) ...")
    host_props = api.properties(host_ids, ["summary|parentCluster"],
        progress=lambda m: log(f"Host-Eigenschaften: {m}"))

    log("Lese VM-Metriken (vCPU, RAM) ...")
    vm_stats = api.latest_stats(vm_ids,
        ["config|hardware|num_Cpu", "mem|guest_provisioned"],
        progress=lambda m: log(f"VM-Metriken: {m}"))
    log("Lese VM-Eigenschaften (Cluster, PowerState) ...")
    vm_props = api.properties(vm_ids,
        ["summary|parentCluster", "summary|runtime|powerState",
         "config|hardware|numCpu", "config|hardware|memoryKB"],
        progress=lambda m: log(f"VM-Eigenschaften: {m}"))

    # Storage-Kapazität aus den Datastores (vSAN + angedockte FC-LUNs).
    # Best effort – schlägt es fehl, bleibt die Anzeige leer, der Rest läuft weiter.
    datastores, ds_stats = [], {}
    try:
        log("Lese Datastores (Storage-Kapazität) ...")
        datastores = api.resources("Datastore")
        ds_ids = [d["identifier"] for d in datastores]
        ds_stats = api.latest_stats(ds_ids,
            ["capacity|total_capacity", "capacity|used_space"],
            progress=lambda m: log(f"Datastore-Metriken: {m}"))
    except Exception as e:
        log(f"Storage-Kapazität nicht verfügbar: {e}")

    data = {}
    for c in clusters:
        data[name_of(c)] = {"hosts": [], "vms": [],
                            "storage": {"cap_gb": 0.0, "used_gb": 0.0}}

    for h in hosts:
        rid = h["identifier"]
        cl = (host_props.get(rid) or {}).get("summary|parentCluster")
        if cl not in data:
            continue  # Standalone-Host ohne Cluster
        st = host_stats.get(rid) or {}
        cores = st.get("cpu|corecount_provisioned")
        ram_kb = st.get("mem|host_provisioned")
        data[cl]["hosts"].append({
            "name": name_of(h),
            "cores": int(cores) if cores else 0,
            "ram_gb": round((ram_kb or 0) / 1024 / 1024, 1),
        })

    for v in vms:
        rid = v["identifier"]
        p = vm_props.get(rid) or {}
        cl = p.get("summary|parentCluster")
        if cl not in data:
            continue
        st = vm_stats.get(rid) or {}
        vcpu = st.get("config|hardware|num_Cpu") or p.get("config|hardware|numCpu")
        ram_kb = st.get("mem|guest_provisioned") or p.get("config|hardware|memoryKB")
        power = str(p.get("summary|runtime|powerState", "?"))
        data[cl]["vms"].append({
            "name": name_of(v),
            "vcpu": int(float(vcpu)) if vcpu else 0,
            "ram_gb": round(float(ram_kb or 0) / 1024 / 1024, 1),
            "on": "on" in power.lower().replace(" ", ""),
        })

    # Datastore -> Cluster über die angedockten Hosts. Jeder Datastore (auch ein
    # von allen Hosts gesehenes FC-LUN oder der vSAN-Datastore) ist in vROps EINE
    # Ressource und wird je Cluster genau EINMAL gezählt – kein Doppeln über die
    # Hosts. Ein cluster-übergreifend geteiltes LUN zählt in jedem Cluster, an
    # dessen Hosts es hängt, einmal.
    host_cluster = {h["identifier"]: (host_props.get(h["identifier"]) or {})
                    .get("summary|parentCluster") for h in hosts}
    if datastores:
        log(f"Ordne {len(datastores)} Datastores den Clustern zu (über Hosts) ...")
    mapped = 0
    for idx, ds in enumerate(datastores, 1):
        did = ds["identifier"]
        st = ds_stats.get(did) or {}
        cap = st.get("capacity|total_capacity")
        used = st.get("capacity|used_space")
        if not cap and not used:
            continue
        try:
            host_ids = api.related(did, {"HostSystem"})
        except Exception:
            host_ids = []
        cls = set()
        for hid in host_ids:
            cl = host_cluster.get(hid)
            if cl in data:
                cls.add(cl)
        if cls:
            mapped += 1
        for cl in cls:
            if cap:
                data[cl]["storage"]["cap_gb"] += float(cap)
            if used:
                data[cl]["storage"]["used_gb"] += float(used)
        if idx % 25 == 0:
            log(f"Datastore-Zuordnung: {idx} / {len(datastores)}")
    if datastores:
        log(f"Storage zugeordnet: {mapped} von {len(datastores)} Datastores; "
            + ", ".join(f"{cl}={round(d['storage']['cap_gb'])} GB"
                        for cl, d in data.items() if d['storage']['cap_gb']))

    return build_summary(data, cpu_factor, failover_hosts)


def build_summary(data, cpu_factor, failover_hosts=1):
    """data: {cluster: {hosts:[{cores,ram_gb}], vms:[{vcpu,ram_gb,on}]}}

    Pro Cluster werden die größten `failover_hosts` Hosts als Ausfallreserve
    (N+1) von der Gesamtkapazität abgezogen."""
    clusters = []
    for cl, d in sorted(data.items()):
        n_spare = min(max(0, failover_hosts), max(0, len(d["hosts"]) - 1))
        spare_cores = sum(sorted((h["cores"] for h in d["hosts"]), reverse=True)[:n_spare])
        spare_ram = sum(sorted((h["ram_gb"] for h in d["hosts"]), reverse=True)[:n_spare])
        cores = sum(h["cores"] for h in d["hosts"]) - spare_cores
        host_ram = round(sum(h["ram_gb"] for h in d["hosts"]) - spare_ram, 1)
        vcpu_cap = cores * cpu_factor
        vcpu_used = sum(v["vcpu"] for v in d["vms"])
        ram_used = round(sum(v["ram_gb"] for v in d["vms"]), 1)
        storage = d.get("storage") or {}
        stor_cap = round(float(storage.get("cap_gb") or 0), 1)
        stor_used = round(float(storage.get("used_gb") or 0), 1)
        clusters.append({
            "name": cl,
            "hostCount": len(d["hosts"]),
            "cores": cores,
            "spareCores": spare_cores,
            "spareRamGb": round(spare_ram, 1),
            "vcpuCap": vcpu_cap,
            "vcpuUsed": vcpu_used,
            "vcpuFree": vcpu_cap - vcpu_used,
            "ramCap": host_ram,
            "ramUsed": ram_used,
            "ramFree": round(host_ram - ram_used, 1),
            "storageCap": stor_cap,
            "storageUsed": stor_used,
            "storageFree": round(stor_cap - stor_used, 1),
            "vmCount": len(d["vms"]),
            "vmOff": sum(1 for v in d["vms"] if not v["on"]),
            "hosts": d["hosts"],
            "vms": d["vms"],
        })
    return clusters

# ------------------------------------------------------------ Beispieldaten --

def sample_data():
    import random
    random.seed(42)
    data = {}
    for ci in range(1, 4):
        cl = f"Cluster-{ci:02d}"
        hosts = [{"name": f"esx{ci}{hi:02d}.firma.local",
                  "cores": random.choice([32, 48, 64]),
                  "ram_gb": random.choice([512, 768, 1024])}
                 for hi in range(1, random.randint(4, 7))]
        vms = [{"name": f"vm-{ci}{vi:03d}",
                "vcpu": random.choice([2, 4, 4, 8, 8, 16]),
                "ram_gb": random.choice([8, 16, 16, 32, 64]),
                "on": random.random() > 0.1}
               for vi in range(1, random.randint(40, 120))]
        cap_gb = len(hosts) * random.choice([8000, 12000, 20000])
        used_gb = round(cap_gb * random.uniform(0.45, 0.85))
        data[cl] = {"hosts": hosts, "vms": vms,
                    "storage": {"cap_gb": cap_gb, "used_gb": used_gb}}
    return data

# ------------------------------------------------------------------ Dashboard --

LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anmeldung – VMware Kapazitätsplanung</title>
<style>
  body { background:#0f172a; color:#e2e8f0; font:14px/1.5 "Segoe UI",system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
  .box { background:#1e293b; border:1px solid #334155; border-radius:12px;
         padding:28px; width:340px; }
  h1 { font-size:17px; margin:0 0 4px; color:#38bdf8; }
  p { color:#94a3b8; font-size:12px; margin:0 0 16px; }
  label { display:block; font-size:12px; color:#94a3b8; margin:12px 0 4px; }
  input { width:100%; box-sizing:border-box; background:#0b1220; border:1px solid #334155;
          color:#e2e8f0; border-radius:6px; padding:8px 10px; font-size:13px; }
  input:focus { outline:none; border-color:#38bdf8; }
  button { width:100%; margin-top:18px; background:#38bdf8; color:#0b1220; border:none;
           border-radius:8px; padding:9px; font-size:13px; font-weight:600; cursor:pointer; }
  .err { color:#ef4444; font-size:12px; margin-top:10px; min-height:16px; }
</style>
</head>
<body>
<form class="box" onsubmit="login(event)">
  <h1>VMware Kapazitätsplanung</h1>
  <p>Anmeldung mit Active-Directory-Konto</p>
  <label>Benutzername</label>
  <input id="u" autocomplete="username" placeholder="vorname.nachname" autofocus>
  <label>Passwort</label>
  <input id="p" type="password" autocomplete="current-password">
  <button>Anmelden</button>
  <div class="err" id="e"></div>
  <p style="margin:14px 0 0;text-align:center">Version __VERSION__</p>
</form>
<script>
async function login(ev) {
  ev.preventDefault();
  const e = document.getElementById("e");
  e.textContent = "";
  try {
    const r = await fetch("api/login", { method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ username: document.getElementById("u").value.trim(),
                             password: document.getElementById("p").value }) });
    if (r.ok) { location.reload(); return; }
    e.textContent = (await r.json()).error || "Anmeldung fehlgeschlagen.";
  } catch (x) { e.textContent = "Server nicht erreichbar."; }
}
</script>
</body>
</html>
"""

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VMware Kapazitätsübersicht pro Cluster</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --text:#e2e8f0;
          --muted:#94a3b8; --ok:#22c55e; --warn:#f59e0b; --crit:#ef4444;
          --accent:#38bdf8; --res:#818cf8; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--text);
         font:14px/1.5 "Segoe UI",system-ui,sans-serif; padding:24px; }
  h1 { font-size:22px; margin-bottom:4px; }
  .sub { color:var(--muted); margin-bottom:20px; }
  .tablewrap { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow-x:auto; }
  .kt { width:100%; border-collapse:collapse; font-size:13px; margin:0; }
  .kt th, .kt td { padding:9px 14px; border-bottom:1px solid var(--line); }
  .kt thead th { color:var(--muted); font-size:12px; background:#0b1220; }
  .kt thead th.sortable { cursor:pointer; user-select:none; white-space:nowrap; }
  .kt thead th.sortable:hover { color:var(--text); }
  .kt thead th .sarr { opacity:.85; font-size:10px; margin-left:2px; }
  .kt tbody tr:hover td { background:#26334a; }
  .kt tbody tr:last-child td { border-bottom:none; }
  .kt .free { font-weight:600; }
  .trtotal td { background:linear-gradient(135deg,#1e293b,#16233b); font-weight:600; }
  .barcol { width:130px; min-width:110px; }
  .bar.mini { height:8px; }
  .hovercard { position:fixed; z-index:20; width:480px; max-width:92vw; display:none;
               max-height:82vh; overflow:auto; border-radius:12px;
               box-shadow:0 14px 44px rgba(0,0,0,.55); }
  .hovercard .card { border-color:#3b5479; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px; }
  .card h2 { font-size:17px; margin-bottom:2px; color:var(--accent); }
  .meta { color:var(--muted); font-size:12px; margin-bottom:12px; }
  .metric { margin-bottom:14px; }
  .metric .row { display:flex; justify-content:space-between; font-size:13px; margin-bottom:4px; }
  .bar { height:10px; background:#0b1220; border-radius:5px; overflow:hidden; display:flex; }
  .bar i { display:block; height:100%; }
  .bar .r { background:repeating-linear-gradient(45deg,var(--res),var(--res) 4px,#4f46e5 4px,#4f46e5 8px); }
  .free { font-weight:600; }
  .kpis { display:flex; gap:14px; margin-top:10px; flex-wrap:wrap; }
  .kpi { background:#0b1220; border-radius:8px; padding:8px 12px; font-size:12px; color:var(--muted); }
  .kpi b { display:block; font-size:16px; color:var(--text); }
  details { margin-top:12px; }
  summary { cursor:pointer; color:var(--muted); font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:12px; margin-top:8px; }
  th,td { text-align:left; padding:4px 6px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; }
  td.num, th.num { text-align:right; }
  .off { color:var(--muted); font-style:italic; }
  .total { background:linear-gradient(135deg,#1e293b,#16233b); border-color:#3b5479; }
  .toolbar { display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap; align-items:center; }
  .filterbox { background:#0b1220; border:1px solid var(--line); color:var(--text);
               border-radius:8px; padding:6px 12px; font-size:12px; width:220px; }
  .filterbox:focus { outline:none; border-color:var(--accent); }
  .tabs { display:inline-flex; background:#0b1220; border:1px solid var(--line);
          border-radius:10px; padding:3px; gap:3px; margin-bottom:16px; }
  .tab { padding:6px 14px; font-size:13px; color:var(--muted); cursor:pointer; border-radius:8px; }
  .tab.active { background:var(--card); color:var(--text); }
  .cl { color:var(--accent); cursor:pointer; }
  .cl:hover { text-decoration:underline; }
  .st { font-size:11px; padding:2px 8px; border-radius:10px; white-space:nowrap; }
  .st.ok { background:rgba(34,197,94,.15); color:var(--ok); }
  .st.pend { background:rgba(245,158,11,.15); color:var(--warn); }
  .st.prog { background:rgba(56,189,248,.15); color:#38bdf8; cursor:help; }
  .st.rej { background:rgba(239,68,68,.15); color:var(--crit); }
  .st.canc { background:rgba(148,163,184,.18); color:var(--muted); cursor:help; }
  .rid { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px;
         color:var(--muted); white-space:nowrap; }
  .btn.approve { border-color:var(--ok); color:var(--ok); }
  .btn.approve:hover { background:rgba(34,197,94,.15); }
  .btn.danger { border-color:var(--crit); color:var(--crit); font-weight:600; }
  .btn.danger:hover { background:rgba(239,68,68,.15); }
  .hc-close { position:absolute; top:10px; right:12px; z-index:1;
              background:none; border:none; color:var(--muted); cursor:pointer; font-size:14px; }
  .hc-close:hover { color:var(--text); }
  .foot { margin-top:28px; text-align:center; color:var(--muted); font-size:11px; }
  .sechead { font-size:13px; color:var(--muted); margin-bottom:8px; }
  .btn { background:#0b1220; border:1px solid var(--line); color:var(--text);
         border-radius:8px; padding:6px 12px; font-size:12px; cursor:pointer; }
  .btn:hover { border-color:var(--accent); }
  .resbox { margin-top:14px; border-top:1px solid var(--line); padding-top:10px; }
  .resbox h3 { font-size:13px; color:var(--res); margin-bottom:6px; }
  .resform { display:grid; grid-template-columns:2fr 110px 70px 80px auto; gap:6px; margin-top:8px; }
  .resform input { background:#0b1220; border:1px solid var(--line); color:var(--text);
                   border-radius:6px; padding:5px 8px; font-size:12px; width:100%; }
  .resform input:focus { outline:none; border-color:var(--res); }
  .del { background:none; border:none; color:var(--crit); cursor:pointer; font-size:13px; }
  .edit { background:none; border:none; color:var(--accent); cursor:pointer; font-size:13px; }
  .err { color:var(--crit); font-size:12px; margin-top:4px; display:none; }
  .btn.primary { background:var(--res); color:#0b1220; border-color:var(--res); font-weight:600; }
  .modal-bg { position:fixed; inset:0; background:rgba(0,0,0,.65); display:none;
              align-items:center; justify-content:center; z-index:10; }
  .modal-bg.open { display:flex; }
  .modal { background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:22px; width:440px; max-width:92vw; }
  .modal h2 { color:var(--res); font-size:16px; margin-bottom:8px; }
  .modal label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }
  .modal input, .modal select, .modal textarea { width:100%; box-sizing:border-box;
           background:#0b1220; border:1px solid var(--line); color:var(--text);
           border-radius:6px; padding:7px 9px; font-size:13px; font-family:inherit; resize:vertical; }
  .modal input:focus, .modal select:focus, .modal textarea:focus { outline:none; border-color:var(--res); }
  .modal .hint { font-size:12px; color:var(--accent); margin-top:10px; }
  .modal .actions { display:flex; justify-content:flex-end; gap:8px; margin-top:16px; }
</style>
</head>
<body>
<h1>Kapazitätsübersicht pro Cluster</h1>
<div class="sub">Quelle: VMware Aria Operations · CPU-Überprovisionierung: Faktor __FACTOR__ (physische Cores) · RAM 1:1 · alle VMs inkl. powered-off · „frei" berücksichtigt genehmigte Reservierungen__FAILNOTE__ · Stand: <span id="stand">__DATE__</span><br>
Klick auf den Clusternamen zeigt Details und Reservierungen. __RESNOTE__</div>
<div class="toolbar">
  <input class="filterbox" id="filter" type="search" placeholder="Cluster filtern …" oninput="render()">
  <button class="btn primary" id="newReqBtn" onclick="openModal()">+ Neue Kapazitätsanfrage</button>
  <button class="btn" id="refreshBtn" onclick="refreshData()">⟳ Jetzt aktualisieren</button>
  <span id="refreshStatus" style="font-size:12px;color:var(--muted)"></span>
  <span id="timer" style="font-size:12px;color:var(--muted);margin-left:auto"></span>
  <button class="btn" onclick="exportRes()">Reservierungen exportieren (JSON)</button>
  <label class="btn" id="importBtn">Reservierungen importieren (JSON)<input type="file" accept=".json" hidden onchange="importRes(event)"></label>
  <span id="userbox" style="font-size:12px;color:var(--muted)"></span>
  <button class="btn" id="logoutBtn" style="display:none" onclick="logout()">Abmelden</button>
</div>
<div class="modal-bg" id="modalBg" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <h2>Neue Kapazitätsanfrage</h2>
    <label>Ziel-Cluster</label>
    <select id="mCluster" onchange="modalHint()"></select>
    <label>Bezeichnung / Projekt</label>
    <input id="mName" placeholder="z. B. SAP-Erweiterung Q4">
    <label>Change-Nummer (CHB… oder CHI…)</label>
    <input id="mChange" placeholder="z. B. CHB0012345">
    <label>vCPU</label>
    <input id="mCpu" type="number" min="0" step="1" placeholder="0">
    <label>RAM (GB)</label>
    <input id="mRam" type="number" min="0" step="1" placeholder="0">
    <label>Storage (GB)</label>
    <input id="mStorage" type="number" min="0" step="1" placeholder="0">
    <div class="hint" id="mHint"></div>
    <div class="hint" id="mValid" style="color:var(--muted)"></div>
    <div class="err" id="mErr"></div>
    <div class="actions">
      <button class="btn" onclick="closeModal()">Abbrechen</button>
      <button class="btn primary" onclick="submitModal()">Beantragen</button>
    </div>
  </div>
</div>
<div class="modal-bg" id="cmtBg" onclick="if(event.target===this)cmtCancel()">
  <div class="modal">
    <h2 id="cmtTitle">Kommentar</h2>
    <div class="hint" id="cmtMsg" style="color:var(--muted);margin-bottom:10px"></div>
    <label>Kommentar (optional)</label>
    <textarea id="cmtInput" maxlength="64" rows="3" oninput="cmtCount()"
              placeholder="kurze Begründung, max. 64 Zeichen"></textarea>
    <div class="hint" id="cmtCnt" style="color:var(--muted);text-align:right"></div>
    <div class="actions">
      <button class="btn" onclick="cmtCancel()">Abbrechen</button>
      <button class="btn primary" id="cmtOk" onclick="cmtConfirm()">OK</button>
    </div>
  </div>
</div>
<div class="tabs">
  <span class="tab active" id="tabKapa" onclick="setView('kapa')">Kapazität</span>
  <span class="tab" id="tabRes" onclick="setView('res')">Reservierungen</span>
  <span class="tab" id="tabApp" onclick="setView('app')">Genehmigungen</span>
  <span class="tab" id="tabAdm" onclick="setView('adm')">Verwaltung</span>
  <span class="tab" id="tabLog" onclick="setView('log')">Log</span>
</div>
<div class="tablewrap" id="kapaView">
<table class="kt" id="ktable">
  <thead><tr><th>Cluster</th><th class="num">Hosts</th><th class="num">VMs</th>
    <th class="num">vCPU frei</th><th class="barcol">vCPU-Auslastung</th>
    <th class="num">RAM frei (GB)</th><th class="barcol">RAM-Auslastung</th>
    <th class="num">Storage frei (GB)</th><th class="barcol">Storage-Auslastung</th>
    <th class="num">Res.</th></tr></thead>
  <tbody id="ktbody"></tbody>
</table>
</div>
<div class="tablewrap" id="resView" style="display:none">
<table class="kt" id="rtable">
  <thead><tr><th>ID</th><th>Anfrage / Projekt</th><th>Cluster</th><th>Change</th><th class="num">vCPU</th>
    <th class="num">RAM (GB)</th><th class="num">Storage (GB)</th><th>von</th><th>Abteilung</th><th>gilt ab</th><th>gültig bis</th><th>Status</th><th id="thDec">entschieden von</th><th>Kommentar</th><th class="nosort"></th></tr></thead>
  <tbody id="rtbody"></tbody>
</table>
</div>
<div class="tablewrap" id="appView" style="display:none">
<table class="kt" id="atable">
  <thead><tr><th>ID</th><th>Anfrage / Projekt</th><th>Cluster</th><th>Change</th><th class="num">vCPU</th>
    <th class="num">RAM (GB)</th><th class="num">Storage (GB)</th>
    <th class="num" title="Frei im Ziel-Cluster nach genehmigten Reservierungen">Cluster frei vCPU</th>
    <th class="num" title="Frei im Ziel-Cluster nach genehmigten Reservierungen">Cluster frei RAM</th>
    <th>von</th><th>Abteilung</th><th>beantragt am</th><th>Fortschritt</th><th class="nosort">Aktion</th></tr></thead>
  <tbody id="atbody"></tbody>
</table>
</div>
<div id="admView" style="display:none">
<div class="sechead">Benutzer und Rollen</div>
<div class="tablewrap">
<table class="kt" id="mtable">
  <thead><tr><th>AD-Benutzer</th><th>Rolle</th><th>Abteilung / Team</th><th class="nosort">Aktion</th></tr></thead>
  <tbody id="mtbody"></tbody>
</table>
</div>
<div class="sechead" style="margin-top:20px">Rollen-Bezeichnungen</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Die angezeigten Namen der Rollen sind frei wählbar. Die Rechte bleiben an der
  internen Rolle (linke Spalte) gebunden und ändern sich dadurch nicht.</div>
<div class="tablewrap">
<table class="kt" id="rntable">
  <thead><tr><th style="width:160px">Interne Rolle</th><th>Angezeigte Bezeichnung</th></tr></thead>
  <tbody id="rnbody"></tbody>
</table>
</div>
<div class="sechead" style="margin-top:20px">Genehmigungs-Teams (Prüfreihenfolge)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Anträge durchlaufen die Teams von oben nach unten. Erst wenn alle Teams
  freigegeben haben, ist ein Antrag genehmigt. Ohne Teams gilt einstufig
  (Admin genehmigt direkt). Reviewer werden oben ihrem Team zugewiesen.</div>
<div class="tablewrap">
<table class="kt" id="tmtable">
  <thead><tr><th style="width:60px">Stufe</th><th>Team</th><th style="width:220px">Aktion</th></tr></thead>
  <tbody id="tmbody"></tbody>
</table>
</div>
<div class="sechead" style="margin-top:20px">API-Tokens für externe Anwendungen (nur lesend, Endpunkte unter /api/v1/)</div>
<div class="tablewrap">
<table class="kt" id="ttable">
  <thead><tr><th>Anwendung</th><th>Token-Anfang</th><th>erstellt</th><th>von</th>
    <th>zuletzt benutzt</th><th class="nosort">Aktion</th></tr></thead>
  <tbody id="ttbody"></tbody>
</table>
</div>
<div class="tablewrap" id="newToken" style="display:none;margin-top:10px;padding:14px">
  <div style="font-size:12px;color:var(--warn);margin-bottom:6px">
    Neues API-Token – wird nur EINMAL angezeigt, jetzt kopieren:</div>
  <code id="newTokenVal" style="font-size:13px;user-select:all;word-break:break-all"></code>
  <button class="btn" style="margin-left:10px"
          onclick="document.getElementById('newToken').style.display='none'">Ausblenden</button>
</div>
</div>
<div class="tablewrap" id="logView" style="display:none">
<table class="kt" id="ltable">
  <thead><tr><th>Zeit</th><th>Benutzer</th><th>Aktion</th><th>Details</th></tr></thead>
  <tbody id="ltbody"></tbody>
</table>
</div>
<div class="hovercard" id="hovercard"></div>
<div class="foot">VMware Kapazitätsplanung · Version __VERSION__</div>
<script>
let CLUSTERS = __DATA__;
const FACTOR = __FACTOR__;
const SERVE = __SERVE__;
const TTL = __TTL__;
const ME = __USERINFO__;   // {user, role} bei aktivierter AD-Anmeldung, sonst null
let TEAMS = __TEAMS__;      // Genehmigungs-Teams in Prüfreihenfolge (leer = einstufig); auf der Verwaltungsseite pflegbar
const LS_KEY = "aria_kapa_reservierungen";

// ---- Rollen ----
const ROLE = ME ? ME.role : "admin";          // ohne AD-Anmeldung: Vollzugriff
const IS_ADMIN = ROLE === "admin";
const IS_REVIEWER = ROLE === "reviewer";
const CAN_REQUEST = IS_ADMIN || ROLE === "anforderer";
// Rollen-Bezeichnungen sind frei wählbar (Verwaltung); Schlüssel bleiben fest.
let ROLE_NAMES = __ROLENAMES__;
const ROLE_ORDER = ["anforderer", "reviewer", "admin", "auditor"];
// Löschen von Reservierungsanfragen ist deaktiviert – stattdessen Storno.
function canDel(r) { return false; }
// Storno: Admin, jemand aus derselben Abteilung oder der Anforderer selbst
// darf eine noch offene/genehmigte Anfrage zurückziehen (bleibt als „storniert“
// in der Historie). Bereits abgelehnte/stornierte lassen sich nicht stornieren.
function canCancel(r) {
  if (r.rejected || r.cancelled || r.foreign) return false;
  if (IS_ADMIN) return true;
  const dept = ME && ME.abteilung;
  return !!(dept && r.abteilung === dept) || !!(ME && r.von === ME.user);
}
// ---- Mehrstufiger Genehmigungsprozess ----
function stageOf(r) { return (r.approvals || []).length; }
function currentTeam(r) {
  if (!TEAMS.length) return null;
  const n = stageOf(r);
  return n < TEAMS.length ? TEAMS[n] : null;
}
// Darf der/die Angemeldete den Antrag in seiner aktuellen Stufe entscheiden?
function canDecide(r) {
  if (IS_ADMIN) return true;
  if (IS_REVIEWER && TEAMS.length && ME) return ME.abteilung === currentTeam(r);
  return false;
}
async function logout() {
  try { await fetch("api/logout", { method: "POST" }); } catch (e) {}
  location.reload();
}

// ---- Reservierungen (Serve-Modus: zentral auf dem Server, sonst localStorage) ----
let RES = [];
if (!SERVE) {
  try { RES = JSON.parse(localStorage.getItem(LS_KEY)) || []; } catch (e) {}
  if (TTL > 0) {
    const cutoff = Date.now() - TTL * 864e5;
    RES = RES.filter(r => !r.created || new Date(r.created).getTime() > cutoff);
  }
}
function saveLocal() { try { localStorage.setItem(LS_KEY, JSON.stringify(RES)); } catch (e) {} }
async function apiRes(method, path, body) {
  const r = await fetch("api/reservations" + (path || ""), {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function setRes(list) { if (Array.isArray(list)) { RES = list; render(); } }
function resFail() { alert("Reservierungen konnten nicht auf dem Server gespeichert werden."); }
// Nur genehmigte Reservierungen zählen gegen die Kapazität
function resFor(cl) { return RES.filter(r => r.cluster === cl && r.approved && !r.cancelled); }
function allFor(cl) { return RES.filter(r => r.cluster === cl); }
function fmtDate(iso) {
  if (!iso) return "–";
  const p = String(iso).slice(0, 10).split("-");
  return p.length === 3 ? p[2] + "." + p[1] + "." + p[0] : iso;
}
function validUntil(r) {
  if (!r.created) return "";
  const d = new Date(r.created + "T12:00:00");
  d.setDate(d.getDate() + (TTL > 0 ? TTL - 1 : 30));
  return d.toISOString().slice(0, 10);
}
function approvalsText(r) {
  // Liste der bisherigen Freigaben; ohne Namen, falls serverseitig gestrippt
  return (r.approvals || []).map(a =>
    a.team + (a.by ? " – " + a.by : "") + " (" + fmtDate(a.on) + ")").join("\n");
}
function stBadge(r) {
  const cmt = r.comment ? " · Kommentar: " + esc(r.comment) : "";
  if (r.rejected) {
    const stufe = r.rejected_team ? " in Stufe „" + esc(r.rejected_team) + "“" : "";
    return `<span class="st rej" title="abgelehnt${r.rejected_by ? " von " + esc(r.rejected_by) : ""}${stufe} am ${fmtDate(r.rejected_on)}${cmt}">abgelehnt</span>`;
  }
  if (r.cancelled)
    return `<span class="st canc" title="storniert${r.cancelled_by ? " von " + esc(r.cancelled_by) : ""} am ${fmtDate(r.cancelled_on)}${cmt}">storniert</span>`;
  if (r.approved)
    return `<span class="st ok" title="genehmigt${r.approved_by ? " von " + esc(r.approved_by) : ""} am ${fmtDate(r.approved_on)}${cmt}">genehmigt</span>`;
  if (TEAMS.length && stageOf(r) > 0) {
    // teilweise freigegeben -> in Prüfung; Mouseover zeigt, wer schon freigab
    const done = approvalsText(r);
    const wartet = currentTeam(r) ? "\nwartet auf: " + currentTeam(r) : "";
    const tip = "Bereits freigegeben:\n" + esc(done) + esc(wartet);
    return `<span class="st prog" title="${tip}">in Prüfung (${stageOf(r)}/${TEAMS.length})</span>`;
  }
  const wartet = TEAMS.length ? ' title="wartet auf: ' + esc(TEAMS[0]) + '"' : "";
  return `<span class="st pend"${wartet}>beantragt</span>`;
}
function isPend(r) { return !r.approved && !r.rejected && !r.cancelled; }
function sumCpu(rv) { return rv.reduce((s,r)=>s+(r.vcpu||0),0); }
function sumRam(rv) { return Math.round(rv.reduce((s,r)=>s+(r.ram_gb||0),0)*10)/10; }

function freeAfter(c) {
  const rv = resFor(c.name);
  return { cpu: c.vcpuFree - sumCpu(rv),
           ram: Math.round((c.ramFree - sumRam(rv)) * 10) / 10,
           stor: Math.round(((c.storageFree || 0) - sumStorage(rv)) * 10) / 10,
           hasStor: (c.storageCap || 0) > 0 };
}

function createRes(c, name, change, vcpu, ram, storage, errEl) {
  errEl.style.display = "none";
  if (!name || (vcpu <= 0 && ram <= 0 && storage <= 0)) {
    errEl.textContent = "Bitte Bezeichnung sowie vCPU, RAM und/oder Storage angeben.";
    errEl.style.display = "block"; return false;
  }
  const ch = String(change || "").toUpperCase().replace(/\s+/g, "");
  if (!/^CH[BI][A-Z0-9-]{3,20}$/.test(ch)) {
    errEl.textContent = "Bitte gültige Change-Nummer angeben (beginnt mit CHB oder CHI).";
    errEl.style.display = "block"; return false;
  }
  const f = freeAfter(c);
  const over = vcpu > f.cpu || ram > f.ram || (f.hasStor && storage > f.stor);
  if (over &&
      !confirm("Achtung: Die Reservierung überschreitet die freie Kapazität dieses Clusters " +
               "(frei: " + f.cpu + " vCPU / " + f.ram + " GB RAM" +
               (f.hasStor ? " / " + f.stor + " GB Storage" : "") +
               "). Trotzdem beantragen?")) return false;
  const item = { cluster: c.name, name: name, change: ch, vcpu: vcpu,
                 ram_gb: ram, storage_gb: storage };
  if (SERVE) {
    apiRes("POST", "", item).then(setRes).catch(resFail);
  } else {
    item.id = Date.now() + "-" + Math.random().toString(36).slice(2,7);
    item.created = new Date().toISOString().slice(0,10);
    item.approvals = [];
    item.approved = false;
    RES.push(item); saveLocal(); render();
  }
  return true;
}

// ---- Kommentar-Dialog (schön statt prompt) ----
let _cmtResolve = null;
function askComment(opts) {
  return new Promise(resolve => {
    _cmtResolve = resolve;
    document.getElementById("cmtTitle").textContent = opts.title || "Kommentar";
    document.getElementById("cmtMsg").textContent = opts.message || "";
    const ok = document.getElementById("cmtOk");
    ok.textContent = opts.okLabel || "OK";
    ok.className = "btn primary" + (opts.okClass ? " " + opts.okClass : "");
    const inp = document.getElementById("cmtInput");
    inp.value = "";
    cmtCount();
    document.getElementById("cmtBg").classList.add("open");
    setTimeout(() => inp.focus(), 30);
  });
}
function cmtCount() {
  const inp = document.getElementById("cmtInput");
  document.getElementById("cmtCnt").textContent = inp.value.length + " / 64 Zeichen";
}
function cmtClose(val) {
  document.getElementById("cmtBg").classList.remove("open");
  const r = _cmtResolve; _cmtResolve = null;
  if (r) r(val);
}
function cmtConfirm() { cmtClose(document.getElementById("cmtInput").value.trim().slice(0, 64)); }
function cmtCancel() { cmtClose(null); }

function rejectRes(id) {
  const r = RES.find(x => x.id === id);
  askComment({ title: "Antrag ablehnen", okLabel: "✕ Ablehnen", okClass: "danger",
    message: "„" + ((r && r.name) || "?") + "“ – die Ablehnung bleibt " +
             (TTL > 0 ? TTL : 31) + " Tage in der Historie sichtbar." }).then(c => {
    if (c === null) return;
    if (SERVE) apiRes("POST", "/" + encodeURIComponent(id) + "/reject",
                      { comment: c }).then(setRes).catch(resFail);
    else if (r) {
      r.rejected = true;
      r.rejected_on = new Date().toISOString().slice(0, 10);
      if (c) r.comment = c;
      saveLocal(); render();
    }
  });
}

function approveRes(id) {
  const r = RES.find(x => x.id === id);
  const team = r ? currentTeam(r) : null;
  askComment({ title: team ? "Freigeben (" + team + ")" : "Antrag genehmigen",
    okLabel: "✓ Bestätigen", message: "„" + ((r && r.name) || "?") + "“" }).then(c => {
    if (c === null) return;
    if (SERVE) apiRes("POST", "/" + encodeURIComponent(id) + "/approve",
                      { comment: c }).then(setRes).catch(resFail);
    else if (r) {
      r.approved = true;
      if (c) r.comment = c;
      saveLocal(); render();
    }
  });
}

function addRes(idx) {
  const c = CLUSTERS[idx];
  const g = s => document.getElementById("f" + idx + s);
  createRes(c, g("n").value.trim(), g("ch").value, parseInt(g("c").value) || 0,
            parseInt(g("r").value) || 0, parseInt(g("s").value) || 0, g("e"));
}
function delRes(id) {
  if (SERVE) apiRes("DELETE", "/" + encodeURIComponent(id)).then(setRes).catch(resFail);
  else { RES = RES.filter(r => r.id !== id); saveLocal(); render(); }
}
function cancelRes(id) {
  const r = RES.find(x => x.id === id);
  askComment({ title: "Anfrage stornieren", okLabel: "⦸ Stornieren", okClass: "danger",
    message: "„" + ((r && r.name) || "?") + "“ bleibt als „storniert“ in der " +
             "Historie und zählt nicht mehr gegen die Kapazität." }).then(c => {
    if (c === null) return;
    if (SERVE) apiRes("POST", "/" + encodeURIComponent(id) + "/cancel",
                      { comment: c }).then(setRes).catch(resFail);
    else if (r) {
      r.cancelled = true;
      r.cancelled_on = new Date().toISOString().slice(0, 10);
      if (c) r.comment = c;
      saveLocal(); render();
    }
  });
}

// ---- Dialog "Neue Kapazitätsanfrage" ----
function openModal(prefIdx) {
  const sel = document.getElementById("mCluster");
  sel.innerHTML = CLUSTERS.map((c, i) =>
    `<option value="${i}" ${prefIdx === i ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  ["mName", "mChange", "mCpu", "mRam", "mStorage"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("mErr").style.display = "none";
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById("mValid").textContent =
    "Gilt ab heute (" + fmtDate(today) + ") bis " +
    fmtDate(validUntil({created: today})) + " · wirksam erst nach Genehmigung.";
  modalHint();
  document.getElementById("modalBg").classList.add("open");
  document.getElementById("mName").focus();
}
function closeModal() { document.getElementById("modalBg").classList.remove("open"); }
function modalHint() {
  const c = CLUSTERS[+document.getElementById("mCluster").value];
  if (!c) return;
  const f = freeAfter(c);
  document.getElementById("mHint").textContent =
    "Frei nach bestehenden Reservierungen: " + fmt(f.cpu) + " vCPU / " + fmt(f.ram) + " GB RAM"
    + (f.hasStor ? " / " + fmt(f.stor) + " GB Storage" : "");
}
function submitModal() {
  const c = CLUSTERS[+document.getElementById("mCluster").value];
  const v = id => document.getElementById(id).value;
  const ok = createRes(c, v("mName").trim(), v("mChange"),
                       parseInt(v("mCpu")) || 0, parseInt(v("mRam")) || 0,
                       parseInt(v("mStorage")) || 0,
                       document.getElementById("mErr"));
  if (ok) closeModal();
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeModal(); cmtCancel(); hideCard(); }
  else if (e.key === "Enter" && (e.ctrlKey || e.metaKey) &&
           document.getElementById("cmtBg").classList.contains("open")) cmtConfirm();
});

function exportRes() {
  const blob = new Blob([JSON.stringify(RES, null, 2)], {type:"application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "kapa_reservierungen.json";
  a.click();
}
function importRes(ev) {
  const f = ev.target.files[0]; if (!f) return;
  f.text().then(t => {
    const d = JSON.parse(t);
    if (!Array.isArray(d)) { alert("Ungültige Datei."); return; }
    if (SERVE) apiRes("PUT", "", d).then(setRes).catch(resFail);
    else { RES = d; saveLocal(); render(); }
  }).catch(() => alert("Datei konnte nicht gelesen werden."));
  ev.target.value = "";
}

// ---- Darstellung ----
function pct(v, cap) { return cap > 0 ? Math.min(100, v / cap * 100) : 0; }
function color(p) { return p < 70 ? "var(--ok)" : p < 90 ? "var(--warn)" : "var(--crit)"; }
function fmt(n) { return n.toLocaleString("de-DE"); }
function esc(s) { return String(s).replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

function metric(label, used, resv, cap, unit) {
  const pu = pct(used, cap);
  const pr = pct(used + resv, cap) - pu;
  const free = Math.round((cap - used - resv) * 10) / 10;
  const c = color(pu + pr);
  return `<div class="metric">
    <div class="row"><span>${label}</span>
      <span>${fmt(used)} belegt${resv ? " + " + fmt(resv) + " reserviert" : ""} / ${fmt(cap)} ${unit} ·
      <span class="free" style="color:${c}">${fmt(free)} ${unit} frei</span></span></div>
    <div class="bar"><i style="width:${pu}%;background:${color(pu)}"></i><i class="r" style="width:${pr}%"></i></div>
  </div>`;
}

function card(c, idx, isTotal) {
  const clRes = isTotal ? RES.filter(r => !TOTAL || TOTAL._names.has(r.cluster)) : allFor(c.name);
  const rv = clRes.filter(r => r.approved && !r.cancelled);
  const rvCpu = sumCpu(rv), rvRam = sumRam(rv), rvStor = sumStorage(rv);
  const hasStor = (c.storageCap || 0) > 0;
  const vmRows = (c.vms || []).sort((a,b)=>b.vcpu-a.vcpu).map(v =>
    `<tr class="${v.on?'':'off'}"><td>${esc(v.name)}${v.on?'':' (aus)'}</td>
     <td class="num">${v.vcpu}</td><td class="num">${fmt(v.ram_gb)}</td></tr>`).join("");
  const hostRows = (c.hosts || []).map(h =>
    `<tr><td>${esc(h.name)}</td><td class="num">${h.cores}</td><td class="num">${fmt(h.ram_gb)}</td></tr>`).join("");
  const resRows = clRes.map(r =>
    `<tr><td>${esc(r.name)}${r.change ? ' <span style="color:var(--muted)">' + esc(r.change) + '</span>' : ''}${isTotal ? ' <span style="color:var(--muted)">(' + esc(r.cluster) + ')</span>' : ''}</td>
     <td class="num">${r.vcpu}</td><td class="num">${fmt(r.ram_gb)}</td><td class="num">${fmt(r.storage_gb || 0)}</td>
     <td>${fmtDate(validUntil(r))}</td><td>${stBadge(r)}</td>
     <td>${canCancel(r) ? `<button class="del" title="Anfrage stornieren" onclick="cancelRes('${esc(r.id)}')">⦸ Storno</button>` : ""}</td></tr>`).join("");
  const resTable = clRes.length ?
    `<table><tr><th>Anfrage</th><th class="num">vCPU</th><th class="num">RAM (GB)</th><th class="num">Storage (GB)</th><th>gültig bis</th><th>Status</th><th></th></tr>${resRows}</table>`
    : `<div style="color:var(--muted);font-size:12px">Keine Reservierungen.</div>`;
  const spare = (c.spareCores || c.spareRamGb) ?
    ` · Ausfallreserve (N+1): ${fmt(c.spareCores)} Cores / ${fmt(c.spareRamGb)} GB abgezogen` : "";
  return `<div class="card ${isTotal?'total':''}">
    <h2>${esc(c.name)}</h2>
    <div class="meta">${c.hostCount} Hosts · ${fmt(c.cores)} nutzbare Cores · ${c.vmCount} VMs${c.vmOff?` (davon ${c.vmOff} aus)`:''} · ${rv.length} genehmigt${clRes.filter(isPend).length?` / ${clRes.filter(isPend).length} beantragt`:''}${clRes.filter(r=>r.rejected).length?` / ${clRes.filter(r=>r.rejected).length} abgelehnt`:''}${spare}</div>
    ${metric("vCPU (Cores × " + FACTOR + ")", c.vcpuUsed, rvCpu, c.vcpuCap, "vCPU")}
    ${metric("RAM", c.ramUsed, rvRam, c.ramCap, "GB")}
    ${hasStor ? metric("Storage", c.storageUsed, rvStor, c.storageCap, "GB") : ""}
    <div class="kpis">
      <div class="kpi">frei nach Reservierungen<b>${fmt(c.vcpuFree - rvCpu)} vCPU / ${fmt(Math.round((c.ramFree - rvRam)*10)/10)} GB${hasStor ? " / " + fmt(Math.round(((c.storageFree||0) - rvStor)*10)/10) + " GB Storage" : ""}</b></div>
      <div class="kpi">reserviert<b>${fmt(rvCpu)} vCPU / ${fmt(rvRam)} GB${hasStor || rvStor ? " / " + fmt(rvStor) + " GB Storage" : ""}</b></div>
      <div class="kpi">Ø VM<b>${c.vmCount?Math.round(c.vcpuUsed/c.vmCount*10)/10:0} vCPU / ${c.vmCount?Math.round(c.ramUsed/c.vmCount):0} GB</b></div>
    </div>
    <div class="resbox">
      <h3>Kapazitätsreservierungen</h3>
      ${resTable}
      ${isTotal || !CAN_REQUEST ? "" : `
      <div class="resform">
        <input id="f${idx}n" placeholder="Bezeichnung / Projekt">
        <input id="f${idx}ch" placeholder="CHB/CHI-Nr.">
        <input id="f${idx}c" type="number" min="0" step="1" placeholder="vCPU">
        <input id="f${idx}r" type="number" min="0" step="1" placeholder="RAM GB">
        <input id="f${idx}s" type="number" min="0" step="1" placeholder="Storage GB">
        <button class="btn" onclick="addRes(${idx})">+ Beantragen</button>
      </div>
      <div class="err" id="f${idx}e"></div>`}
    </div>
    ${isTotal ? "" : `
    <details><summary>Hosts anzeigen</summary>
      <table><tr><th>Host</th><th class="num">Cores</th><th class="num">RAM (GB)</th></tr>${hostRows}</table>
    </details>
    <details><summary>VMs anzeigen</summary>
      <table><tr><th>VM</th><th class="num">vCPU</th><th class="num">RAM (GB)</th></tr>${vmRows}</table>
    </details>`}
  </div>`;
}

// ---- Tabellenansicht mit Hover-Details ----
let TOTAL = null;

function filteredIdx() {
  const q = (document.getElementById("filter").value || "").trim().toLowerCase();
  return CLUSTERS.map((c, i) => i)
                 .filter(i => !q || CLUSTERS[i].name.toLowerCase().includes(q));
}

function miniBar(used, resv, cap) {
  const pu = pct(used, cap), pr = pct(used + resv, cap) - pu;
  return `<div class="bar mini"><i style="width:${pu}%;background:${color(pu + pr)}"></i><i class="r" style="width:${pr}%"></i></div>`;
}

function row(c, idx, isTotal) {
  const clRes = isTotal ? RES.filter(r => TOTAL._names.has(r.cluster)) : allFor(c.name);
  const rv = clRes.filter(r => r.approved && !r.cancelled);
  const pend = clRes.filter(isPend).length;
  const rvCpu = sumCpu(rv), rvRam = sumRam(rv), rvStor = sumStorage(rv);
  const fCpu = c.vcpuFree - rvCpu;
  const fRam = Math.round((c.ramFree - rvRam) * 10) / 10;
  const cCpu = color(pct(c.vcpuUsed + rvCpu, c.vcpuCap));
  const cRam = color(pct(c.ramUsed + rvRam, c.ramCap));
  const hasStor = (c.storageCap || 0) > 0;
  const fStor = Math.round(((c.storageFree || 0) - rvStor) * 10) / 10;
  const cStor = color(pct((c.storageUsed || 0) + rvStor, c.storageCap || 0));
  return `<tr class="${isTotal ? 'trtotal' : ''}">
    <td class="cl" title="Details anzeigen" onclick="toggleCard(${idx},this)">${esc(c.name)}</td>
    <td class="num">${fmt(c.hostCount)}</td>
    <td class="num">${fmt(c.vmCount)}</td>
    <td class="num free" style="color:${cCpu}">${fmt(fCpu)}</td>
    <td class="barcol">${miniBar(c.vcpuUsed, rvCpu, c.vcpuCap)}</td>
    <td class="num free" style="color:${cRam}">${fmt(fRam)}</td>
    <td class="barcol">${miniBar(c.ramUsed, rvRam, c.ramCap)}</td>
    <td class="num free" style="color:${hasStor ? cStor : 'var(--muted)'}" title="${hasStor ? '' : 'keine Storage-Daten aus Aria'}">${hasStor ? fmt(fStor) : '–'}</td>
    <td class="barcol">${hasStor ? miniBar(c.storageUsed || 0, rvStor, c.storageCap) : ''}</td>
    <td class="num">${rv.length || "–"}${pend ? ` <span class="st pend">+${pend}</span>` : ""}</td></tr>`;
}

// ---- Ansichten: Kapazität / Reservierungen / Genehmigungen / Verwaltung ----
// endsWith statt ===, damit die Routen auch hinter einem Proxy-Unterpfad
// (z. B. https://host/capa/reservierungen) funktionieren
let VIEW = (location.pathname.endsWith("/reservierungen") || location.hash === "#reservierungen") ? "res"
         : (location.pathname.endsWith("/genehmigungen") || location.hash === "#genehmigungen") ? "app"
         : (location.pathname.endsWith("/verwaltung") || location.hash === "#verwaltung") ? "adm"
         : (location.pathname.endsWith("/log") || location.hash === "#log") ? "log"
         : "kapa";
if ((VIEW === "adm" || VIEW === "log") && !IS_ADMIN) VIEW = "kapa";

function setView(v) {
  VIEW = v;
  const tabs = { kapa: "tabKapa", res: "tabRes", app: "tabApp", adm: "tabAdm", log: "tabLog" };
  const views = { kapa: "kapaView", res: "resView", app: "appView", adm: "admView", log: "logView" };
  for (const k in tabs) {
    document.getElementById(tabs[k]).classList.toggle("active", v === k);
    document.getElementById(views[k]).style.display = v === k ? "" : "none";
  }
  document.getElementById("filter").placeholder =
    v === "kapa" ? "Cluster filtern …" : v === "adm" ? "Benutzer filtern …"
    : v === "log" ? "Log filtern …" : "Reservierungen filtern …";
  try {
    history.replaceState(null, "",
      v === "res" ? "#reservierungen" : v === "app" ? "#genehmigungen"
      : v === "adm" ? "#verwaltung" : v === "log" ? "#log" : location.pathname);
  } catch (e) {}
  hideCard();
  if (v === "adm") { loadRoles(); loadTokens(); loadTeams(); loadRoleNames(); }
  if (v === "log") loadLog();
  render();
}

// ---- API-Tokens (Verwaltung) ----
let TOKENS = {};
async function apiTokens(method, path, body) {
  const r = await fetch("api/tokens" + (path || ""), {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadTokens() {
  apiTokens("GET").then(d => { TOKENS = d || {}; if (VIEW === "adm") render(); })
                  .catch(() => {});
}
function addToken() {
  const n = document.getElementById("tknName").value.trim();
  if (!n) return;
  apiTokens("POST", "", { name: n }).then(d => {
    TOKENS = d.tokens || {};
    document.getElementById("newTokenVal").textContent = d.token;
    document.getElementById("newToken").style.display = "";
    render();
  }).catch(() => alert("Token konnte nicht erstellt werden."));
}
function delToken(id) {
  const t = TOKENS[id];
  if (!confirm("API-Token „" + ((t && t.name) || id) + "“ widerrufen? " +
               "Die Anwendung verliert sofort den Zugriff.")) return;
  apiTokens("DELETE", "/" + encodeURIComponent(id))
    .then(d => { TOKENS = d; render(); })
    .catch(() => alert("Widerruf fehlgeschlagen."));
}
function renderTokenTable() {
  const rows = Object.keys(TOKENS)
    .sort((a, b) => (TOKENS[a].created || "").localeCompare(TOKENS[b].created || ""))
    .map(id => { const t = TOKENS[id];
      return `<tr><td>${esc(t.name)}</td>
       <td style="font-family:monospace">${esc(t.prefix || "")}</td>
       <td>${fmtDate(t.created)}</td><td>${esc(t.created_by || "–")}</td>
       <td>${t.last_used ? esc(String(t.last_used).replace("T", " ")) : "nie"}</td>
       <td><button class="del" onclick="delToken('${esc(id)}')">✕ Widerrufen</button></td></tr>`; })
    .join("");
  document.getElementById("ttbody").innerHTML =
    `<tr><td colspan="5"><input class="filterbox" style="width:100%" id="tknName"
       placeholder="Name der Anwendung, z. B. Grafana oder CMDB-Sync"
       onkeydown="if(event.key==='Enter')addToken()"></td>
     <td><button class="btn approve" onclick="addToken()">+ Token erzeugen</button></td></tr>` +
    (rows || `<tr><td colspan="6" style="color:var(--muted)">Keine API-Tokens vorhanden.</td></tr>`);
  reSort("ttable");
}

// ---- Audit-Log (nur Admins) ----
let LOGS = [];
function loadLog() {
  fetch("api/log").then(r => r.json())
    .then(d => { if (Array.isArray(d)) { LOGS = d; if (VIEW === "log") render(); } })
    .catch(() => {});
}
function renderLogTable() {
  const q = (document.getElementById("filter").value || "").trim().toLowerCase();
  const list = LOGS.filter(e => !q ||
    (e.user || "").toLowerCase().includes(q) ||
    (e.action || "").toLowerCase().includes(q) ||
    (e.detail || "").toLowerCase().includes(q));
  document.getElementById("ltbody").innerHTML = list.map(e =>
    `<tr><td style="white-space:nowrap">${esc((e.ts || "").replace("T", " "))}</td>
     <td>${esc(e.user || "–")}</td><td>${esc(e.action || "")}</td>
     <td>${esc(e.detail || "")}</td></tr>`).join("") ||
    `<tr><td colspan="4" style="color:var(--muted)">Keine Log-Einträge${q ? " für diesen Filter" : ""}.</td></tr>`;
  reSort("ltable");
}

// ---- Verwaltung: AD-Benutzer → Rollen ----
let ROLES = {};   // {benutzer: rolle}
async function apiRoles(method, path, body) {
  const r = await fetch("api/roles" + (path || ""), {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadRoles() {
  apiRoles("GET").then(d => { ROLES = d || {}; if (VIEW === "adm") render(); })
                 .catch(() => {});
}
function addRole() {
  const u = document.getElementById("admUser").value.trim().toLowerCase();
  const rl = document.getElementById("admRole").value;
  const ab = document.getElementById("admDept").value.trim();
  if (!u) return;
  apiRoles("POST", "", { user: u, role: rl, abteilung: ab })
    .then(d => { ROLES = d; render(); document.getElementById("admUser").value = "";
                 document.getElementById("admDept").value = ""; })
    .catch(() => alert("Speichern fehlgeschlagen."));
}
function delRole(u) {
  if (!confirm("Rollenzuweisung für „" + u + "“ entfernen?")) return;
  apiRoles("DELETE", "/" + encodeURIComponent(u))
    .then(d => { ROLES = d; if (EDIT_USER === u) EDIT_USER = null; render(); })
    .catch(() => alert("Löschen fehlgeschlagen."));
}
let EDIT_USER = null;   // Benutzer, dessen Zeile gerade bearbeitet wird
function editRole(u) { EDIT_USER = u; render(); document.getElementById("editDept").focus(); }
function cancelEditRole() { EDIT_USER = null; render(); }
function saveEditRole(u) {
  const rl = document.getElementById("editRole").value;
  const ab = document.getElementById("editDept").value.trim();
  apiRoles("POST", "", { user: u, role: rl, abteilung: ab })
    .then(d => { ROLES = d; EDIT_USER = null; render(); })
    .catch(() => alert("Speichern fehlgeschlagen."));
}
// ---- Genehmigungs-Teams (Prüfreihenfolge) ----
async function apiTeams(method, body) {
  const r = await fetch("api/teams", {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadTeams() {
  apiTeams("GET").then(d => { TEAMS = (d && d.teams) || []; if (VIEW === "adm") render(); })
                 .catch(() => {});
}
function putTeams(list) {
  apiTeams("PUT", list).then(d => { TEAMS = (d && d.teams) || []; render(); })
                       .catch(() => alert("Speichern der Teams fehlgeschlagen."));
}
function addTeam() {
  const el = document.getElementById("newTeam");
  const t = (el.value || "").trim();
  if (!t) return;
  if (TEAMS.includes(t)) { alert("Team „" + t + "“ existiert bereits."); return; }
  el.value = "";
  putTeams(TEAMS.concat([t]));
}
function delTeam(i) {
  if (!confirm("Team „" + TEAMS[i] + "“ aus dem Genehmigungsprozess entfernen?")) return;
  putTeams(TEAMS.filter((_, j) => j !== i));
}
function moveTeam(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= TEAMS.length) return;
  const t = TEAMS.slice();
  [t[i], t[j]] = [t[j], t[i]];
  putTeams(t);
}
let TEAM_EDIT = -1;   // Index des gerade umbenannten Teams
function editTeam(i) { TEAM_EDIT = i; render(); const el = document.getElementById("teamEdit"); if (el) { el.focus(); el.select(); } }
function cancelEditTeam() { TEAM_EDIT = -1; render(); }
function saveTeamRename(i) {
  const val = (document.getElementById("teamEdit").value || "").trim();
  const old = TEAMS[i];
  if (!val || val === old) { TEAM_EDIT = -1; render(); return; }
  fetch("api/teams/rename", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ old: old, new: val }) })
    .then(r => { if (!r.ok) throw new Error(); return r.json(); })
    .then(d => { if (d && d.teams) TEAMS = d.teams;
                 if (d && d.roles) ROLES = d.roles;
                 TEAM_EDIT = -1; render(); })
    .catch(() => alert("Umbenennen fehlgeschlagen (Name evtl. schon vergeben)."));
}
function renderTeams() {
  const rows = TEAMS.map((t, i) => {
    if (i === TEAM_EDIT) {
      return `<tr><td class="num">${i + 1}</td>
        <td><input class="filterbox" style="width:100%" id="teamEdit" value="${esc(t)}"
             onkeydown="if(event.key==='Enter')saveTeamRename(${i});if(event.key==='Escape')cancelEditTeam()"></td>
        <td><button class="btn approve" onclick="saveTeamRename(${i})">✓ Speichern</button>
            <button class="btn" onclick="cancelEditTeam()">Abbrechen</button></td></tr>`;
    }
    return `<tr><td class="num">${i + 1}</td><td>${esc(t)}</td>
     <td><button class="edit" title="nach oben" ${i === 0 ? "disabled" : ""} onclick="moveTeam(${i},-1)">↑</button>
         <button class="edit" title="nach unten" ${i === TEAMS.length - 1 ? "disabled" : ""} onclick="moveTeam(${i},1)">↓</button>
         <button class="edit" title="Team umbenennen" onclick="editTeam(${i})">✎ Umbenennen</button>
         <button class="del" title="Team entfernen" onclick="delTeam(${i})">✕ Entfernen</button></td></tr>`;
  }).join("");
  document.getElementById("tmbody").innerHTML =
    `<tr><td></td><td><input class="filterbox" style="width:100%" id="newTeam"
         placeholder="Neues Team, z. B. Team Betrieb"
         onkeydown="if(event.key==='Enter')addTeam()"></td>
     <td><button class="btn approve" onclick="addTeam()">+ Hinzufügen</button></td></tr>` +
    (rows || `<tr><td colspan="3" style="color:var(--muted)">Keine Teams – einstufig (Admin genehmigt direkt).</td></tr>`);
}

// ---- Rollen-Bezeichnungen frei umbenennen ----
async function apiRoleNames(method, body) {
  const r = await fetch("api/rolenames", {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadRoleNames() {
  apiRoleNames("GET").then(d => { if (d && d.rolenames) ROLE_NAMES = d.rolenames;
                                  if (VIEW === "adm") render(); }).catch(() => {});
}
function saveRoleNames() {
  const body = {};
  ROLE_ORDER.forEach(k => { body[k] = (document.getElementById("rn_" + k).value || "").trim(); });
  apiRoleNames("PUT", body).then(d => { if (d && d.rolenames) ROLE_NAMES = d.rolenames;
                                        render(); }).catch(() => alert("Speichern fehlgeschlagen."));
}
function renderRoleNames() {
  const rows = ROLE_ORDER.map(k =>
    `<tr><td style="color:var(--muted)">${esc(k)}</td>
     <td><input class="filterbox" style="width:100%" id="rn_${k}" value="${esc(ROLE_NAMES[k] || k)}"
          onkeydown="if(event.key==='Enter')saveRoleNames()"></td></tr>`).join("");
  document.getElementById("rnbody").innerHTML = rows +
    `<tr><td></td><td><button class="btn approve" onclick="saveRoleNames()">✓ Bezeichnungen speichern</button></td></tr>`;
}

// Rollen-Auswahl (Dropdown) und das rollenabhängige Feld (Team/Abteilung)
function roleSelect(id, sel, onchange) {
  return `<select id="${id}" class="filterbox" style="width:100%" onchange="${onchange}">
    ${ROLE_ORDER.map(x => `<option value="${x}" ${sel === x ? "selected" : ""}>${esc(ROLE_NAMES[x] || x)}</option>`).join("")}
  </select>`;
}
function roleField(id, role, val, u) {
  if (role === "reviewer")
    return `<select id="${id}" class="filterbox" style="width:100%">
      <option value="">${TEAMS.length ? "– Team wählen –" : "(erst Teams anlegen)"}</option>
      ${TEAMS.map(t => `<option value="${esc(t)}" ${val === t ? "selected" : ""}>${esc(t)}</option>`).join("")}
    </select>`;
  const enter = u ? ` onkeydown="if(event.key==='Enter')saveEditRole('${esc(u)}')"` : "";
  if (role === "anforderer")
    return `<input id="${id}" class="filterbox" style="width:100%" placeholder="Abteilung" value="${esc(val || "")}"${enter}>`;
  return `<input id="${id}" class="filterbox" style="width:100%" placeholder="– für diese Rolle nicht nötig" value="${esc(val || "")}" disabled>`;
}
function syncRoleField(pfx, u) {
  const roleEl = document.getElementById(pfx === "adm" ? "admRole" : "editRole");
  const valEl = document.getElementById(pfx === "adm" ? "admDept" : "editDept");
  const val = valEl ? valEl.value : "";
  document.getElementById(pfx === "adm" ? "admFieldCell" : "editFieldCell").innerHTML =
    roleField(pfx === "adm" ? "admDept" : "editDept", roleEl.value, val, pfx === "edit" ? u : null);
}
function renderAdmTable() {
  const q = (document.getElementById("filter").value || "").trim().toLowerCase();
  const users = Object.keys(ROLES).sort().filter(u =>
    !q || u.includes(q) || (ROLES[u].abteilung || "").toLowerCase().includes(q));
  const rows = users.map(u => {
    if (u === EDIT_USER) {
      const cur = ROLES[u] || {};
      return `<tr><td>${esc(u)}</td>
        <td>${roleSelect("editRole", cur.role, "syncRoleField('edit','" + esc(u) + "')")}</td>
        <td id="editFieldCell">${roleField("editDept", cur.role, cur.abteilung || "", u)}</td>
        <td><button class="btn approve" onclick="saveEditRole('${esc(u)}')">✓ Speichern</button>
            <button class="btn" onclick="cancelEditRole()">Abbrechen</button></td></tr>`;
    }
    const r = ROLES[u];
    const label = r.role === "reviewer" && r.abteilung ? "Team: " + r.abteilung
                : r.abteilung || "–";
    return `<tr><td>${esc(u)}</td><td>${esc(ROLE_NAMES[r.role] || r.role)}</td>
     <td>${esc(label)}</td>
     <td><button class="edit" title="Rolle/Team bearbeiten" onclick="editRole('${esc(u)}')">✎ Bearbeiten</button>
         <button class="del" title="Zuweisung entfernen" onclick="delRole('${esc(u)}')">✕ Löschen</button></td></tr>`;
  }).join("");
  const firstRole = ROLE_ORDER[0];
  document.getElementById("mtbody").innerHTML =
    `<tr><td><input class="filterbox" style="width:100%" id="admUser"
         placeholder="benutzer@firma.local oder vorname.nachname"></td>
     <td>${roleSelect("admRole", firstRole, "syncRoleField('adm')")}</td>
     <td id="admFieldCell">${roleField("admDept", firstRole, "", null)}</td>
     <td><button class="btn approve" onclick="addRole()">+ Zuweisen</button></td></tr>` +
    (rows || `<tr><td colspan="4" style="color:var(--muted)">Noch keine Rollen zugewiesen.</td></tr>`);
  reSort("mtable");
}

function filterRes(list) {
  const q = (document.getElementById("filter").value || "").trim().toLowerCase();
  return list.filter(r => !q ||
      (r.name || "").toLowerCase().includes(q) ||
      (r.cluster || "").toLowerCase().includes(q))
    .slice().sort((a, b) => (a.cluster || "").localeCompare(b.cluster || "") ||
                            (a.created || "").localeCompare(b.created || ""));
}

function sumStorage(rv) { return Math.round(rv.reduce((s,r)=>s+(r.storage_gb||0),0)*10)/10; }

function renderResTable() {
  const list = filterRes(RES);
  const appr = list.filter(r => r.approved && !r.cancelled);
  const showDec = ROLE !== "anforderer";
  const nCols = showDec ? 15 : 14;
  const rows = list.map(r =>
    `<tr><td class="rid" title="Eindeutige ID der Anfrage">${esc(r.id || "–")}</td>
     <td>${esc(r.name)}</td><td>${esc(r.cluster)}</td><td>${esc(r.change || "–")}</td>
     <td class="num">${fmt(r.vcpu || 0)}</td><td class="num">${fmt(r.ram_gb || 0)}</td><td class="num">${fmt(r.storage_gb || 0)}</td>
     <td>${esc(r.von || "–")}</td><td>${esc(r.abteilung || "–")}</td>
     <td>${fmtDate(r.created)}</td><td>${fmtDate(validUntil(r))}</td><td>${stBadge(r)}</td>
     ${showDec ? `<td>${esc(r.approved_by || r.rejected_by || r.cancelled_by || "–")}</td>` : ""}
     <td>${esc(r.comment || "–")}</td>
     <td>${canCancel(r) ? `<button class="del" title="Anfrage stornieren" onclick="cancelRes('${esc(r.id)}')">⦸ Storno</button>` : ""}</td></tr>`).join("");
  document.getElementById("rtbody").innerHTML =
    `<tr class="trtotal"><td></td><td>Summe genehmigt (${appr.length} von ${list.length})</td><td></td><td></td>
     <td class="num">${fmt(sumCpu(appr))}</td><td class="num">${fmt(sumRam(appr))}</td><td class="num">${fmt(sumStorage(appr))}</td>
     <td colspan="${nCols - 7}"></td></tr>` +
    (rows || `<tr><td colspan="${nCols}" style="color:var(--muted)">Keine Reservierungen.</td></tr>`);
  reSort("rtable");
}

function renderAppTable() {
  const list = filterRes(RES.filter(isPend));
  const action = r => {
    if (canDecide(r)) {
      const team = currentTeam(r);
      const lbl = team ? "✓ Freigeben (" + esc(team) + ")" : "✓ Genehmigen";
      return `<button class="btn approve" onclick="approveRes('${esc(r.id)}')">${lbl}</button>
       <button class="btn" style="color:var(--crit)" title="Ablehnen"
               onclick="rejectRes('${esc(r.id)}')">✕ Ablehnen</button>`;
    }
    const team = currentTeam(r);
    return '<span class="st pend">wartet auf ' + (team ? esc(team) : "Genehmigung") + '</span>';
  };
  const freeCells = r => {
    const c = CLUSTERS.find(x => x.name === r.cluster);
    if (!c) return '<td class="num" colspan="2" style="color:var(--muted)">Cluster unbekannt</td>';
    const f = freeAfter(c);   // frei nach genehmigten Reservierungen
    const cpuOk = (r.vcpu || 0) <= f.cpu, ramOk = (r.ram_gb || 0) <= f.ram;
    const cell = (v, ok, unit) =>
      `<td class="num free" style="color:${ok ? "var(--ok)" : "var(--crit)"}"
           title="${ok ? "Antrag passt" : "Antrag überschreitet die freie Kapazität"}">${fmt(v)}${ok ? "" : " ⚠"}</td>`;
    return cell(f.cpu, cpuOk) + cell(f.ram, ramOk);
  };
  const rows = list.map(r =>
    `<tr><td class="rid" title="Eindeutige ID der Anfrage">${esc(r.id || "–")}</td>
     <td>${esc(r.name)}</td><td>${esc(r.cluster)}</td><td>${esc(r.change || "–")}</td>
     <td class="num">${fmt(r.vcpu || 0)}</td><td class="num">${fmt(r.ram_gb || 0)}</td><td class="num">${fmt(r.storage_gb || 0)}</td>
     ${freeCells(r)}
     <td>${esc(r.von || "–")}</td><td>${esc(r.abteilung || "–")}</td>
     <td>${fmtDate(r.created)}</td><td>${stBadge(r)}</td>
     <td>${action(r)}</td></tr>`).join("");
  document.getElementById("atbody").innerHTML =
    rows || `<tr><td colspan="14" style="color:var(--muted)">Keine offenen Anträge – alles genehmigt.</td></tr>`;
  reSort("atable");
}

function render() {
  const pend = RES.filter(isPend).length;
  document.getElementById("tabApp").textContent = "Genehmigungen" + (pend ? " (" + pend + ")" : "");
  if (VIEW === "res") { renderResTable(); return; }
  if (VIEW === "app") { renderAppTable(); return; }
  if (VIEW === "adm") { renderAdmTable(); renderRoleNames(); renderTeams(); renderTokenTable(); return; }
  if (VIEW === "log") { renderLogTable(); return; }
  const idxs = filteredIdx();
  const vis = idxs.map(i => CLUSTERS[i]);
  TOTAL = {
    name: idxs.length === CLUSTERS.length ? "Gesamt (alle Cluster)" : "Gesamt (Filter)",
    hostCount: vis.reduce((s,c)=>s+c.hostCount,0),
    cores: vis.reduce((s,c)=>s+c.cores,0),
    vcpuCap: vis.reduce((s,c)=>s+c.vcpuCap,0),
    vcpuUsed: vis.reduce((s,c)=>s+c.vcpuUsed,0),
    ramCap: Math.round(vis.reduce((s,c)=>s+c.ramCap,0)*10)/10,
    ramUsed: Math.round(vis.reduce((s,c)=>s+c.ramUsed,0)*10)/10,
    storageCap: Math.round(vis.reduce((s,c)=>s+(c.storageCap||0),0)*10)/10,
    storageUsed: Math.round(vis.reduce((s,c)=>s+(c.storageUsed||0),0)*10)/10,
    vmCount: vis.reduce((s,c)=>s+c.vmCount,0),
    vmOff: vis.reduce((s,c)=>s+c.vmOff,0),
    spareCores: vis.reduce((s,c)=>s+(c.spareCores||0),0),
    spareRamGb: Math.round(vis.reduce((s,c)=>s+(c.spareRamGb||0),0)*10)/10,
    _names: new Set(vis.map(c => c.name)),
  };
  TOTAL.vcpuFree = TOTAL.vcpuCap - TOTAL.vcpuUsed;
  TOTAL.ramFree = Math.round((TOTAL.ramCap - TOTAL.ramUsed)*10)/10;
  TOTAL.storageFree = Math.round((TOTAL.storageCap - TOTAL.storageUsed)*10)/10;
  document.getElementById("ktbody").innerHTML =
    row(TOTAL, -1, true) +
    (idxs.length ? idxs.map(i => row(CLUSTERS[i], i, false)).join("")
                 : '<tr><td colspan="10" style="color:var(--muted)">Kein Cluster entspricht dem Filter.</td></tr>');
  reSort("ktable");
  if (hoverIdx !== null && hc.style.display === "block")
    hc.innerHTML = '<button class="hc-close" title="Schließen" onclick="hideCard()">✕</button>' +
                   card(hoverIdx === -1 ? TOTAL : CLUSTERS[hoverIdx], hoverIdx, hoverIdx === -1);
}

// ---- Detail-Popover (Klick auf Clusternamen) ----
let hoverIdx = null;
const hc = document.getElementById("hovercard");

function showCard(idx, rowEl) {
  hoverIdx = idx;
  hc.innerHTML = '<button class="hc-close" title="Schließen" onclick="hideCard()">✕</button>' +
                 card(idx === -1 ? TOTAL : CLUSTERS[idx], idx, idx === -1);
  hc.style.display = "block";
  const r = rowEl.getBoundingClientRect();
  let top = r.bottom + 6;
  if (top + hc.offsetHeight > innerHeight - 10)
    top = Math.max(10, innerHeight - hc.offsetHeight - 10);
  hc.style.top = top + "px";
  hc.style.left = Math.min(Math.max(10, r.left + 60), Math.max(10, innerWidth - hc.offsetWidth - 10)) + "px";
}
function toggleCard(idx, cell) {
  if (hoverIdx === idx && hc.style.display === "block") hideCard();
  else showCard(idx, cell.parentElement);
}
function hideCard() { hc.style.display = "none"; hoverIdx = null; }
document.addEventListener("click", e => {
  if (hc.style.display === "block" && !hc.contains(e.target) && !e.target.closest(".cl"))
    hideCard();
});

// ---- Rollenabhängige Sichtbarkeit ----
if (ME) {
  document.getElementById("userbox").textContent =
    ME.user + (ME.abteilung ? " · " + ME.abteilung : "") + " · " + (ROLE_NAMES[ROLE] || ROLE);
  document.getElementById("logoutBtn").style.display = "";
}
if (!CAN_REQUEST) document.getElementById("newReqBtn").style.display = "none";
if (!IS_ADMIN) document.getElementById("importBtn").style.display = "none";
if (!IS_ADMIN) document.getElementById("refreshBtn").style.display = "none";
if (!IS_ADMIN || !SERVE) document.getElementById("tabAdm").style.display = "none";
if (!IS_ADMIN || !SERVE) document.getElementById("tabLog").style.display = "none";
if (ROLE === "anforderer") {
  const th = document.getElementById("thDec");
  if (th) th.remove();   // Anforderer sehen nicht, wer entschieden hat
}

// ---- Sortierbare Tabellen (Klick auf die Spaltenüberschrift) ----
const SORT_CFG = { ktable:{pin:1}, rtable:{pin:1}, atable:{pin:0},
                   ltable:{pin:0}, mtable:{pin:1}, ttable:{pin:1} };
const sortState = {};
function cellVal(td) {
  let t = (td ? td.textContent : "").trim();
  if (!t || t === "–" || t === "—") return { n: null, s: "" };
  const dm = t.match(/^(\d{2})\.(\d{2})\.(\d{4})$/);       // Datum dd.mm.yyyy
  if (dm) return { n: Number(dm[3] + dm[2] + dm[1]), s: t };
  const cl = t.replace(/[⚠+]/g, "").trim();               // deutsche Zahl
  if (/^-?\d{1,3}(\.\d{3})*(,\d+)?$/.test(cl) || /^-?\d+(,\d+)?$/.test(cl)) {
    const v = parseFloat(cl.replace(/\./g, "").replace(",", "."));
    if (!isNaN(v)) return { n: v, s: t };
  }
  return { n: null, s: t.toLowerCase() };
}
function sortCmp(a, b, dir) {
  let r;
  if (a.n !== null && b.n !== null) r = a.n - b.n;
  else if (a.n !== null) r = -1;          // Zahlen vor Text
  else if (b.n !== null) r = 1;
  else r = a.s < b.s ? -1 : a.s > b.s ? 1 : 0;
  return dir === "desc" ? -r : r;
}
function applySort(id) {
  const st = sortState[id]; if (!st) return;
  const table = document.getElementById(id); if (!table) return;
  const tb = table.tBodies[0]; if (!tb) return;
  const pin = (SORT_CFG[id] || {}).pin || 0;
  const rows = Array.from(tb.rows);
  const pinned = rows.slice(0, pin);
  const rest = rows.slice(pin);
  const isPlace = r => r.cells.length === 1 && r.cells[0].hasAttribute("colspan");
  const data = rest.filter(r => !isPlace(r)), place = rest.filter(isPlace);
  if (data.length > 1)
    data.sort((x, y) => sortCmp(cellVal(x.cells[st.col]), cellVal(y.cells[st.col]), st.dir));
  [...pinned, ...data, ...place].forEach(r => tb.appendChild(r));
}
function markArrows(id) {
  const table = document.getElementById(id); if (!table || !table.tHead) return;
  const st = sortState[id];
  Array.from(table.tHead.rows[0].cells).forEach(th => {
    if (!th.classList.contains("sortable")) return;
    const base = th.getAttribute("data-base") || "";
    const active = st && st.col === th.cellIndex;
    th.innerHTML = base + (active ? '<span class="sarr">' + (st.dir === "asc" ? "▲" : "▼") + "</span>" : "");
  });
}
function sortBy(id, col) {
  const st = sortState[id];
  sortState[id] = { col: col, dir: (st && st.col === col && st.dir === "asc") ? "desc" : "asc" };
  markArrows(id); applySort(id);
}
function reSort(id) { if (sortState[id]) applySort(id); }
function initSortable() {
  Object.keys(SORT_CFG).forEach(id => {
    const table = document.getElementById(id); if (!table || !table.tHead) return;
    Array.from(table.tHead.rows[0].cells).forEach(th => {
      if (th.classList.contains("barcol") || th.classList.contains("nosort")) return;
      th.classList.add("sortable");
      th.setAttribute("data-base", th.textContent);
      th.addEventListener("click", () => sortBy(id, th.cellIndex));
    });
  });
}
initSortable();

setView(VIEW);
if (!SERVE) document.getElementById("refreshBtn").style.display = "none";

// ---- Live-Abruf & Auto-Update (nur im Serve-Modus) ----
let nextRefresh = null;   // Zeitpunkt (ms) der nächsten automatischen Aktualisierung

async function refreshData() {
  try { await fetch("api/refresh", { method: "POST" }); pollStatus(); }
  catch (e) { document.getElementById("refreshStatus").textContent = "Server nicht erreichbar."; }
}

async function pollStatus() {
  let s;
  try { s = await (await fetch("api/status")).json(); } catch (e) { return; }
  const st = document.getElementById("refreshStatus");
  document.getElementById("refreshBtn").disabled = !!s.refreshing;
  nextRefresh = (s.next != null) ? Date.now() + s.next * 1000 : null;
  if (s.refreshing) st.textContent = "Lade Daten aus Aria … " + (s.progress || "");
  else if (s.error) st.textContent = "Fehler beim letzten Abruf: " + s.error;
  else st.textContent = "";
  if (!s.refreshing && s.updated &&
      s.updated !== document.getElementById("stand").textContent) {
    try {
      const d = await (await fetch("api/data")).json();
      CLUSTERS = d.clusters || [];
      document.getElementById("stand").textContent = d.updated || "";
      render();
    } catch (e) {}
  }
}

function tickTimer() {
  const el = document.getElementById("timer");
  if (nextRefresh === null) { el.textContent = ""; return; }
  const s = Math.max(0, Math.round((nextRefresh - Date.now()) / 1000));
  el.textContent = "Auto-Update in " + Math.floor(s / 60) + ":" +
                   String(s % 60).padStart(2, "0") + " min";
}

// Einmalige Migration: alte localStorage-Reservierungen auf den Server übernehmen
function migrateLocalRes(serverList) {
  if (serverList.length || localStorage.getItem(LS_KEY + "_migriert")) return;
  let old = [];
  try { old = JSON.parse(localStorage.getItem(LS_KEY)) || []; } catch (e) {}
  if (!old.length) return;
  if (confirm(old.length + " Reservierung(en) aus dem Browser-Speicher gefunden " +
              "(alter Speicherort). Auf den Server übernehmen?\n" +
              "Sie erscheinen dann mit Status „beantragt“ und können unter " +
              "„Genehmigungen“ freigegeben werden.")) {
    apiRes("PUT", "", old).then(l => {
      setRes(l);
      localStorage.removeItem(LS_KEY);
    }).catch(resFail);
  }
  try { localStorage.setItem(LS_KEY + "_migriert", "1"); } catch (e) {}
}

if (SERVE) {
  apiRes("GET").then(l => { setRes(l); migrateLocalRes(l); }).catch(() => {});
  pollStatus();
  setInterval(pollStatus, 3000);
  setInterval(tickTimer, 1000);
}
</script>
</body>
</html>
"""


def json_for_html(obj):
    """JSON so kodieren, dass es sicher in ein <script>-Tag eingebettet werden
    kann. json.dumps escaped `<`, `>`, `&` und die Zeilentrenner U+2028/U+2029
    nicht – ohne diese Ersetzung könnte ein aus Aria stammender Name wie
    `</script>...` aus dem Script-Tag ausbrechen (Stored XSS)."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_html(clusters, cpu_factor, serve_mode=False, updated=None, res_ttl=31,
                failover_hosts=1, userinfo=None, teams=None, rolenames=None):
    valid_days = res_ttl - 1 if res_ttl > 0 else 30
    resnote = (f"Neue Reservierungen gelten ab dem Anlagetag für {valid_days} Tage, "
               "zählen erst nach Genehmigung gegen die Kapazität und werden "
               + (f"nach {res_ttl} Tagen automatisch entfernt. " if res_ttl > 0
                  else "nicht automatisch entfernt. "))
    resnote += ("Speicherung zentral auf dem Server." if serve_mode
                else "Speicherung lokal im Browser.")
    if teams:
        resnote += (" Genehmigung mehrstufig: " + " → ".join(teams)
                    + " (erst wenn alle freigegeben haben, ist der Antrag genehmigt).")
    if failover_hosts == 1:
        failnote = " · Ausfallreserve (N+1): größter Host je Cluster abgezogen"
    elif failover_hosts > 1:
        failnote = (f" · Ausfallreserve (N+{failover_hosts}): größte {failover_hosts} "
                    "Hosts je Cluster abgezogen")
    else:
        failnote = ""
    return (HTML_TEMPLATE
            .replace("__DATA__", json_for_html(clusters))
            .replace("__FACTOR__", str(cpu_factor))
            .replace("__SERVE__", "true" if serve_mode else "false")
            .replace("__TTL__", str(res_ttl))
            .replace("__USERINFO__", json_for_html(userinfo))
            .replace("__TEAMS__", json_for_html(teams or []))
            .replace("__ROLENAMES__", json_for_html(rolenames or DEFAULT_ROLE_NAMES))
            .replace("__RESNOTE__", resnote)
            .replace("__FAILNOTE__", failnote)
            .replace("__VERSION__", VERSION)
            .replace("__DATE__", updated or datetime.now().strftime("%d.%m.%Y %H:%M")))


def render_dashboard(clusters, cpu_factor, path, res_ttl=31, failover_hosts=1):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(clusters, cpu_factor, res_ttl=res_ttl,
                            failover_hosts=failover_hosts))

# ------------------------------------------------------------- Serve-Modus ---

def serve(args, password):
    import secrets
    import threading
    import time
    import uuid
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

    state = {"clusters": [], "updated": None, "refreshing": False,
             "progress": "", "error": None, "last": None}
    interval = max(0, args.refresh_interval)
    migrate_data_files(args.cache, args.res_file, args.roles_file)

    # ---- AD-Anmeldung, Sessions und Rollen ----
    auth_enabled = bool(args.ad_url)
    admin_seed = {u.strip().lower() for u in (args.admin_user or "").split(",") if u.strip()}
    sessions = {}                    # token -> {"user", "role", "exp"}
    session_ttl = 12 * 3600
    roles_lock = threading.Lock()
    VALID_ROLES = ("admin", "anforderer", "auditor", "reviewer")

    # Mehrstufiger Genehmigungsprozess: Team-Namen (in Prüfreihenfolge) werden
    # auf der Admin-Seite gepflegt und in data/kapa_teams.json abgelegt.
    # --approval-teams dient nur noch als Erstbefüllung, falls die Datei fehlt.
    # Leer = einstufig (Admin genehmigt direkt). In-place mutiert, damit die
    # Closures (current_team, approve/reject) immer den aktuellen Stand sehen.
    teams_lock = threading.Lock()

    def clean_teams(seq):
        out = []
        for t in seq:
            t = str(t or "").strip()
            if t and t not in out:
                out.append(t)
        return out

    def load_teams():
        if os.path.exists(args.teams_file):
            try:
                with open(args.teams_file, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, list):
                    return clean_teams(d)
            except Exception as e:
                print(f"Team-Datei unlesbar, starte ohne Teams: {e}",
                      file=sys.stderr)
            return []
        # Datei fehlt: einmalig aus --approval-teams befüllen (Migration)
        seed = clean_teams((args.approval_teams or "").split(","))
        if seed:
            try:
                ensure_dir(args.teams_file)
                with open(args.teams_file, "w", encoding="utf-8") as f:
                    json.dump(seed, f, ensure_ascii=False, indent=2)
            except OSError as e:
                print(f"Team-Datei nicht schreibbar: {e}", file=sys.stderr)
        return seed

    def save_teams():
        ensure_dir(args.teams_file)
        with open(args.teams_file, "w", encoding="utf-8") as f:
            json.dump(approval_teams, f, ensure_ascii=False, indent=2)

    approval_teams = load_teams()

    # ---- Frei wählbare Rollen-Bezeichnungen (Schlüssel bleiben fest) ----
    rolenames_lock = threading.Lock()

    def load_rolenames():
        d = dict(DEFAULT_ROLE_NAMES)
        if os.path.exists(args.rolenames_file):
            try:
                with open(args.rolenames_file, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for k in ROLE_KEYS:
                        v = raw.get(k)
                        if isinstance(v, str) and v.strip():
                            d[k] = v.strip()
            except Exception as e:
                print(f"Rollennamen-Datei unlesbar, nutze Standard: {e}",
                      file=sys.stderr)
        return d

    def save_rolenames():
        ensure_dir(args.rolenames_file)
        with open(args.rolenames_file, "w", encoding="utf-8") as f:
            json.dump(role_names, f, ensure_ascii=False, indent=2)

    role_names = load_rolenames()

    def current_team(r):
        """Team, das als Nächstes freigeben muss – None, wenn keine Teams
        konfiguriert sind oder alle Stufen bereits freigegeben haben."""
        if not approval_teams:
            return None
        n = len(r.get("approvals") or [])
        return approval_teams[n] if n < len(approval_teams) else None

    # ---- Brute-Force-Bremse für den Login ----
    # Pro Schlüssel (Benutzer bzw. Absender-IP) werden Fehlversuche gezählt;
    # ab LOGIN_MAX innerhalb von LOGIN_WINDOW Sekunden wird für die Restzeit
    # des Fensters gesperrt. Verhindert Password-Spraying gegen das AD und ein
    # versehentliches Aussperren echter AD-Konten.
    LOGIN_MAX = 5
    LOGIN_WINDOW = 300
    login_fails = {}                 # key -> [Zeitstempel, ...]
    login_lock = threading.Lock()

    def login_blocked(*keys):
        now = time.time()
        with login_lock:
            for k in keys:
                hits = [t for t in login_fails.get(k, []) if now - t < LOGIN_WINDOW]
                login_fails[k] = hits
                if len(hits) >= LOGIN_MAX:
                    return True
            return False

    def login_note_fail(*keys):
        now = time.time()
        with login_lock:
            for k in keys:
                hits = [t for t in login_fails.get(k, []) if now - t < LOGIN_WINDOW]
                hits.append(now)
                login_fails[k] = hits

    def login_reset(*keys):
        with login_lock:
            for k in keys:
                login_fails.pop(k, None)

    def load_roles():
        """Rollendatei: {benutzer: {role, abteilung}}; alte Form {benutzer: rolle}
        wird beim Laden migriert."""
        if os.path.exists(args.roles_file):
            try:
                with open(args.roles_file, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    out = {}
                    for k, v in d.items():
                        if isinstance(v, str) and v in VALID_ROLES:
                            out[str(k).lower()] = {"role": v, "abteilung": ""}
                        elif isinstance(v, dict) and v.get("role") in VALID_ROLES:
                            out[str(k).lower()] = {
                                "role": v["role"],
                                "abteilung": str(v.get("abteilung") or "")}
                    return out
            except Exception as e:
                print(f"Rollendatei unlesbar, starte leer: {e}", file=sys.stderr)
        return {}

    def save_roles():
        ensure_dir(args.roles_file)
        with open(args.roles_file, "w", encoding="utf-8") as f:
            json.dump(roles, f, ensure_ascii=False, indent=2, sort_keys=True)

    roles = load_roles()

    def normalize_user(name):
        name = str(name or "").strip().lower()
        if name and "@" not in name and "\\" not in name and args.ad_domain:
            name = name + "@" + args.ad_domain
        return name

    def role_entry(user):
        if user in admin_seed:
            return {"role": "admin", "abteilung": ""}
        return roles.get(user)

    if auth_enabled:
        if not args.ad_url.startswith("ldaps://"):
            print("WARNUNG: --ad-url ohne ldaps:// – Passwörter gehen unverschlüsselt "
                  "über das Netz.", file=sys.stderr)
        if not admin_seed and not roles:
            print("WARNUNG: Kein Admin definiert (--admin-user) und Rollendatei leer – "
                  "alle AD-Nutzer melden sich als Anforderer an, niemand kann "
                  "genehmigen oder die Verwaltung öffnen.", file=sys.stderr)

    # Zwischen-Cache der letzten Abfrage von Platte laden
    if os.path.exists(args.cache):
        try:
            with open(args.cache, encoding="utf-8") as f:
                c = json.load(f)
            state["clusters"] = c.get("clusters", [])
            state["updated"] = c.get("updated")
            state["last"] = os.path.getmtime(args.cache)
            print(f"Cache geladen: {args.cache} (Stand {state['updated']}, "
                  f"{len(state['clusters'])} Cluster)", file=sys.stderr)
        except Exception as e:
            print(f"Cache unlesbar, starte leer: {e}", file=sys.stderr)

    # ---- Reservierungen: serverseitige Datei, Ablauf nach --res-ttl-days ----
    res_lock = threading.Lock()

    def prune_res(lst):
        if args.res_ttl_days <= 0:
            return lst
        cutoff = (datetime.now() - timedelta(days=args.res_ttl_days)).date().isoformat()

        def ref_date(r):
            # Abgelehnte/stornierte bleiben ab dem Ereignisdatum in der Historie
            if r.get("rejected"):
                return str(r.get("rejected_on") or r.get("created") or "9999")
            if r.get("cancelled"):
                return str(r.get("cancelled_on") or r.get("created") or "9999")
            return str(r.get("created") or "9999")
        return [r for r in lst if ref_date(r) >= cutoff]

    def load_res():
        if os.path.exists(args.res_file):
            try:
                with open(args.res_file, encoding="utf-8") as f:
                    lst = json.load(f)
                if isinstance(lst, list):
                    for r in lst:
                        if isinstance(r, dict):
                            r.setdefault("id", uuid.uuid4().hex[:12])
                    print(f"Reservierungen geladen: {args.res_file} ({len(lst)})",
                          file=sys.stderr)
                    return prune_res(lst)
            except Exception as e:
                print(f"Reservierungsdatei unlesbar, starte leer: {e}", file=sys.stderr)
        return []

    def save_res():
        ensure_dir(args.res_file)
        with open(args.res_file, "w", encoding="utf-8") as f:
            json.dump(reservations, f, ensure_ascii=False, indent=2)

    reservations = load_res()

    # ---- API-Tokens für externe Anwendungen (nur lesend) ----
    tokens_lock = threading.Lock()

    def load_tokens():
        if os.path.exists(args.tokens_file):
            try:
                with open(args.tokens_file, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    return {k: v for k, v in d.items()
                            if isinstance(v, dict) and v.get("hash")}
            except Exception as e:
                print(f"Token-Datei unlesbar, starte leer: {e}", file=sys.stderr)
        return {}

    def save_tokens():
        ensure_dir(args.tokens_file)
        with open(args.tokens_file, "w", encoding="utf-8") as f:
            json.dump(tokens, f, ensure_ascii=False, indent=2)

    tokens = load_tokens()

    def token_list():
        """Tokenliste ohne Hashes (für die Verwaltung)."""
        return {tid: {k: v for k, v in t.items() if k != "hash"}
                for tid, t in tokens.items()}

    def res_status(r):
        if r.get("rejected"):
            return "abgelehnt"
        if r.get("cancelled"):
            return "storniert"
        if r.get("approved"):
            return "genehmigt"
        return "in Prüfung" if r.get("approvals") else "beantragt"

    def valid_until(r):
        if not r.get("created"):
            return ""
        try:
            d = (datetime.fromisoformat(str(r["created"]))
                 + timedelta(days=args.res_ttl_days - 1
                             if args.res_ttl_days > 0 else 30))
            return d.date().isoformat()
        except ValueError:
            return ""

    def res_csv(rows):
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["id", "name", "change", "cluster", "vcpu", "ram_gb",
                    "storage_gb", "von", "abteilung", "gilt_ab", "gueltig_bis",
                    "status", "entschieden_von", "freigaben", "kommentar"])
        for r in rows:
            freigaben = "; ".join(
                f"{a.get('team') or '?'}: {a.get('by') or '?'}"
                for a in (r.get("approvals") or []))
            w.writerow([r.get("id", ""), r.get("name", ""), r.get("change", ""),
                        r.get("cluster", ""), r.get("vcpu", 0),
                        r.get("ram_gb", 0), r.get("storage_gb", 0),
                        r.get("von", ""),
                        r.get("abteilung", ""), r.get("created", ""),
                        valid_until(r), res_status(r),
                        r.get("approved_by") or r.get("rejected_by") or "",
                        freigaben, r.get("comment", "")])
        return buf.getvalue()

    # ---- Audit-Log (JSONL, nur für Admins einsehbar) ----
    log_lock = threading.Lock()

    def audit(user, action, detail=""):
        entry = {"ts": datetime.now().isoformat(timespec="seconds"),
                 "user": user or "system", "action": action, "detail": detail}
        try:
            with log_lock:
                ensure_dir(args.log_file)
                with open(args.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"Audit-Log nicht schreibbar: {e}", file=sys.stderr)

    def read_log(limit=500):
        try:
            with log_lock, open(args.log_file, encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return list(reversed(out))   # neueste zuerst

    def res_detail(r):
        stor = r.get("storage_gb") or 0
        return (f"{r.get('name', '?')} [{r.get('id') or '?'}] "
                f"({r.get('change') or 'ohne Change'}, "
                f"{r.get('cluster', '?')}, {r.get('vcpu', 0)} vCPU / "
                f"{r.get('ram_gb', 0)} GB RAM"
                + (f" / {stor} GB Storage" if stor else "") + ")")

    def notify_mail(r, action, admin):
        """Report-Mail zu Genehmigung/Ablehnung im Hintergrund verschicken."""
        if not args.smtp_server:
            return

        def worker():
            try:
                send_mail(args,
                          f"Kapazitätsreservierung {action}: {r.get('name', '?')} "
                          f"({r.get('cluster', '?')})",
                          reservation_mail_body(r, action, admin, args.res_ttl_days),
                          extra_to=(r.get("von") or "",))
            except Exception as e:
                print(f"Mail-Versand fehlgeschlagen: {e}", file=sys.stderr)
        threading.Thread(target=worker, daemon=True).start()

    def visible_res(s):
        """Sichtbare Reservierungen je Rolle: Admin/Prüfung alles; Anforderer nur
        die eigene Abteilung – fremde genehmigte bleiben anonymisiert enthalten,
        damit die freie Kapazität stimmt."""
        if s["role"] in ("admin", "auditor", "reviewer"):
            return list(reservations)
        dept = s.get("abteilung") or ""
        out = []
        for r in reservations:
            mine = (dept and r.get("abteilung") == dept) or r.get("von") == s["user"]
            if mine:
                # Anforderer sehen nicht, WER entschieden hat – der Fortschritt
                # (welches Team schon freigegeben hat) bleibt jedoch sichtbar,
                # nur ohne Namen.
                d = {k: v for k, v in r.items()
                     if k not in ("approved_by", "rejected_by")}
                if isinstance(d.get("approvals"), list):
                    d["approvals"] = [{"team": a.get("team"), "on": a.get("on")}
                                      for a in d["approvals"]]
                out.append(d)
            elif r.get("approved") and not r.get("cancelled"):
                # bewusst ohne Name, von, Change, Kommentar; storniert zählt nicht
                out.append({"id": r.get("id"), "cluster": r.get("cluster"),
                            "name": "(andere Abteilung)", "vcpu": r.get("vcpu"),
                            "ram_gb": r.get("ram_gb"),
                            "storage_gb": r.get("storage_gb"),
                            "created": r.get("created"),
                            "approved": True, "foreign": True})
        return out

    def do_refresh():
        state.update(refreshing=True, error=None, progress="Verbinde mit Aria Operations ...")
        try:
            if args.sample:
                time.sleep(2)  # Demo: Ladezeit simulieren
                clusters = build_summary(sample_data(), args.cpu_factor, args.failover_hosts)
            else:
                api = AriaOps(args.url, args.user, password, args.auth_source,
                              verify_tls=not args.insecure)
                clusters = collect(api, args.cpu_factor,
                                   progress=lambda m: state.update(progress=m),
                                   failover_hosts=args.failover_hosts)
            state["clusters"] = clusters
            state["updated"] = datetime.now().strftime("%d.%m.%Y %H:%M")
            ensure_dir(args.cache)
            with open(args.cache, "w", encoding="utf-8") as f:
                json.dump({"updated": state["updated"], "clusters": clusters},
                          f, ensure_ascii=False)
        except Exception as e:
            state["error"] = str(e)
        finally:
            state["last"] = time.time()
            state["refreshing"] = False

    def scheduler():
        # Erster Start ohne Cache: sofort Daten holen
        if not state["clusters"] and not state["refreshing"]:
            do_refresh()
        while interval > 0:
            wait = (state["last"] or 0) + interval - time.time()
            if wait <= 0:
                if not state["refreshing"]:
                    do_refresh()
                time.sleep(1)
            else:
                time.sleep(min(wait, 10))

    def backup_loop():
        time.sleep(60)   # erst nach dem Anlauf (Cache/Migration abgeschlossen)
        while True:
            try:
                name = sftp_backup(args)
                print(f"Backup übertragen: {name} -> {args.backup_target}",
                      file=sys.stderr)
                audit(None, "Automatisches Backup", name)
                try:
                    n = backup_rotate(args)
                    if n:
                        audit(None, "Backup-Rotation",
                              f"{n} Archiv(e) älter als "
                              f"{args.backup_keep_days} Tage gelöscht")
                except Exception as e:
                    audit(None, "Backup-Rotation fehlgeschlagen", str(e))
            except Exception as e:
                print(f"Backup fehlgeschlagen: {e}", file=sys.stderr)
                audit(None, "Automatisches Backup fehlgeschlagen", str(e))
            if args.backup_interval <= 0:
                return
            time.sleep(args.backup_interval)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, ctype, code=200, headers=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            # Sicherheits-Header (Defense-in-depth): Clickjacking verhindern,
            # MIME-Sniffing abschalten, Skripte/Objekte auf same-origin begrenzen
            # (fängt zusätzlich einen etwaigen HTML-Injection-Ausbruch ab).
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200, headers=None):
            self._send(json.dumps(obj, ensure_ascii=False),
                       "application/json; charset=utf-8", code, headers)

        MAX_BODY = 2 * 1024 * 1024   # 2 MiB – reicht für große Reservierungs-Importe

        def _body(self):
            # Body-Größe begrenzen: /api/login ruft das VOR der Anmeldung auf,
            # ein riesiger Content-Length würde sonst den Prozess-Speicher fluten.
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > self.MAX_BODY:
                    return None
                return json.loads(self.rfile.read(n).decode() or "null")
            except Exception:
                return None

        # ---- Sitzungen / Berechtigungen ----
        def _cookie_token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "kapa_session":
                    return v
            return None

        def _session(self):
            if not auth_enabled:
                return {"user": None, "role": "admin", "abteilung": ""}  # ohne AD wie bisher
            s = sessions.get(self._cookie_token())
            if s and s["exp"] > time.time():
                s["exp"] = time.time() + session_ttl
                return s
            return None

        def _require(self, *allowed):
            s = self._session()
            if not s:
                self._json({"error": "Nicht angemeldet"}, 401)
                return None
            if allowed and s["role"] not in allowed:
                self._json({"error": "Keine Berechtigung für diese Aktion"}, 403)
                return None
            return s

        def _bearer(self):
            """API-Token aus dem Authorization-Header prüfen (nur lesend)."""
            h = self.headers.get("Authorization") or ""
            if not h.startswith("Bearer "):
                return None
            th = hashlib.sha256(h[7:].strip().encode()).hexdigest()
            now = datetime.now()
            with tokens_lock:
                for tid, t in tokens.items():
                    if t.get("hash") == th:
                        stale = str(t.get("last_used") or "")[:10] != now.date().isoformat()
                        t["last_used"] = now.isoformat(timespec="seconds")
                        if stale:
                            save_tokens()
                        return dict(t, id=tid)
            audit(None, "API-Zugriff abgewiesen", "ungültiges Bearer-Token")
            return None

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            route = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if route in ("/", "/index.html", "/reservierungen",
                         "/genehmigungen", "/verwaltung", "/log"):
                s = self._session()
                if auth_enabled and not s:
                    self._send(LOGIN_TEMPLATE.replace("__VERSION__", VERSION),
                               "text/html; charset=utf-8")
                    return
                userinfo = ({"user": s["user"], "role": s["role"],
                             "abteilung": s.get("abteilung") or ""}
                            if auth_enabled else None)
                self._send(render_html(state["clusters"], args.cpu_factor,
                                       serve_mode=True,
                                       updated=state["updated"] or
                                       "noch keine Daten – erster Abruf läuft ...",
                                       res_ttl=args.res_ttl_days,
                                       failover_hosts=args.failover_hosts,
                                       userinfo=userinfo, teams=approval_teams,
                                       rolenames=role_names),
                           "text/html; charset=utf-8")
            elif route == "/api/data":
                if not self._require():
                    return
                self._json({"updated": state["updated"], "clusters": state["clusters"]})
            elif route == "/api/status":
                if not self._require():
                    return
                nxt = None
                if interval > 0 and state["last"]:
                    nxt = max(0, int(state["last"] + interval - time.time()))
                self._json({"refreshing": state["refreshing"],
                            "progress": state["progress"], "error": state["error"],
                            "updated": state["updated"], "next": nxt})
            elif self.path == "/api/reservations":
                s = self._require()
                if not s:
                    return
                with res_lock:
                    reservations[:] = prune_res(reservations)
                    self._json(visible_res(s))
            elif route in ("/api/v1/reservations", "/api/v1/data", "/api/v1/status"):
                # Stabile v1-API für externe Anwendungen: Bearer-Token oder Session
                tok = self._bearer()
                s = None
                if not tok:
                    s = self._session()
                    if not s:
                        self._json({"error": "Bearer-Token oder Anmeldung "
                                             "erforderlich"}, 401)
                        return
                if route == "/api/v1/status":
                    nxt = None
                    if interval > 0 and state["last"]:
                        nxt = max(0, int(state["last"] + interval - time.time()))
                    self._json({"version": VERSION, "updated": state["updated"],
                                "refreshing": state["refreshing"], "next": nxt})
                elif route == "/api/v1/data":
                    self._json({"updated": state["updated"],
                                "clusters": state["clusters"]})
                else:
                    with res_lock:
                        reservations[:] = prune_res(reservations)
                        data = (visible_res(s) if s and not tok
                                else list(reservations))
                    for key in ("cluster", "abteilung"):
                        if key in query:
                            data = [r for r in data if r.get(key) == query[key][0]]
                    if "status" in query:
                        data = [r for r in data
                                if res_status(r) == query["status"][0]]
                    if query.get("format", [""])[0] == "csv":
                        self._send(res_csv(data), "text/csv; charset=utf-8")
                    else:
                        self._json(data)
            elif route == "/api/tokens":
                if not self._require("admin"):
                    return
                with tokens_lock:
                    self._json(token_list())
            elif route == "/api/roles":
                if not self._require("admin"):
                    return
                with roles_lock:
                    self._json(dict(roles))
            elif route == "/api/teams":
                if not self._require("admin"):
                    return
                with teams_lock:
                    self._json({"teams": list(approval_teams)})
            elif route == "/api/rolenames":
                if not self._require("admin"):
                    return
                with rolenames_lock:
                    self._json({"rolenames": dict(role_names)})
            elif route == "/api/log":
                if not self._require("admin"):
                    return
                self._json(read_log())
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/api/login":
                if not auth_enabled:
                    self.send_error(404)
                    return
                body = self._body() or {}
                user = normalize_user(body.get("username"))
                pw = str(body.get("password") or "")
                # Einheitliche Antwort für „kein Konto", „keine Rolle" und
                # „falsches Passwort" – verrät nicht, welche Konten berechtigt sind.
                # (Keine IP im Log: hinter dem nginx-Proxy wäre das immer 127.0.0.1.)
                bad = {"error": "Benutzername oder Passwort falsch."}
                if login_blocked(user):
                    audit(user, "Anmeldung gesperrt", "zu viele Fehlversuche")
                    self._json({"error": "Zu viele Fehlversuche – bitte einige "
                                         "Minuten warten und erneut versuchen."}, 429)
                    return
                if not user:
                    login_note_fail(user)
                    self._json(bad, 401)
                    return
                try:
                    ok = ldap_bind(args.ad_url, user, pw, insecure=args.ad_insecure)
                except Exception as e:
                    self._json({"error": f"Active Directory nicht erreichbar: {e}"}, 502)
                    return
                if not ok:
                    login_note_fail(user)
                    audit(user, "Anmeldung fehlgeschlagen", "falsches Passwort")
                    self._json(bad, 401)
                    return
                login_reset(user)
                # Rolle: explizit zugewiesen (Verwaltung), sonst Standard =
                # Anforderer. Ohne eigene Rolle darf man nur beantragen, nichts
                # freigeben.
                explicit = role_entry(user)
                entry = explicit or {"role": "anforderer", "abteilung": ""}
                audit(user, "Anmeldung", f"Rolle: {entry['role']}"
                      + ("" if explicit else " (Standard)")
                      + (f", Abteilung: {entry.get('abteilung')}"
                         if entry.get("abteilung") else ""))
                token = secrets.token_urlsafe(32)
                sessions[token] = {"user": user, "role": entry["role"],
                                   "abteilung": entry.get("abteilung") or "",
                                   "exp": time.time() + session_ttl}
                secure = "" if args.cookie_insecure else " Secure;"
                self._json({"user": user, "role": entry["role"],
                            "abteilung": entry.get("abteilung") or ""},
                           headers={"Set-Cookie": f"kapa_session={token}; "
                                    f"HttpOnly;{secure} SameSite=Lax; Path=/"})
            elif self.path == "/api/logout":
                old = sessions.pop(self._cookie_token(), None)
                if old:
                    audit(old["user"], "Abmeldung")
                secure = "" if args.cookie_insecure else " Secure;"
                self._json({"ok": True},
                           headers={"Set-Cookie": "kapa_session=; Max-Age=0; "
                                    f"HttpOnly;{secure} SameSite=Lax; Path=/"})
            elif self.path == "/api/refresh":
                if not self._require("admin"):
                    return
                if not state["refreshing"]:
                    audit(self._session()["user"], "Datenabruf aus Aria gestartet")
                    threading.Thread(target=do_refresh, daemon=True).start()
                self._json({"started": True}, 202)
            elif self.path == "/api/reservations":
                s = self._require("admin", "anforderer")
                if not s:
                    return
                item = self._body()
                if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                    self._json({"error": "Ungültige Reservierung"}, 400)
                    return
                change = re.sub(r"\s+", "", str(item.get("change") or "")).upper()
                if not re.fullmatch(r"CH[BI][A-Z0-9-]{3,20}", change):
                    self._json({"error": "Ungültige Change-Nummer "
                                         "(muss mit CHB oder CHI beginnen)"}, 400)
                    return
                try:
                    entry = {"id": uuid.uuid4().hex[:12],
                             "cluster": str(item.get("cluster") or ""),
                             "name": str(item.get("name")).strip(),
                             "change": change,
                             "vcpu": int(float(item.get("vcpu") or 0)),
                             "ram_gb": int(float(item.get("ram_gb") or 0)),
                             "storage_gb": int(float(item.get("storage_gb") or 0)),
                             "von": s["user"] or "",
                             "abteilung": s.get("abteilung") or "",
                             "created": datetime.now().date().isoformat(),
                             "approvals": [],
                             "approved": False}
                except (TypeError, ValueError):
                    self._json({"error": "Ungültige Zahlenwerte"}, 400)
                    return
                with res_lock:
                    reservations[:] = prune_res(reservations)
                    reservations.append(entry)
                    save_res()
                    self._json(visible_res(s))
                audit(s["user"], "Antrag erstellt", res_detail(entry))
            elif (self.path.startswith("/api/reservations/")
                    and self.path.endswith("/approve")):
                s = self._require("admin", "reviewer")
                if not s:
                    return
                rid = urllib.parse.unquote(
                    self.path[len("/api/reservations/"):-len("/approve")])
                comment = str((self._body() or {}).get("comment") or "").strip()[:64]
                notify = None
                action = None
                err = None
                with res_lock:
                    r = next((x for x in reservations
                              if x.get("id") == rid and not x.get("rejected")
                              and not x.get("approved")), None)
                    if r is None:
                        err = ("Antrag nicht gefunden oder bereits entschieden.", 404)
                    else:
                        team = current_team(r)
                        # Reviewer dürfen nur freigeben, wenn ihr Team an der Reihe ist
                        if s["role"] == "reviewer" and (
                                not approval_teams
                                or (s.get("abteilung") or "") != (team or "")):
                            err = ("Ihr Team ist für diesen Antrag gerade nicht "
                                   "an der Reihe.", 403)
                        else:
                            today = datetime.now().date().isoformat()
                            if approval_teams:
                                r.setdefault("approvals", []).append(
                                    {"team": team, "by": s["user"] or "",
                                     "on": today, "comment": comment})
                                if len(r["approvals"]) >= len(approval_teams):
                                    r["approved"] = True
                                    r["approved_on"] = today
                                    r["approved_by"] = s["user"] or ""
                                    if comment:
                                        r["comment"] = comment
                                    action = "genehmigt"
                                else:
                                    action = f"von {team} freigegeben"
                            else:
                                # einstufig (keine Teams konfiguriert)
                                r["approved"] = True
                                r["approved_on"] = today
                                r["approved_by"] = s["user"] or ""
                                if comment:
                                    r["comment"] = comment
                                action = "genehmigt"
                            notify = dict(r)
                            save_res()
                    resp = None if err else visible_res(s)
                if err:
                    self._json({"error": err[0]}, err[1])
                    return
                self._json(resp)
                if notify:
                    verb = ("Antrag genehmigt" if action == "genehmigt"
                            else "Antrag freigegeben")
                    audit(s["user"], verb, res_detail(notify) + f" – {action}"
                          + (f", Kommentar: {comment}" if comment else ""))
                    notify_mail(notify, action, s["user"] or "unbekannt")
            elif (self.path.startswith("/api/reservations/")
                    and self.path.endswith("/reject")):
                s = self._require("admin", "reviewer")
                if not s:
                    return
                rid = urllib.parse.unquote(
                    self.path[len("/api/reservations/"):-len("/reject")])
                comment = str((self._body() or {}).get("comment") or "").strip()[:64]
                notify = None
                err = None
                with res_lock:
                    r = next((x for x in reservations
                              if x.get("id") == rid and not x.get("approved")
                              and not x.get("rejected")), None)
                    if r is None:
                        err = ("Antrag nicht gefunden oder bereits entschieden.", 404)
                    else:
                        team = current_team(r)
                        if s["role"] == "reviewer" and (
                                not approval_teams
                                or (s.get("abteilung") or "") != (team or "")):
                            err = ("Ihr Team ist für diesen Antrag gerade nicht "
                                   "an der Reihe.", 403)
                        else:
                            r["rejected"] = True
                            r["rejected_on"] = datetime.now().date().isoformat()
                            r["rejected_by"] = s["user"] or ""
                            if team:
                                r["rejected_team"] = team
                            if comment:
                                r["comment"] = comment
                            notify = dict(r)
                            save_res()
                    resp = None if err else visible_res(s)
                if err:
                    self._json({"error": err[0]}, err[1])
                    return
                self._json(resp)
                if notify:
                    audit(s["user"], "Antrag abgelehnt", res_detail(notify)
                          + (f" (Stufe {notify.get('rejected_team')})"
                             if notify.get("rejected_team") else "")
                          + (f" – Kommentar: {comment}" if comment else ""))
                    notify_mail(notify, "abgelehnt", s["user"] or "unbekannt")
            elif (self.path.startswith("/api/reservations/")
                    and self.path.endswith("/cancel")):
                # Storno: jemand aus derselben Abteilung (oder der Anforderer
                # selbst bzw. ein Admin) zieht die Anfrage zurück. Sie bleibt als
                # „storniert" in der Historie und zählt nicht mehr gegen die
                # Kapazität.
                s = self._require("admin", "anforderer", "reviewer")
                if not s:
                    return
                rid = urllib.parse.unquote(
                    self.path[len("/api/reservations/"):-len("/cancel")])
                comment = str((self._body() or {}).get("comment") or "").strip()[:64]
                notify = None
                err = None
                with res_lock:
                    r = next((x for x in reservations
                              if x.get("id") == rid and not x.get("rejected")
                              and not x.get("cancelled")), None)
                    if r is None:
                        err = ("Antrag nicht gefunden oder bereits "
                               "abgeschlossen.", 404)
                    else:
                        dept = s.get("abteilung") or ""
                        same_dept = bool(dept) and r.get("abteilung") == dept
                        if not (s["role"] == "admin" or same_dept
                                or r.get("von") == s["user"]):
                            err = ("Nur die eigene Abteilung (oder ein Admin) "
                                   "darf diese Anfrage stornieren.", 403)
                        else:
                            r["cancelled"] = True
                            r["cancelled_on"] = datetime.now().date().isoformat()
                            r["cancelled_by"] = s["user"] or ""
                            if comment:
                                r["comment"] = comment
                            notify = dict(r)
                            save_res()
                    resp = None if err else visible_res(s)
                if err:
                    self._json({"error": err[0]}, err[1])
                    return
                self._json(resp)
                if notify:
                    audit(s["user"], "Antrag storniert", res_detail(notify)
                          + (f" – Kommentar: {comment}" if comment else ""))
                    notify_mail(notify, "storniert", s["user"] or "unbekannt")
            elif self.path == "/api/backup":
                if not self._require("admin"):
                    return
                try:
                    with res_lock:
                        name = sftp_backup(args)
                    audit(self._session()["user"], "Backup ausgelöst", name)
                    rotated = 0
                    try:
                        rotated = backup_rotate(args)
                        if rotated:
                            audit(self._session()["user"], "Backup-Rotation",
                                  f"{rotated} Archiv(e) gelöscht")
                    except Exception as e:
                        audit(self._session()["user"],
                              "Backup-Rotation fehlgeschlagen", str(e))
                    self._json({"ok": True, "backup": name, "rotated": rotated})
                except Exception as e:
                    audit(self._session()["user"], "Backup fehlgeschlagen", str(e))
                    self._json({"error": str(e)}, 502)
            elif self.path == "/api/tokens":
                s = self._require("admin")
                if not s:
                    return
                name = str((self._body() or {}).get("name") or "").strip()
                if not name:
                    self._json({"error": "Name der Anwendung erforderlich"}, 400)
                    return
                raw = "kapa_" + secrets.token_urlsafe(24)
                tid = uuid.uuid4().hex[:8]
                with tokens_lock:
                    tokens[tid] = {"name": name,
                                   "hash": hashlib.sha256(raw.encode()).hexdigest(),
                                   "prefix": raw[:11] + "…", "scope": "read",
                                   "created": datetime.now().date().isoformat(),
                                   "created_by": s["user"] or "",
                                   "last_used": ""}
                    save_tokens()
                audit(s["user"], "API-Token erstellt",
                      f"{name} ({raw[:11]}…, nur lesend)")
                self._json({"token": raw, "tokens": token_list()})
            elif self.path == "/api/roles":
                if not self._require("admin"):
                    return
                body = self._body() or {}
                user = normalize_user(body.get("user"))
                role = body.get("role")
                dept = str(body.get("abteilung") or "").strip()
                if not user or role not in VALID_ROLES:
                    self._json({"error": "Benutzer und gültige Rolle erforderlich"}, 400)
                    return
                with roles_lock:
                    roles[user] = {"role": role, "abteilung": dept}
                    save_roles()
                    self._json(dict(roles))
                audit(self._session()["user"], "Rolle zugewiesen",
                      f"{user} -> {role}" + (f" ({dept})" if dept else ""))
            elif self.path == "/api/teams/rename":
                s = self._require("admin")
                if not s:
                    return
                body = self._body() or {}
                old = str(body.get("old") or "").strip()
                new = str(body.get("new") or "").strip()[:60]
                if not old or not new:
                    self._json({"error": "alter und neuer Name erforderlich"}, 400)
                    return
                with teams_lock, roles_lock:
                    if old not in approval_teams:
                        self._json({"error": "Team nicht gefunden"}, 404)
                        return
                    if new != old and new in approval_teams:
                        self._json({"error": "Ein Team mit diesem Namen "
                                             "existiert bereits."}, 400)
                        return
                    approval_teams[approval_teams.index(old)] = new
                    save_teams()
                    # Zugewiesene Reviewer auf den neuen Team-Namen umziehen
                    moved = 0
                    for entry in roles.values():
                        if entry.get("role") == "reviewer" and entry.get("abteilung") == old:
                            entry["abteilung"] = new
                            moved += 1
                    if moved:
                        save_roles()
                    # Auch aktive Sessions aktualisieren (kein Neu-Login nötig)
                    for sess in sessions.values():
                        if sess.get("role") == "reviewer" and sess.get("abteilung") == old:
                            sess["abteilung"] = new
                    result_roles = dict(roles)
                audit(s["user"], "Team umbenannt",
                      f"„{old}“ → „{new}“" + (f" ({moved} Reviewer übernommen)" if moved else ""))
                self._json({"teams": list(approval_teams), "roles": result_roles})
            else:
                self.send_error(404)

        def do_PUT(self):
            if self.path == "/api/reservations":
                if not self._require("admin"):
                    return
                data = self._body()
                if not isinstance(data, list):
                    self._json({"error": "Liste erwartet"}, 400)
                    return
                cleaned = []
                for r in data:
                    if not isinstance(r, dict):
                        continue
                    r = dict(r)
                    r.setdefault("id", uuid.uuid4().hex[:12])
                    r.setdefault("created", datetime.now().date().isoformat())
                    r.setdefault("approved", False)
                    cleaned.append(r)
                with res_lock:
                    reservations[:] = prune_res(cleaned)
                    save_res()
                    self._json(list(reservations))
                audit(self._session()["user"], "Reservierungen importiert",
                      f"{len(cleaned)} Einträge (ersetzt Bestand)")
            elif self.path == "/api/teams":
                s = self._require("admin")
                if not s:
                    return
                body = self._body()
                if not isinstance(body, list):
                    self._json({"error": "Liste von Team-Namen erwartet"}, 400)
                    return
                new = clean_teams(body)
                with teams_lock:
                    approval_teams[:] = new   # in-place, damit Closures es sehen
                    save_teams()
                audit(s["user"], "Genehmigungs-Teams geändert",
                      " → ".join(new) if new else "(keine – einstufig)")
                self._json({"teams": list(approval_teams)})
            elif self.path == "/api/rolenames":
                s = self._require("admin")
                if not s:
                    return
                body = self._body()
                if not isinstance(body, dict):
                    self._json({"error": "Objekt mit Rollen-Bezeichnungen erwartet"}, 400)
                    return
                with rolenames_lock:
                    for k in ROLE_KEYS:
                        v = body.get(k)
                        if isinstance(v, str) and v.strip():
                            role_names[k] = v.strip()[:60]
                        else:
                            role_names[k] = DEFAULT_ROLE_NAMES[k]
                    save_rolenames()
                    result = dict(role_names)
                audit(s["user"], "Rollen-Bezeichnungen geändert",
                      ", ".join(f"{k}={result[k]}" for k in ROLE_KEYS))
                self._json({"rolenames": result})
            else:
                self.send_error(404)

        def do_DELETE(self):
            if self.path.startswith("/api/reservations/"):
                # Löschen von Reservierungsanfragen ist deaktiviert – Anträge
                # laufen über Genehmigung/Ablehnung und den automatischen Ablauf.
                self._json({"error": "Das Löschen von Reservierungen ist "
                                     "deaktiviert."}, 403)
            elif self.path.startswith("/api/tokens/"):
                s = self._require("admin")
                if not s:
                    return
                tid = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
                with tokens_lock:
                    removed = tokens.pop(tid, None)
                    save_tokens()
                    self._json(token_list())
                if removed:
                    audit(s["user"], "API-Token widerrufen", removed.get("name", tid))
            elif self.path.startswith("/api/roles/"):
                s = self._require("admin")
                if not s:
                    return
                user = urllib.parse.unquote(self.path.rsplit("/", 1)[1]).lower()
                with roles_lock:
                    removed = roles.pop(user, None)
                    save_roles()
                    self._json(dict(roles))
                if removed:
                    audit(s["user"], "Rolle entfernt",
                          f"{user} (war {removed.get('role')})")
            else:
                self.send_error(404)

        def log_message(self, *a):
            pass

    threading.Thread(target=scheduler, daemon=True).start()
    if args.backup_target:
        threading.Thread(target=backup_loop, daemon=True).start()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Dashboard läuft: http://localhost:{args.port}  (Strg+C zum Beenden)"
          + (f" · Auto-Refresh alle {interval // 60} min" if interval else "")
          + (f" · AD-Anmeldung: {args.ad_url}" if auth_enabled else " · ohne Anmeldung"),
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.", file=sys.stderr)

# ------------------------------------------------------------------- main ----

def apply_config_file(ap, path):
    """INI-Datei (Sektion [kapa]) als neue Defaults in den Parser übernehmen.
    Schlüssel entsprechen den Optionsnamen (Bindestrich oder Unterstrich)."""
    import configparser
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        with open(path, encoding="utf-8") as f:
            cp.read_file(f)
    except (OSError, configparser.Error) as e:
        ap.error(f"Konfigurationsdatei {path}: {e}")
    sec = cp["kapa"] if cp.has_section("kapa") else cp[cp.default_section]
    by_dest = {a.dest: a for a in ap._actions}
    defaults = {}
    for key, raw in sec.items():
        dest = key.strip().lower().replace("-", "_")
        action = by_dest.get(dest)
        if action is None or dest in ("help", "version", "config"):
            ap.error(f"Unbekannte Option '{key}' in {path}")
        raw = raw.strip()
        if isinstance(action, argparse._StoreTrueAction):
            defaults[dest] = raw.lower() in ("1", "true", "yes", "ja", "on")
        elif action.type is int:
            try:
                defaults[dest] = int(raw)
            except ValueError:
                ap.error(f"Option '{key}' in {path}: Ganzzahl erwartet, '{raw}' erhalten")
        else:
            defaults[dest] = raw
    ap.set_defaults(**defaults)


def main():
    ap = argparse.ArgumentParser(description="Aria Ops Kapazitätsauswertung pro Cluster")
    ap.add_argument("--version", action="version", version=f"aria_kapa {VERSION}")
    ap.add_argument("--url", help="Basis-URL, z.B. https://aria-ops.firma.de")
    ap.add_argument("--user", help="Benutzername")
    ap.add_argument("--password",
                    help="Passwort (alternativ --password-file oder Umgebungsvariable "
                         "ARIA_PASSWORD, sonst interaktive Abfrage)")
    ap.add_argument("--password-file",
                    help="Datei mit dem Aria-Passwort (z. B. systemd LoadCredential)")
    ap.add_argument("--smtp-password-file", help="Datei mit dem SMTP-Passwort")
    ap.add_argument("--backup-password-file", help="Datei mit dem Backup-SSH-Passwort")
    ap.add_argument("--auth-source", default="local", help="Auth-Quelle (Standard: local)")
    ap.add_argument("--cpu-factor", type=int, default=6, help="CPU-Überprovisionierungsfaktor (Standard: 6)")
    ap.add_argument("--failover-hosts", type=int, default=1,
                    help="Ausfall-Hosts pro Cluster (N+1): die größten N Hosts werden "
                         "von der Kapazität abgezogen (Standard: 1, 0 = aus)")
    ap.add_argument("--insecure", action="store_true", help="TLS-Zertifikat nicht prüfen (Self-Signed)")
    ap.add_argument("--output", default="kapa_dashboard.html", help="Ausgabedatei")
    ap.add_argument("--json", help="Rohdaten zusätzlich als JSON speichern")
    ap.add_argument("--sample", action="store_true", help="Demo mit Beispieldaten")
    ap.add_argument("--serve", action="store_true",
                    help="Als lokaler Webserver laufen (Live-Abruf per Knopf, Disk-Cache)")
    ap.add_argument("--port", type=int, default=8080, help="Port für --serve (Standard: 8080)")
    ap.add_argument("--bind", default="0.0.0.0", help="Bind-Adresse für --serve")
    ap.add_argument("--data-dir", default="data",
                    help="Basisordner für alle Laufzeitdaten (Cache, Reservierungen, "
                         "Rollen, Teams, Log, Tokens). Standard: data/ neben dem "
                         "Skript. WICHTIG bei CI/CD: auf einen Pfad AUSSERHALB des "
                         "Deploy-Verzeichnisses legen (z. B. /var/lib/kapa), sonst "
                         "löscht die Pipeline die Daten bei jedem Deploy. Gilt für "
                         "alle *-file-Optionen ohne eigene Pfadangabe.")
    ap.add_argument("--cache", default="kapa_cache.json",
                    help="Cache-Datei der letzten Abfrage (Standard: <data-dir>/kapa_cache.json)")
    ap.add_argument("--refresh-interval", type=int, default=1800,
                    help="Automatische Aktualisierung im Serve-Modus in Sekunden "
                         "(0 = aus, Standard: 1800 = 30 min)")
    ap.add_argument("--res-file", default="kapa_reservierungen.json",
                    help="Reservierungsdatei im Serve-Modus "
                         "(Standard: data/kapa_reservierungen.json)")
    ap.add_argument("--res-ttl-days", type=int, default=31,
                    help="Reservierungen nach N Tagen ab Anlage automatisch löschen "
                         "(0 = nie, Standard: 31)")
    ap.add_argument("--approval-teams", default="",
                    help="Erstbefüllung der Genehmigungs-Teams (komma-getrennt, in "
                         "Prüfreihenfolge), z. B. 'Team Netzwerk,Team Security,Team "
                         "Betrieb'. Wird nur übernommen, wenn die Team-Datei noch "
                         "nicht existiert; danach erfolgt die Pflege auf der "
                         "Verwaltungsseite. Leer = einstufig (Admin genehmigt direkt).")
    ap.add_argument("--ad-url", default="",
                    help="Active Directory für die Anmeldung, z. B. "
                         "ldaps://dc01.firma.local (ohne Angabe: kein Login nötig)")
    ap.add_argument("--ad-domain", default="",
                    help="AD-Domäne für Benutzernamen ohne @, z. B. firma.local")
    ap.add_argument("--ad-insecure", action="store_true",
                    help="LDAPS-Zertifikat nicht prüfen (Self-Signed)")
    ap.add_argument("--cookie-insecure", action="store_true",
                    help="Session-Cookie ohne Secure-Flag setzen (nur für lokalen "
                         "HTTP-Test ohne HTTPS-Proxy; im Betrieb NICHT verwenden)")
    ap.add_argument("--admin-user", default="",
                    help="Immer-Admin(s), kommagetrennt, z. B. admin@firma.local "
                         "(Bootstrap für die Rollenverwaltung)")
    ap.add_argument("--roles-file", default="kapa_rollen.json",
                    help="Rollendatei (Standard: data/kapa_rollen.json)")
    ap.add_argument("--smtp-server", default="",
                    help="Mailserver für Reports, z. B. mail.firma.local:25 "
                         "(ohne Angabe: keine Mails)")
    ap.add_argument("--smtp-from", default="",
                    help="Absenderadresse (Standard: kapa-dashboard@localhost)")
    ap.add_argument("--smtp-to", default="",
                    help="Report-Empfänger, kommagetrennt; der Anforderer erhält "
                         "die Mail zusätzlich automatisch")
    ap.add_argument("--smtp-user", default="", help="SMTP-Anmeldung (optional)")
    ap.add_argument("--smtp-password", default="", help="SMTP-Passwort (optional)")
    ap.add_argument("--smtp-tls", action="store_true", help="STARTTLS verwenden")
    ap.add_argument("--config",
                    help="INI-Datei mit allen Optionen (Sektion [kapa], Schlüssel wie "
                         "die Optionsnamen); Kommandozeile überschreibt die Datei")
    ap.add_argument("--backup-target", default="",
                    help="SFTP/SCP-Backupziel, z. B. backup@srv:/backup/kapa "
                         "(ohne Angabe: kein Backup)")
    ap.add_argument("--backup-port", type=int, default=22, help="SSH-Port (Standard: 22)")
    ap.add_argument("--backup-key", default="",
                    help="SSH-Private-Key für das Backup (empfohlen)")
    ap.add_argument("--backup-password", default="",
                    help="SSH-Passwort (alternativ BACKUP_PASSWORD; erfordert sshpass)")
    ap.add_argument("--backup-interval", type=int, default=43200,
                    help="Backup-Intervall in Sekunden (Standard: 43200 = zweimal "
                         "täglich, 0 = nur einmal beim Start)")
    ap.add_argument("--backup-keep-days", type=int, default=30,
                    help="Backups auf dem Ziel nach N Tagen löschen "
                         "(Standard: 30, 0 = nie aufräumen)")
    ap.add_argument("--log-file", default="kapa_log.jsonl",
                    help="Audit-Log-Datei (Standard: data/kapa_log.jsonl)")
    ap.add_argument("--tokens-file", default="kapa_tokens.json",
                    help="API-Token-Datei (Standard: data/kapa_tokens.json)")
    ap.add_argument("--teams-file", default="kapa_teams.json",
                    help="Datei mit den Genehmigungs-Teams (Standard: "
                         "data/kapa_teams.json); Pflege über die Verwaltungsseite")
    ap.add_argument("--rolenames-file", default="kapa_rollennamen.json",
                    help="Datei mit den frei wählbaren Rollen-Bezeichnungen "
                         "(Standard: data/kapa_rollennamen.json); Pflege über die "
                         "Verwaltungsseite")
    # Erst --config einlesen, dann endgültig parsen (CLI schlägt INI)
    pre, _ = ap.parse_known_args()
    if pre.config:
        apply_config_file(ap, pre.config)
    args = ap.parse_args()

    # JSON-Datendateien ohne Pfadangabe unter --data-dir ablegen (Standard data/)
    base = args.data_dir or "data"
    args.cache = data_path(args.cache, base)
    args.res_file = data_path(args.res_file, base)
    args.roles_file = data_path(args.roles_file, base)
    args.log_file = data_path(args.log_file, base)
    args.tokens_file = data_path(args.tokens_file, base)
    args.teams_file = data_path(args.teams_file, base)
    args.rolenames_file = data_path(args.rolenames_file, base)
    if args.json:
        args.json = data_path(args.json, base)

    # Zugangsdaten: Parameter > Passwort-Datei > Umgebungsvariable
    def secret(value, path, env_key):
        if value:
            return value
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read().strip()
            except OSError as e:
                ap.error(f"Passwort-Datei {path}: {e}")
        return os.environ.get(env_key, "")

    args.password = secret(args.password, args.password_file, "ARIA_PASSWORD") or None
    args.smtp_password = secret(args.smtp_password, args.smtp_password_file,
                                "SMTP_PASSWORD")
    args.backup_password = secret(args.backup_password, args.backup_password_file,
                                  "BACKUP_PASSWORD")

    if args.serve:
        pw = None
        if not args.sample:
            if not args.url or not args.user:
                ap.error("--url und --user sind erforderlich (oder --sample für Demo)")
            pw = args.password or getpass.getpass("Passwort: ")
        serve(args, pw)
        return

    if args.sample:
        clusters = build_summary(sample_data(), args.cpu_factor, args.failover_hosts)
    else:
        if not args.url or not args.user:
            ap.error("--url und --user sind erforderlich (oder --sample für Demo)")
        pw = args.password or getpass.getpass("Passwort: ")
        api = AriaOps(args.url, args.user, pw, args.auth_source,
                      verify_tls=not args.insecure)
        clusters = collect(api, args.cpu_factor, failover_hosts=args.failover_hosts)

    if args.json:
        ensure_dir(args.json)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(clusters, f, ensure_ascii=False, indent=2)

    render_dashboard(clusters, args.cpu_factor, args.output,
                     args.res_ttl_days, args.failover_hosts)

    print(f"\n{'Cluster':<22}{'Hosts':>6}{'Cores':>7}{'vCPU-Kap':>10}{'vCPU-belegt':>12}"
          f"{'vCPU-frei':>10}{'RAM-Kap GB':>12}{'RAM-belegt':>12}{'RAM-frei':>10}")
    for c in clusters:
        print(f"{c['name']:<22}{c['hostCount']:>6}{c['cores']:>7}{c['vcpuCap']:>10}"
              f"{c['vcpuUsed']:>12}{c['vcpuFree']:>10}{c['ramCap']:>12}"
              f"{c['ramUsed']:>12}{c['ramFree']:>10}")
    print(f"\nDashboard geschrieben: {args.output}")


if __name__ == "__main__":
    main()
