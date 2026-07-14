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
from datetime import datetime

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


def collect(api, cpu_factor, progress=None):
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

    return build_summary(data, cpu_factor)


def build_summary(data, cpu_factor):
    """data: {cluster: {hosts:[{cores,ram_gb}], vms:[{vcpu,ram_gb,on}]}}"""
    clusters = []
    for cl, d in sorted(data.items()):
        cores = sum(h["cores"] for h in d["hosts"])
        host_ram = round(sum(h["ram_gb"] for h in d["hosts"]), 1)
        vcpu_cap = cores * cpu_factor
        vcpu_used = sum(v["vcpu"] for v in d["vms"])
        ram_used = round(sum(v["ram_gb"] for v in d["vms"]), 1)
        clusters.append({
            "name": cl,
            "hostCount": len(d["hosts"]),
            "cores": cores,
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
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(420px,1fr)); gap:16px; }
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
  .toolbar { display:flex; gap:10px; margin-bottom:20px; }
  .btn { background:#0b1220; border:1px solid var(--line); color:var(--text);
         border-radius:8px; padding:6px 12px; font-size:12px; cursor:pointer; }
  .btn:hover { border-color:var(--accent); }
  .resbox { margin-top:14px; border-top:1px solid var(--line); padding-top:10px; }
  .resbox h3 { font-size:13px; color:var(--res); margin-bottom:6px; }
  .resform { display:grid; grid-template-columns:2fr 70px 80px 1fr auto; gap:6px; margin-top:8px; }
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
<div class="sub">Quelle: VMware Aria Operations · CPU-Überprovisionierung: Faktor __FACTOR__ (physische Cores) · RAM 1:1 · alle VMs inkl. powered-off · Stand: <span id="stand">__DATE__</span><br>
Reservierungen werden lokal im Browser gespeichert und bleiben auch nach Neu-Generierung des Dashboards erhalten.</div>
<div class="toolbar">
  <button class="btn primary" onclick="openModal()">+ Neue Kapazitätsanfrage</button>
  <button class="btn" id="refreshBtn" onclick="refreshData()">⟳ Daten aus Aria abrufen</button>
  <span id="refreshStatus" style="align-self:center;font-size:12px;color:var(--muted)"></span>
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
    <label>geplant ab (optional)</label>
    <input id="mDate" type="date">
    <div class="hint" id="mHint"></div>
    <div class="err" id="mErr"></div>
    <div class="actions">
      <button class="btn" onclick="closeModal()">Abbrechen</button>
      <button class="btn primary" onclick="submitModal()">Reservieren</button>
    </div>
  </div>
</div>
<div class="grid" id="grid"></div>
<script>
let CLUSTERS = __DATA__;
const FACTOR = __FACTOR__;
const SERVE = __SERVE__;
const LS_KEY = "aria_kapa_reservierungen";

// ---- Reservierungen (localStorage) ----
let RES = (() => { try { return JSON.parse(localStorage.getItem(LS_KEY)) || []; } catch(e) { return []; } })();
function saveRes() { try { localStorage.setItem(LS_KEY, JSON.stringify(RES)); } catch(e) {} render(); }
function resFor(cl) { return RES.filter(r => r.cluster === cl); }
function sumCpu(rv) { return rv.reduce((s,r)=>s+(r.vcpu||0),0); }
function sumRam(rv) { return Math.round(rv.reduce((s,r)=>s+(r.ram_gb||0),0)*10)/10; }

function freeAfter(c) {
  const rv = resFor(c.name);
  return { cpu: c.vcpuFree - sumCpu(rv),
           ram: Math.round((c.ramFree - sumRam(rv)) * 10) / 10 };
}

function createRes(c, name, vcpu, ram, date, errEl) {
  errEl.style.display = "none";
  if (!name || (vcpu <= 0 && ram <= 0)) {
    errEl.textContent = "Bitte Bezeichnung sowie vCPU und/oder RAM angeben.";
    errEl.style.display = "block"; return false;
  }
  const f = freeAfter(c);
  if ((vcpu > f.cpu || ram > f.ram) &&
      !confirm("Achtung: Die Reservierung überschreitet die freie Kapazität dieses Clusters " +
               "(frei: " + f.cpu + " vCPU / " + f.ram + " GB). Trotzdem anlegen?")) return false;
  RES.push({ id: Date.now() + "-" + Math.random().toString(36).slice(2,7),
             cluster: c.name, name: name, vcpu: vcpu, ram_gb: ram, date: date,
             created: new Date().toISOString().slice(0,10) });
  saveRes();
  return true;
}

function addRes(idx) {
  const c = CLUSTERS[idx];
  const g = s => document.getElementById("f" + idx + s);
  createRes(c, g("n").value.trim(), parseInt(g("c").value) || 0,
            parseFloat(g("r").value) || 0, g("d").value, g("e"));
}
function delRes(id) { RES = RES.filter(r => r.id !== id); saveRes(); }

