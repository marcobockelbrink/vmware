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

VERSION = "1.22"

# Interne Rollen-Schlüssel (steuern die Rechte, unveränderlich) und ihre
# Standard-Bezeichnungen. Die Bezeichnungen lassen sich auf der Verwaltungsseite
# frei umbenennen (data/kapa_rollennamen.json) – die Schlüssel bleiben gleich.
ROLE_KEYS = ("admin", "anforderer", "reviewer", "auditor")
DEFAULT_ROLE_NAMES = {"admin": "Administrator", "anforderer": "Anforderer",
                      "reviewer": "Reviewer", "auditor": "Technische Prüfung"}

# Mail-Benachrichtigungen je interner Rolle. Ereignisse: created (Anlage),
# rejected (Ablehnung), approved (endgültige Genehmigung), team_turn (ein Team
# ist im Freigabe-Workflow an der Reihe). Empfänger (Modell "Gemischt"):
#   anforderer -> der Antragsteller (r.von), kein eigenes Adressfeld
#   admin/auditor -> feste Verteiler-Adresse (Feld "email")
#   reviewer/team_turn -> die pro Team hinterlegte Adresse (team_email)
NOTIFY_EVENTS = ("created", "rejected", "approved", "team_turn")
# Welche Ereignisse je Rolle überhaupt wählbar sind (Rest wird als "–" gezeigt):
NOTIFY_ROLE_EVENTS = {
    "anforderer": ("created", "rejected", "approved"),
    "admin":      ("created", "rejected", "approved", "team_turn"),
    "auditor":    ("created", "rejected", "approved", "team_turn"),
    "reviewer":   ("team_turn",),
}
DEFAULT_NOTIFY = {
    "role": {
        "anforderer": {"created": False, "rejected": True,  "approved": True},
        "admin":      {"created": False, "rejected": False, "approved": False,
                       "team_turn": False, "email": ""},
        "auditor":    {"created": False, "rejected": False, "approved": False,
                       "team_turn": False, "email": ""},
        "reviewer":   {"team_turn": True},
    },
    "team_email": {},        # {Team-Name: Verteiler-Adresse}
}

import argparse
import getpass
import hashlib
import json
import os
import re
import ssl
import sys
import tempfile
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

# ---------------------------------------------------------------- Suite API --

class AriaOps:
    def __init__(self, base_url, username, password, auth_source="local",
                 verify_tls=True, proxy=""):
        self.base = base_url.rstrip("/") + "/suite-api/api"
        self.token = None
        self.ctx = None
        if not verify_tls:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        # Optionaler HTTP(S)-Proxy für abgesicherte Umgebungen: alle Aria-Aufrufe
        # laufen dann über einen build_opener mit ProxyHandler (statt urlopen).
        self.opener = None
        if proxy:
            handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
            if self.ctx is not None:
                handlers.append(urllib.request.HTTPSHandler(context=self.ctx))
            self.opener = urllib.request.build_opener(*handlers)
        self._login(username, password, auth_source)

    def _open(self, req, timeout=120):
        if self.opener is not None:
            return self.opener.open(req, timeout=timeout)
        return urllib.request.urlopen(req, context=self.ctx, timeout=timeout)

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
            with self._open(req, timeout=120) as r:
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
            with self._open(req, timeout=120) as r:
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

    def all_properties(self, resource_id):
        """ALLE Eigenschaften einer Ressource als {key: value}. vSphere-Tags
        liefert vROps als Eigenschaften (nicht über /tags)."""
        resp = self._request("GET", f"/resources/{resource_id}/properties")
        out = {}
        for p in resp.get("property", []):
            name = p.get("name")
            if name:
                out[name] = p.get("value")
        return out

    def resources_by_tag(self, category, name, resource_kind=None,
                         adapter_kind="VMWARE"):
        """Ressourcen mit dem Tag category:name (paginiert). Best effort."""
        body = {"resourceTag": [{"category": category, "name": name}]}
        if resource_kind:
            body["resourceKind"] = [resource_kind]
        if adapter_kind:
            body["adapterKind"] = [adapter_kind]
        out, page = [], 0
        while True:
            resp = self._request("POST", "/resources/query", body=body,
                                 params={"pageSize": 1000, "page": page})
            items = resp.get("resourceList", [])
            out.extend(items)
            total = resp.get("pageInfo", {}).get("totalCount", len(out))
            page += 1
            if len(out) >= total or not items:
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


def _ber_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, payload):
    return bytes([tag]) + _ber_len(len(payload)) + payload


def _read_tlv(sock, buf):
    """Ein vollständiges BER-Element aus dem Socket lesen. Gibt (tag, value,
    restpuffer) zurück."""
    while len(buf) < 2:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("LDAP-Verbindung vorzeitig beendet")
        buf += chunk
    tag = buf[0]
    lb = buf[1]
    idx = 2
    if lb & 0x80:
        num = lb & 0x7F
        while len(buf) < 2 + num:
            buf += sock.recv(4096)
        length = int.from_bytes(buf[2:2 + num], "big")
        idx = 2 + num
    else:
        length = lb
    while len(buf) < idx + length:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("LDAP-Verbindung vorzeitig beendet")
        buf += chunk
    return tag, buf[idx:idx + length], buf[idx + length:]


def _iter_tlv(data):
    """Untergeordnete BER-Elemente einer SEQUENCE/SET durchlaufen."""
    i = 0
    while i + 1 < len(data):
        tag = data[i]
        lb = data[i + 1]
        i += 2
        if lb & 0x80:
            num = lb & 0x7F
            length = int.from_bytes(data[i:i + num], "big")
            i += num
        else:
            length = lb
        yield tag, data[i:i + length]
        i += length


def _dn_to_cn(dn):
    """CN aus einem DN ziehen: 'CN=KapaAdmins,OU=..' -> 'KapaAdmins'."""
    first = dn.split(",")[0].strip()
    if first[:3].upper() == "CN=":
        return first[3:]
    return first


def ldap_member_of(url, bind_dn, bind_pw, base_dn, user_upn,
                   timeout=10, insecure=False):
    """Über ein Service-Konto die AD-Gruppen (CNs) des Benutzers ermitteln.
    Bindet als bind_dn, sucht (userPrincipalName=user_upn) und liest memberOf.
    Gibt die Liste der Gruppen-CNs zurück (leer bei Fehler/keine Gruppen)."""
    import socket
    if not (bind_dn and bind_pw and base_dn and user_upn):
        return []
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host, port = u.hostname, u.port or (636 if u.scheme == "ldaps" else 389)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        # 1) Bind als Service-Konto
        bind_req = _tlv(0x60, _tlv(0x02, b"\x03")
                        + _tlv(0x04, bind_dn.encode())
                        + _tlv(0x80, bind_pw.encode()))
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x01") + bind_req))
        buf = b""
        tag, val, buf = _read_tlv(sock, buf)       # LDAPMessage (BindResponse)
        # resultCode im BindResponse prüfen
        ok = False
        for t, v in _iter_tlv(val):
            if t == 0x61:                          # BindResponse
                for t2, v2 in _iter_tlv(v):
                    if t2 == 0x0A:
                        ok = (v2[:1] == b"\x00")
                        break
                break
        if not ok:
            return []
        # 2) SearchRequest (messageID 2)
        filt = _tlv(0xA3, _tlv(0x04, b"userPrincipalName")
                    + _tlv(0x04, user_upn.encode()))
        attrs = _tlv(0x30, _tlv(0x04, b"memberOf"))
        search = _tlv(0x63,
                      _tlv(0x04, base_dn.encode())   # baseObject
                      + _tlv(0x0A, b"\x02")          # scope: subtree
                      + _tlv(0x0A, b"\x00")          # derefAliases: never
                      + _tlv(0x02, b"\x00")          # sizeLimit: 0
                      + _tlv(0x02, bytes([timeLimit := 30]))  # timeLimit
                      + _tlv(0x01, b"\x00")          # typesOnly: false
                      + filt + attrs)
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x02") + search))
        # 3) Antworten lesen bis SearchResultDone (0x65)
        cns = []
        for _ in range(1000):
            tag, val, buf = _read_tlv(sock, buf)    # LDAPMessage
            op = None
            for t, v in _iter_tlv(val):
                if t in (0x64, 0x65):
                    op = (t, v)
                    break
            if not op:
                continue
            if op[0] == 0x65:                        # SearchResultDone
                break
            # SearchResultEntry: objectName, PartialAttributeList
            parts = list(_iter_tlv(op[1]))
            for t, v in parts:
                if t == 0x30:                        # PartialAttributeList
                    for _t, attr in _iter_tlv(v):    # je Attribut
                        sub = list(_iter_tlv(attr))
                        if not sub:
                            continue
                        name = sub[0][1].decode(errors="replace")
                        if name.lower() != "memberof":
                            continue
                        for st, sv in sub[1:]:
                            if st == 0x31:            # SET OF values
                                for vt, vv in _iter_tlv(sv):
                                    cns.append(_dn_to_cn(vv.decode(errors="replace")))
        return cns
    finally:
        sock.close()


def ldap_user_attr(url, bind_dn, bind_pw, base_dn, user_upn, attr,
                   timeout=10, insecure=False):
    """Ein einzelnes AD-Attribut eines Benutzers über das Service-Konto lesen
    (z. B. 'mail', 'proxyAddresses'). Bindet als Service-Konto, sucht
    (userPrincipalName=user_upn) und gibt den ersten Attributwert als String
    zurück ('' bei Fehler/kein Wert)."""
    import socket
    if not (bind_dn and bind_pw and base_dn and user_upn and attr):
        return ""
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host, port = u.hostname, u.port or (636 if u.scheme == "ldaps" else 389)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        bind_req = _tlv(0x60, _tlv(0x02, b"\x03")
                        + _tlv(0x04, bind_dn.encode())
                        + _tlv(0x80, bind_pw.encode()))
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x01") + bind_req))
        buf = b""
        tag, val, buf = _read_tlv(sock, buf)
        ok = False
        for t, v in _iter_tlv(val):
            if t == 0x61:
                for t2, v2 in _iter_tlv(v):
                    if t2 == 0x0A:
                        ok = (v2[:1] == b"\x00")
                        break
                break
        if not ok:
            return ""
        filt = _tlv(0xA3, _tlv(0x04, b"userPrincipalName")
                    + _tlv(0x04, user_upn.encode()))
        attrs = _tlv(0x30, _tlv(0x04, attr.encode()))
        search = _tlv(0x63,
                      _tlv(0x04, base_dn.encode())
                      + _tlv(0x0A, b"\x02") + _tlv(0x0A, b"\x00")
                      + _tlv(0x02, b"\x00") + _tlv(0x02, bytes([30]))
                      + _tlv(0x01, b"\x00") + filt + attrs)
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x02") + search))
        for _ in range(1000):
            tag, val, buf = _read_tlv(sock, buf)
            op = None
            for t, v in _iter_tlv(val):
                if t in (0x64, 0x65):
                    op = (t, v)
                    break
            if not op:
                continue
            if op[0] == 0x65:                        # SearchResultDone
                break
            for t, v in _iter_tlv(op[1]):
                if t == 0x30:                        # PartialAttributeList
                    for _t, a in _iter_tlv(v):
                        sub = list(_iter_tlv(a))
                        if not sub:
                            continue
                        if sub[0][1].decode(errors="replace").lower() != attr.lower():
                            continue
                        for st, sv in sub[1:]:
                            if st == 0x31:            # SET OF values
                                for vt, vv in _iter_tlv(sv):
                                    val = vv.decode(errors="replace").strip()
                                    if val:
                                        return val
        return ""
    finally:
        sock.close()

# ----------------------------------------------------------- Datendateien ----

def ensure_dir(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def atomic_write(path, text):
    """Datei atomar schreiben: erst in eine Temp-Datei im selben Verzeichnis,
    dann per os.replace umbenennen. Ein Absturz mitten im Schreiben kann die
    Zieldatei damit nicht mehr beschädigen (kein halb geschriebenes JSON)."""
    ensure_dir(path)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ----------------------------------------------------------- Datenspeicher ----
# Zwei Backends hinter derselben API:
#   JSON   – je Sammlung eine atomar geschriebene .json-Datei (Standard,
#            menschenlesbar/editierbar).
#   SQLite – eine data/kapa.db (Standardbibliothek, kein Server): kleine
#            Sammlungen als Key-Value-Blobs, Reservierungen als eigene Tabelle
#            mit INKREMENTELLEN Schreibzugriffen (nur die geänderte Zeile).

class JsonStore:
    """Backend: je Sammlung eine JSON-Datei, atomar geschrieben."""
    def __init__(self, paths):
        self.paths = paths            # {name: dateipfad}

    def load(self, name, default):
        p = self.paths[name]
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"{p} unlesbar, nutze Standard: {e}", file=sys.stderr)
        return default

    def save(self, name, obj):
        atomic_write(self.paths[name], _dumps(obj))

    # Reservierungen: JSON kennt keine Einzelzeilen -> ganze Liste atomar sichern
    def res_load(self):
        return self.load("res", [])

    def res_save_all(self, items):
        self.save("res", items)

    def res_put(self, entry, items):    # items = aktuelle In-Memory-Liste
        self.save("res", items)

    def res_delete(self, ids, items):
        self.save("res", items)

    def close(self):
        pass


class SqliteStore:
    """Backend: eine SQLite-Datei. Reservierungen inkrementell, kleine
    Sammlungen als JSON-Blobs in einer kv-Tabelle."""
    def __init__(self, db_path):
        import sqlite3
        ensure_dir(db_path)
        # Eine Verbindung, von mehreren HTTP-Threads genutzt -> eigener Lock,
        # weil execute+commit-Folgen sonst ineinanderlaufen können.
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv"
                        "(name TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS reservations"
                        "(id TEXT PRIMARY KEY, created TEXT, data TEXT NOT NULL)")
        self.db.commit()

    def load(self, name, default):
        with self.lock:
            row = self.db.execute("SELECT data FROM kv WHERE name=?",
                                  (name,)).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except ValueError:
                pass
        return default

    def save(self, name, obj):
        with self.lock:
            self.db.execute("INSERT INTO kv(name, data) VALUES(?, ?) "
                            "ON CONFLICT(name) DO UPDATE SET data=excluded.data",
                            (name, json.dumps(obj, ensure_ascii=False)))
            self.db.commit()

    def res_load(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM reservations ORDER BY created").fetchall()
        return [json.loads(r[0]) for r in rows]

    def res_save_all(self, items):     # ganzen Bestand ersetzen (Import/Reconcile)
        with self.lock, self.db:
            self.db.execute("DELETE FROM reservations")
            self.db.executemany(
                "INSERT INTO reservations(id, created, data) VALUES(?,?,?)",
                [(e.get("id"), e.get("created", ""), json.dumps(e, ensure_ascii=False))
                 for e in items])

    def res_put(self, entry, items):   # inkrementell: nur diese Zeile (upsert)
        with self.lock:
            self.db.execute(
                "INSERT INTO reservations(id, created, data) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET created=excluded.created, "
                "data=excluded.data",
                (entry.get("id"), entry.get("created", ""),
                 json.dumps(entry, ensure_ascii=False)))
            self.db.commit()

    def res_delete(self, ids, items):  # inkrementell: nur diese Zeilen
        if ids:
            with self.lock:
                self.db.executemany("DELETE FROM reservations WHERE id=?",
                                    [(i,) for i in ids])
                self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()


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
    """Basis-Kommando + Umgebung für scp/sftp mit Key- oder Passwort-Auth.

    Wichtig für den systemd-Betrieb: der Dienst läuft gehärtet (ProtectHome,
    ProtectSystem=strict), das Home-Verzeichnis ist also nicht nutzbar. Deshalb
    liegt die known_hosts-Datei im beschreibbaren Datenordner und der Key wird
    ausschließlich über --backup-key angegeben (IdentitiesOnly)."""
    known = args.backup_known_hosts or os.path.join(
        os.path.dirname(args.res_file) or ".", "known_hosts")
    base = [prog, "-q", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known}",
            "-o", "ConnectTimeout=15", "-o", f"Port={args.backup_port}"]
    env = os.environ.copy()
    if args.backup_key:
        base += ["-i", args.backup_key, "-o", "IdentitiesOnly=yes",
                 "-o", "BatchMode=yes"]
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
    db = getattr(args, "db_file", "")
    files = [p for p in (args.cache, args.res_file, args.roles_file,
                         args.log_file, args.tokens_file, args.teams_file,
                         args.rolenames_file, args.selector_file, args.notify_file,
                         db, db + "-wal", db + "-shm")
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

def send_mail(args, subject, body, extra_to=(), to_override=None, html=None):
    """Report-Mail über den konfigurierten SMTP-Server (--smtp-server).

    to_override: exakte Empfängerliste (aus den Mail-Regeln). Ist sie gesetzt,
    wird --smtp-to NICHT zusätzlich angeschrieben. html: optionale HTML-Fassung
    (multipart/alternative; Clients ohne HTML zeigen den Klartext)."""
    import smtplib
    from email.message import EmailMessage
    if not args.smtp_server:
        return
    if to_override is not None:
        seen, to = set(), []
        for t in to_override:
            t = str(t or "").strip()
            if "@" in t and t not in seen:
                seen.add(t)
                to.append(t)
    else:
        to = [t.strip() for t in (args.smtp_to or "").split(",") if t.strip()]
        to += [t for t in extra_to if t and "@" in t and t not in to]
    if not to:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = args.smtp_from or "kapa-dashboard@localhost"
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    host, _, port = args.smtp_server.partition(":")
    with smtplib.SMTP(host, int(port or 25), timeout=15) as smtp:
        if args.smtp_tls:
            smtp.starttls()
        if args.smtp_user:
            smtp.login(args.smtp_user, args.smtp_password or "")
        smtp.send_message(msg)


def _res_valid_date(r, res_ttl):
    if r.get("created"):
        try:
            d = datetime.fromisoformat(r["created"]) + \
                timedelta(days=(res_ttl - 1 if res_ttl > 0 else 30))
            return d.strftime("%d.%m.%Y")
        except ValueError:
            pass
    return ""


def _res_rows(r, res_ttl):
    """Feldliste (Label, Wert) für die Reservierungs-Mail."""
    return [
        ("ID", r.get("id") or "–"),
        ("Anfrage", r.get("name") or "?"),
        ("Change / Jira", r.get("change") or "–"),
        ("Cluster", r.get("cluster") or "?"),
        ("vCPU", r.get("vcpu", 0)),
        ("RAM", f"{r.get('ram_gb', 0)} GB"),
        ("Storage", f"{r.get('storage_gb') or 0} GB"),
        ("Team", r.get("abteilung") or "–"),
        ("Beantragt", f"von {r.get('von') or '–'} am {r.get('created') or '–'}"),
        ("Gültig bis", _res_valid_date(r, res_ttl) or "–"),
    ]


def reservation_mail_body(r, action, admin, res_ttl):
    """Klartext-Fassung (Fallback für Clients ohne HTML)."""
    approvals = r.get("approvals") or []
    freigaben = ("\n" + "\n".join(
        f"             - {a.get('team') or '?'}: {a.get('by') or '?'} "
        f"am {a.get('on') or '?'}" for a in approvals)) if approvals else " –"
    lines = [f"{label + ':':<14}{value}" for label, value in _res_rows(r, res_ttl)]
    return (f"Kapazitätsreservierung {action}\n\n"
            + "\n".join(lines) + "\n"
            f"{'Freigaben:':<14}{freigaben}\n"
            f"{'Kommentar:':<14}{r.get('comment') or '–'}\n\n"
            f"{action} von {admin} am {datetime.now().strftime('%d.%m.%Y %H:%M')}\n")


def reservation_mail_html(r, action, admin, res_ttl):
    """Dezente HTML-Fassung – neutral (Grautöne), ohne Farben/Bilder."""
    esc = _html_escape
    approvals = r.get("approvals") or []
    freigaben = "<br>".join(
        f"{esc(a.get('team') or '?')} · {esc(str(a.get('by') or '?'))} "
        f"am {esc(str(a.get('on') or '?'))}" for a in approvals) or "–"
    rows = _res_rows(r, res_ttl) + [("Freigaben", None), ("Kommentar", r.get("comment") or "–")]
    tr = []
    for label, value in rows:
        v = freigaben if label == "Freigaben" else esc(str(value))
        tr.append(
            '<tr>'
            '<td style="padding:7px 14px 7px 0;color:#6b7280;white-space:nowrap;'
            'border-bottom:1px solid #eef0f3;vertical-align:top">' + esc(label) + '</td>'
            '<td style="padding:7px 0;border-bottom:1px solid #eef0f3;'
            'vertical-align:top">' + v + '</td></tr>')
    now = datetime.now().strftime('%d.%m.%Y %H:%M')
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,'
        'Helvetica,Arial,sans-serif;color:#1f2937;font-size:14px;line-height:1.5;'
        'max-width:600px">'
        '<div style="font-size:17px;font-weight:600;margin:0 0 2px">'
        f'Kapazitätsreservierung {esc(action)}</div>'
        '<div style="color:#6b7280;font-size:13px;margin:0 0 16px">'
        f'{esc(r.get("name") or "?")} &middot; {esc(r.get("cluster") or "?")}</div>'
        '<table style="border-collapse:collapse;width:100%;border-top:1px solid #eef0f3">'
        + "".join(tr) + '</table>'
        '<div style="color:#9ca3af;font-size:12px;margin-top:16px">'
        f'{esc(action)} von {esc(str(admin or "System"))} am {now}</div>'
        '<div style="color:#c3c8d0;font-size:11px;margin-top:4px">'
        'VMware Kapazitätsplanung</div></div>')

