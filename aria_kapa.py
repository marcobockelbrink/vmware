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

import argparse
import getpass
import json
import os
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

    data = {}
    for c in clusters:
        data[name_of(c)] = {"hosts": [], "vms": []}

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
        data[cl] = {"hosts": hosts, "vms": vms}
    return data

# ------------------------------------------------------------------ Dashboard --

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
  .btn.approve { border-color:var(--ok); color:var(--ok); }
  .btn.approve:hover { background:rgba(34,197,94,.15); }
  .hc-close { position:absolute; top:10px; right:12px; z-index:1;
              background:none; border:none; color:var(--muted); cursor:pointer; font-size:14px; }
  .hc-close:hover { color:var(--text); }
  .btn { background:#0b1220; border:1px solid var(--line); color:var(--text);
         border-radius:8px; padding:6px 12px; font-size:12px; cursor:pointer; }
  .btn:hover { border-color:var(--accent); }
  .resbox { margin-top:14px; border-top:1px solid var(--line); padding-top:10px; }
  .resbox h3 { font-size:13px; color:var(--res); margin-bottom:6px; }
  .resform { display:grid; grid-template-columns:2fr 70px 80px auto; gap:6px; margin-top:8px; }
  .resform input { background:#0b1220; border:1px solid var(--line); color:var(--text);
                   border-radius:6px; padding:5px 8px; font-size:12px; width:100%; }
  .resform input:focus { outline:none; border-color:var(--res); }
  .del { background:none; border:none; color:var(--crit); cursor:pointer; font-size:13px; }
  .err { color:var(--crit); font-size:12px; margin-top:4px; display:none; }
  .btn.primary { background:var(--res); color:#0b1220; border-color:var(--res); font-weight:600; }
  .modal-bg { position:fixed; inset:0; background:rgba(0,0,0,.65); display:none;
              align-items:center; justify-content:center; z-index:10; }
  .modal-bg.open { display:flex; }
  .modal { background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:22px; width:440px; max-width:92vw; }
  .modal h2 { color:var(--res); font-size:16px; margin-bottom:8px; }
  .modal label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }
  .modal input, .modal select { width:100%; background:#0b1220; border:1px solid var(--line);
           color:var(--text); border-radius:6px; padding:7px 9px; font-size:13px; }
  .modal input:focus, .modal select:focus { outline:none; border-color:var(--res); }
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
  <button class="btn primary" onclick="openModal()">+ Neue Kapazitätsanfrage</button>
  <button class="btn" id="refreshBtn" onclick="refreshData()">⟳ Jetzt aktualisieren</button>
  <span id="refreshStatus" style="font-size:12px;color:var(--muted)"></span>
  <span id="timer" style="font-size:12px;color:var(--muted);margin-left:auto"></span>
  <button class="btn" onclick="exportRes()">Reservierungen exportieren (JSON)</button>
  <label class="btn">Reservierungen importieren (JSON)<input type="file" accept=".json" hidden onchange="importRes(event)"></label>
</div>
<div class="modal-bg" id="modalBg" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <h2>Neue Kapazitätsanfrage</h2>
    <label>Ziel-Cluster</label>
    <select id="mCluster" onchange="modalHint()"></select>
    <label>Bezeichnung / Projekt</label>
    <input id="mName" placeholder="z. B. SAP-Erweiterung Q4">
    <label>vCPU</label>
    <input id="mCpu" type="number" min="0" placeholder="0">
    <label>RAM (GB)</label>
    <input id="mRam" type="number" min="0" placeholder="0">
    <div class="hint" id="mHint"></div>
    <div class="hint" id="mValid" style="color:var(--muted)"></div>
    <div class="err" id="mErr"></div>
    <div class="actions">
      <button class="btn" onclick="closeModal()">Abbrechen</button>
      <button class="btn primary" onclick="submitModal()">Beantragen</button>
    </div>
  </div>