// ---- Dialog "Neue Kapazitätsanfrage" ----
function openModal(prefIdx) {
  const sel = document.getElementById("mCluster");
  sel.innerHTML = CLUSTERS.map((c, i) =>
    `<option value="${i}" ${prefIdx === i ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  ["mName", "mCpu", "mRam", "mDate"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("mErr").style.display = "none";
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
                       parseFloat(v("mRam")) || 0, v("mDate"),
                       document.getElementById("mErr"));
  if (ok) closeModal();
}
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

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
    if (Array.isArray(d)) { RES = d; saveRes(); }
    else alert("Ungültige Datei.");
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
  const rv = isTotal ? RES : resFor(c.name);
  const rvCpu = sumCpu(rv), rvRam = sumRam(rv);
  const vmRows = (c.vms || []).sort((a,b)=>b.vcpu-a.vcpu).map(v =>
    `<tr class="${v.on?'':'off'}"><td>${esc(v.name)}${v.on?'':' (aus)'}</td>
     <td class="num">${v.vcpu}</td><td class="num">${fmt(v.ram_gb)}</td></tr>`).join("");
  const hostRows = (c.hosts || []).map(h =>
    `<tr><td>${esc(h.name)}</td><td class="num">${h.cores}</td><td class="num">${fmt(h.ram_gb)}</td></tr>`).join("");
  const resRows = rv.map(r =>
    `<tr><td>${esc(r.name)}${isTotal ? ' <span style="color:var(--muted)">(' + esc(r.cluster) + ')</span>' : ''}</td>
     <td class="num">${r.vcpu}</td><td class="num">${fmt(r.ram_gb)}</td>
     <td>${r.date || "–"}</td>
     <td><button class="del" title="Reservierung löschen" onclick="delRes('${r.id}')">✕</button></td></tr>`).join("");
  const resTable = rv.length ?
    `<table><tr><th>Anfrage</th><th class="num">vCPU</th><th class="num">RAM (GB)</th><th>geplant ab</th><th></th></tr>${resRows}</table>`
    : `<div style="color:var(--muted);font-size:12px">Keine Reservierungen.</div>`;
  return `<div class="card ${isTotal?'total':''}">
    <h2>${esc(c.name)}</h2>
    <div class="meta">${c.hostCount} Hosts · ${fmt(c.cores)} phys. Cores · ${c.vmCount} VMs${c.vmOff?` (davon ${c.vmOff} aus)`:''} · ${rv.length} Reservierung${rv.length===1?'':'en'}</div>
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
        <input id="f${idx}d" type="date" title="geplant ab">
        <button class="btn" onclick="addRes(${idx})">+ Reservieren</button>
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

function render() {
  const total = {
    name: "Gesamt (alle Cluster)",
    hostCount: CLUSTERS.reduce((s,c)=>s+c.hostCount,0),
    cores: CLUSTERS.reduce((s,c)=>s+c.cores,0),
    vcpuCap: CLUSTERS.reduce((s,c)=>s+c.vcpuCap,0),
    vcpuUsed: CLUSTERS.reduce((s,c)=>s+c.vcpuUsed,0),
    ramCap: Math.round(CLUSTERS.reduce((s,c)=>s+c.ramCap,0)*10)/10,
    ramUsed: Math.round(CLUSTERS.reduce((s,c)=>s+c.ramUsed,0)*10)/10,
    vmCount: CLUSTERS.reduce((s,c)=>s+c.vmCount,0),
    vmOff: CLUSTERS.reduce((s,c)=>s+c.vmOff,0),
  };
  total.vcpuFree = total.vcpuCap - total.vcpuUsed;
  total.ramFree = Math.round((total.ramCap - total.ramUsed)*10)/10;
  document.getElementById("grid").innerHTML =
    card(total, -1, true) + CLUSTERS.map((c, i) => card(c, i, false)).join("");
}
render();
if (!SERVE) document.getElementById("refreshBtn").style.display = "none";

// ---- Live-Abruf aus Aria (nur im Serve-Modus) ----
async function refreshData() {
  const btn = document.getElementById("refreshBtn");
  const st = document.getElementById("refreshStatus");
  btn.disabled = true;
  try { await fetch("/api/refresh", { method: "POST" }); }
  catch (e) { st.textContent = "Server nicht erreichbar."; btn.disabled = false; return; }
  const poll = setInterval(async () => {
    let s;
    try { s = await (await fetch("/api/status")).json(); } catch (e) { return; }
    if (s.refreshing) {
      st.textContent = "Lade Daten aus Aria … " + (s.progress || "");
      return;
    }
    clearInterval(poll);
    btn.disabled = false;
    if (s.error) { st.textContent = "Fehler: " + s.error; return; }
    try {
      const d = await (await fetch("/api/data")).json();
      CLUSTERS = d.clusters || [];
      document.getElementById("stand").textContent = d.updated || "";
      st.textContent = "Aktualisiert.";
      render();
    } catch (e) { st.textContent = "Daten konnten nicht geladen werden."; }
  }, 2000);
}
</script>
</body>
</html>
"""


def render_html(clusters, cpu_factor, serve_mode=False, updated=None):
    return (HTML_TEMPLATE
            .replace("__DATA__", json.dumps(clusters, ensure_ascii=False))
            .replace("__FACTOR__", str(cpu_factor))
            .replace("__SERVE__", "true" if serve_mode else "false")
            .replace("__DATE__", updated or datetime.now().strftime("%d.%m.%Y %H:%M")))


def render_dashboard(clusters, cpu_factor, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(clusters, cpu_factor))

# ------------------------------------------------------------- Serve-Modus ---

def serve(args, password):
    import threading
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

    state = {"clusters": [], "updated": None, "refreshing": False,
             "progress": "", "error": None}

    # Zwischen-Cache der letzten Abfrage von Platte laden
    if os.path.exists(args.cache):
        try:
            with open(args.cache, encoding="utf-8") as f:
                c = json.load(f)
            state["clusters"] = c.get("clusters", [])
            state["updated"] = c.get("updated")
            print(f"Cache geladen: {args.cache} (Stand {state['updated']}, "
                  f"{len(state['clusters'])} Cluster)", file=sys.stderr)
        except Exception as e:
            print(f"Cache unlesbar, starte leer: {e}", file=sys.stderr)

    def do_refresh():
        state.update(refreshing=True, error=None, progress="Verbinde mit Aria Operations ...")
        try:
            if args.sample:
                import time; time.sleep(2)  # Demo: Ladezeit simulieren
                clusters = build_summary(sample_data(), args.cpu_factor)
            else:
                api = AriaOps(args.url, args.user, password, args.auth_source,
                              verify_tls=not args.insecure)
                clusters = collect(api, args.cpu_factor,
                                   progress=lambda m: state.update(progress=m))
            state["clusters"] = clusters
            state["updated"] = datetime.now().strftime("%d.%m.%Y %H:%M")
            with open(args.cache, "w", encoding="utf-8") as f:
                json.dump({"updated": state["updated"], "clusters": clusters},
                          f, ensure_ascii=False)
        except Exception as e:
            state["error"] = str(e)
        finally:
            state["refreshing"] = False

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

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(render_html(state["clusters"], args.cpu_factor,
                                       serve_mode=True,
                                       updated=state["updated"] or
                                       "noch keine Daten – bitte „Daten aus Aria abrufen“ klicken"),
                           "text/html; charset=utf-8")
            elif self.path == "/api/data":
                self._json({"updated": state["updated"], "clusters": state["clusters"]})
            elif self.path == "/api/status":
                self._json({k: state[k] for k in
                            ("refreshing", "progress", "error", "updated")})
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/api/refresh":
                if not state["refreshing"]:
                    threading.Thread(target=do_refresh, daemon=True).start()
                self._json({"started": True}, 202)
            else:
                self.send_error(404)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Dashboard läuft: http://localhost:{args.port}  (Strg+C zum Beenden)",
          file=sys.stderr)
    srv.serve_forever()

# ------------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser(description="Aria Ops Kapazitätsauswertung pro Cluster")
    ap.add_argument("--url", help="Basis-URL, z.B. https://aria-ops.firma.de")
    ap.add_argument("--user", help="Benutzername")
    ap.add_argument("--password", help="Passwort (sonst interaktive Abfrage)")
    ap.add_argument("--auth-source", default="local", help="Auth-Quelle (Standard: local)")
    ap.add_argument("--cpu-factor", type=int, default=6, help="CPU-Überprovisionierungsfaktor (Standard: 6)")
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
        clusters = build_summary(sample_data(), args.cpu_factor)
    else:
        if not args.url or not args.user:
            ap.error("--url und --user sind erforderlich (oder --sample für Demo)")
        pw = args.password or getpass.getpass("Passwort: ")
        api = AriaOps(args.url, args.user, pw, args.auth_source,
                      verify_tls=not args.insecure)
        clusters = collect(api, args.cpu_factor)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(clusters, f, ensure_ascii=False, indent=2)

    render_dashboard(clusters, args.cpu_factor, args.output)

    print(f"\n{'Cluster':<22}{'Hosts':>6}{'Cores':>7}{'vCPU-Kap':>10}{'vCPU-belegt':>12}"
          f"{'vCPU-frei':>10}{'RAM-Kap GB':>12}{'RAM-belegt':>12}{'RAM-frei':>10}")
    for c in clusters:
        print(f"{c['name']:<22}{c['hostCount']:>6}{c['cores']:>7}{c['vcpuCap']:>10}"
              f"{c['vcpuUsed']:>12}{c['vcpuFree']:>10}{c['ramCap']:>12}"
              f"{c['ramUsed']:>12}{c['ramFree']:>10}")
    print(f"\nDashboard geschrieben: {args.output}")


if __name__ == "__main__":
    main()