# ------------------------------------------------------------- Datenabfrage --

def name_of(res):
    return res.get("resourceKey", {}).get("name", "?")


def is_uplink_pg(name, vlan):
    """dvSwitch-Uplink-/Trunk-Portgruppen erkennen (keine echten Netz-VLANs):
    Name enthält 'uplink' ODER die VLAN ist eine breite Trunk-Range (z. B.
    0-4094). Solche Gruppen tragen alle VLANs und gehören nicht in die
    VLAN-Übersicht."""
    if "uplink" in str(name or "").lower():
        return True
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(vlan or ""))
    return bool(m) and int(m.group(2)) - int(m.group(1)) >= 100


def strip_uplinks(clusters, hide=True):
    """Uplink-/Trunk-Portgruppen aus den fertigen Cluster-Dicts entfernen
    (an einer Stelle, damit Serve- und Demo-Modus gleich reagieren)."""
    if not hide:
        return clusters
    for c in clusters:
        pgs = c.get("portgroups")
        if pgs:
            c["portgroups"] = [p for p in pgs
                               if not is_uplink_pg(p.get("name"), p.get("vlan"))]
    return clusters


def collect(api, cpu_factor, progress=None, failover_hosts=1, exclude_tag="",
            tag_property="", vsan_factor=0.5):
    # Detail-Log geht ins stderr (journal); die Oberfläche bekommt nur einen
    # grob geschätzten Prozentwert über report().
    log = lambda m: print(m, file=sys.stderr)

    def report(pct):
        if progress:
            try:
                progress(str(int(pct)) + " %")
            except Exception:
                pass

    report(3)
    log("Lese Cluster ...")
    clusters = api.resources("ClusterComputeResource")
    # Workload-Badge je Cluster (vROps, 0-100 %) – best effort.
    cl_workload = {}
    try:
        cw = api.latest_stats([c["identifier"] for c in clusters], ["badge|workload"])
        for cid, st in cw.items():
            v = st.get("badge|workload")
            if v is not None:
                cl_workload[cid] = round(float(v))
        log(f"Cluster-Workload gelesen: {len(cl_workload)}/{len(clusters)}")
    except Exception as e:
        log(f"Cluster-Workload nicht verfügbar: {e}")
    report(8)
    log(f"Lese ESXi-Hosts ... ({len(clusters)} Cluster gefunden)")
    hosts = api.resources("HostSystem")
    report(15)
    log(f"Lese VMs ... ({len(hosts)} Hosts gefunden)")
    vms = api.resources("VirtualMachine")
    report(22)
    log(f"{len(vms)} VMs gefunden")

    # Systeme mit dem Ausschluss-Tag (z. B. Kapa_Filter:Ja) aus der Auswertung
    # nehmen – best effort, bei Fehler wird nichts ausgeschlossen.
    exclude_vms = set()
    if exclude_tag and ":" in exclude_tag:
        cat, _, val = exclude_tag.partition(":")
        cat, val = cat.strip(), val.strip()
        if cat and val:
            try:
                log(f"Lese Systeme mit Tag {cat}={val} (werden ausgeschlossen) ...")
                tagged = api.resources_by_tag(cat, val, "VirtualMachine")
                exclude_vms = {t["identifier"] for t in tagged}
                log(f"{len(exclude_vms)} Systeme per Tag {cat}={val} ausgeschlossen")
            except Exception as e:
                log(f"Tag-Filter nicht anwendbar: {e}")

    host_ids = [h["identifier"] for h in hosts]
    vm_ids = [v["identifier"] for v in vms]

    report(30)
    log("Lese Host-Metriken (Cores, RAM) ...")
    host_stats = api.latest_stats(host_ids,
        ["cpu|corecount_provisioned", "mem|host_provisioned"],
        progress=lambda m: log(f"Host-Metriken: {m}"))
    log("Lese Host-Zuordnung (Cluster) ...")
    host_props = api.properties(host_ids, ["summary|parentCluster"],
        progress=lambda m: log(f"Host-Eigenschaften: {m}"))

    report(50)
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
    # Der Typ (VMFS/NFS/vSAN) kommt als Eigenschaft; die Schlüssel unterscheiden
    # sich je nach vROps-Version, deshalb mehrere Kandidaten in einem Bulk-Aufruf.
    DS_TYPE_KEYS = ["summary|type", "summary|datastore_type",
                    "config|fileSystemType", "summary|fileSystemType"]
    datastores, ds_stats, ds_props = [], {}, {}
    try:
        report(68)
        log("Lese Datastores (Storage-Kapazität) ...")
        datastores = api.resources("Datastore")
        ds_ids = [d["identifier"] for d in datastores]
        ds_stats = api.latest_stats(ds_ids,
            ["capacity|total_capacity", "capacity|used_space"],
            progress=lambda m: log(f"Datastore-Metriken: {m}"))
        ds_props = api.properties(ds_ids, DS_TYPE_KEYS,
            progress=lambda m: log(f"Datastore-Typ: {m}"))
    except Exception as e:
        log(f"Storage-Kapazität nicht verfügbar: {e}")

    def ds_type(did, name):
        """Storage-Typ eines Datastores (erste gefüllte Kandidaten-Eigenschaft)."""
        p = ds_props.get(did) or {}
        for k in DS_TYPE_KEYS:
            v = str(p.get(k) or "").strip()
            if v:
                return v
        # Fallback: vSAN-Datastores heißen praktisch immer so
        return "vSAN" if "vsan" in (name or "").lower() else ""

    def is_vsan(typ, name):
        return "vsan" in (typ or "").lower() or "vsan" in (name or "").lower()

    data = {}
    for c in clusters:
        data[name_of(c)] = {"hosts": [], "vms": [], "tags": [], "portgroups": [],
                            "workload": cl_workload.get(c["identifier"]),
                            "storage": {"cap_gb": 0.0, "used_gb": 0.0, "luns": []}}

    # vSphere-Tags je Cluster: vROps liefert sie als EIGENSCHAFTEN der Ressource
    # (/resources/{id}/properties), nicht über einen Tag-Endpunkt. Ohne
    # --tag-property werden alle Eigenschaften genommen, deren Schlüssel "tag"
    # enthält; das Log nennt die erkannten Schlüssel zur Kontrolle.
    report(80)
    log("Lese vSphere-Tags der Cluster (Eigenschaften) ...")
    seen_keys, raw_sample = set(), []
    for c in clusters:
        try:
            props = api.all_properties(c["identifier"])
        except Exception as e:
            log(f"Eigenschaften nicht lesbar – keine vSphere-Tags: {e}")
            break
        tags = []
        for k, v in sorted(props.items()):
            kl = k.lower()
            if tag_property:
                if not kl.startswith(tag_property.lower()):
                    continue
            elif "tag" not in kl:
                continue
            val = str(v or "").strip()
            if not val:
                continue
            seen_keys.add(k)
            if val[:1] in ("[", "{") and not raw_sample:
                raw_sample.append(f"{k} = {val[:200]}")
            for label in tag_labels(k, val):
                if label not in tags:
                    tags.append(label)
        data[name_of(c)]["tags"] = sorted(tags)
    found = sum(len(d["tags"]) for d in data.values())
    log(f"vSphere-Tags gefunden: {found}"
        + (f" · Eigenschaften: {', '.join(sorted(seen_keys))}" if seen_keys
           else " – keine Eigenschaft mit 'tag' im Namen gefunden"))
    if raw_sample:
        log(f"Tag-Rohwert (Auszug): {raw_sample[0]}")

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
        if rid in exclude_vms:
            continue  # per Tag ausgeschlossen (z. B. Kapa_Filter:Ja)
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
    # Je Cluster genügt EIN Host: alle Hosts eines Clusters sehen dieselben LUNs
    # (vSAN/FC). Das spart Abfragen (1 je Cluster statt 1 je Datastore). Liefert
    # ein Host nichts (z. B. Wartung), wird der nächste versucht.
    ds_by_id = {d["identifier"]: d for d in datastores}
    if datastores:
        log(f"Ordne {len(datastores)} Datastores den Clustern zu (über Hosts) ...")
    for cl in data:
        cl_hosts = [h["identifier"] for h in hosts
                    if host_cluster.get(h["identifier"]) == cl]
        seen = []
        for hid in cl_hosts:
            try:
                seen = api.related(hid, {"Datastore"})
            except Exception as e:
                log(f"Datastore-Beziehungen für {cl} nicht lesbar: {e}")
                seen = []
            if seen:
                break
        for did in dict.fromkeys(seen):        # dedupliziert, Reihenfolge egal
            ds = ds_by_id.get(did)
            st = ds_stats.get(did) or {}
            cap = st.get("capacity|total_capacity")
            used = st.get("capacity|used_space")
            if not ds or (not cap and not used):
                continue
            nm = name_of(ds)
            typ = ds_type(did, nm)
            # vSAN spiegelt (RAID-1): brutto zählt nur anteilig als nutzbar.
            # Kapazität UND Belegung werden mit dem Faktor gerechnet, sonst
            # käme die Auslastung nicht hin (vROps meldet beide brutto).
            f = vsan_factor if is_vsan(typ, nm) else 1.0
            raw_cap, raw_used = float(cap or 0), float(used or 0)
            eff_cap, eff_used = raw_cap * f, raw_used * f
            data[cl]["storage"]["cap_gb"] += eff_cap
            data[cl]["storage"]["used_gb"] += eff_used
            data[cl]["storage"]["luns"].append({
                "name": nm,
                "type": typ or "unbekannt",
                "cap_gb": eff_cap,
                "used_gb": eff_used,
                "raw_cap_gb": raw_cap,
                "factor": f})
    if datastores:
        typen = sorted({l["type"] for d in data.values() for l in d["storage"]["luns"]})
        log("Storage zugeordnet: "
            + (", ".join(f"{cl}={len(d['storage']['luns'])} LUNs / "
                         f"{round(d['storage']['cap_gb'])} GB"
                         for cl, d in data.items() if d["storage"]["luns"])
               or "keinem Cluster (Beziehungen prüfen)")
            + (f" · Typen: {', '.join(typen)}" if typen else "")
            + (f" · vSAN mit Faktor {vsan_factor} gerechnet"
               if any(l["factor"] != 1.0 for d in data.values()
                      for l in d["storage"]["luns"]) else ""))

    # dvSwitches + Portgruppen je Cluster (Netzwerk-Reiter im Popup + VLAN-Suche).
    # Zuordnung wie beim Storage über die Hosts: dvSwitch -> HostSystem ->
    # summary|parentCluster. Ein dvSwitch kann sich über mehrere Cluster
    # erstrecken; er (und seine Portgruppen) erscheint dann bei jedem. Die
    # VLAN-Nummer ist best effort (Property-Schlüssel je vROps-Version anders).
    # Fehler = leerer Bereich, der Rest läuft weiter.
    VLAN_IN_NAME = re.compile(r"vlan[\s_\-]?(\d{1,4})", re.I)
    try:
        report(90)
        log("Lese dvSwitches und Portgruppen ...")
        dvs_list = api.resources("VmwareDistributedVirtualSwitch")
        pg_list = api.resources("DistributedVirtualPortgroup")
        pg_name = {p["identifier"]: name_of(p) for p in pg_list}

        # VLAN kommt je Portgruppe aus /resources/{id}/properties. Die Schlüssel
        # heißen je nach vROps-Version anders, und über die Bulk-Abfrage kommen
        # diese Config-Properties oft nicht zurück – deshalb pro Portgruppe die
        # Eigenschaften einzeln holen (gecacht) und jeden Schlüssel mit "vlan" im
        # Namen auswerten. "0" ist ein gültiger Wert (untagged), Trunk-Ranges wie
        # "0-4094" werden unverändert übernommen.
        pg_prop_cache = {}

        def pg_props(pid):
            if pid not in pg_prop_cache:
                try:
                    pg_prop_cache[pid] = api.all_properties(pid)
                except Exception:
                    pg_prop_cache[pid] = {}
            return pg_prop_cache[pid]

        def _clean_vlan(v):
            s = str(v).strip()
            return "" if s.lower() in ("", "none", "null") else s

        def pg_vlan(pid):
            props = pg_props(pid)
            vlanish = [(k, v) for k, v in props.items() if "vlan" in k.lower()]
            # bevorzugt einen konkreten ...vlanId-Wert
            for k, v in vlanish:
                if "vlanid" in k.lower().replace("|", "").replace("_", ""):
                    s = _clean_vlan(v)
                    if s:
                        return s
            # sonst irgendein vlan-Wert (z. B. Trunk-Range "0-4094")
            for k, v in vlanish:
                s = _clean_vlan(v)
                if s:
                    return s
            # Rückfall: VLAN aus dem Portgruppen-Namen (z. B. "...-VLAN205")
            m = VLAN_IN_NAME.search(pg_name.get(pid, ""))
            return m.group(1) if m else ""

        # Diagnose: echte Schlüssel/Werte der ersten Portgruppe ins Log, damit
        # sich der genaue VLAN-Schlüssel notfalls ablesen lässt.
        if pg_list:
            s0 = pg_props(pg_list[0]["identifier"])
            vlan0 = {k: s0[k] for k in s0 if "vlan" in k.lower()}
            log("Portgruppen-Properties (Beispiel 1. Portgruppe) – vlan-Schlüssel: "
                + (", ".join(f"{k}={s0[k]}" for k in vlan0) if vlan0
                   else "keiner mit 'vlan' im Namen"))
            log("Alle Property-Schlüssel der 1. Portgruppe: "
                + ((", ".join(sorted(s0)))[:600] or "keine"))

        # Der dvSwitch dient nur zur Zuordnung Portgruppe -> Cluster; sein Name
        # wird bewusst NICHT ausgeliefert (nur die Portgruppen je Cluster).
        for dv in dvs_list:
            did = dv["identifier"]
            try:
                dv_hosts = api.related(did, {"HostSystem"})
            except Exception as e:
                log(f"dvSwitch-Hosts nicht lesbar: {e}")
                dv_hosts = []
            dv_clusters = {host_cluster.get(h) for h in dv_hosts} & set(data)
            if not dv_clusters:
                continue
            try:
                dv_pg_ids = api.related(did, {"DistributedVirtualPortgroup"})
            except Exception:
                dv_pg_ids = []
            pgs = [{"name": pg_name.get(pid, "?"), "vlan": pg_vlan(pid)}
                   for pid in dict.fromkeys(dv_pg_ids) if pid in pg_name]
            for cl in dv_clusters:
                data[cl]["portgroups"].extend(pgs)
        # je Cluster nach Name deduplizieren und sortieren
        for d in data.values():
            seen, uniq = set(), []
            for pg in sorted(d["portgroups"], key=lambda x: x["name"].lower()):
                key = (pg["name"], pg["vlan"])
                if key not in seen:
                    seen.add(key)
                    uniq.append(pg)
            d["portgroups"] = uniq
        n_pg = sum(len(d["portgroups"]) for d in data.values())
        log(f"dvSwitches: {len(dvs_list)}, Portgruppen: {len(pg_list)} · zugeordnet: "
            + (", ".join(f"{cl}={len(d['portgroups'])}"
                         for cl, d in data.items() if d["portgroups"])
               or "keinem Cluster (Beziehungen prüfen)")
            + (f" · {n_pg} Portgruppen sichtbar" if n_pg else ""))
    except Exception as e:
        log(f"Netzwerk-Daten (dvSwitch) nicht verfügbar: {e}")

    report(100)
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
            "datastores": sorted(
                [{"name": l.get("name"),
                  "type": l.get("type") or "",
                  "cap_gb": round(float(l.get("cap_gb") or 0), 1),
                  "used_gb": round(float(l.get("used_gb") or 0), 1),
                  "raw_cap_gb": round(float(l.get("raw_cap_gb") or 0), 1),
                  "factor": float(l.get("factor") or 1.0)}
                 for l in (storage.get("luns") or [])],
                key=lambda l: l["cap_gb"], reverse=True),
            "vmCount": len(d["vms"]),
            "vmOff": sum(1 for v in d["vms"] if not v["on"]),
            "tags": list(d.get("tags") or []),
            "portgroups": list(d.get("portgroups") or []),
            "workload": d.get("workload"),
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
        # Beispiel-LUNs: ein großer vSAN-Datastore (Spiegelung -> 50 % nutzbar)
        # plus mehrere FC-LUNs (VMFS, brutto = nutzbar)
        raw = random.choice([40000, 80000])
        luns = [{"name": f"vsan-cl{ci}", "type": "vSAN", "factor": 0.5,
                 "raw_cap_gb": raw, "cap_gb": raw * 0.5, "used_gb": 0}]
        for li in range(1, random.randint(3, 7)):
            cap = random.choice([2000, 4000, 8000])
            luns.append({"name": f"FC-LUN-{ci}{li:02d}", "type": "VMFS",
                         "factor": 1.0, "raw_cap_gb": cap,
                         "cap_gb": cap, "used_gb": 0})
        for l in luns:
            l["used_gb"] = round(l["cap_gb"] * random.uniform(0.35, 0.9))
        cap_gb = sum(l["cap_gb"] for l in luns)
        used_gb = sum(l["used_gb"] for l in luns)
        tags = [f"Standort: {random.choice(['RZ-Nord', 'RZ-Sued'])}",
                f"Umgebung: {random.choice(['Produktion', 'Test'])}",
                f"Betreuung: {random.choice(['Team Netzwerk', 'Team Betrieb'])}",
                "Kapa_Filter: Nein"]
        # Beispiel-Portgruppen, die (wie in echt) IP-Netze im Namen tragen –
        # so lässt sich die VLAN-Suche offline ausprobieren.
        pgs = []
        for pi in range(random.randint(3, 6)):
            octet = 10 + ci * 10 + pi
            vlan = 100 * ci + pi
            zweck = random.choice(["Prod", "Test", "Mgmt", "Backup", "DMZ"])
            pgs.append({"name": f"PG-{zweck}-10.{ci}.{octet}.0_24-VLAN{vlan}",
                        "vlan": str(vlan)})
        # Eine Uplink-/Trunk-Portgruppe (VLAN 0-4094) – wird standardmäßig
        # ausgeblendet (siehe --show-uplink-portgroups).
        pgs.append({"name": f"dvSwitch-{cl}-DVUplinks", "vlan": "0-4094"})
        pgs.sort(key=lambda x: x["name"].lower())
        data[cl] = {"hosts": hosts, "vms": vms, "tags": tags, "portgroups": pgs,
                    "workload": random.choice([38, 52, 61, 74, 83]),
                    "storage": {"cap_gb": cap_gb, "used_gb": used_gb, "luns": luns}}
    return data

# ------------------------------------------------------------------ Dashboard --