</div>
<div class="tabs">
  <span class="tab active" id="tabKapa" onclick="setView('kapa')">Kapazität</span>
  <span class="tab" id="tabRes" onclick="setView('res')">Reservierungen</span>
  <span class="tab" id="tabApp" onclick="setView('app')">Genehmigungen</span>
</div>
<div class="tablewrap" id="kapaView">
<table class="kt" id="ktable">
  <thead><tr><th>Cluster</th><th class="num">Hosts</th><th class="num">VMs</th>
    <th class="num">vCPU frei</th><th class="barcol">vCPU-Auslastung</th>
    <th class="num">RAM frei (GB)</th><th class="barcol">RAM-Auslastung</th>
    <th class="num">Res.</th></tr></thead>
  <tbody id="ktbody"></tbody>
</table>
</div>
<div class="tablewrap" id="resView" style="display:none">
<table class="kt" id="rtable">
  <thead><tr><th>Anfrage / Projekt</th><th>Cluster</th><th class="num">vCPU</th>
    <th class="num">RAM (GB)</th><th>gilt ab</th><th>gültig bis</th><th>Status</th><th></th></tr></thead>
  <tbody id="rtbody"></tbody>
</table>
</div>
<div class="tablewrap" id="appView" style="display:none">
<table class="kt" id="atable">
  <thead><tr><th>Anfrage / Projekt</th><th>Cluster</th><th class="num">vCPU</th>
    <th class="num">RAM (GB)</th><th>beantragt am</th><th>gültig bis</th><th>Aktion</th></tr></thead>
  <tbody id="atbody"></tbody>