def openapi_spec():
    """OpenAPI-3.0-Beschreibung der lesenden v1-API. Importierbar in Swagger
    Editor/Postman; die eingebaute Seite /api/v1/docs rendert sie direkt."""
    reservation = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Eindeutige ID"},
            "name": {"type": "string", "description": "Bezeichnung / Projekt"},
            "change": {"type": "string", "description": "Change-Nummer / Jira-Ticket (optional)"},
            "cluster": {"type": "string"},
            "vcpu": {"type": "integer"},
            "ram_gb": {"type": "integer"},
            "storage_gb": {"type": "integer", "description": "nur informativ"},
            "von": {"type": "string", "description": "Anforderer"},
            "abteilung": {"type": "string", "description": "Team/Abteilung"},
            "created": {"type": "string", "description": "gilt ab (ISO-Datum)"},
            "approvals": {"type": "array", "description": "bisherige Team-Freigaben (Prüfreihenfolge)",
                          "items": {"type": "object", "properties": {
                              "team": {"type": "string"}, "by": {"type": "string"},
                              "on": {"type": "string"}, "comment": {"type": "string"}}}},
            "approved": {"type": "boolean", "description": "vollständig genehmigt (alle Stufen)"},
            "approved_on": {"type": "string"}, "approved_by": {"type": "string"},
            "rejected": {"type": "boolean"}, "rejected_on": {"type": "string"},
            "rejected_by": {"type": "string"}, "rejected_team": {"type": "string"},
            "cancelled": {"type": "boolean"}, "cancelled_on": {"type": "string"},
            "cancelled_by": {"type": "string"}, "comment": {"type": "string"},
        },
    }
    cluster = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "hostCount": {"type": "integer"}, "cores": {"type": "integer"},
            "vcpuCap": {"type": "number"}, "vcpuUsed": {"type": "number"},
            "vcpuFree": {"type": "number", "description": "vor Abzug genehmigter Reservierungen"},
            "ramCap": {"type": "number"}, "ramUsed": {"type": "number"}, "ramFree": {"type": "number"},
            "storageCap": {"type": "number"}, "storageUsed": {"type": "number"}, "storageFree": {"type": "number"},
            "vmCount": {"type": "integer"}, "vmOff": {"type": "integer"},
            "workload": {"type": "integer", "nullable": True,
                         "description": "vROps-Workload-Badge in % (nicht für Anforderer)"},
            "portgroups": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "vlan": {"type": "string"}}}},
        },
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "VMware Kapazitätsplanung – API",
            "version": VERSION,
            "description": "Stabile, **nur lesende** REST-API. Authentifizierung "
                           "per Bearer-Token (Admins erzeugen es im Tab „Verwaltung“) "
                           "oder per Browser-Session.",
        },
        "servers": [{"url": "/", "description": "dieser Server (hinter einem "
                     "Proxy-Unterpfad wie /capa entsprechend anpassen)"}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer",
                               "description": "Header: Authorization: Bearer kapa_…"},
                "cookieAuth": {"type": "apiKey", "in": "cookie", "name": "kapa_session"},
            },
            "schemas": {"Reservation": reservation, "Cluster": cluster,
                        "Status": {"type": "object", "properties": {
                            "version": {"type": "string"}, "updated": {"type": "string"},
                            "refreshing": {"type": "boolean"},
                            "next": {"type": "integer", "nullable": True,
                                     "description": "Sekunden bis zur nächsten Aktualisierung"}}},
                        "Data": {"type": "object", "properties": {
                            "updated": {"type": "string"},
                            "clusters": {"type": "array", "items": {"$ref": "#/components/schemas/Cluster"}}}}},
        },
        "security": [{"bearerAuth": []}, {"cookieAuth": []}],
        "paths": {
            "/api/v1/status": {"get": {
                "summary": "Status & Aktualität",
                "description": "Version, Zeitpunkt des letzten Aria-Abrufs, ob gerade "
                               "aktualisiert wird und Sekunden bis zum nächsten Abruf.",
                "responses": {"200": {"description": "OK", "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Status"}}}},
                    "401": {"description": "Token/Anmeldung fehlt oder ungültig"}}}},
            "/api/v1/data": {"get": {
                "summary": "Cluster-Kapazitäten",
                "description": "Cluster-Kennzahlen aus dem letzten Aria-Abruf. "
                               "vcpuFree/ramFree sind VOR Abzug genehmigter Reservierungen.",
                "responses": {"200": {"description": "OK", "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Data"}}}},
                    "401": {"description": "Token/Anmeldung fehlt oder ungültig"}}}},
            "/api/v1/reservations": {"get": {
                "summary": "Reservierungen (Kapazitätsanfragen)",
                "description": "Alle Reservierungen. Kombinierbare Filter; als CSV mit "
                               "format=csv (Semikolon, Excel-tauglich).",
                "parameters": [
                    {"name": "cluster", "in": "query", "schema": {"type": "string"},
                     "description": "nur dieses Cluster"},
                    {"name": "abteilung", "in": "query", "schema": {"type": "string"},
                     "description": "nur dieses Team/diese Abteilung"},
                    {"name": "status", "in": "query", "schema": {"type": "string",
                     "enum": ["beantragt", "in Prüfung", "genehmigt", "abgelehnt", "storniert"]}},
                    {"name": "format", "in": "query", "schema": {"type": "string",
                     "enum": ["json", "csv"], "default": "json"}},
                ],
                "responses": {"200": {"description": "OK", "content": {
                    "application/json": {"schema": {"type": "array",
                        "items": {"$ref": "#/components/schemas/Reservation"}}},
                    "text/csv": {"schema": {"type": "string"}}}},
                    "401": {"description": "Token/Anmeldung fehlt oder ungültig"}}}},
        },
    }


# Selbst-enthaltene, offline lauffähige API-Doku (kein CDN/Swagger-UI nötig);
# rendert /api/v1/openapi.json und bietet ein einfaches „Ausführen" je Endpunkt.
API_DOCS_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>API-Dokumentation – VMware Kapazitätsplanung</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --text:#e2e8f0; --muted:#94a3b8;
          --accent:#38bdf8; --ok:#22c55e; --get:#0ea5e9; }
  * { box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font:14px/1.55 "Segoe UI",system-ui,sans-serif;
         margin:0; padding:24px; }
  .wrap { max-width:960px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 2px; }
  .sub { color:var(--muted); margin-bottom:20px; }
  a { color:var(--accent); }
  .authbox { background:var(--card); border:1px solid var(--line); border-radius:12px;
             padding:14px 16px; margin-bottom:20px; }
  .authbox label { font-size:12px; color:var(--muted); display:block; margin-bottom:4px; }
  .authbox input { width:100%; background:#0b1220; border:1px solid var(--line); color:var(--text);
                   border-radius:8px; padding:9px 12px; font-size:13px; font-family:monospace; }
  .hint { color:var(--muted); font-size:12px; margin-top:6px; }
  .ep { background:var(--card); border:1px solid var(--line); border-radius:12px;
        margin-bottom:14px; overflow:hidden; }
  .ephead { display:flex; align-items:center; gap:10px; padding:12px 16px; cursor:pointer; }
  .method { font-weight:700; font-size:12px; padding:3px 8px; border-radius:6px;
            background:rgba(14,165,233,.15); color:var(--get); letter-spacing:.5px; }
  .path { font-family:monospace; font-size:14px; }
  .summary { color:var(--muted); margin-left:auto; font-size:13px; }
  .epbody { padding:0 16px 16px; border-top:1px solid var(--line); }
  .epbody p { color:var(--muted); }
  table { border-collapse:collapse; width:100%; font-size:13px; margin:8px 0; }
  th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:600; }
  td input { background:#0b1220; border:1px solid var(--line); color:var(--text);
             border-radius:6px; padding:5px 8px; font-size:13px; width:100%; }
  .btn { background:var(--accent); color:#08131f; border:none; border-radius:8px;
         padding:8px 14px; font-size:13px; font-weight:600; cursor:pointer; margin-top:6px; }
  pre { background:#0b1220; border:1px solid var(--line); border-radius:8px; padding:12px;
        overflow:auto; font-size:12px; max-height:360px; }
  .status { font-weight:600; margin:8px 0 4px; }
  .foot { color:var(--muted); font-size:12px; margin-top:24px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>API-Dokumentation <span style="color:var(--muted);font-weight:400">· v__VERSION__</span></h1>
  <div class="sub">Lesende REST-API unter <code>/api/v1/</code> ·
    <a href="openapi.json">OpenAPI-Spec (JSON)</a> – importierbar in Swagger Editor/Postman.</div>

  <div class="authbox">
    <label for="tok">Bearer-Token (im Dashboard unter „Verwaltung → API-Tokens" erzeugen)</label>
    <input id="tok" placeholder="kapa_…" autocomplete="off" spellcheck="false">
    <div class="hint">Wird nur lokal im Browser gespeichert und bei „Ausführen" als
      <code>Authorization: Bearer …</code> mitgeschickt. Angemeldete Admins können auch ohne
      Token testen (Session-Cookie).</div>
  </div>

  <div id="eps"></div>
  <div class="foot">Self-contained – kein externes Swagger-UI/CDN. Details je Feld: siehe OpenAPI-Spec.</div>
</div>
<script>
const BASE = location.pathname.replace(/\/docs\/?$/, "");   // /api/v1  (auch hinter Proxy)
const tokEl = document.getElementById("tok");
try { tokEl.value = localStorage.getItem("kapa_api_token") || ""; } catch (e) {}
tokEl.addEventListener("input", () => { try { localStorage.setItem("kapa_api_token", tokEl.value); } catch (e) {} });

function esc(s){ return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

fetch("openapi.json").then(r => r.json()).then(spec => {
  const box = document.getElementById("eps");
  Object.keys(spec.paths).forEach(p => {
    const op = spec.paths[p].get; if (!op) return;
    const id = p.replace(/\W+/g, "_");
    const params = op.parameters || [];
    const prows = params.map(pa => `<tr><td style="width:130px"><code>${esc(pa.name)}</code></td>
      <td>${esc(pa.description || "")}${pa.schema && pa.schema.enum ? ' <span style="color:var(--muted)">('+pa.schema.enum.map(esc).join(" | ")+')</span>' : ''}</td>
      <td style="width:180px"><input data-p="${esc(pa.name)}" data-ep="${id}" placeholder="${pa.schema && pa.schema.enum ? esc(pa.schema.enum[0]) : ''}"></td></tr>`).join("");
    const ptable = params.length ? `<table><tr><th>Parameter</th><th>Bedeutung</th><th>Wert (optional)</th></tr>${prows}</table>` : "";
    box.insertAdjacentHTML("beforeend", `<div class="ep">
      <div class="ephead" onclick="var b=this.nextElementSibling; b.style.display = b.style.display==='none'?'':'none';">
        <span class="method">GET</span><span class="path">${esc(p)}</span>
        <span class="summary">${esc(op.summary || "")}</span></div>
      <div class="epbody" style="display:none">
        <p>${esc(op.description || "")}</p>
        ${ptable}
        <button class="btn" onclick="run('${id}','${esc(p)}')">Ausführen</button>
        <div class="status" id="st_${id}"></div>
        <pre id="out_${id}" style="display:none"></pre>
      </div></div>`);
  });
}).catch(() => { document.getElementById("eps").textContent = "OpenAPI-Spec nicht ladbar."; });

function run(id, path) {
  const st = document.getElementById("st_" + id), out = document.getElementById("out_" + id);
  const qs = [];
  document.querySelectorAll(`input[data-ep="${id}"]`).forEach(i => {
    if (i.value.trim()) qs.push(encodeURIComponent(i.dataset.p) + "=" + encodeURIComponent(i.value.trim()));
  });
  const url = BASE + path.replace(/^\/api\/v1/, "") + (qs.length ? "?" + qs.join("&") : "");
  const h = {};
  if (tokEl.value.trim()) h["Authorization"] = "Bearer " + tokEl.value.trim();
  st.textContent = "… " + url;
  fetch(url, { headers: h }).then(async r => {
    const ct = r.headers.get("content-type") || "";
    const body = await r.text();
    st.textContent = "HTTP " + r.status + " · " + url;
    out.style.display = "";
    out.textContent = ct.includes("json") ? JSON.stringify(JSON.parse(body), null, 2) : body;
  }).catch(e => { st.textContent = "Fehler: " + e.message; out.style.display = "none"; });
}
</script>
</body>
</html>"""


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
  <p style="margin:6px 0 0;text-align:center;color:#64748b;font-size:11px">__CONTACT__</p>
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
  .hovercard { position:fixed; z-index:20; width:1180px; max-width:96vw; display:none;
               height:auto; max-height:86vh; min-width:420px; min-height:220px;
               overflow:auto; resize:both; border-radius:12px;
               box-shadow:0 14px 44px rgba(0,0,0,.55); }
  .hovercard .card { border-color:#3b5479; }
  .hovercard .card h2 { cursor:move; user-select:none; }
  .hovercard .card h2::before { content:"⠿ "; color:var(--muted); font-size:14px; }
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
  .ctabs { margin:0 0 14px; }
  .ctabs .tab { padding:5px 11px; font-size:12px; }
  .tagbox { margin-top:14px; border-top:1px solid var(--line); padding-top:10px; }
  .tagbox h3 { font-size:12px; color:var(--muted); font-weight:600; margin-bottom:6px; }
  .tag { display:inline-block; background:#0b1220; border:1px solid var(--line);
         border-radius:6px; padding:2px 8px; margin:0 4px 4px 0;
         font-size:11px; color:var(--text); }
  .selbar { display:flex; align-items:center; gap:12px; flex-wrap:wrap;
            background:#0b1220; border:1px solid var(--line); border-radius:10px;
            padding:10px 14px; margin-bottom:12px; }
  .selbar .sellabel { font-size:12px; color:var(--muted); font-weight:600; }
  .selbar label { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--muted); }
  .selbar select { background:#0f172a; border:1px solid var(--line); color:var(--text);
                   border-radius:6px; padding:5px 8px; font-size:13px; }
  .selbar .btn { padding:5px 10px; }
  .netbox { margin-top:12px; }
  .netbox:first-child { margin-top:0; }
  .netbox h3 { font-size:13px; color:var(--text); margin-bottom:6px; }
  .vlanbar { display:flex; align-items:center; gap:12px; margin-bottom:12px; }
  .vlanbar input { flex:1; max-width:520px; background:#0f172a; border:1px solid var(--line);
                   color:var(--text); border-radius:8px; padding:9px 12px; font-size:14px; }
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
  .subtabs { margin-bottom:18px; }
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
  a.btn { text-decoration:none; display:inline-block; line-height:normal; }
  .resbox { margin-top:14px; border-top:1px solid var(--line); padding-top:10px; }
  .resbox h3 { font-size:13px; color:var(--res); margin-bottom:6px; }
  .resform { display:grid; grid-template-columns:2fr 1.4fr 80px 90px 90px; gap:6px; margin-top:8px; }
  .resform input { background:#0b1220; border:1px solid var(--line); color:var(--text);
                   border-radius:6px; padding:5px 8px; font-size:12px; width:100%; }
  .resform input:focus { outline:none; border-color:var(--res); }
  .resform button { grid-column:1 / -1; }   /* Beantragen-Knopf über die volle Breite */
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
<div class="sub">
  <button class="btn" onclick="showInfo('infoCalc','Info Kapa-Berechnung')">ℹ Info Kapa-Berechnung</button>
  <button class="btn" onclick="showInfo('infoHelp','Hilfe')">? Hilfe</button>
  <span style="color:var(--muted);font-size:12px;margin-left:8px">Stand: <span id="stand">__DATE__</span></span>
</div>
<div id="infoCalc" style="display:none">Quelle: VMware Aria Operations · CPU-Überprovisionierung: Faktor __FACTOR__ (physische Cores) · RAM 1:1 · alle VMs inkl. powered-off · „frei" berücksichtigt genehmigte Reservierungen__FAILNOTE__</div>
<div id="infoHelp" style="display:none">Klick auf den Clusternamen zeigt Details und Reservierungen. __RESNOTE__</div>
<div class="modal-bg" id="infoBg" onclick="if(event.target===this)closeInfo()">
  <div class="modal">
    <h2 id="infoTitle"></h2>
    <div id="infoBody" style="font-size:13px;line-height:1.6;color:var(--text)"></div>
    <div class="actions"><button class="btn primary" onclick="closeInfo()">Schließen</button></div>
  </div>
</div>
<div class="toolbar">
  <input class="filterbox" id="filter" type="search" placeholder="Cluster filtern …" oninput="render()">
  <button class="btn primary" id="newReqBtn" onclick="openModal()">+ Neue Kapazitätsanfrage</button>
  <button class="btn" id="refreshBtn" onclick="refreshData()">⟳ Jetzt aktualisieren</button>
  <span id="refreshStatus" style="font-size:12px;color:var(--muted)"></span>
  <span id="timer" style="font-size:12px;color:var(--muted);margin-left:auto"></span>
  <a class="btn" id="csvBtn" href="api/v1/reservations?format=csv"
     download="reservierungen.csv" title="Reservierungen als CSV (Semikolon, für Excel)">CSV exportieren</a>
  <button class="btn" onclick="exportRes()">Reservierungen exportieren (JSON)</button>
  <label class="btn" id="importBtn">Reservierungen importieren (JSON)<input type="file" accept=".json" hidden onchange="importRes(event)"></label>
  <span id="userbox" style="font-size:12px;color:var(--muted)"></span>
  <button class="btn" id="logoutBtn" style="display:none" onclick="logout()">Abmelden</button>
</div>
<div class="modal-bg" id="modalBg" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <h2>Neue Kapazitätsanfrage</h2>
    <label>Ziel-Cluster</label>
    <input id="mClusterSearch" type="search" placeholder="Cluster suchen …"
           oninput="fillClusterSelect()" autocomplete="off" style="margin-bottom:6px">
    <select id="mCluster" size="1" onchange="modalHint()"></select>
    <label>Bezeichnung / Projekt</label>
    <input id="mName" placeholder="z. B. SAP-Erweiterung Q4">
    <label>Change / Jira Ticket (optional)</label>
    <input id="mChange">
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
<div class="modal-bg" id="askBg" onclick="if(event.target===this)askClose(false)">
  <div class="modal">
    <h2 id="askTitle"></h2>
    <div class="hint" id="askMsg" style="color:var(--muted);margin-bottom:16px;white-space:pre-line"></div>
    <div class="actions">
      <button class="btn" id="askCancel" onclick="askClose(false)">Abbrechen</button>
      <button class="btn primary" id="askOk" onclick="askClose(true)">OK</button>
    </div>
  </div>
</div>
<div class="tabs">
  <span class="tab active" id="tabKapa" onclick="setView('kapa')">Kapazität</span>
  <span class="tab" id="tabVlan" onclick="setView('vlan')">VLAN-Suche</span>
  <span class="tab" id="tabRes" onclick="setView('res')">Reservierungen</span>
  <span class="tab" id="tabApp" onclick="setView('app')">Genehmigungen</span>
  <span class="tab" id="tabArch" onclick="setView('arch')">Archiv</span>
  <span class="tab" id="tabAdm" onclick="setView('adm')">Verwaltung</span>
  <span class="tab" id="tabLog" onclick="setView('log')">Log</span>
</div>
<div id="kapaView">
<div class="selbar" id="clusterSelector" style="display:none"></div>
<div class="tablewrap">
<table class="kt" id="ktable">
  <thead><tr><th>Cluster</th><th class="num">Hosts</th><th class="num">VMs</th>
    <th class="num">vCPU frei</th><th class="barcol">vCPU-Auslastung</th>
    <th class="num">RAM frei (GB)</th><th class="barcol">RAM-Auslastung</th>
    <th class="num">Storage frei (GB)</th><th class="barcol">Storage-Auslastung</th>
    <th class="num">Res.</th></tr></thead>
  <tbody id="ktbody"></tbody>
</table>
</div>
</div>
<div id="vlanView" style="display:none">
<div class="hint" style="color:var(--muted);margin:4px 0 10px">
  Portgruppen aller Cluster durchsuchen – z. B. nach einer IP-Adresse oder
  einem Netz aus dem Portgruppen-Namen. Das Ergebnis zeigt, an welchem Cluster
  das Netz hängt. Teil-Eingaben genügen
  (z. B. <code>10.2.30</code> oder <code>VLAN205</code>).</div>
<div class="vlanbar">
  <input id="vlanQ" placeholder="IP-Adresse, Netz oder Portgruppen-Name suchen …"
         oninput="renderVlan()" autocomplete="off">
  <span id="vlanCount" style="color:var(--muted);font-size:13px"></span>
</div>
<div class="tablewrap">
<table class="kt" id="vtable">
  <thead><tr><th>Portgruppe</th><th class="num">VLAN</th><th>Cluster</th></tr></thead>
  <tbody id="vtbody"></tbody>
</table>
</div>
</div>
<div id="resView" style="display:none">
<div class="vlanbar" style="margin-bottom:12px">
  <input id="resSearch" class="filterbox" style="max-width:520px" type="search"
         placeholder="Reservierungen durchsuchen – Name, Cluster, Change, Anforderer, Team, ID, Status …"
         oninput="renderResTable()" autocomplete="off">
  <span id="resCount" style="color:var(--muted);font-size:13px"></span>
</div>
<div class="tablewrap">
<table class="kt" id="rtable">
  <thead><tr><th>ID</th><th>Anfrage / Projekt</th><th>Cluster</th><th>Change</th><th class="num">vCPU</th>
    <th class="num">RAM (GB)</th><th class="num">Storage (GB)</th><th>von</th><th>Team</th><th>gilt ab</th><th>gültig bis</th><th>Status</th><th id="thDec">entschieden von</th><th>Kommentar</th><th class="nosort"></th></tr></thead>
  <tbody id="rtbody"></tbody>
</table>
</div>
</div>
<div class="tablewrap" id="appView" style="display:none">
<table class="kt" id="atable">
  <thead><tr><th>ID</th><th>Anfrage / Projekt</th><th>Cluster</th><th>Change</th><th class="num">vCPU</th>
    <th class="num">RAM (GB)</th><th class="num">Storage (GB)</th>
    <th class="num" title="Frei im Ziel-Cluster nach genehmigten Reservierungen">Cluster frei vCPU</th>
    <th class="num" title="Frei im Ziel-Cluster nach genehmigten Reservierungen">Cluster frei RAM</th>
    <th>von</th><th>Team</th><th>beantragt am</th><th>Fortschritt</th><th class="nosort">Aktion</th></tr></thead>
  <tbody id="atbody"></tbody>
</table>
</div>
<div id="archView" style="display:none">
<div class="hint" style="color:var(--muted);margin:4px 0 10px">
  Archiv der <b>abgelehnten</b> und <b>stornierten</b> Kapazitätsanfragen (Historie,
  zählt nicht gegen die Kapazität). Sichtbarkeit wie bei den Reservierungen:
  Anforderer sehen die des eigenen Teams, Reviewer/Admin/Auditor alle.</div>
<div class="vlanbar" style="margin-bottom:12px">
  <input id="archSearch" class="filterbox" style="max-width:520px" type="search"
         placeholder="Archiv durchsuchen – Name, Cluster, Change, Anforderer, Team, ID, Status …"
         oninput="renderArchiveTable()" autocomplete="off">
  <span id="archCount" style="color:var(--muted);font-size:13px"></span>
</div>
<div class="tablewrap">
<table class="kt" id="artable">
  <thead><tr><th>ID</th><th>Anfrage / Projekt</th><th>Cluster</th><th>Change</th><th class="num">vCPU</th>
    <th class="num">RAM (GB)</th><th class="num">Storage (GB)</th><th>von</th><th>Team</th>
    <th>angelegt</th><th>erledigt am</th><th>Status</th><th>durch</th><th>Kommentar</th></tr></thead>
  <tbody id="arbody"></tbody>
</table>
</div>
</div>
<div id="admView" style="display:none">
<div class="tabs subtabs">
  <span class="tab active" id="atabUsers" onclick="setAdmTab('users')">Benutzer &amp; Rollen</span>
  <span class="tab" id="atabMail" onclick="setAdmTab('mail')">Mail</span>
  <span class="tab" id="atabConf" onclick="setAdmTab('conf')">Backup &amp; Konfiguration</span>
</div>

<div id="admGrpUsers">
<div class="sechead">Benutzer und Rollen</div>
<div class="tablewrap">
<table class="kt" id="mtable">
  <thead><tr><th>Typ</th><th>Benutzer / AD-Gruppe</th><th>Rolle</th><th>Team</th><th class="nosort">Aktion</th></tr></thead>
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
  (Admin genehmigt direkt). Reviewer werden oben ihrem Team zugewiesen.
  Die <b>E-Mail/Verteiler</b> je Team wird angeschrieben, sobald das Team im
  Workflow an der Reihe ist (siehe „Mail-Benachrichtigungen"); mit „✓ Team-Adressen
  speichern" sichern.</div>
<div class="tablewrap">
<table class="kt" id="tmtable">
  <thead><tr><th style="width:60px">Stufe</th><th>Team</th><th>E-Mail / Verteiler (Team ist dran)</th><th style="width:220px" class="nosort">Aktion</th></tr></thead>
  <tbody id="tmbody"></tbody>
</table>
</div>
<button class="btn approve" style="margin-top:8px" onclick="saveNotify()">✓ Team-Adressen speichern</button>

<div class="sechead" style="margin-top:20px">Cluster-Selektor (Filter nach vSphere-Tags)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Bis zu 3 Stufen, jede Stufe eine Tag-Kategorie. In der Kapazitätsübersicht
  erscheinen dann kaskadierende Auswahllisten (Stufe 2 zeigt nur Werte, die zur
  Wahl in Stufe 1 passen). Die Kategorien kommen aus den vorhandenen
  Cluster-Tags – sind noch keine Daten geladen, ist die Liste leer.</div>
<div class="tablewrap">
<table class="kt" id="seltable">
  <thead><tr><th style="width:60px">Stufe</th><th style="width:320px">Tag-Kategorie</th><th>Anzeigename im Selektor</th></tr></thead>
  <tbody id="selbody"></tbody>
</table>
</div>
<div class="sechead" style="margin-top:20px">API-Tokens für externe Anwendungen (nur lesend, Endpunkte unter /api/v1/)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  📖 <a href="api/v1/docs" target="_blank" rel="noopener">API-Dokumentation öffnen</a>
  (interaktiv, mit „Ausführen") · <a href="api/v1/openapi.json" target="_blank" rel="noopener">OpenAPI-Spec</a>
  zum Import in Swagger/Postman.</div>
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
</div><!-- admGrpUsers -->

<div id="admGrpMail" style="display:none">
<div class="sechead">Mail-Benachrichtigungen (je interner Rolle)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Legt fest, bei welchem Ereignis eine Mail rausgeht. <b>Anforderer</b> = der
  jeweilige Antragsteller (automatisch). <b>Admin/Auditor</b> = die eingetragene
  Verteiler-Adresse. <b>Reviewer / „Team ist dran"</b> = die Team-Adresse aus der
  Teams-Tabelle (Reiter „Benutzer &amp; Rollen"). Voraussetzung ist ein
  konfigurierter SMTP-Server. „Freigabe" meint die endgültige Genehmigung.</div>
<div class="tablewrap">
<table class="kt" id="notifytable">
  <thead><tr><th style="width:150px">Interne Rolle</th><th>Verteiler-Adresse</th>
    <th class="num nosort">Anlage</th><th class="num nosort">Ablehnung</th>
    <th class="num nosort">Freigabe</th><th class="num nosort">Team ist dran</th></tr></thead>
  <tbody id="ntbody"></tbody>
</table>
</div>
<button class="btn approve" style="margin-top:8px" onclick="saveNotify()">✓ Mail-Regeln speichern</button>
<span id="notifySaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
<div class="sechead" style="margin-top:20px">SMTP / Versand (aus der Konfiguration)</div>
<div id="configMail"></div>
</div><!-- admGrpMail -->

<div id="admGrpConf" style="display:none">
<div id="backupSection" style="display:none">
  <div class="sechead">Backup</div>
  <div class="hint" style="color:var(--muted);margin-bottom:8px">
    Sichert alle Laufzeitdaten (Reservierungen, Rollen, Teams, Selektor, Log,
    Tokens) als tar.gz auf das konfigurierte SFTP-Ziel. Läuft automatisch nach
    dem konfigurierten Intervall – hier lässt sich ein Backup sofort auslösen.</div>
  <button class="btn primary" id="backupBtn" onclick="runBackup()">💾 Backup jetzt erstellen</button>
  <span id="backupStatus" style="font-size:12px;margin-left:10px"></span>
</div>
<div class="sechead" style="margin-top:20px">Konfiguration (schreibgeschützt)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Die im System gesetzten Werte (aus INI, kapa.env bzw. Kommandozeile). Nur zur
  Ansicht – Änderungen erfolgen in der Konfiguration und erfordern einen Neustart.
  <b>Passwörter werden nie angezeigt</b> (nur, ob gesetzt).</div>
<div id="configSheet"></div>
</div><!-- admGrpConf -->
</div>
<div class="tablewrap" id="logView" style="display:none">
<table class="kt" id="ltable">
  <thead><tr><th>Zeit</th><th>Benutzer</th><th>Aktion</th><th>Details</th></tr></thead>
  <tbody id="ltbody"></tbody>
</table>
</div>
<div class="hovercard" id="hovercard"></div>
<div class="foot">VMware Kapazitätsplanung · Version __VERSION____CONTACT_FOOT__</div>
<script>
let CLUSTERS = __DATA__;
const FACTOR = __FACTOR__;
const SERVE = __SERVE__;
const TTL = __TTL__;
const ME = __USERINFO__;   // {user, role} bei aktivierter AD-Anmeldung, sonst null
let TEAMS = __TEAMS__;      // Genehmigungs-Teams in Prüfreihenfolge (leer = einstufig); auf der Verwaltungsseite pflegbar
let SELECTOR = __SELECTOR__; // Tag-Kategorien des Cluster-Selektors (max 3, kaskadierend)
let SEL_VALUES = {};         // gewählte Werte je Kategorie
const HAS_BACKUP = __BACKUP__; // ist ein SFTP-Backup-Ziel konfiguriert?
const LS_KEY = "aria_kapa_reservierungen";

// ---- vSphere-Tags / Cluster-Selektor ----
function tagCategories() {   // alle im Datenbestand vorkommenden Tag-Kategorien
  const set = new Set();
  CLUSTERS.forEach(c => (c.tags || []).forEach(t => {
    const i = t.indexOf(": "); if (i > 0) set.add(t.slice(0, i)); }));
  return [...set].sort();
}
function clusterTagVals(c, cat) {   // Werte, die Cluster c für Kategorie cat hat
  const p = cat + ": ";
  return (c.tags || []).filter(t => t.startsWith(p)).map(t => t.slice(p.length));
}
function selCat(i) { return SELECTOR[i] ? SELECTOR[i].category : ""; }
function selMatch(c) {       // erfüllt Cluster c alle gewählten Selektor-Stufen?
  return SELECTOR.every(s => {
    const v = SEL_VALUES[s.category];
    return !v || clusterTagVals(c, s.category).includes(v);
  });
}
// Werte für Stufe i – kaskadierend: nur die, die zu den höheren Stufen passen
function selectorOptions(i) {
  const upper = SELECTOR.slice(0, i);
  const base = CLUSTERS.filter(c => upper.every(s => {
    const v = SEL_VALUES[s.category]; return !v || clusterTagVals(c, s.category).includes(v); }));
  const vals = new Set();
  base.forEach(c => clusterTagVals(c, selCat(i)).forEach(v => vals.add(v)));
  return [...vals].sort();
}
function onSelectorChange(i, val) {
  SEL_VALUES[selCat(i)] = val;
  // tiefere Stufen zurücksetzen, wenn ihr Wert nicht mehr wählbar ist
  for (let j = i + 1; j < SELECTOR.length; j++) {
    const opts = selectorOptions(j);
    if (SEL_VALUES[selCat(j)] && !opts.includes(SEL_VALUES[selCat(j)]))
      SEL_VALUES[selCat(j)] = "";
  }
  render();
}
function resetSelector() { SEL_VALUES = {}; render(); }
function renderClusterSelector() {
  const box = document.getElementById("clusterSelector");
  if (!box) return;
  const active = SELECTOR.filter(s => tagCategories().includes(s.category));
  if (!active.length) { box.innerHTML = ""; box.style.display = "none"; return; }
  box.style.display = "";
  const any = SELECTOR.some(s => SEL_VALUES[s.category]);
  box.innerHTML = '<span class="sellabel">Cluster-Selektor:</span>' +
    SELECTOR.map((s, i) => {
      if (!tagCategories().includes(s.category)) return "";
      const opts = selectorOptions(i);
      const cur = SEL_VALUES[s.category] || "";
      return `<label>${esc(s.label || s.category)}
        <select onchange="onSelectorChange(${i}, this.value)">
          <option value="">alle</option>
          ${opts.map(v => `<option value="${esc(v)}" ${cur === v ? "selected" : ""}>${esc(v)}</option>`).join("")}
        </select></label>`;
    }).join("") +
    (any ? '<button class="btn" onclick="resetSelector()">Zurücksetzen</button>' : "");
}

// ---- Rollen ----
const ROLE = ME ? ME.role : "admin";          // ohne AD-Anmeldung: Vollzugriff
const IS_ADMIN = ROLE === "admin";
const IS_REVIEWER = ROLE === "reviewer";
const CAN_REQUEST = IS_ADMIN || ROLE === "anforderer";
// Rollen-Bezeichnungen sind frei wählbar (Verwaltung); Schlüssel bleiben fest.
let ROLE_NAMES = __ROLENAMES__;
const ROLE_ORDER = ["anforderer", "reviewer", "admin", "auditor"];
let NOTIFY = __NOTIFY__;    // Mail-Regeln je interner Rolle + Team-Adressen
// Welche Ereignisse je Rolle wählbar sind (Rest = "–"); Reihenfolge = Spalten
const NOTIFY_EVENTS = [["created","Anlage"],["rejected","Ablehnung"],["approved","Freigabe"],["team_turn","Team ist dran"]];
const NOTIFY_ROLE_EVENTS = {
  anforderer: ["created","rejected","approved"],
  admin:      ["created","rejected","approved","team_turn"],
  auditor:    ["created","rejected","approved","team_turn"],
  reviewer:   ["team_turn"],
};
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
function resFail() { notify("Reservierungen konnten nicht auf dem Server gespeichert werden."); }
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

async function createRes(c, name, change, vcpu, ram, storage, errEl) {
  errEl.style.display = "none";
  if (!name || (vcpu <= 0 && ram <= 0 && storage <= 0)) {
    errEl.textContent = "Bitte Bezeichnung sowie vCPU, RAM und/oder Storage angeben.";
    errEl.style.display = "block"; return false;
  }
  const ch = String(change || "").trim();   // Change/Jira optional, frei wählbar
  const f = freeAfter(c);
  const over = vcpu > f.cpu || ram > f.ram || (f.hasStor && storage > f.stor);
  if (over) {
    const goOn = await askConfirm({
      title: "Freie Kapazität überschritten",
      okLabel: "Trotzdem beantragen", okClass: "danger",
      message: "Die Reservierung überschreitet die freie Kapazität von " +
        esc(c.name) + ".\nFrei: " + fmt(f.cpu) + " vCPU / " + fmt(f.ram) + " GB RAM" +
        (f.hasStor ? " / " + fmt(f.stor) + " GB Storage" : "") + "." });
    if (!goOn) return false;
  }
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

// ---- Schicke Bestätigung / Hinweis (ersetzt native confirm()/alert()) ----
let _askResolve = null;
function askConfirm(opts) {
  return new Promise(resolve => {
    _askResolve = resolve;
    document.getElementById("askTitle").textContent = opts.title || "Bestätigen";
    document.getElementById("askMsg").textContent = opts.message || "";
    const ok = document.getElementById("askOk");
    ok.textContent = opts.okLabel || "OK";
    ok.className = "btn primary" + (opts.okClass ? " " + opts.okClass : "");
    const cancel = document.getElementById("askCancel");
    cancel.style.display = opts.hideCancel ? "none" : "";
    cancel.textContent = opts.cancelLabel || "Abbrechen";
    document.getElementById("askBg").classList.add("open");
    setTimeout(() => ok.focus(), 30);
  });
}
function notify(message, title) {
  return askConfirm({ title: title || "Hinweis", message: message, okLabel: "OK", hideCancel: true });
}
function askClose(val) {
  document.getElementById("askBg").classList.remove("open");
  const r = _askResolve; _askResolve = null;
  if (r) r(!!val);
}

// ---- Info-/Hilfe-Popup ----
function showInfo(srcId, title) {
  document.getElementById("infoTitle").textContent = title;
  document.getElementById("infoBody").innerHTML = document.getElementById(srcId).innerHTML;
  document.getElementById("infoBg").classList.add("open");
}
function closeInfo() { document.getElementById("infoBg").classList.remove("open"); }

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
function fillClusterSelect(prefIdx) {
  const sel = document.getElementById("mCluster");
  const q = (document.getElementById("mClusterSearch").value || "").trim().toLowerCase();
  const prev = sel.value;
  const opts = CLUSTERS.map((c, i) => ({ i: i, name: c.name }))
                       .filter(o => !q || o.name.toLowerCase().includes(q));
  sel.innerHTML = opts.length
    ? opts.map(o => `<option value="${o.i}">${esc(o.name)}</option>`).join("")
    : `<option value="">(kein Cluster passt)</option>`;
  const want = (prefIdx !== undefined && prefIdx !== null) ? String(prefIdx) : prev;
  if (want !== "" && opts.some(o => String(o.i) === want)) sel.value = want;
  modalHint();
}
function openModal(prefIdx) {
  document.getElementById("mClusterSearch").value = "";
  fillClusterSelect(prefIdx);
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
async function submitModal() {
  const c = CLUSTERS[+document.getElementById("mCluster").value];
  const v = id => document.getElementById(id).value;
  const ok = await createRes(c, v("mName").trim(), v("mChange"),
                       parseInt(v("mCpu")) || 0, parseInt(v("mRam")) || 0,
                       parseInt(v("mStorage")) || 0,
                       document.getElementById("mErr"));
  if (ok) closeModal();
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeModal(); cmtCancel(); closeInfo(); hideCard(); }
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
    if (!Array.isArray(d)) { notify("Ungültige Datei."); return; }
    if (SERVE) apiRes("PUT", "", d).then(setRes).catch(resFail);
    else { RES = d; saveLocal(); render(); }
  }).catch(() => notify("Datei konnte nicht gelesen werden."));
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
  const ds = (c.datastores || []).slice().sort(DS_SORT === "util"
    ? (a, b) => (b.used_gb / (b.cap_gb || 1)) - (a.used_gb / (a.cap_gb || 1))
    : (a, b) => b.cap_gb - a.cap_gb);
  const dsRows = ds.map(d => {
    const p = d.cap_gb ? Math.round(d.used_gb / d.cap_gb * 100) : 0;
    // Bei vSAN zeigen wir die NUTZBARE Kapazität (Faktor) – Brutto im Tooltip
    const net = (d.factor && d.factor !== 1)
      ? ` title="brutto ${fmt(Math.round(d.raw_cap_gb))} GB · mit Faktor ${d.factor} als nutzbar gerechnet"` : "";
    const typ = (d.factor && d.factor !== 1)
      ? `${esc(d.type)} <span style="color:var(--muted)">(${Math.round(d.factor * 100)} %)</span>`
      : esc(d.type || "–");
    return `<tr${net}><td>${esc(d.name)}</td><td>${typ}</td>
      <td class="num">${fmt(Math.round(d.cap_gb))}</td>
      <td class="num">${fmt(Math.round(d.used_gb))}</td>
      <td class="num" style="color:${color(p)}">${p}%</td>
      <td class="num">${fmt(Math.round((d.cap_gb - d.used_gb) * 10) / 10)}</td></tr>`;
  }).join("");
  const dsLink = m => `<a href="#" onclick="setDsSort('${m}');return false"${DS_SORT === m ? ' style="font-weight:600;color:var(--text)"' : ""}>${m === "size" ? "Größe" : "Belegung"}</a>`;
  const dsBlock = ds.length ? `
      <div style="margin:6px 0 8px;font-size:12px;color:var(--muted)">${ds.length} Datastores / LUNs · Sortierung: ${dsLink("size")} · ${dsLink("util")}${ds.some(d => d.factor && d.factor !== 1) ? " · vSAN wird als nutzbare Kapazität gerechnet (Spiegelung)" : ""}</div>
      <table><tr><th>Datastore / LUN</th><th>Typ</th><th class="num">Größe (GB)</th><th class="num">Belegt (GB)</th><th class="num">Belegt %</th><th class="num">Frei (GB)</th></tr>${dsRows}</table>`
    : `<div style="color:var(--muted);font-size:12px">Keine Storage-Daten aus Aria.</div>`;
  const tagBlock = (c.tags || []).length ? `
    <div class="tagbox">
      <h3>vSphere-Tags</h3>
      <div>${(c.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div>
    </div>` : "";
  // Reservierungen ANDERER Teams (foreign) tauchen für Anforderer nicht als
  // Zeile auf – sie zählen aber weiter in „reserviert"/„frei" hinein (rv).
  // Abgelehnte/stornierte liegen im Archiv und erscheinen hier nicht.
  const shownRes = clRes.filter(r => !r.foreign && !isArchived(r));
  const foreignN = clRes.filter(r => r.foreign).length;
  const resRows = shownRes.map(r =>
    `<tr><td>${esc(r.name)}${r.change ? ' <span style="color:var(--muted)">' + esc(r.change) + '</span>' : ''}${isTotal ? ' <span style="color:var(--muted)">(' + esc(r.cluster) + ')</span>' : ''}</td>
     <td class="num">${r.vcpu}</td><td class="num">${fmt(r.ram_gb)}</td><td class="num">${fmt(r.storage_gb || 0)}</td>
     <td>${fmtDate(validUntil(r))}</td><td>${stBadge(r)}</td>
     <td>${canCancel(r) ? `<button class="del" title="Anfrage stornieren" onclick="cancelRes('${esc(r.id)}')">⦸ Storno</button>` : ""}</td></tr>`).join("");
  const foreignNote = foreignN ? `<div style="color:var(--muted);font-size:12px;margin-top:6px">+ ${foreignN} genehmigte Reservierung(en) anderer Teams – in „reserviert“ berücksichtigt.</div>` : "";
  const resTable = (shownRes.length ?
    `<table><tr><th>Anfrage</th><th class="num">vCPU</th><th class="num">RAM (GB)</th><th class="num">Storage (GB)</th><th>gültig bis</th><th>Status</th><th></th></tr>${resRows}</table>`
    : `<div style="color:var(--muted);font-size:12px">Keine Reservierungen.</div>`) + foreignNote;
  const spare = (c.spareCores || c.spareRamGb) ?
    ` · Ausfallreserve (N+1): ${fmt(c.spareCores)} Cores / ${fmt(c.spareRamGb)} GB abgezogen` : "";
  // ---- Inhalte je Reiter ----
  const paneCpu = `
    ${metric("vCPU (Cores × " + FACTOR + ")", c.vcpuUsed, rvCpu, c.vcpuCap, "vCPU")}
    ${metric("RAM", c.ramUsed, rvRam, c.ramCap, "GB")}
    <div class="kpis">
      <div class="kpi">frei nach Reservierungen<b>${fmt(c.vcpuFree - rvCpu)} vCPU / ${fmt(Math.round((c.ramFree - rvRam)*10)/10)} GB</b></div>
      <div class="kpi">reserviert<b>${fmt(rvCpu)} vCPU / ${fmt(rvRam)} GB</b></div>
      <div class="kpi">Ø VM<b>${c.vmCount?Math.round(c.vcpuUsed/c.vmCount*10)/10:0} vCPU / ${c.vmCount?Math.round(c.ramUsed/c.vmCount):0} GB</b></div>
      ${(c.workload != null && ROLE !== "anforderer") ? `<div class="kpi">Workload (vROps)<b style="color:${color(c.workload)}">${c.workload} %</b></div>` : ""}
    </div>
    <div class="resbox">
      <h3>Kapazitätsreservierungen</h3>
      ${resTable}
      ${isTotal || !CAN_REQUEST ? "" : `
      <div class="resform">
        <input id="f${idx}n" placeholder="Bezeichnung / Projekt">
        <input id="f${idx}ch" placeholder="Change / Jira Ticket (optional)">
        <input id="f${idx}c" type="number" min="0" step="1" placeholder="vCPU">
        <input id="f${idx}r" type="number" min="0" step="1" placeholder="RAM GB">
        <input id="f${idx}s" type="number" min="0" step="1" placeholder="Storage GB">
        <button class="btn" onclick="addRes(${idx})">+ Beantragen</button>
      </div>
      <div class="err" id="f${idx}e"></div>`}
    </div>
    ${tagBlock}`;
  const paneStorage = `
    ${hasStor ? metric("Storage", c.storageUsed, rvStor, c.storageCap, "GB") : ""}
    ${hasStor ? `<div class="kpis">
      <div class="kpi">frei nach Reservierungen<b>${fmt(Math.round(((c.storageFree||0) - rvStor)*10)/10)} GB</b></div>
      <div class="kpi">reserviert<b>${fmt(rvStor)} GB</b></div>
    </div>` : ""}
    ${dsBlock}`;
  const paneHosts = `<table><tr><th>Host</th><th class="num">Cores</th><th class="num">RAM (GB)</th></tr>${hostRows}</table>`;
  const paneVms = `<table><tr><th>VM</th><th class="num">vCPU</th><th class="num">RAM (GB)</th></tr>${vmRows}</table>`;
  // ---- Netzwerk-Reiter: Portgruppen des Clusters (mit VLAN-/IP-Suche) ----
  const allPg = c.portgroups || [];
  const nPg = allPg.length;
  const nq = CARD_NET_Q.trim().toLowerCase();
  const shownPg = nq ? allPg.filter(p =>
      ((p.name || "") + " " + (p.vlan || "")).toLowerCase().includes(nq)) : allPg;
  const pgRows = shownPg.map(p =>
    `<tr><td>${esc(p.name)}</td><td class="num">${esc(p.vlan || "–")}</td></tr>`).join("")
    || `<tr><td colspan="2" style="color:var(--muted)">Keine Portgruppe passt zur Suche.</td></tr>`;
  const paneNet = nPg ? `<div class="netbox">
      <h3>Portgruppen <span style="color:var(--muted);font-weight:400">· ${nPg}</span></h3>
      <div class="vlanbar" style="margin-bottom:8px">
        <input id="cardNetQ" class="filterbox" style="max-width:340px"
               placeholder="VLAN / IP / Portgruppe suchen …" value="${esc(CARD_NET_Q)}"
               oninput="onCardNetInput()">
        <span id="cardNetCount" style="color:var(--muted);font-size:12px">${nq ? shownPg.length + " von " + nPg : ""}</span>
      </div>
      <table><thead><tr><th>Portgruppe</th><th class="num">VLAN</th></tr></thead>
        <tbody id="cardNetBody">${pgRows}</tbody></table>
    </div>`
    : `<div style="color:var(--muted);font-size:12px">Keine Portgruppen-Daten aus Aria.</div>`;

  // Gesamt-Karte hat keine Host-/VM-Listen
  const avail = isTotal ? [["cpu", "CPU & RAM"], ["storage", "Storage"]]
    : [["cpu", "CPU & RAM"], ["storage", "Storage"],
       ["net", "Netzwerk" + (nPg ? " (" + nPg + ")" : "")],
       ["hosts", "Hosts (" + (c.hosts || []).length + ")"],
       ["vms", "VMs (" + c.vmCount + ")"]];
  const tab = avail.some(t => t[0] === CARD_TAB) ? CARD_TAB : "cpu";
  const tabBar = `<div class="tabs ctabs">${avail.map(([k, l]) =>
    `<span class="tab ${tab === k ? "active" : ""}" onclick="setCardTab('${k}')">${l}</span>`).join("")}</div>`;
  const pane = tab === "storage" ? paneStorage : tab === "hosts" ? paneHosts
             : tab === "vms" ? paneVms : tab === "net" ? paneNet : paneCpu;

  return `<div class="card ${isTotal?'total':''}">
    <h2>${esc(c.name)}</h2>
    <div class="meta">${c.hostCount} Hosts · ${fmt(c.cores)} nutzbare Cores · ${c.vmCount} VMs${c.vmOff?` (davon ${c.vmOff} aus)`:''} · ${rv.length} genehmigt${clRes.filter(isPend).length?` / ${clRes.filter(isPend).length} beantragt`:''}${clRes.filter(r=>r.rejected).length?` / ${clRes.filter(r=>r.rejected).length} abgelehnt`:''}${spare}</div>
    ${tabBar}
    ${pane}
  </div>`;
}

// ---- Reiter in der Detailkarte ----
let CARD_TAB = "cpu";   // cpu | storage | net | hosts | vms
function setCardTab(t) { CARD_TAB = t; rerenderCard(); }

// ---- VLAN-/Portgruppen-Suche im Netzwerk-Reiter (nur diese Karte) ----
let CARD_NET_Q = "";
function onCardNetInput() {
  const el = document.getElementById("cardNetQ");
  CARD_NET_Q = el ? el.value : "";
  renderCardNetBody();     // nur die Tabelle neu füllen, Eingabefeld behält Fokus
}
function renderCardNetBody() {
  const body = document.getElementById("cardNetBody");
  if (!body) return;
  const c = hoverIdx >= 0 ? CLUSTERS[hoverIdx] : null;
  const all = (c && c.portgroups) || [];
  const q = CARD_NET_Q.trim().toLowerCase();
  const shown = q ? all.filter(p =>
      ((p.name || "") + " " + (p.vlan || "")).toLowerCase().includes(q)) : all;
  body.innerHTML = shown.map(p =>
    `<tr><td>${esc(p.name)}</td><td class="num">${esc(p.vlan || "–")}</td></tr>`).join("")
    || `<tr><td colspan="2" style="color:var(--muted)">Keine Portgruppe passt zur Suche.</td></tr>`;
  const cnt = document.getElementById("cardNetCount");
  if (cnt) cnt.textContent = q ? shown.length + " von " + all.length : "";
}

// ---- Storage-Detail (LUN-Liste) im Storage-Reiter ----
let DS_SORT = "size";
function setDsSort(m) { DS_SORT = m; rerenderCard(); }
function rerenderCard() {
  if (hoverIdx !== null && hc.style.display === "block")
    hc.innerHTML = '<button class="hc-close" title="Schließen" onclick="hideCard()">✕</button>' +
                   card(hoverIdx === -1 ? TOTAL : CLUSTERS[hoverIdx], hoverIdx, hoverIdx === -1);
}

// ---- Tabellenansicht mit Hover-Details ----
let TOTAL = null;

function filteredIdx() {
  const q = (document.getElementById("filter").value || "").trim().toLowerCase();
  return CLUSTERS.map((c, i) => i)
                 .filter(i => (!q || CLUSTERS[i].name.toLowerCase().includes(q))
                              && selMatch(CLUSTERS[i]));
}

function miniBar(used, resv, cap) {
  const pu = pct(used, cap), pr = pct(used + resv, cap) - pu;
  // Prozentwert als Tooltip: die Auslastung ist damit nicht nur über die Farbe
  // erkennbar (Farbsehschwäche).
  const tip = Math.round(pu + pr) + "% belegt" + (resv ? " (inkl. Reservierungen)" : "");
  return `<div class="bar mini" title="${tip}"><i style="width:${pu}%;background:${color(pu + pr)}"></i><i class="r" style="width:${pr}%"></i></div>`;
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
    <td class="num free ${hasStor ? 'cl' : ''}" style="color:${hasStor ? cStor : 'var(--muted)'}" title="${hasStor ? 'Datastores/LUNs anzeigen' : 'keine Storage-Daten aus Aria'}" ${hasStor ? `onclick="openStorage(${idx},this)"` : ''}>${hasStor ? fmt(fStor) : '–'}</td>
    <td class="barcol">${hasStor ? miniBar(c.storageUsed || 0, rvStor, c.storageCap) : ''}</td>
    <td class="num">${rv.length || "–"}${pend ? ` <span class="st pend">+${pend}</span>` : ""}</td></tr>`;
}

// ---- Ansichten: Kapazität / Reservierungen / Genehmigungen / Verwaltung ----
// endsWith statt ===, damit die Routen auch hinter einem Proxy-Unterpfad
// (z. B. https://host/capa/reservierungen) funktionieren
let VIEW = (location.pathname.endsWith("/vlan-suche") || location.hash === "#vlan-suche") ? "vlan"
         : (location.pathname.endsWith("/reservierungen") || location.hash === "#reservierungen") ? "res"
         : (location.pathname.endsWith("/genehmigungen") || location.hash === "#genehmigungen") ? "app"
         : (location.pathname.endsWith("/archiv") || location.hash === "#archiv") ? "arch"
         : (location.pathname.endsWith("/verwaltung") || location.hash === "#verwaltung") ? "adm"
         : (location.pathname.endsWith("/log") || location.hash === "#log") ? "log"
         : "kapa";
if ((VIEW === "adm" || VIEW === "log") && !IS_ADMIN) VIEW = "kapa";

function setView(v) {
  VIEW = v;
  const tabs = { kapa: "tabKapa", vlan: "tabVlan", res: "tabRes", app: "tabApp", arch: "tabArch", adm: "tabAdm", log: "tabLog" };
  const views = { kapa: "kapaView", vlan: "vlanView", res: "resView", app: "appView", arch: "archView", adm: "admView", log: "logView" };
  for (const k in tabs) {
    document.getElementById(tabs[k]).classList.toggle("active", v === k);
    document.getElementById(views[k]).style.display = v === k ? "" : "none";
  }
  document.getElementById("filter").style.display = (v === "vlan" || v === "res" || v === "arch") ? "none" : "";
  document.getElementById("filter").placeholder =
    v === "kapa" ? "Cluster filtern …" : v === "adm" ? "Benutzer filtern …"
    : v === "log" ? "Log filtern …" : "Reservierungen filtern …";
  try {
    history.replaceState(null, "",
      v === "res" ? "#reservierungen" : v === "app" ? "#genehmigungen"
      : v === "arch" ? "#archiv" : v === "adm" ? "#verwaltung" : v === "log" ? "#log"
      : v === "vlan" ? "#vlan-suche" : location.pathname);
  } catch (e) {}
  hideCard();
  if (v === "adm") { loadRoles(); loadTokens(); loadTeams(); loadSelector(); loadRoleNames(); loadNotify(); loadConfig(); }
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
// ---- Backup manuell auslösen ----
function runBackup() {
  const st = document.getElementById("backupStatus");
  const btn = document.getElementById("backupBtn");
  btn.disabled = true;
  st.style.color = "var(--muted)";
  st.textContent = "Backup läuft … (kann einen Moment dauern)";
  fetch("api/backup", { method: "POST" })
    .then(r => r.json())
    .then(d => {
      if (d.error) { st.style.color = "var(--crit)"; st.textContent = "Fehlgeschlagen: " + d.error; }
      else {
        st.style.color = "var(--ok)";
        st.textContent = "✓ Backup erstellt: " + d.backup
          + (d.rotated ? " · " + d.rotated + " alte(s) Archiv(e) gelöscht" : "");
      }
    })
    .catch(() => { st.style.color = "var(--crit)"; st.textContent = "Server nicht erreichbar."; })
    .finally(() => { btn.disabled = false; });
}
function addToken() {
  const n = document.getElementById("tknName").value.trim();
  if (!n) return;
  apiTokens("POST", "", { name: n }).then(d => {
    TOKENS = d.tokens || {};
    document.getElementById("newTokenVal").textContent = d.token;
    document.getElementById("newToken").style.display = "";
    render();
  }).catch(() => notify("Token konnte nicht erstellt werden."));
}
function delToken(id) {
  const t = TOKENS[id];
  askConfirm({ title: "API-Token widerrufen", okLabel: "Widerrufen", okClass: "danger",
    message: "„" + ((t && t.name) || id) + "“ widerrufen? Die Anwendung verliert sofort den Zugriff." })
    .then(ok => { if (!ok) return;
      apiTokens("DELETE", "/" + encodeURIComponent(id))
        .then(d => { TOKENS = d; render(); })
        .catch(() => notify("Widerruf fehlgeschlagen.")); });
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
  const kind = document.getElementById("admKind").value;
  let u = document.getElementById("admUser").value.trim();
  if (kind === "user") u = u.toLowerCase();
  const rl = document.getElementById("admRole").value;
  const ab = document.getElementById("admDept").value.trim();
  if (!u) return;
  apiRoles("POST", "", { user: u, role: rl, abteilung: ab, kind: kind })
    .then(d => { ROLES = d; render(); })
    .catch(() => notify("Speichern fehlgeschlagen."));
}
function delRole(u) {
  askConfirm({ title: "Rollenzuweisung entfernen", okLabel: "Entfernen", okClass: "danger",
    message: "Rollenzuweisung für „" + u + "“ entfernen?" }).then(ok => { if (!ok) return;
    apiRoles("DELETE", "/" + encodeURIComponent(u))
      .then(d => { ROLES = d; if (EDIT_USER === u) EDIT_USER = null; render(); })
      .catch(() => notify("Löschen fehlgeschlagen.")); });
}
let EDIT_USER = null;   // Benutzer, dessen Zeile gerade bearbeitet wird
function editRole(u) { EDIT_USER = u; render(); document.getElementById("editDept").focus(); }
function cancelEditRole() { EDIT_USER = null; render(); }
function saveEditRole(u) {
  const rl = document.getElementById("editRole").value;
  const ab = document.getElementById("editDept").value.trim();
  apiRoles("POST", "", { user: u, role: rl, abteilung: ab })
    .then(d => { ROLES = d; EDIT_USER = null; render(); })
    .catch(() => notify("Speichern fehlgeschlagen."));
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
                       .catch(() => notify("Speichern der Teams fehlgeschlagen."));
}
function addTeam() {
  const el = document.getElementById("newTeam");
  const t = (el.value || "").trim();
  if (!t) return;
  if (TEAMS.includes(t)) { notify("Team „" + t + "“ existiert bereits."); return; }
  el.value = "";
  putTeams(TEAMS.concat([t]));
}
function delTeam(i) {
  askConfirm({ title: "Team entfernen", okLabel: "Entfernen", okClass: "danger",
    message: "Team „" + TEAMS[i] + "“ aus dem Genehmigungsprozess entfernen?" })
    .then(ok => { if (ok) putTeams(TEAMS.filter((_, j) => j !== i)); });
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
    .catch(() => notify("Umbenennen fehlgeschlagen (Name evtl. schon vergeben)."));
}
function teamMail(t) { return ((NOTIFY.team_email || {})[t]) || ""; }
function renderTeams() {
  const rows = TEAMS.map((t, i) => {
    const mailCell = `<td><input class="filterbox" style="width:100%" id="teamMail${i}"
         data-team="${esc(t)}" placeholder="team@firma.de" value="${esc(teamMail(t))}"></td>`;
    if (i === TEAM_EDIT) {
      return `<tr><td class="num">${i + 1}</td>
        <td><input class="filterbox" style="width:100%" id="teamEdit" value="${esc(t)}"
             onkeydown="if(event.key==='Enter')saveTeamRename(${i});if(event.key==='Escape')cancelEditTeam()"></td>
        ${mailCell}
        <td><button class="btn approve" onclick="saveTeamRename(${i})">✓ Speichern</button>
            <button class="btn" onclick="cancelEditTeam()">Abbrechen</button></td></tr>`;
    }
    return `<tr><td class="num">${i + 1}</td><td>${esc(t)}</td>
     ${mailCell}
     <td><button class="edit" title="nach oben" ${i === 0 ? "disabled" : ""} onclick="moveTeam(${i},-1)">↑</button>
         <button class="edit" title="nach unten" ${i === TEAMS.length - 1 ? "disabled" : ""} onclick="moveTeam(${i},1)">↓</button>
         <button class="edit" title="Team umbenennen" onclick="editTeam(${i})">✎ Umbenennen</button>
         <button class="del" title="Team entfernen" onclick="delTeam(${i})">✕ Entfernen</button></td></tr>`;
  }).join("");
  document.getElementById("tmbody").innerHTML =
    `<tr><td></td><td><input class="filterbox" style="width:100%" id="newTeam"
         placeholder="Neues Team, z. B. Team Betrieb"
         onkeydown="if(event.key==='Enter')addTeam()"></td><td></td>
     <td><button class="btn approve" onclick="addTeam()">+ Hinzufügen</button></td></tr>` +
    (rows || `<tr><td colspan="4" style="color:var(--muted)">Keine Teams – einstufig (Admin genehmigt direkt).</td></tr>`);
}

// ---- Mail-Benachrichtigungen (Matrix Rolle × Ereignis) ----
async function apiNotify(method, body) {
  const r = await fetch("api/notify", { method: method,
    headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadNotify() {
  apiNotify("GET").then(d => { if (d && d.notify) NOTIFY = d.notify;
                               if (VIEW === "adm") render(); }).catch(() => {});
}
function renderNotify() {
  const rows = ROLE_ORDER.map(role => {
    const rc = (NOTIFY.role || {})[role] || {};
    const allowed = NOTIFY_ROLE_EVENTS[role] || [];
    const addr = role === "anforderer"
        ? `<span style="color:var(--muted)">= Antragsteller (automatisch)</span>`
      : role === "reviewer"
        ? `<span style="color:var(--muted)">pro Team (Tabelle oben)</span>`
        : `<input class="filterbox" style="width:100%" id="nt_email_${role}"
             placeholder="verteiler@firma.de" value="${esc(rc.email || "")}">`;
    const cells = NOTIFY_EVENTS.map(([ev]) => allowed.includes(ev)
        ? `<td class="num"><input type="checkbox" id="nt_${role}_${ev}" ${rc[ev] ? "checked" : ""}></td>`
        : `<td class="num" style="color:var(--muted)">–</td>`).join("");
    return `<tr><td>${esc(ROLE_NAMES[role] || role)} <span style="color:var(--muted)">(${role})</span></td>
      <td>${addr}</td>${cells}</tr>`;
  }).join("");
  document.getElementById("ntbody").innerHTML = rows;
}
function saveNotify() {
  const body = { role: {}, team_email: {} };
  ROLE_ORDER.forEach(role => {
    const rc = {};
    (NOTIFY_ROLE_EVENTS[role] || []).forEach(ev => {
      const el = document.getElementById("nt_" + role + "_" + ev);
      if (el) rc[ev] = el.checked;
    });
    const em = document.getElementById("nt_email_" + role);
    if (em) rc.email = em.value.trim();
    body.role[role] = rc;
  });
  document.querySelectorAll('#tmbody input[id^="teamMail"]').forEach(inp => {
    const t = inp.getAttribute("data-team"); const v = (inp.value || "").trim();
    if (t && v) body.team_email[t] = v;
  });
  apiNotify("PUT", body).then(d => {
    if (d && d.notify) NOTIFY = d.notify;
    const st = document.getElementById("notifySaved");
    if (st) { st.textContent = "✓ gespeichert"; setTimeout(() => { if (st) st.textContent = ""; }, 2500); }
    render();
  }).catch(() => notify("Speichern der Mail-Regeln fehlgeschlagen."));
}

// ---- Unter-Reiter der Verwaltung + read-only Konfiguration ----
let ADM_TAB = "users";
function setAdmTab(t) {
  ADM_TAB = t;
  const tabs = { users: "atabUsers", mail: "atabMail", conf: "atabConf" };
  const grps = { users: "admGrpUsers", mail: "admGrpMail", conf: "admGrpConf" };
  for (const k in tabs) {
    const tb = document.getElementById(tabs[k]); if (tb) tb.classList.toggle("active", k === t);
    const gr = document.getElementById(grps[k]); if (gr) gr.style.display = k === t ? "" : "none";
  }
}
let CONFIG = null;
function loadConfig() {
  fetch("api/config").then(r => r.ok ? r.json() : null).then(d => {
    if (d && d.config) { CONFIG = d.config; if (VIEW === "adm") { renderConfig("configSheet"); renderConfig("configMail", "Mail / SMTP"); } }
  }).catch(() => {});
}
function renderConfig(containerId, onlyGroup) {
  const el = document.getElementById(containerId); if (!el) return;
  if (!CONFIG) { el.innerHTML = '<div style="color:var(--muted);font-size:12px">Lade Konfiguration …</div>'; return; }
  const groups = onlyGroup ? (CONFIG[onlyGroup] ? { [onlyGroup]: CONFIG[onlyGroup] } : {}) : CONFIG;
  const keys = Object.keys(groups);
  if (!keys.length) { el.innerHTML = '<div style="color:var(--muted);font-size:12px">–</div>'; return; }
  el.innerHTML = keys.map(g => {
    const rows = Object.keys(groups[g]).map(k =>
      `<tr><td style="color:var(--muted);width:300px">${esc(k)}</td><td>${esc(String(groups[g][k]))}</td></tr>`).join("");
    const head = onlyGroup ? "" : `<div class="sechead" style="margin-top:14px">${esc(g)}</div>`;
    return `${head}<div class="tablewrap"><table class="kt">${rows}</table></div>`;
  }).join("");
}

// ---- Cluster-Selektor konfigurieren ----
async function apiSelector(method, body) {
  const r = await fetch("api/selector", { method: method,
    headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadSelector() {
  apiSelector("GET").then(d => { if (d && d.selector) SELECTOR = d.selector;
                                 if (VIEW === "adm") render(); }).catch(() => {});
}
function saveSelector() {
  const list = [0, 1, 2].map(i => ({
      category: document.getElementById("sel" + i).value,
      label: (document.getElementById("sellbl" + i).value || "").trim()
    })).filter(x => x.category);
  apiSelector("PUT", list).then(d => {
    if (d && d.selector) SELECTOR = d.selector;
    const cats = SELECTOR.map(s => s.category);
    Object.keys(SEL_VALUES).forEach(k => { if (!cats.includes(k)) delete SEL_VALUES[k]; });
    const st = document.getElementById("selSaved");
    if (st) { st.textContent = "✓ gespeichert"; setTimeout(() => { if (st) st.textContent = ""; }, 2500); }
    render();
  }).catch(() => notify("Speichern des Selektors fehlgeschlagen."));
}
function renderSelector() {
  const all = [...new Set([...tagCategories(), ...SELECTOR.map(s => s.category)])].sort();
  const rows = [0, 1, 2].map(i => {
    const cur = SELECTOR[i] || {category: "", label: ""};
    const warn = cur.category && !tagCategories().includes(cur.category)
      ? ' <span class="st pend" title="Kategorie derzeit in keinem Cluster-Tag">⚠ derzeit ohne Werte</span>' : "";
    return `<tr><td class="num">${i + 1}</td>
      <td><select id="sel${i}" class="filterbox" style="max-width:280px">
        <option value="">– keine –</option>
        ${all.map(c => `<option value="${esc(c)}" ${cur.category === c ? "selected" : ""}>${esc(c)}</option>`).join("")}
      </select>${warn}</td>
      <td><input id="sellbl${i}" class="filterbox" style="width:100%"
           placeholder="Anzeigename (leer = Kategorie)" value="${esc(cur.label && cur.label !== cur.category ? cur.label : "")}"
           onkeydown="if(event.key==='Enter')saveSelector()"></td></tr>`;
  }).join("");
  document.getElementById("selbody").innerHTML = rows +
    `<tr><td></td><td colspan="2">
       <button class="btn approve" onclick="saveSelector()">✓ Selektor speichern</button>
       <span id="selSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
       ${all.length ? "" : '<span style="color:var(--muted);font-size:12px;margin-left:8px">Noch keine Tag-Kategorien – erst nach dem ersten Aria-Abruf verfügbar.</span>'}
     </td></tr>`;
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
                                        render(); }).catch(() => notify("Speichern fehlgeschlagen."));
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
  // Anforderer und Reviewer werden einem Team (aus teams.json) zugeordnet;
  // Admin/Auditor brauchen kein Team.
  if (role === "reviewer" || role === "anforderer")
    return `<select id="${id}" class="filterbox" style="width:100%">
      <option value="">${TEAMS.length ? "– Team wählen –" : "(erst Teams anlegen)"}</option>
      ${TEAMS.map(t => `<option value="${esc(t)}" ${val === t ? "selected" : ""}>${esc(t)}</option>`).join("")}
    </select>`;
  return `<input id="${id}" class="filterbox" style="width:100%" placeholder="– für diese Rolle nicht nötig" value="" disabled>`;
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
    const r = ROLES[u];
    const isGroup = r && r.kind === "group";
    const typ = isGroup ? "AD-Gruppe" : "Benutzer";
    if (u === EDIT_USER) {
      const cur = ROLES[u] || {};
      return `<tr><td>${esc(typ)}</td><td>${esc(u)}</td>
        <td>${roleSelect("editRole", cur.role, "syncRoleField('edit','" + esc(u) + "')")}</td>
        <td id="editFieldCell">${roleField("editDept", cur.role, cur.abteilung || "", u)}</td>
        <td><button class="btn approve" onclick="saveEditRole('${esc(u)}')">✓ Speichern</button>
            <button class="btn" onclick="cancelEditRole()">Abbrechen</button></td></tr>`;
    }
    // Team-Zuweisung prüfen: nach Umbenennen/Löschen eines Teams (oder aus der
    // früheren Freitext-Abteilung) kann hier ein Team stehen, das es nicht gibt.
    const needsTeam = r.role === "anforderer" || r.role === "reviewer";
    const orphan = needsTeam && r.abteilung && TEAMS.length && !TEAMS.includes(r.abteilung);
    const warn = orphan
      ? ' <span class="st rej" title="Dieses Team gibt es nicht (mehr) – bitte neu zuweisen">⚠ unbekannt</span>'
      : (needsTeam && !r.abteilung ? ' <span class="st pend" title="Ohne Team: sieht nur eigene Anfragen bzw. kann nichts freigeben">⚠ kein Team</span>' : "");
    const mailCell = isGroup ? esc(u)
      : `<span title="${r.mail ? 'AD-Mail: ' + esc(r.mail) : 'keine AD-Mail aufgelöst (nur mit --ad-mail-attribute)'}">${esc(u)}${r.mail ? ' <span style="color:var(--muted)" title="AD-Mail: ' + esc(r.mail) + '">✉</span>' : ''}</span>`;
    return `<tr><td>${esc(typ)}</td><td>${mailCell}</td><td>${esc(ROLE_NAMES[r.role] || r.role)}</td>
     <td>${esc(r.abteilung || "–")}${warn}</td>
     <td><button class="edit" title="Rolle/Team bearbeiten" onclick="editRole('${esc(u)}')">✎ Bearbeiten</button>
         <button class="del" title="Zuweisung entfernen" onclick="delRole('${esc(u)}')">✕ Löschen</button></td></tr>`;
  }).join("");
  const firstRole = ROLE_ORDER[0];
  document.getElementById("mtbody").innerHTML =
    `<tr><td><select id="admKind" class="filterbox" style="width:100%" onchange="admKindSync()">
         <option value="user">Benutzer</option><option value="group">AD-Gruppe</option></select></td>
     <td><input class="filterbox" style="width:100%" id="admUser"
         placeholder="benutzer@firma.local oder vorname.nachname"></td>
     <td>${roleSelect("admRole", firstRole, "syncRoleField('adm')")}</td>
     <td id="admFieldCell">${roleField("admDept", firstRole, "", null)}</td>
     <td><button class="btn approve" onclick="addRole()">+ Zuweisen</button></td></tr>` +
    (rows || `<tr><td colspan="5" style="color:var(--muted)">Noch keine Rollen zugewiesen.</td></tr>`);
  reSort("mtable");
}
function admKindSync() {
  const g = document.getElementById("admKind").value === "group";
  document.getElementById("admUser").placeholder = g
    ? "AD-Gruppenname (CN), z. B. Kapa-Admins"
    : "benutzer@firma.local oder vorname.nachname";
}

// Status als reiner Text (für Filter/Export – stBadge liefert HTML)
function statusText(r) {
  if (r.rejected) return "abgelehnt";
  if (r.cancelled) return "storniert";
  if (r.approved) return "genehmigt";
  return (TEAMS.length && stageOf(r) > 0) ? "in Prüfung" : "beantragt";
}
function filterRes(list, q) {
  q = (q || "").trim().toLowerCase();
  const hit = r => [r.name, r.cluster, r.change, r.von, r.abteilung, r.id,
                    statusText(r)].some(v => (v || "").toLowerCase().includes(q));
  return list.filter(r => !q || hit(r))
    .slice().sort((a, b) => (a.cluster || "").localeCompare(b.cluster || "") ||
                            (a.created || "").localeCompare(b.created || ""));
}

function sumStorage(rv) { return Math.round(rv.reduce((s,r)=>s+(r.storage_gb||0),0)*10)/10; }

// Cluster-Name als Tabellenzelle – klickbar (öffnet dieselbe Detailkarte wie
// auf der Kapazitätsseite), sofern der Cluster in den aktuellen Daten existiert.
function clusterTd(name) {
  const i = CLUSTERS.findIndex(c => c.name === name);
  return i >= 0
    ? `<td class="cl" title="Cluster-Details anzeigen" onclick="toggleCard(${i}, this)">${esc(name)}</td>`
    : `<td>${esc(name || "–")}</td>`;
}

function isArchived(r) { return !!(r.rejected || r.cancelled); }

function renderResTable() {
  const q = (document.getElementById("resSearch") || {}).value || "";
  // „(anderes Team)" nicht auflisten; abgelehnte/stornierte liegen im Archiv
  const own = RES.filter(r => !r.foreign && !isArchived(r));
  const list = filterRes(own, q);
  const appr = list.filter(r => r.approved && !r.cancelled);
  const cnt = document.getElementById("resCount");
  if (cnt) cnt.textContent = q.trim() ? list.length + " von " + own.length + " Anfragen" : own.length + " Anfragen";
  const showDec = ROLE !== "anforderer";
  const nCols = showDec ? 15 : 14;
  const rows = list.map(r =>
    `<tr><td class="rid" title="Eindeutige ID der Anfrage">${esc(r.id || "–")}</td>
     <td>${esc(r.name)}</td>${clusterTd(r.cluster)}<td>${esc(r.change || "–")}</td>
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
    (rows || `<tr><td colspan="${nCols}" style="color:var(--muted)">Keine aktiven Reservierungen.</td></tr>`);
  reSort("rtable");
}

function renderArchiveTable() {
  const q = (document.getElementById("archSearch") || {}).value || "";
  const arch = RES.filter(r => !r.foreign && isArchived(r));
  const list = filterRes(arch, q);
  const cnt = document.getElementById("archCount");
  if (cnt) cnt.textContent = q.trim() ? list.length + " von " + arch.length + " Einträgen" : arch.length + " Einträge";
  const rows = list.map(r => {
    const done = r.cancelled ? r.cancelled_on : r.rejected_on;
    const by = r.cancelled ? r.cancelled_by : r.rejected_by;
    return `<tr><td class="rid">${esc(r.id || "–")}</td>
     <td>${esc(r.name)}</td>${clusterTd(r.cluster)}<td>${esc(r.change || "–")}</td>
     <td class="num">${fmt(r.vcpu || 0)}</td><td class="num">${fmt(r.ram_gb || 0)}</td><td class="num">${fmt(r.storage_gb || 0)}</td>
     <td>${esc(r.von || "–")}</td><td>${esc(r.abteilung || "–")}</td>
     <td>${fmtDate(r.created)}</td><td>${fmtDate(done)}</td><td>${stBadge(r)}</td>
     <td>${esc(by || "–")}</td><td>${esc(r.comment || "–")}</td></tr>`;
  }).join("");
  document.getElementById("arbody").innerHTML =
    rows || `<tr><td colspan="14" style="color:var(--muted)">Archiv ist leer.</td></tr>`;
  reSort("artable");
}

function renderAppTable() {
  const list = filterRes(RES.filter(isPend), (document.getElementById("filter") || {}).value || "");
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
     <td>${esc(r.name)}</td>${clusterTd(r.cluster)}<td>${esc(r.change || "–")}</td>
     <td class="num">${fmt(r.vcpu || 0)}</td><td class="num">${fmt(r.ram_gb || 0)}</td><td class="num">${fmt(r.storage_gb || 0)}</td>
     ${freeCells(r)}
     <td>${esc(r.von || "–")}</td><td>${esc(r.abteilung || "–")}</td>
     <td>${fmtDate(r.created)}</td><td>${stBadge(r)}</td>
     <td>${action(r)}</td></tr>`).join("");
  document.getElementById("atbody").innerHTML =
    rows || `<tr><td colspan="14" style="color:var(--muted)">Keine offenen Anträge – alles genehmigt.</td></tr>`;
  reSort("atable");
}

// ---- VLAN-/Portgruppen-Suche (cluster-übergreifend) ----
// Hängt ein Netz an mehreren Clustern, liefert der Server die Portgruppe je
// Cluster einmal – die Suche zeigt dann pro Cluster eine Zeile, damit
// „wo hängt das Netz" vollständig ist.
let VLAN_INDEX = null;
function vlanIndex() {
  if (VLAN_INDEX) return VLAN_INDEX;
  const out = [];
  CLUSTERS.forEach((c, ci) => (c.portgroups || []).forEach(p =>
    out.push({ pg: p.name || "", vlan: p.vlan || "", cluster: c.name, cidx: ci })));
  VLAN_INDEX = out;
  return out;
}
function renderVlan() {
  const q = (document.getElementById("vlanQ").value || "").trim().toLowerCase();
  const all = vlanIndex();
  const hits = q ? all.filter(r =>
    r.pg.toLowerCase().includes(q) || String(r.vlan).toLowerCase().includes(q)) : all;
  document.getElementById("vtbody").innerHTML = hits.map(r =>
    `<tr><td>${esc(r.pg)}</td><td class="num">${esc(r.vlan || "–")}</td>
     <td class="cl" title="Cluster-Details anzeigen" onclick="toggleCard(${r.cidx}, this)">${esc(r.cluster)}</td></tr>`).join("")
    || `<tr><td colspan="3" style="color:var(--muted)">${all.length
         ? "Keine Portgruppe passt zur Suche." : "Keine Portgruppen-Daten aus Aria."}</td></tr>`;
  document.getElementById("vlanCount").textContent = all.length
    ? (q ? hits.length + " von " + all.length + " Portgruppen"
         : all.length + " Portgruppen gesamt") : "";
  reSort("vtable");
}

function render() {
  const pend = RES.filter(isPend).length;
  document.getElementById("tabApp").textContent = "Genehmigungen" + (pend ? " (" + pend + ")" : "");
  if (VIEW === "vlan") { renderVlan(); return; }
  if (VIEW === "res") { renderResTable(); return; }
  if (VIEW === "app") { renderAppTable(); return; }
  if (VIEW === "arch") { renderArchiveTable(); return; }
  if (VIEW === "adm") { renderAdmTable(); renderRoleNames(); renderTeams(); renderNotify(); renderSelector(); renderTokenTable(); renderConfig("configSheet"); renderConfig("configMail", "Mail / SMTP"); setAdmTab(ADM_TAB); return; }
  if (VIEW === "log") { renderLogTable(); return; }
  renderClusterSelector();
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
    datastores: vis.reduce((s,c)=>s.concat(c.datastores||[]),[]),
    _names: new Set(vis.map(c => c.name)),
  };
  TOTAL.vcpuFree = TOTAL.vcpuCap - TOTAL.vcpuUsed;
  TOTAL.ramFree = Math.round((TOTAL.ramCap - TOTAL.ramUsed)*10)/10;
  TOTAL.storageFree = Math.round((TOTAL.storageCap - TOTAL.storageUsed)*10)/10;
  document.getElementById("ktbody").innerHTML =
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
  if (idx !== hoverIdx) CARD_NET_Q = "";   // beim Öffnen eines anderen Clusters Suche leeren
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
function openStorage(idx, cell) {
  CARD_TAB = "storage";           // Klick auf den Storage-Wert -> Storage-Reiter
  showCard(idx, cell.parentElement);
}
function hideCard() { hc.style.display = "none"; hoverIdx = null; }
// Klick außerhalb schließt die Detailkarte. WICHTIG: in der Capture-Phase (true)
// prüfen – Elemente in der Karte (Reiter, Sortier-Links) ersetzen beim Klick den
// Karteninhalt. In der Bubble-Phase wäre e.target dann schon aus dem DOM entfernt
// und hc.contains(e.target) fälschlich false -> die Karte hätte sich geschlossen.
document.addEventListener("click", e => {
  if (hc.style.display === "block" && !hc.contains(e.target) && !e.target.closest(".cl"))
    hideCard();
}, true);

// ---- Detailkarte per Titel (⠿) frei verschieben ----
(function () {
  let drag = false, sx = 0, sy = 0, ox = 0, oy = 0;
  hc.addEventListener("mousedown", e => {
    const h = e.target.closest("h2");
    if (!h || !hc.contains(h) || e.target.closest("button,a,input,select,textarea")) return;
    const r = hc.getBoundingClientRect();
    drag = true; sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
    hc.style.right = "auto"; hc.style.bottom = "auto";
    hc.style.left = ox + "px"; hc.style.top = oy + "px";
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
  document.addEventListener("mousemove", e => {
    if (!drag) return;
    const nx = Math.max(4, Math.min(ox + e.clientX - sx, innerWidth - 60));
    const ny = Math.max(4, Math.min(oy + e.clientY - sy, innerHeight - 40));
    hc.style.left = nx + "px"; hc.style.top = ny + "px";
  });
  document.addEventListener("mouseup", () => { drag = false; document.body.style.userSelect = ""; });
})();

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
if (HAS_BACKUP && SERVE) document.getElementById("backupSection").style.display = "";
if (ROLE === "anforderer") {
  const th = document.getElementById("thDec");
  if (th) th.remove();   // Anforderer sehen nicht, wer entschieden hat
}

// ---- Sortierbare Tabellen (Klick auf die Spaltenüberschrift) ----
const SORT_CFG = { ktable:{pin:0}, rtable:{pin:1}, atable:{pin:0}, artable:{pin:0},
                   ltable:{pin:0}, mtable:{pin:1}, ttable:{pin:1}, vtable:{pin:0} };
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
if (!SERVE) document.getElementById("csvBtn").style.display = "none";  // API nur im Serve-Modus

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
  askConfirm({ title: "Alte Reservierungen übernehmen?", okLabel: "Übernehmen",
    message: old.length + " Reservierung(en) aus dem Browser-Speicher gefunden " +
             "(alter Speicherort). Auf den Server übernehmen?\nSie erscheinen dann " +
             "mit Status „beantragt“ und können unter „Genehmigungen“ freigegeben werden." })
    .then(ok => {
      if (ok) apiRes("PUT", "", old).then(l => {
        setRes(l); localStorage.removeItem(LS_KEY); }).catch(resFail);
      try { localStorage.setItem(LS_KEY + "_migriert", "1"); } catch (e) {}
    });
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


def parse_tag_json(raw):
    """vSphere-Tags aus einem JSON-Wert (z. B. der Eigenschaft 'TagJson') lesen.

    Die Struktur unterscheidet sich je nach vROps-/vCenter-Version, deshalb
    werden die üblichen Formen abgefangen:
      ["Standort: RZ-Nord", ...]
      [{"category": "Standort", "name": "RZ-Nord"}, ...]   (auch categoryName/tagName)
      {"Standort": "RZ-Nord", "Umgebung": ["Test", "Prod"]}
      {"tags": [ ... ]}
    Rückgabe: Liste 'Kategorie: Wert' (bzw. nur Wert ohne Kategorie).
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out = []

    def add(cat, name):
        cat, name = str(cat or "").strip(), str(name or "").strip()
        if not name:
            return
        label = (f"{cat}: {name}"
                 if cat and cat.lower() not in ("tag", "tags", "tagjson") else name)
        if label not in out:
            out.append(label)

    def walk(node, cat=""):
        if isinstance(node, (str, int, float)):
            add(cat, node)
        elif isinstance(node, list):
            for item in node:
                walk(item, cat)
        elif isinstance(node, dict):
            c = (node.get("category") or node.get("categoryName")
                 or node.get("Category") or node.get("category_name") or "")
            n = (node.get("name") or node.get("tagName") or node.get("tag")
                 or node.get("Name") or node.get("value") or "")
            if n:
                add(c or cat, n)
                return
            for k, v in node.items():          # {"Standort": "RZ-Nord", ...}
                walk(v, k)

    walk(data)
    return out


def tag_labels(key, val):
    """Anzeige-Labels einer Tag-Eigenschaft. JSON-Werte (TagJson) werden
    aufgeschlüsselt; sonst wird 'Kategorie: Wert' aus Schlüssel/Wert gebildet."""
    s = str(val or "").strip()
    if not s:
        return []
    if s[:1] in ("[", "{"):
        # Rohes JSON niemals als Chip anzeigen – lieber nichts.
        return parse_tag_json(s)
    cat = (key.split(":")[-1] if ":" in key else key.split("|")[-1]).strip()
    return [s if not cat or cat.lower() in ("tag", "tags", "tagjson")
            else f"{cat}: {s}"]


def int_or(default):
    """argparse-Typ für Zahlen: ein leerer Wert (z. B. eine nicht gesetzte
    Umgebungsvariable, die systemd als '' durchreicht) ergibt den Standard,
    statt den Dienststart mit 'invalid int value' abzubrechen."""
    def conv(s):
        s = str(s or "").strip()
        return default if not s else int(s)
    return conv


def _drop_empty_long_opts(argv):
    """Langoptionen mit leerem Wert aus der Argumentliste entfernen.

    Die systemd-Unit baut Argumente wie '--backup-target ${BACKUP_TARGET}' aus
    kapa.env. Ist die Variable leer, käme '--backup-target ''' an und würde einen
    in der INI gesetzten Wert überschreiben (leer schlägt INI). Ein leerer Wert
    bedeutet in dieser App überall 'nicht gesetzt' – wir lassen ihn hier weg,
    damit stattdessen INI bzw. eingebauter Standard greift. '--opt=' und
    '--opt ''' werden beide verworfen; '--opt=wert' und Flags bleiben."""
    out, i, n = [], 0, len(argv)
    while i < n:
        a = argv[i]
        if a.startswith("--") and "=" in a:
            if a.split("=", 1)[1] == "":
                i += 1
                continue
            out.append(a)
        elif a.startswith("--") and i + 1 < n and argv[i + 1] == "":
            i += 2                     # '--opt' und der leere Folgewert: beide weg
            continue
        else:
            out.append(a)
        i += 1
    return out


def _html_escape(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def render_html(clusters, cpu_factor, serve_mode=False, updated=None, res_ttl=31,
                failover_hosts=1, userinfo=None, teams=None, rolenames=None,
                contact="", selector=None, backup=False, notify=None):
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
            .replace("__SELECTOR__", json_for_html(selector or []))
            .replace("__BACKUP__", "true" if backup else "false")
            .replace("__ROLENAMES__", json_for_html(rolenames or DEFAULT_ROLE_NAMES))
            .replace("__NOTIFY__", json_for_html(notify or DEFAULT_NOTIFY))
            .replace("__RESNOTE__", resnote)
            .replace("__FAILNOTE__", failnote)
            .replace("__VERSION__", VERSION)
            .replace("__CONTACT_FOOT__",
                     " · " + _html_escape(contact) if contact else "")
            .replace("__DATE__", updated or datetime.now().strftime("%d.%m.%Y %H:%M")))


def render_dashboard(clusters, cpu_factor, path, res_ttl=31, failover_hosts=1,
                     contact=""):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(clusters, cpu_factor, res_ttl=res_ttl,
                            failover_hosts=failover_hosts, contact=contact))

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

    # ---- Datenspeicher (JSON-Dateien oder SQLite) ----
    _coll_paths = {"res": args.res_file, "roles": args.roles_file,
                   "teams": args.teams_file, "selector": args.selector_file,
                   "rolenames": args.rolenames_file, "tokens": args.tokens_file,
                   "notify": args.notify_file}
    if args.storage == "sqlite":
        store = SqliteStore(args.db_file)
        # Einmal-Migration: vorhandene JSON-Daten in die (leere) DB übernehmen
        _MISS = object()
        for _n in ("roles", "teams", "selector", "rolenames", "tokens", "notify"):
            p = _coll_paths[_n]
            if os.path.exists(p) and store.load(_n, _MISS) is _MISS:
                try:
                    with open(p, encoding="utf-8") as f:
                        store.save(_n, json.load(f))
                    print(f"Migriert nach SQLite: {os.path.basename(p)}",
                          file=sys.stderr)
                except Exception as e:
                    print(f"Migration {p} fehlgeschlagen: {e}", file=sys.stderr)
        if os.path.exists(args.res_file) and not store.res_load():
            try:
                with open(args.res_file, encoding="utf-8") as f:
                    _lst = json.load(f)
                if isinstance(_lst, list) and _lst:
                    for _r in _lst:
                        if isinstance(_r, dict):
                            _r.setdefault("id", uuid.uuid4().hex[:12])
                    store.res_save_all(_lst)
                    print(f"Migriert nach SQLite: {len(_lst)} Reservierungen",
                          file=sys.stderr)
            except Exception as e:
                print(f"Migration Reservierungen fehlgeschlagen: {e}", file=sys.stderr)
        print(f"Datenspeicher: SQLite ({args.db_file})", file=sys.stderr)
    else:
        store = JsonStore(_coll_paths)

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

    _MISS = object()

    def load_teams():
        d = store.load("teams", _MISS)
        if d is not _MISS:
            return clean_teams(d) if isinstance(d, list) else []
        # Kein Bestand: einmalig aus --approval-teams befüllen (Migration)
        seed = clean_teams((args.approval_teams or "").split(","))
        if seed:
            try:
                store.save("teams", seed)
            except OSError as e:
                print(f"Team-Datei nicht schreibbar: {e}", file=sys.stderr)
        return seed

    def save_teams():
        store.save("teams", approval_teams)

    approval_teams = load_teams()

    # ---- Cluster-Selektor: bis zu 3 Tag-Kategorien (kaskadierender Filter) ----
    selector_lock = threading.Lock()

    def clean_selector(seq):
        # Einträge: {"category": "<Tag-Kategorie>", "label": "<Anzeigename>"}.
        # Alte Form (Liste von Strings) wird migriert: label = category.
        out, seen = [], set()
        for item in seq:
            if isinstance(item, str):
                cat = label = item.strip()
            elif isinstance(item, dict):
                cat = str(item.get("category") or "").strip()
                label = str(item.get("label") or "").strip() or cat
            else:
                continue
            if cat and cat not in seen:
                seen.add(cat)
                out.append({"category": cat, "label": label})
        return out[:3]

    def load_selector():
        d = store.load("selector", None)
        if isinstance(d, list):
            return clean_selector(d)
        return []

    def save_selector():
        store.save("selector", cluster_selector)

    cluster_selector = load_selector()

    # ---- Frei wählbare Rollen-Bezeichnungen (Schlüssel bleiben fest) ----
    rolenames_lock = threading.Lock()

    def load_rolenames():
        d = dict(DEFAULT_ROLE_NAMES)
        raw = store.load("rolenames", None)
        if isinstance(raw, dict):
            for k in ROLE_KEYS:
                v = raw.get(k)
                if isinstance(v, str) and v.strip():
                    d[k] = v.strip()
        return d

    def save_rolenames():
        store.save("rolenames", role_names)

    role_names = load_rolenames()

    # ---- Mail-Benachrichtigungsregeln (je interner Rolle + Team-Adressen) ----
    notify_lock = threading.Lock()

    def _merge_notify(raw):
        cfg = {"role": {}, "team_email": {}}
        rawrole = (raw or {}).get("role") if isinstance(raw, dict) else None
        rawrole = rawrole if isinstance(rawrole, dict) else {}
        for role, events in NOTIFY_ROLE_EVENTS.items():
            base = dict(DEFAULT_NOTIFY["role"][role])
            got = rawrole.get(role) if isinstance(rawrole.get(role), dict) else {}
            for ev in events:
                base[ev] = bool(got.get(ev, base.get(ev, False)))
            if role in ("admin", "auditor"):
                base["email"] = str(got.get("email") or "").strip()[:200]
            cfg["role"][role] = base
        rawmail = (raw or {}).get("team_email") if isinstance(raw, dict) else None
        if isinstance(rawmail, dict):
            for k, v in rawmail.items():
                cfg["team_email"][str(k)] = str(v or "").strip()[:200]
        return cfg

    def load_notify():
        return _merge_notify(store.load("notify", None))

    def save_notify():
        store.save("notify", notify_cfg)

    notify_cfg = load_notify()

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
        """Rollen: {benutzer: {role, abteilung}}; alte Form {benutzer: rolle}
        wird beim Laden migriert."""
        d = store.load("roles", None)
        if isinstance(d, dict):
            out = {}
            for k, v in d.items():
                if isinstance(v, str) and v in VALID_ROLES:
                    out[str(k).lower()] = {"role": v, "abteilung": "",
                                           "kind": "user"}
                elif isinstance(v, dict) and v.get("role") in VALID_ROLES:
                    kind = "group" if v.get("kind") == "group" else "user"
                    key = str(k) if kind == "group" else str(k).lower()
                    out[key] = {"role": v["role"],
                                "abteilung": str(v.get("abteilung") or ""),
                                "kind": kind}
            return out
        return {}

    def save_roles():
        store.save("roles", roles)

    roles = load_roles()

    def normalize_user(name):
        name = str(name or "").strip().lower()
        if name and "@" not in name and "\\" not in name and args.ad_domain:
            name = name + "@" + args.ad_domain
        return name

    ROLE_RANK = {"admin": 3, "reviewer": 2, "auditor": 1, "anforderer": 0}

    def role_entry(user):
        """Direkt zugewiesene Benutzerrolle (keine Gruppen)."""
        if user in admin_seed:
            return {"role": "admin", "abteilung": ""}
        e = roles.get(user)
        if e and e.get("kind", "user") != "group":
            return {"role": e["role"], "abteilung": e.get("abteilung", "")}
        return None

    def group_role(cns):
        """Beste Rolle aus den AD-Gruppen des Benutzers (höchste Berechtigung)."""
        cnset = {c.strip().lower() for c in cns if c and c.strip()}
        best = None
        with roles_lock:
            for key, e in roles.items():
                if e.get("kind") == "group" and key.strip().lower() in cnset:
                    if best is None or ROLE_RANK.get(e["role"], 0) > ROLE_RANK.get(best["role"], 0):
                        best = e
        if best:
            return {"role": best["role"], "abteilung": best.get("abteilung", "")}
        return None

    def has_group_entries():
        with roles_lock:
            return any(e.get("kind") == "group" for e in roles.values())

    # AD-Mailadresse eines Benutzers (aus --ad-mail-attribute) auflösen, gecacht.
    # Dient auch der Sichtkontrolle in der Verwaltung (Mouseover je Benutzer).
    user_mail_cache = {}

    def resolve_user_mail(upn):
        if not (args.ad_mail_attribute and args.ad_bind_dn and upn):
            return ""
        if upn not in user_mail_cache:
            m = ""
            try:
                m = ldap_user_attr(args.ad_url, args.ad_bind_dn,
                                   args.ad_bind_password, args.ad_base_dn, upn,
                                   args.ad_mail_attribute,
                                   insecure=args.ad_insecure) or ""
            except Exception as e:
                print(f"AD-Mail für {upn} nicht auflösbar: {e}", file=sys.stderr)
            user_mail_cache[upn] = m
        return user_mail_cache[upn]

    def roles_with_mail():
        """Kopie der Rollen; Benutzer-Einträge um die aufgelöste AD-Mail
        ergänzt (nur wenn --ad-mail-attribute gesetzt ist)."""
        with roles_lock:
            items = list(roles.items())
        out = {}
        for k, v in items:
            e = dict(v)
            if e.get("kind", "user") != "group":
                m = resolve_user_mail(k)
                if m:
                    e["mail"] = m
            out[k] = e
        return out

    def public_config():
        """Im System gesetzte Werte für die schreibgeschützte Konfig-Ansicht.
        NIE Passwörter/Geheimnisse – nur, ob sie gesetzt sind (ja/nein)."""
        j = lambda b: "ja" if b else "nein"
        pw = lambda name: j(bool(getattr(args, name, "")))
        return {
            "Aria Operations": {
                "URL": args.url or "–",
                "Benutzer": args.user or "–",
                "Auth-Quelle": args.auth_source or "local",
                "Zertifikat prüfen": j(not args.insecure),
                "Proxy": args.aria_proxy or "– (direkt)",
                "Auto-Refresh (Sek.)": args.refresh_interval,
                "Aria-Passwort gesetzt": j(bool(getattr(args, "password", None))),
            },
            "Berechnung": {
                "CPU-Faktor": args.cpu_factor,
                "Failover-Hosts (N+1)": args.failover_hosts,
                "vSAN-Faktor": args.vsan_factor,
                "Reservierung gültig (Tage)": args.res_ttl_days,
                "Uplink-Portgruppen anzeigen": j(args.show_uplink_portgroups),
                "Ausschluss-Tag": args.exclude_tag or "–",
                "Tag-Präfix": args.tag_property or "(alle mit 'tag')",
            },
            "Mail / SMTP": {
                "SMTP-Server": args.smtp_server or "– (kein Mailversand)",
                "Absender": args.smtp_from or "–",
                "smtp-to (Fallback Admin)": args.smtp_to or "–",
                "STARTTLS": j(args.smtp_tls),
                "SMTP-Passwort gesetzt": pw("smtp_password"),
                "AD-Mail-Attribut": args.ad_mail_attribute or "– (UPN)",
            },
            "Backup": {
                "Ziel": args.backup_target or "– (kein Backup)",
                "Port": args.backup_port,
                "Intervall (Sek.)": args.backup_interval,
                "Aufbewahrung (Tage)": args.backup_keep_days,
                "SSH-Key": args.backup_key or "–",
                "Backup-Passwort gesetzt": pw("backup_password"),
            },
            "Active Directory": {
                "URL": args.ad_url or "– (keine AD-Anmeldung)",
                "Domäne": args.ad_domain or "–",
                "Admin-User": args.admin_user or "–",
                "Service-Konto (Bind-DN)": args.ad_bind_dn or "–",
                "Basis-DN": args.ad_base_dn or "–",
                "Bind-Passwort gesetzt": pw("ad_bind_password"),
            },
            "Server / Speicher": {
                "Bind-Adresse": args.bind,
                "Port": args.port,
                "Datenspeicher": args.storage,
                "Daten-Verzeichnis": args.data_dir or "data",
                "Kontakt / Impressum": args.contact_info or "–",
            },
        }

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
        # Abgelehnte und stornierte Anfragen bleiben DAUERHAFT im Archiv erhalten;
        # nur aktive/genehmigte laufen nach --res-ttl-days ab (geben Kapazität frei).
        return [r for r in lst
                if r.get("rejected") or r.get("cancelled")
                or str(r.get("created") or "9999") >= cutoff]

    def load_res():
        try:
            lst = store.res_load()
        except Exception as e:
            print(f"Reservierungen unlesbar, starte leer: {e}", file=sys.stderr)
            return []
        changed = False
        for r in lst:
            if isinstance(r, dict) and "id" not in r:
                r["id"] = uuid.uuid4().hex[:12]
                changed = True
        kept = prune_res(lst)
        # Einmalige Reconciliation beim Start (nachgerüstete IDs / abgelaufene
        # Einträge dauerhaft festschreiben, damit der Speicher konsistent ist).
        if changed or len(kept) != len(lst):
            try:
                store.res_save_all(kept)
            except Exception as e:
                print(f"Reservierungen konnten nicht bereinigt werden: {e}",
                      file=sys.stderr)
        print(f"Reservierungen geladen: {len(kept)}", file=sys.stderr)
        return kept

    def save_res():
        """Ganze Liste sichern (JSON: Datei, SQLite: Tabelle neu aufbauen).
        Für einzelne Änderungen res_put()/res_drop() bevorzugen."""
        store.res_save_all(reservations)

    def res_put(entry):
        """Eine Reservierung sichern (SQLite: nur diese Zeile per Upsert)."""
        store.res_put(entry, reservations)

    def res_drop(ids):
        """Reservierungen mit diesen IDs aus dem Speicher entfernen."""
        store.res_delete(list(ids), reservations)

    def prune_reservations():
        """Abgelaufene Reservierungen aus Liste UND Speicher entfernen."""
        kept = prune_res(reservations)
        if len(kept) != len(reservations):
            keep_ids = {id(r) for r in kept}
            dropped = [r.get("id") for r in reservations if id(r) not in keep_ids]
            reservations[:] = kept
            res_drop([d for d in dropped if d])

    reservations = load_res()

    # ---- API-Tokens für externe Anwendungen (nur lesend) ----
    tokens_lock = threading.Lock()

    def load_tokens():
        d = store.load("tokens", None)
        if isinstance(d, dict):
            return {k: v for k, v in d.items()
                    if isinstance(v, dict) and v.get("hash")}
        return {}

    def save_tokens():
        store.save("tokens", tokens)

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
    # Rotation: ab LOG_MAX_BYTES wird die Datei zu .1 (…, .LOG_KEEP) weggerollt,
    # damit sie nicht unbegrenzt wächst. Gelesen wird nur das Dateiende.
    log_lock = threading.Lock()
    LOG_MAX_BYTES = 10 * 1024 * 1024
    LOG_KEEP = 3

    def rotate_log():
        """Nur mit gehaltenem log_lock aufrufen."""
        try:
            if os.path.getsize(args.log_file) < LOG_MAX_BYTES:
                return
            oldest = f"{args.log_file}.{LOG_KEEP}"
            if os.path.exists(oldest):
                os.remove(oldest)
            for i in range(LOG_KEEP - 1, 0, -1):
                src, dst = f"{args.log_file}.{i}", f"{args.log_file}.{i + 1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            os.replace(args.log_file, f"{args.log_file}.1")
        except OSError as e:
            print(f"Audit-Log-Rotation fehlgeschlagen: {e}", file=sys.stderr)

    def audit(user, action, detail=""):
        entry = {"ts": datetime.now().isoformat(timespec="seconds"),
                 "user": user or "system", "action": action, "detail": detail}
        try:
            with log_lock:
                ensure_dir(args.log_file)
                with open(args.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                rotate_log()
        except OSError as e:
            print(f"Audit-Log nicht schreibbar: {e}", file=sys.stderr)

    def _tail_lines(path, limit):
        """Die letzten Zeilen einer Datei lesen, ohne sie ganz einzulesen."""
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                chunk = min(size, max(64 * 1024, limit * 400))
                f.seek(size - chunk)
                data = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        lines = data.splitlines()
        if chunk < size and lines:
            lines = lines[1:]      # erste, evtl. angeschnittene Zeile verwerfen
        return lines

    def read_log(limit=500):
        with log_lock:
            lines = _tail_lines(args.log_file, limit)
            # Direkt nach einer Rotation ist die aktuelle Datei fast leer –
            # dann die vorherige Generation mit heranziehen.
            if len(lines) < limit and os.path.exists(f"{args.log_file}.1"):
                lines = _tail_lines(f"{args.log_file}.1", limit - len(lines)) + lines
        out = []
        for ln in lines[-limit:]:
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

    def _split_addrs(s):
        return [a for a in re.split(r"[,;\s]+", str(s or "")) if "@" in a]

    def mail_recipients(kind, r, team=None):
        """Empfänger für ein Ereignis nach den Mail-Regeln (Modell Gemischt)."""
        with notify_lock:
            roles = dict(notify_cfg.get("role") or {})
            team_email = dict(notify_cfg.get("team_email") or {})
        to = []
        # Anforderer -> der Antragsteller selbst
        if kind in ("created", "rejected", "approved") \
                and (roles.get("anforderer") or {}).get(kind):
            to += _split_addrs(r.get("von_mail") or r.get("von"))
        # Admin / Auditor -> feste Verteiler-Adresse (Admin fällt auf --smtp-to zurück)
        for role in ("admin", "auditor"):
            rc = roles.get(role) or {}
            if rc.get(kind):
                em = rc.get("email") or (args.smtp_to if role == "admin" else "")
                to += _split_addrs(em)
        # Team ist dran -> Adresse des aktuell zuständigen Teams
        if kind == "team_turn" and (roles.get("reviewer") or {}).get("team_turn") and team:
            to += _split_addrs(team_email.get(team))
        return to

    def mail_event(kind, r, team=None, actor=""):
        """Ereignis-Mail nach den Mail-Regeln im Hintergrund verschicken."""
        if not args.smtp_server:
            return
        to = mail_recipients(kind, r, team)
        if not to:
            return
        name, cluster = r.get("name", "?"), r.get("cluster", "?")
        if kind == "team_turn":
            action = f"wartet auf Freigabe durch {team}"
            subject = f"Kapazitätsreservierung {action}: {name} ({cluster})"
        else:
            action = {"created": "beantragt", "rejected": "abgelehnt",
                      "approved": "genehmigt"}.get(kind, kind)
            subject = f"Kapazitätsreservierung {action}: {name} ({cluster})"
        body = reservation_mail_body(r, action, actor or "System", args.res_ttl_days)
        html = reservation_mail_html(r, action, actor or "System", args.res_ttl_days)

        def worker():
            try:
                send_mail(args, subject, body, to_override=to, html=html)
            except Exception as e:
                print(f"Mail-Versand fehlgeschlagen: {e}", file=sys.stderr)
        threading.Thread(target=worker, daemon=True).start()

    def public_res(r):
        """Reservierung ohne serverinterne Felder (z. B. die aufgelöste
        Empfänger-Mailadresse von_mail) – so wie sie an Clients gehen darf."""
        return {k: v for k, v in r.items() if k != "von_mail"}

    def clusters_for(role):
        """Cluster-Daten je Rolle: Anforderer sehen den Workload-Wert nicht
        (weder im UI noch im Payload)."""
        cl = state["clusters"]
        if role == "anforderer":
            return [{k: v for k, v in c.items() if k != "workload"} for c in cl]
        return cl

    def visible_res(s):
        """Sichtbare Reservierungen je Rolle: Admin, Auditor und Reviewer sehen
        ALLE Anfragen. Nur Anforderer sind auf ihr eigenes Team beschränkt –
        fremde genehmigte bleiben anonymisiert enthalten, damit die freie
        Kapazität stimmt."""
        if s["role"] in ("admin", "auditor", "reviewer"):
            return [public_res(r) for r in reservations]
        team = s.get("abteilung") or ""
        out = []
        for r in reservations:
            mine = (team and r.get("abteilung") == team) or r.get("von") == s["user"]
            if mine:
                # Anforderer sehen nicht, WER entschieden hat – der Fortschritt
                # (welches Team schon freigegeben hat) bleibt jedoch sichtbar,
                # nur ohne Namen.
                d = {k: v for k, v in r.items()
                     if k not in ("approved_by", "rejected_by", "von_mail")}
                if isinstance(d.get("approvals"), list):
                    d["approvals"] = [{"team": a.get("team"), "on": a.get("on")}
                                      for a in d["approvals"]]
                out.append(d)
            elif r.get("approved") and not r.get("cancelled"):
                # bewusst ohne Name, von, Change, Kommentar; storniert zählt nicht
                out.append({"id": r.get("id"), "cluster": r.get("cluster"),
                            "name": "(anderes Team)", "vcpu": r.get("vcpu"),
                            "ram_gb": r.get("ram_gb"),
                            "storage_gb": r.get("storage_gb"),
                            "created": r.get("created"),
                            "approved": True, "foreign": True})
        return out

    def do_refresh():
        state.update(refreshing=True, error=None, progress="0 %")
        try:
            if args.sample:
                time.sleep(2)  # Demo: Ladezeit simulieren
                clusters = build_summary(sample_data(), args.cpu_factor, args.failover_hosts)
            else:
                api = AriaOps(args.url, args.user, password, args.auth_source,
                              verify_tls=not args.insecure, proxy=args.aria_proxy)
                clusters = collect(api, args.cpu_factor,
                                   progress=lambda m: state.update(progress=m),
                                   failover_hosts=args.failover_hosts,
                                   exclude_tag=args.exclude_tag,
                                   tag_property=args.tag_property,
                                   vsan_factor=args.vsan_factor)
            strip_uplinks(clusters, not args.show_uplink_portgroups)
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

    def purge_stale():
        """Abgelaufene Sitzungen und alte Login-Fehlversuche verwerfen, damit
        beides nicht unbegrenzt im Speicher wächst."""
        now = time.time()
        for t in [t for t, s in list(sessions.items()) if s.get("exp", 0) <= now]:
            sessions.pop(t, None)
        with login_lock:
            for k in [k for k, v in list(login_fails.items())
                      if not v or now - max(v) > LOGIN_WINDOW]:
                login_fails.pop(k, None)

    def maintenance():
        while True:
            time.sleep(300)
            try:
                purge_stale()
            except Exception as e:
                print(f"Aufräumen fehlgeschlagen: {e}", file=sys.stderr)

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
                         "/genehmigungen", "/archiv", "/verwaltung", "/log"):
                s = self._session()
                if auth_enabled and not s:
                    self._send(LOGIN_TEMPLATE.replace("__VERSION__", VERSION)
                               .replace("__CONTACT__", _html_escape(args.contact_info)),
                               "text/html; charset=utf-8")
                    return
                userinfo = ({"user": s["user"], "role": s["role"],
                             "abteilung": s.get("abteilung") or ""}
                            if auth_enabled else None)
                self._send(render_html(clusters_for(userinfo["role"] if userinfo else None),
                                       args.cpu_factor,
                                       serve_mode=True,
                                       updated=state["updated"] or
                                       "noch keine Daten – erster Abruf läuft ...",
                                       res_ttl=args.res_ttl_days,
                                       failover_hosts=args.failover_hosts,
                                       userinfo=userinfo, teams=approval_teams,
                                       rolenames=role_names, contact=args.contact_info,
                                       selector=cluster_selector,
                                       backup=bool(args.backup_target),
                                       notify=notify_cfg),
                           "text/html; charset=utf-8")
            elif route == "/api/data":
                s = self._require()
                if not s:
                    return
                self._json({"updated": state["updated"],
                            "clusters": clusters_for(s["role"])})
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
                    prune_reservations()
                    self._json(visible_res(s))
            elif route == "/api/v1/openapi.json":
                self._send(json.dumps(openapi_spec(), ensure_ascii=False, indent=2),
                           "application/json; charset=utf-8")
            elif route in ("/api/v1/docs", "/api/v1/docs/"):
                self._send(API_DOCS_HTML.replace("__VERSION__", VERSION),
                           "text/html; charset=utf-8")
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
                    # Token = externe Anwendung (Workload ok); Session eines
                    # Anforderers bekommt den Workload wie im UI nicht.
                    self._json({"updated": state["updated"],
                                "clusters": (state["clusters"] if tok
                                             else clusters_for(s["role"]))})
                else:
                    with res_lock:
                        prune_reservations()
                        data = (visible_res(s) if s and not tok
                                else [public_res(r) for r in reservations])
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
                self._json(roles_with_mail())
            elif route == "/api/teams":
                if not self._require("admin"):
                    return
                with teams_lock:
                    self._json({"teams": list(approval_teams)})
            elif route == "/api/selector":
                if not self._require("admin"):
                    return
                with selector_lock:
                    self._json({"selector": list(cluster_selector)})
            elif route == "/api/rolenames":
                if not self._require("admin"):
                    return
                with rolenames_lock:
                    self._json({"rolenames": dict(role_names)})
            elif route == "/api/notify":
                if not self._require("admin"):
                    return
                with notify_lock:
                    self._json({"notify": json.loads(json.dumps(notify_cfg))})
            elif route == "/api/config":
                if not self._require("admin"):
                    return
                self._json({"config": public_config()})
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
                # Rolle: 1) direkt zugewiesen (Verwaltung), 2) über AD-Gruppe,
                # 3) Standard = Anforderer. Ohne Rolle darf man nur beantragen.
                explicit = role_entry(user)
                source = ""
                if explicit:
                    entry = explicit
                else:
                    grp = None
                    if args.ad_bind_dn and has_group_entries():
                        try:
                            cns = ldap_member_of(args.ad_url, args.ad_bind_dn,
                                                 args.ad_bind_password, args.ad_base_dn,
                                                 user, insecure=args.ad_insecure)
                            grp = group_role(cns)
                        except Exception as e:
                            print(f"AD-Gruppensuche fehlgeschlagen: {e}", file=sys.stderr)
                    entry = grp or {"role": "anforderer", "abteilung": ""}
                    source = " (AD-Gruppe)" if grp else " (Standard)"
                audit(user, "Anmeldung", f"Rolle: {entry['role']}{source}"
                      + (f", Abteilung/Team: {entry.get('abteilung')}"
                         if entry.get("abteilung") else ""))
                # Mailadresse des Anforderers: standardmäßig der Anmeldename (UPN);
                # optional aus einem anderen AD-Attribut (--ad-mail-attribute).
                mail_addr = ""
                if args.ad_mail_attribute and args.ad_bind_dn:
                    try:
                        mail_addr = ldap_user_attr(
                            args.ad_url, args.ad_bind_dn, args.ad_bind_password,
                            args.ad_base_dn, user, args.ad_mail_attribute,
                            insecure=args.ad_insecure)
                    except Exception as e:
                        print(f"AD-Mailattribut-Suche fehlgeschlagen: {e}",
                              file=sys.stderr)
                token = secrets.token_urlsafe(32)
                sessions[token] = {"user": user, "role": entry["role"],
                                   "abteilung": entry.get("abteilung") or "",
                                   "mail": mail_addr,
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
                # Change / Jira-Ticket ist freiwillig und frei wählbar (kein Format)
                change = str(item.get("change") or "").strip()[:60]
                try:
                    entry = {"id": uuid.uuid4().hex[:12],
                             "cluster": str(item.get("cluster") or ""),
                             "name": str(item.get("name")).strip(),
                             "change": change,
                             "vcpu": int(float(item.get("vcpu") or 0)),
                             "ram_gb": int(float(item.get("ram_gb") or 0)),
                             "storage_gb": int(float(item.get("storage_gb") or 0)),
                             "von": s["user"] or "",
                             "von_mail": s.get("mail") or "",
                             "abteilung": s.get("abteilung") or "",
                             "created": datetime.now().date().isoformat(),
                             "approvals": [],
                             "approved": False}
                except (TypeError, ValueError):
                    self._json({"error": "Ungültige Zahlenwerte"}, 400)
                    return
                with res_lock:
                    prune_reservations()
                    reservations.append(entry)
                    res_put(entry)
                    self._json(visible_res(s))
                audit(s["user"], "Antrag erstellt", res_detail(entry))
                mail_event("created", entry, actor=s["user"] or "")
                if approval_teams:               # erstes Team ist ab jetzt dran
                    mail_event("team_turn", entry, team=current_team(entry),
                               actor=s["user"] or "")
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
                            res_put(r)
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
                    if notify.get("approved"):    # letzte Stufe -> endgültig genehmigt
                        mail_event("approved", notify, actor=s["user"] or "")
                    else:                          # Zwischenstufe -> nächstes Team ist dran
                        nt = current_team(notify)
                        if nt:
                            mail_event("team_turn", notify, team=nt,
                                       actor=s["user"] or "")
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
                            res_put(r)
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
                    mail_event("rejected", notify, actor=s["user"] or "")
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
                            res_put(r)
                    resp = None if err else visible_res(s)
                if err:
                    self._json({"error": err[0]}, err[1])
                    return
                self._json(resp)
                if notify:
                    audit(s["user"], "Antrag storniert", res_detail(notify)
                          + (f" – Kommentar: {comment}" if comment else ""))
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
                kind = "group" if body.get("kind") == "group" else "user"
                # Gruppennamen NICHT als UPN normalisieren
                key = (str(body.get("user") or "").strip() if kind == "group"
                       else normalize_user(body.get("user")))
                role = body.get("role")
                dept = str(body.get("abteilung") or "").strip()
                if not key or role not in VALID_ROLES:
                    self._json({"error": "Name/Gruppe und gültige Rolle erforderlich"}, 400)
                    return
                with roles_lock:
                    roles[key] = {"role": role, "abteilung": dept, "kind": kind}
                    save_roles()
                self._json(roles_with_mail())
                audit(self._session()["user"],
                      "AD-Gruppe zugewiesen" if kind == "group" else "Rolle zugewiesen",
                      f"{key} -> {role}" + (f" ({dept})" if dept else ""))
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
                    # Team-Mail-Adresse auf den neuen Namen umziehen
                    with notify_lock:
                        te = notify_cfg.get("team_email") or {}
                        if old in te:
                            te[new] = te.pop(old)
                            save_notify()
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
                audit(s["user"], "Team umbenannt",
                      f"„{old}“ → „{new}“" + (f" ({moved} Reviewer übernommen)" if moved else ""))
                self._json({"teams": list(approval_teams), "roles": roles_with_mail()})
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
                    self._json([public_res(r) for r in reservations])
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
                # verwaiste Team-Mail-Adressen entfernen
                with notify_lock:
                    te = notify_cfg.get("team_email") or {}
                    orphan = [k for k in te if k not in new]
                    for k in orphan:
                        te.pop(k, None)
                    if orphan:
                        save_notify()
                audit(s["user"], "Genehmigungs-Teams geändert",
                      " → ".join(new) if new else "(keine – einstufig)")
                self._json({"teams": list(approval_teams)})
            elif self.path == "/api/selector":
                s = self._require("admin")
                if not s:
                    return
                body = self._body()
                if not isinstance(body, list):
                    self._json({"error": "Liste von Tag-Kategorien erwartet"}, 400)
                    return
                new = clean_selector(body)
                with selector_lock:
                    cluster_selector[:] = new
                    save_selector()
                audit(s["user"], "Cluster-Selektor geändert",
                      " → ".join(f"{e['label']} ({e['category']})" for e in new)
                      if new else "(keiner)")
                self._json({"selector": list(cluster_selector)})
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
            elif self.path == "/api/notify":
                s = self._require("admin")
                if not s:
                    return
                body = self._body()
                if not isinstance(body, dict):
                    self._json({"error": "Objekt mit Mail-Regeln erwartet"}, 400)
                    return
                # team_email nur für tatsächlich existierende Teams behalten
                clean = _merge_notify(body)
                with teams_lock:
                    valid = set(approval_teams)
                clean["team_email"] = {k: v for k, v in clean["team_email"].items()
                                       if k in valid and v}
                with notify_lock:
                    notify_cfg.clear()
                    notify_cfg.update(clean)
                    save_notify()
                    result = json.loads(json.dumps(notify_cfg))
                on = [f"{role}:{ev}" for role, evs in NOTIFY_ROLE_EVENTS.items()
                      for ev in evs if (result["role"].get(role) or {}).get(ev)]
                audit(s["user"], "Mail-Regeln geändert",
                      (", ".join(on) or "keine")
                      + (f"; Team-Adressen: {len(result['team_email'])}"
                         if result["team_email"] else ""))
                self._json({"notify": result})
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
                self._json(roles_with_mail())
                if removed:
                    audit(s["user"], "Rolle entfernt",
                          f"{user} (war {removed.get('role')})")
            else:
                self.send_error(404)

        def log_message(self, *a):
            pass

    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=maintenance, daemon=True).start()
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
        elif callable(action.type):
            # getippte Optionen (int, float, int_or) auch aus der INI korrekt
            # konvertieren – sonst käme z. B. port als String an.
            try:
                defaults[dest] = action.type(raw)
            except (ValueError, argparse.ArgumentTypeError) as e:
                ap.error(f"Option '{key}' in {path}: ungültiger Wert '{raw}' ({e})")
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
    ap.add_argument("--cpu-factor", type=int_or(6), default=6, help="CPU-Überprovisionierungsfaktor (Standard: 6)")
    ap.add_argument("--failover-hosts", type=int_or(1), default=1,
                    help="Ausfall-Hosts pro Cluster (N+1): die größten N Hosts werden "
                         "von der Kapazität abgezogen (Standard: 1, 0 = aus)")
    ap.add_argument("--exclude-tag", default="",
                    help="VMs mit diesem vROps-Tag aus der Auswertung ausschließen, "
                         "Format Kategorie:Wert, z. B. Kapa_Filter:Ja (leer = aus)")
    ap.add_argument("--vsan-factor", type=float, default=0.5,
                    help="Anteil der vSAN-Bruttokapazität, der als nutzbar zählt "
                         "(Standard: 0.5 für RAID-1/Spiegelung; 1 = brutto). "
                         "Wirkt auf Kapazität UND Belegung des vSAN-Datastores.")
    ap.add_argument("--tag-property", default="",
                    help="Eigenschafts-Präfix für vSphere-Tags des Clusters, z. B. "
                         "'summary|tag'. Ohne Angabe werden alle Eigenschaften "
                         "genommen, deren Schlüssel 'tag' enthält (das Log nennt "
                         "die erkannten Schlüssel)")
    ap.add_argument("--show-uplink-portgroups", action="store_true",
                    help="dvSwitch-Uplink-/Trunk-Portgruppen NICHT ausblenden. "
                         "Standard: ausblenden (Portgruppen mit 'uplink' im Namen "
                         "oder breiter VLAN-Trunk-Range wie 0-4094 sind keine "
                         "echten Netz-VLANs)")
    ap.add_argument("--contact-info", default="",
                    help="Kontakt-/Impressumszeile (Abteilung/Firma + Mailadresse "
                         "für Rückfragen), wird im Footer und auf der Login-Maske "
                         "angezeigt")
    ap.add_argument("--insecure", action="store_true", help="TLS-Zertifikat nicht prüfen (Self-Signed)")
    ap.add_argument("--aria-proxy", default="",
                    help="Optionaler HTTP(S)-Proxy für die Aria-Anfragen, z. B. "
                         "http://proxy.firma.local:3128 (für abgesicherte Umgebungen; "
                         "ohne Angabe direkte Verbindung)")
    ap.add_argument("--output", default="kapa_dashboard.html", help="Ausgabedatei")
    ap.add_argument("--json", help="Rohdaten zusätzlich als JSON speichern")
    ap.add_argument("--sample", action="store_true", help="Demo mit Beispieldaten")
    ap.add_argument("--serve", action="store_true",
                    help="Als lokaler Webserver laufen (Live-Abruf per Knopf, Disk-Cache)")
    ap.add_argument("--port", type=int_or(8080), default=8080, help="Port für --serve (Standard: 8080)")
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
    ap.add_argument("--refresh-interval", type=int_or(1800), default=1800,
                    help="Automatische Aktualisierung im Serve-Modus in Sekunden "
                         "(0 = aus, Standard: 1800 = 30 min)")
    ap.add_argument("--res-file", default="kapa_reservierungen.json",
                    help="Reservierungsdatei im Serve-Modus "
                         "(Standard: data/kapa_reservierungen.json)")
    ap.add_argument("--res-ttl-days", type=int_or(31), default=31,
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
    ap.add_argument("--ad-bind-dn", default="",
                    help="Service-Konto (DN oder UPN) für die AD-Gruppensuche; "
                         "ohne Angabe werden nur direkt zugewiesene Benutzer "
                         "berechtigt (keine AD-Gruppen)")
    ap.add_argument("--ad-bind-password", default="",
                    help="Passwort des Service-Kontos (alternativ "
                         "--ad-bind-password-file oder AD_BIND_PASSWORD)")
    ap.add_argument("--ad-bind-password-file", default="",
                    help="Datei mit dem Service-Konto-Passwort")
    ap.add_argument("--ad-base-dn", default="",
                    help="Basis-DN für die Gruppensuche, z. B. DC=firma,DC=local "
                         "(ohne Angabe aus --ad-domain abgeleitet)")
    ap.add_argument("--ad-mail-attribute", default="",
                    help="AD-Attribut, aus dem die Empfänger-Mailadresse des "
                         "Anforderers gelesen wird (z. B. 'mail'). Ohne Angabe wird "
                         "der Anmeldename (UPN) als Adresse verwendet. Erfordert ein "
                         "Service-Konto (--ad-bind-dn); wird bei der Anmeldung "
                         "aufgelöst und mit der Reservierung gespeichert")
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
    ap.add_argument("--backup-port", type=int_or(22), default=22, help="SSH-Port (Standard: 22)")
    ap.add_argument("--backup-key", default="",
                    help="SSH-Private-Key für das Backup (empfohlen). Muss vom "
                         "Dienst-Benutzer lesbar sein und AUSSERHALB des Home-"
                         "Verzeichnisses liegen (z. B. /etc/kapa/), weil die "
                         "systemd-Härtung ProtectHome=true Home-Verzeichnisse "
                         "ausblendet.")
    ap.add_argument("--backup-known-hosts", default="",
                    help="known_hosts-Datei für das Backup (Standard: "
                         "known_hosts im Datenordner – muss beschreibbar sein)")
    ap.add_argument("--backup-password", default="",
                    help="SSH-Passwort (alternativ BACKUP_PASSWORD; erfordert sshpass)")
    ap.add_argument("--backup-interval", type=int_or(43200), default=43200,
                    help="Backup-Intervall in Sekunden (Standard: 43200 = zweimal "
                         "täglich, 0 = nur einmal beim Start)")
    ap.add_argument("--backup-keep-days", type=int_or(30), default=30,
                    help="Backups auf dem Ziel nach N Tagen löschen "
                         "(Standard: 30, 0 = nie aufräumen)")
    ap.add_argument("--log-file", default="kapa_log.jsonl",
                    help="Audit-Log-Datei (Standard: data/kapa_log.jsonl)")
    ap.add_argument("--tokens-file", default="kapa_tokens.json",
                    help="API-Token-Datei (Standard: data/kapa_tokens.json)")
    ap.add_argument("--teams-file", default="kapa_teams.json",
                    help="Datei mit den Genehmigungs-Teams (Standard: "
                         "data/kapa_teams.json); Pflege über die Verwaltungsseite")
    ap.add_argument("--storage", default="json", choices=("json", "sqlite"),
                    help="Datenspeicherung: 'json' (Standard, je Sammlung eine "
                         "editierbare Datei) oder 'sqlite' (eine data/kapa.db, "
                         "Reservierungen mit inkrementellen Schreibzugriffen). "
                         "Beim Wechsel auf sqlite werden vorhandene JSON-Daten "
                         "einmalig automatisch übernommen.")
    ap.add_argument("--db-file", default="kapa.db",
                    help="SQLite-Datei bei --storage sqlite (Standard: data/kapa.db)")
    ap.add_argument("--selector-file", default="kapa_selektor.json",
                    help="Datei mit den Tag-Kategorien des Cluster-Selektors "
                         "(Standard: data/kapa_selektor.json); Pflege über die "
                         "Verwaltungsseite")
    ap.add_argument("--rolenames-file", default="kapa_rollennamen.json",
                    help="Datei mit den frei wählbaren Rollen-Bezeichnungen "
                         "(Standard: data/kapa_rollennamen.json); Pflege über die "
                         "Verwaltungsseite")
    ap.add_argument("--notify-file", default="kapa_mail.json",
                    help="Datei mit den Mail-Benachrichtigungsregeln je Rolle "
                         "(Standard: data/kapa_mail.json); Pflege über die "
                         "Verwaltungsseite")
    # Leere Werte von Langoptionen verwerfen, BEVOR geparst wird. Grund: Die
    # systemd-Unit baut Argumente wie "--backup-target ${BACKUP_TARGET}" aus
    # kapa.env; ist die Variable leer, käme ein leeres Argument an und würde den
    # INI-Wert überschreiben (leer schlägt INI). Ein leerer Wert bedeutet hier
    # überall "nicht gesetzt", also lassen wir ihn weg – dann greift die INI bzw.
    # der eingebaute Standard. (String-Pendant zu int_or() für Zahlen.)
    argv = _drop_empty_long_opts(sys.argv[1:])
    # Erst --config einlesen, dann endgültig parsen (CLI schlägt INI)
    pre, _ = ap.parse_known_args(argv)
    if pre.config:
        apply_config_file(ap, pre.config)
    args = ap.parse_args(argv)

    # JSON-Datendateien ohne Pfadangabe unter --data-dir ablegen (Standard data/)
    base = args.data_dir or "data"
    args.cache = data_path(args.cache, base)
    args.res_file = data_path(args.res_file, base)
    args.roles_file = data_path(args.roles_file, base)
    args.log_file = data_path(args.log_file, base)
    args.tokens_file = data_path(args.tokens_file, base)
    args.teams_file = data_path(args.teams_file, base)
    args.selector_file = data_path(args.selector_file, base)
    args.db_file = data_path(args.db_file, base)
    args.rolenames_file = data_path(args.rolenames_file, base)
    args.notify_file = data_path(args.notify_file, base)
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
    args.ad_bind_password = secret(args.ad_bind_password, args.ad_bind_password_file,
                                   "AD_BIND_PASSWORD")
    # Basis-DN aus der Domäne ableiten, falls nicht gesetzt (firma.local -> DC=firma,DC=local)
    if not args.ad_base_dn and args.ad_domain:
        args.ad_base_dn = ",".join("DC=" + p for p in args.ad_domain.split("."))

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
                      verify_tls=not args.insecure, proxy=args.aria_proxy)
        clusters = collect(api, args.cpu_factor, failover_hosts=args.failover_hosts,
                           exclude_tag=args.exclude_tag,
                           tag_property=args.tag_property,
                           vsan_factor=args.vsan_factor)
        strip_uplinks(clusters, not args.show_uplink_portgroups)

    if args.json:
        ensure_dir(args.json)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(clusters, f, ensure_ascii=False, indent=2)

    render_dashboard(clusters, args.cpu_factor, args.output,
                     args.res_ttl_days, args.failover_hosts,
                     contact=args.contact_info)

    print(f"\n{'Cluster':<22}{'Hosts':>6}{'Cores':>7}{'vCPU-Kap':>10}{'vCPU-belegt':>12}"
          f"{'vCPU-frei':>10}{'RAM-Kap GB':>12}{'RAM-belegt':>12}{'RAM-frei':>10}")
    for c in clusters:
        print(f"{c['name']:<22}{c['hostCount']:>6}{c['cores']:>7}{c['vcpuCap']:>10}"
              f"{c['vcpuUsed']:>12}{c['vcpuFree']:>10}{c['ramCap']:>12}"
              f"{c['ramUsed']:>12}{c['ramFree']:>10}")
    print(f"\nDashboard geschrieben: {args.output}")


if __name__ == "__main__":
    main()