</table>
</div>
<div class="hovercard" id="hovercard"></div>
<script>
let CLUSTERS = __DATA__;
const FACTOR = __FACTOR__;
const SERVE = __SERVE__;
const TTL = __TTL__;
const LS_KEY = "aria_kapa_reservierungen";

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
  const r = await fetch("/api/reservations" + (path || ""), {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function setRes(list) { if (Array.isArray(list)) { RES = list; render(); } }
function resFail() { alert("Reservierungen konnten nicht auf dem Server gespeichert werden."); }
// Nur genehmigte Reservierungen zählen gegen die Kapazität
function resFor(cl) { return RES.filter(r => r.cluster === cl && r.approved); }
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
function stBadge(r) {
  return r.approved ? '<span class="st ok">genehmigt</span>'
                    : '<span class="st pend">beantragt</span>';
}
function sumCpu(rv) { return rv.reduce((s,r)=>s+(r.vcpu||0),0); }
function sumRam(rv) { return Math.round(rv.reduce((s,r)=>s+(r.ram_gb||0),0)*10)/10; }

function freeAfter(c) {
  const rv = resFor(c.name);
  return { cpu: c.vcpuFree - sumCpu(rv),
           ram: Math.round((c.ramFree - sumRam(rv)) * 10) / 10 };
}

function createRes(c, name, vcpu, ram, errEl) {
  errEl.style.display = "none";
  if (!name || (vcpu <= 0 && ram <= 0)) {
    errEl.textContent = "Bitte Bezeichnung sowie vCPU und/oder RAM angeben.";
    errEl.style.display = "block"; return false;
  }
  const f = freeAfter(c);
  if ((vcpu > f.cpu || ram > f.ram) &&
      !confirm("Achtung: Die Reservierung überschreitet die freie Kapazität dieses Clusters " +
               "(frei: " + f.cpu + " vCPU / " + f.ram + " GB). Trotzdem beantragen?")) return false;
  const item = { cluster: c.name, name: name, vcpu: vcpu, ram_gb: ram };
  if (SERVE) {
    apiRes("POST", "", item).then(setRes).catch(resFail);
  } else {
    item.id = Date.now() + "-" + Math.random().toString(36).slice(2,7);
    item.created = new Date().toISOString().slice(0,10);
    item.approved = false;
    RES.push(item); saveLocal(); render();
  }
  return true;
}

function rejectRes(id) {
  const r = RES.find(x => x.id === id);
  if (confirm("Antrag „" + ((r && r.name) || "?") + "“ ablehnen und löschen?")) delRes(id);
}

function approveRes(id) {
  if (SERVE) apiRes("POST", "/" + encodeURIComponent(id) + "/approve").then(setRes).catch(resFail);
  else {
    const r = RES.find(x => x.id === id);
    if (r) { r.approved = true; saveLocal(); render(); }
  }
}

function addRes(idx) {
  const c = CLUSTERS[idx];
  const g = s => document.getElementById("f" + idx + s);
  createRes(c, g("n").value.trim(), parseInt(g("c").value) || 0,
            parseFloat(g("r").value) || 0, g("e"));
}
function delRes(id) {
  if (SERVE) apiRes("DELETE", "/" + encodeURIComponent(id)).then(setRes).catch(resFail);
  else { RES = RES.filter(r => r.id !== id); saveLocal(); render(); }
}

// ---- Dialog "Neue Kapazitätsanfrage" ----
function openModal(prefIdx) {
  const sel = document.getElementById("mCluster");
  sel.innerHTML = CLUSTERS.map((c, i) =>
    `<option value="${i}" ${prefIdx === i ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  ["mName", "mCpu", "mRam"].forEach(id => document.getElementById(id).value = "");
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
    "Frei nach bestehenden Reservierungen: " + fmt(f.cpu) + " vCPU / " + fmt(f.ram) + " GB RAM";
}
function submitModal() {
  const c = CLUSTERS[+document.getElementById("mCluster").value];
  const v = id => document.getElementById(id).value;
  const ok = createRes(c, v("mName").trim(), parseInt(v("mCpu")) || 0,
                       parseFloat(v("mRam")) || 0,
                       document.getElementById("mErr"));
  if (ok) closeModal();
}
document.addEventListener("keydown", e => { if (e.key === "Escape") { closeModal(); hideCard(); } });

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
  const rv = clRes.filter(r => r.approved);
  const rvCpu = sumCpu(rv), rvRam = sumRam(rv);
  const vmRows = (c.vms || []).sort((a,b)=>b.vcpu-a.vcpu).map(v =>
    `<tr class="${v.on?'':'off'}"><td>${esc(v.name)}${v.on?'':' (aus)'}</td>
     <td class="num">${v.vcpu}</td><td class="num">${fmt(v.ram_gb)}</td></tr>`).join("");
  const hostRows = (c.hosts || []).map(h =>
    `<tr><td>${esc(h.name)}</td><td class="num">${h.cores}</td><td class="num">${fmt(h.ram_gb)}</td></tr>`).join("");
  const resRows = clRes.map(r =>
    `<tr><td>${esc(r.name)}${isTotal ? ' <span style="color:var(--muted)">(' + esc(r.cluster) + ')</span>' : ''}</td>
     <td class="num">${r.vcpu}</td><td class="num">${fmt(r.ram_gb)}</td>
     <td>${fmtDate(validUntil(r))}</td><td>${stBadge(r)}</td>
     <td><button class="del" title="Reservierung löschen" onclick="delRes('${r.id}')">✕</button></td></tr>`).join("");
  const resTable = clRes.length ?
    `<table><tr><th>Anfrage</th><th class="num">vCPU</th><th class="num">RAM (GB)</th><th>gültig bis</th><th>Status</th><th></th></tr>${resRows}</table>`
    : `<div style="color:var(--muted);font-size:12px">Keine Reservierungen.</div>`;
  const spare = (c.spareCores || c.spareRamGb) ?
    ` · Ausfallreserve (N+1): ${fmt(c.spareCores)} Cores / ${fmt(c.spareRamGb)} GB abgezogen` : "";
  return `<div class="card ${isTotal?'total':''}">
    <h2>${esc(c.name)}</h2>
    <div class="meta">${c.hostCount} Hosts · ${fmt(c.cores)} nutzbare Cores · ${c.vmCount} VMs${c.vmOff?` (davon ${c.vmOff} aus)`:''} · ${rv.length} genehmigt${clRes.length-rv.length?` / ${clRes.length-rv.length} beantragt`:''}${spare}</div>
    ${metric("vCPU (Cores × " + FACTOR + ")", c.vcpuUsed, rvCpu, c.vcpuCap, "vCPU")}
    ${metric("RAM", c.ramUsed, rvRam, c.ramCap, "GB")}
    <div class="kpis">
      <div class="kpi">frei nach Reservierungen<b>${fmt(c.vcpuFree - rvCpu)} vCPU / ${fmt(Math.round((c.ramFree - rvRam)*10)/10)} GB</b></div>
      <div class="kpi">reserviert<b>${fmt(rvCpu)} vCPU / ${fmt(rvRam)} GB</b></div>
      <div class="kpi">Ø VM<b>${c.vmCount?Math.round(c.vcpuUsed/c.vmCount*10)/10:0} vCPU / ${c.vmCount?Math.round(c.ramUsed/c.vmCount):0} GB</b></div>
    </div>
    <div class="resbox">
      <h3>Kapazitätsreservierungen</h3>
      ${resTable}
      ${isTotal ? "" : `
      <div class="resform">
        <input id="f${idx}n" placeholder="Bezeichnung / Projekt">
        <input id="f${idx}c" type="number" min="0" placeholder="vCPU">
        <input id="f${idx}r" type="number" min="0" placeholder="RAM GB">
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
  const rv = clRes.filter(r => r.approved);
  const pend = clRes.length - rv.length;
  const rvCpu = sumCpu(rv), rvRam = sumRam(rv);
  const fCpu = c.vcpuFree - rvCpu;
  const fRam = Math.round((c.ramFree - rvRam) * 10) / 10;
  const cCpu = color(pct(c.vcpuUsed + rvCpu, c.vcpuCap));
  const cRam = color(pct(c.ramUsed + rvRam, c.ramCap));
  return `<tr class="${isTotal ? 'trtotal' : ''}">
    <td class="cl" title="Details anzeigen" onclick="toggleCard(${idx},this)">${esc(c.name)}</td>
    <td class="num">${fmt(c.hostCount)}</td>
    <td class="num">${fmt(c.vmCount)}</td>
    <td class="num free" style="color:${cCpu}">${fmt(fCpu)}</td>
    <td class="barcol">${miniBar(c.vcpuUsed, rvCpu, c.vcpuCap)}</td>
    <td class="num free" style="color:${cRam}">${fmt(fRam)}</td>
    <td class="barcol">${miniBar(c.ramUsed, rvRam, c.ramCap)}</td>
    <td class="num">${rv.length || "–"}${pend ? ` <span class="st pend">+${pend}</span>` : ""}</td></tr>`;
}

// ---- Ansichten: Kapazität / Reservierungen / Genehmigungen ----
let VIEW = (location.pathname === "/reservierungen" || location.hash === "#reservierungen") ? "res"
         : (location.pathname === "/genehmigungen" || location.hash === "#genehmigungen") ? "app"
         : "kapa";

function setView(v) {
  VIEW = v;
  document.getElementById("tabKapa").classList.toggle("active", v === "kapa");
  document.getElementById("tabRes").classList.toggle("active", v === "res");
  document.getElementById("tabApp").classList.toggle("active", v === "app");
  document.getElementById("kapaView").style.display = v === "kapa" ? "" : "none";
  document.getElementById("resView").style.display = v === "res" ? "" : "none";
  document.getElementById("appView").style.display = v === "app" ? "" : "none";
  document.getElementById("filter").placeholder =
    v === "kapa" ? "Cluster filtern …" : "Reservierungen filtern …";
  try {
    history.replaceState(null, "",
      v === "res" ? "#reservierungen" : v === "app" ? "#genehmigungen" : location.pathname);
  } catch (e) {}
  hideCard();
  render();
}

function filterRes(list) {
  const q = (document.getElementById("filter").value || "").trim().toLowerCase();
  return list.filter(r => !q ||
      (r.name || "").toLowerCase().includes(q) ||
      (r.cluster || "").toLowerCase().includes(q))
    .slice().sort((a, b) => (a.cluster || "").localeCompare(b.cluster || "") ||
                            (a.created || "").localeCompare(b.created || ""));
}

function renderResTable() {
  const list = filterRes(RES);
  const appr = list.filter(r => r.approved);
  const rows = list.map(r =>
    `<tr><td>${esc(r.name)}</td><td>${esc(r.cluster)}</td>
     <td class="num">${fmt(r.vcpu || 0)}</td><td class="num">${fmt(r.ram_gb || 0)}</td>
     <td>${fmtDate(r.created)}</td><td>${fmtDate(validUntil(r))}</td><td>${stBadge(r)}</td>
     <td><button class="del" title="Reservierung löschen" onclick="delRes('${esc(r.id)}')">✕</button></td></tr>`).join("");
  document.getElementById("rtbody").innerHTML =
    `<tr class="trtotal"><td>Summe genehmigt (${appr.length} von ${list.length})</td><td></td>
     <td class="num">${fmt(sumCpu(appr))}</td><td class="num">${fmt(sumRam(appr))}</td>
     <td></td><td></td><td></td><td></td></tr>` +
    (rows || `<tr><td colspan="8" style="color:var(--muted)">Keine Reservierungen.</td></tr>`);
}

function renderAppTable() {
  const list = filterRes(RES.filter(r => !r.approved));
  const rows = list.map(r =>
    `<tr><td>${esc(r.name)}</td><td>${esc(r.cluster)}</td>
     <td class="num">${fmt(r.vcpu || 0)}</td><td class="num">${fmt(r.ram_gb || 0)}</td>
     <td>${fmtDate(r.created)}</td><td>${fmtDate(validUntil(r))}</td>
     <td><button class="btn approve" onclick="approveRes('${esc(r.id)}')">✓ Genehmigen</button>
         <button class="btn" style="color:var(--crit)" title="Ablehnen und löschen"
                 onclick="rejectRes('${esc(r.id)}')">✕ Ablehnen</button></td></tr>`).join("");
  document.getElementById("atbody").innerHTML =
    rows || `<tr><td colspan="7" style="color:var(--muted)">Keine offenen Anträge – alles genehmigt.</td></tr>`;
}

function render() {
  const pend = RES.filter(r => !r.approved).length;
  document.getElementById("tabApp").textContent = "Genehmigungen" + (pend ? " (" + pend + ")" : "");
  if (VIEW === "res") { renderResTable(); return; }
  if (VIEW === "app") { renderAppTable(); return; }
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
    vmCount: vis.reduce((s,c)=>s+c.vmCount,0),
    vmOff: vis.reduce((s,c)=>s+c.vmOff,0),
    spareCores: vis.reduce((s,c)=>s+(c.spareCores||0),0),
    spareRamGb: Math.round(vis.reduce((s,c)=>s+(c.spareRamGb||0),0)*10)/10,
    _names: new Set(vis.map(c => c.name)),
  };
  TOTAL.vcpuFree = TOTAL.vcpuCap - TOTAL.vcpuUsed;
  TOTAL.ramFree = Math.round((TOTAL.ramCap - TOTAL.ramUsed)*10)/10;
  document.getElementById("ktbody").innerHTML =
    row(TOTAL, -1, true) +
    (idxs.length ? idxs.map(i => row(CLUSTERS[i], i, false)).join("")
                 : '<tr><td colspan="8" style="color:var(--muted)">Kein Cluster entspricht dem Filter.</td></tr>');
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

setView(VIEW);
if (!SERVE) document.getElementById("refreshBtn").style.display = "none";

// ---- Live-Abruf & Auto-Update (nur im Serve-Modus) ----
let nextRefresh = null;   // Zeitpunkt (ms) der nächsten automatischen Aktualisierung

async function refreshData() {
  try { await fetch("/api/refresh", { method: "POST" }); pollStatus(); }
  catch (e) { document.getElementById("refreshStatus").textContent = "Server nicht erreichbar."; }
}

async function pollStatus() {
  let s;
  try { s = await (await fetch("/api/status")).json(); } catch (e) { return; }
  const st = document.getElementById("refreshStatus");
  document.getElementById("refreshBtn").disabled = !!s.refreshing;
  nextRefresh = (s.next != null) ? Date.now() + s.next * 1000 : null;
  if (s.refreshing) st.textContent = "Lade Daten aus Aria … " + (s.progress || "");
  else if (s.error) st.textContent = "Fehler beim letzten Abruf: " + s.error;
  else st.textContent = "";
  if (!s.refreshing && s.updated &&
      s.updated !== document.getElementById("stand").textContent) {
    try {
      const d = await (await fetch("/api/data")).json();
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


def render_html(clusters, cpu_factor, serve_mode=False, updated=None, res_ttl=31,
                failover_hosts=1):
    valid_days = res_ttl - 1 if res_ttl > 0 else 30
    resnote = (f"Neue Reservierungen gelten ab dem Anlagetag für {valid_days} Tage, "
               "zählen erst nach Genehmigung gegen die Kapazität und werden "
               + (f"nach {res_ttl} Tagen automatisch entfernt. " if res_ttl > 0
                  else "nicht automatisch entfernt. "))
    resnote += ("Speicherung zentral auf dem Server." if serve_mode
                else "Speicherung lokal im Browser.")
    if failover_hosts == 1:
        failnote = " · Ausfallreserve (N+1): größter Host je Cluster abgezogen"
    elif failover_hosts > 1:
        failnote = (f" · Ausfallreserve (N+{failover_hosts}): größte {failover_hosts} "
                    "Hosts je Cluster abgezogen")
    else:
        failnote = ""
    return (HTML_TEMPLATE
            .replace("__DATA__", json.dumps(clusters, ensure_ascii=False))
            .replace("__FACTOR__", str(cpu_factor))
            .replace("__SERVE__", "true" if serve_mode else "false")
            .replace("__TTL__", str(res_ttl))
            .replace("__RESNOTE__", resnote)
            .replace("__FAILNOTE__", failnote)
            .replace("__DATE__", updated or datetime.now().strftime("%d.%m.%Y %H:%M")))


def render_dashboard(clusters, cpu_factor, path, res_ttl=31, failover_hosts=1):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(clusters, cpu_factor, res_ttl=res_ttl,
                            failover_hosts=failover_hosts))

# ------------------------------------------------------------- Serve-Modus ---

def serve(args, password):
    import threading
    import time
    import uuid
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

    state = {"clusters": [], "updated": None, "refreshing": False,
             "progress": "", "error": None, "last": None}
    interval = max(0, args.refresh_interval)

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
        return [r for r in lst if str(r.get("created") or "9999") >= cutoff]

    def load_res():
        if os.path.exists(args.res_file):
            try:
                with open(args.res_file, encoding="utf-8") as f:
                    lst = json.load(f)
                if isinstance(lst, list):
                    print(f"Reservierungen geladen: {args.res_file} ({len(lst)})",
                          file=sys.stderr)
                    return prune_res(lst)
            except Exception as e:
                print(f"Reservierungsdatei unlesbar, starte leer: {e}", file=sys.stderr)
        return []

    def save_res():
        with open(args.res_file, "w", encoding="utf-8") as f:
            json.dump(reservations, f, ensure_ascii=False, indent=2)

    reservations = load_res()

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

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, ctype, code=200):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False),
                       "application/json; charset=utf-8", code)

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n).decode() or "null")
            except Exception:
                return None

        def do_GET(self):
            if self.path in ("/", "/index.html", "/reservierungen", "/genehmigungen"):
                self._send(render_html(state["clusters"], args.cpu_factor,
                                       serve_mode=True,
                                       updated=state["updated"] or
                                       "noch keine Daten – erster Abruf läuft ...",
                                       res_ttl=args.res_ttl_days,
                                       failover_hosts=args.failover_hosts),
                           "text/html; charset=utf-8")
            elif self.path == "/api/data":
                self._json({"updated": state["updated"], "clusters": state["clusters"]})
            elif self.path == "/api/status":
                nxt = None
                if interval > 0 and state["last"]:
                    nxt = max(0, int(state["last"] + interval - time.time()))
                self._json({"refreshing": state["refreshing"],
                            "progress": state["progress"], "error": state["error"],
                            "updated": state["updated"], "next": nxt})
            elif self.path == "/api/reservations":
                with res_lock:
                    reservations[:] = prune_res(reservations)
                    self._json(list(reservations))
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/api/refresh":
                if not state["refreshing"]:
                    threading.Thread(target=do_refresh, daemon=True).start()
                self._json({"started": True}, 202)
            elif self.path == "/api/reservations":
                item = self._body()
                if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                    self._json({"error": "Ungültige Reservierung"}, 400)
                    return
                try:
                    entry = {"id": uuid.uuid4().hex[:12],
                             "cluster": str(item.get("cluster") or ""),
                             "name": str(item.get("name")).strip(),
                             "vcpu": int(item.get("vcpu") or 0),
                             "ram_gb": float(item.get("ram_gb") or 0),
                             "created": datetime.now().date().isoformat(),
                             "approved": False}
                except (TypeError, ValueError):
                    self._json({"error": "Ungültige Zahlenwerte"}, 400)
                    return
                with res_lock:
                    reservations[:] = prune_res(reservations)
                    reservations.append(entry)
                    save_res()
                    self._json(list(reservations))
            elif (self.path.startswith("/api/reservations/")
                    and self.path.endswith("/approve")):
                rid = urllib.parse.unquote(
                    self.path[len("/api/reservations/"):-len("/approve")])
                with res_lock:
                    for r in reservations:
                        if r.get("id") == rid:
                            r["approved"] = True
                            r["approved_on"] = datetime.now().date().isoformat()
                    save_res()
                    self._json(list(reservations))
            else:
                self.send_error(404)

        def do_PUT(self):
            if self.path == "/api/reservations":
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
            else:
                self.send_error(404)

        def do_DELETE(self):
            if self.path.startswith("/api/reservations/"):
                rid = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
                with res_lock:
                    reservations[:] = [r for r in reservations if r.get("id") != rid]
                    save_res()
                    self._json(list(reservations))
            else:
                self.send_error(404)

        def log_message(self, *a):
            pass

    threading.Thread(target=scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Dashboard läuft: http://localhost:{args.port}  (Strg+C zum Beenden)"
          + (f" · Auto-Refresh alle {interval // 60} min" if interval else ""),
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.", file=sys.stderr)

# ------------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser(description="Aria Ops Kapazitätsauswertung pro Cluster")
    ap.add_argument("--url", help="Basis-URL, z.B. https://aria-ops.firma.de")
    ap.add_argument("--user", help="Benutzername")
    ap.add_argument("--password", help="Passwort (sonst interaktive Abfrage)")
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
    ap.add_argument("--cache", default="kapa_cache.json",
                    help="Cache-Datei der letzten Abfrage (Standard: kapa_cache.json)")
    ap.add_argument("--refresh-interval", type=int, default=1800,
                    help="Automatische Aktualisierung im Serve-Modus in Sekunden "
                         "(0 = aus, Standard: 1800 = 30 min)")
    ap.add_argument("--res-file", default="kapa_reservierungen.json",
                    help="Reservierungsdatei im Serve-Modus (Standard: kapa_reservierungen.json)")
    ap.add_argument("--res-ttl-days", type=int, default=31,
                    help="Reservierungen nach N Tagen ab Anlage automatisch löschen "
                         "(0 = nie, Standard: 31)")
    args = ap.parse_args()

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
