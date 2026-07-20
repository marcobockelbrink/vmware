#!/usr/bin/env python3
"""Smoke-Tests für aria_kapa.py — nur Standardbibliothek, kein pytest nötig.

Startet den Server im Sample-Modus auf einem freien Port und prüft die
wichtigsten Abläufe Ende-zu-Ende (HTTP-Ebene, wie ein echter Client):

    python3 tests/smoke.py            # aus der Projektwurzel

Exit-Code 0 = alles grün. Gedacht als Sicherheitsnetz vor jedem Release.
"""
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "aria_kapa.py")

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def req(method, path, body=None, headers=None, raw=False):
    """HTTP-Aufruf; Rückgabe (status, body_bytes|json, headers)."""
    url = BASE + path
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            payload = resp.read()
            if not raw and "json" in (resp.headers.get("Content-Type") or ""):
                payload = json.loads(payload.decode())
            return resp.status, payload, dict(resp.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            payload = json.loads(payload.decode())
        except Exception:
            pass
        return e.code, payload, dict(e.headers)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_up(timeout=30):
    """Warten, bis der Server läuft UND der erste Demo-Abruf durch ist."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st, body, _ = req("GET", "/healthz")
            if st == 200 and body.get("clusters", 0) > 0 \
                    and not body.get("refreshing"):
                return body
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Server kam nicht hoch — siehe " + LOG)


# ----------------------------------------------------------------- Start ----
PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
DATA = tempfile.mkdtemp(prefix="kapa_smoke_")
LOG = os.path.join(DATA, "server.log")
proc = subprocess.Popen(
    [sys.executable, APP, "--serve", "--sample", "--bind", "127.0.0.1",
     "--port", str(PORT), "--data-dir", DATA,
     "--id-prefix", "KAPA-", "--id-length", "6"],
    stdout=open(LOG, "w"), stderr=subprocess.STDOUT)

try:
    print(f"Server: {BASE}  (Daten: {DATA})")
    health = wait_up()

    print("== healthz / Seite / gzip ==")
    check("healthz ok", health.get("status") == "ok" and health.get("clusters") == 3, str(health))
    st, page, hdr = req("GET", "/", raw=True)
    page = page.decode()
    check("Hauptseite 200 + Marker", st == 200 and "TANZU_MHZ" in page
          and "openClusterHash" in page and 'id="themeBtn"' in page)
    st, gz, hdr = req("GET", "/", headers={"Accept-Encoding": "gzip"}, raw=True)
    check("gzip aktiv + Inhalt intakt",
          hdr.get("Content-Encoding") == "gzip" and len(gz) < len(page)
          and "Kapazitätsübersicht" in gzip.decompress(gz).decode())

    print("== Teams / Reservierungen / Härtung ==")
    st, _, _ = req("PUT", "/api/teams", ["Team Netzwerk", "Team Security"])
    check("Teams speichern", st == 200)
    st, res, _ = req("POST", "/api/reservations",
                     {"name": "Smoke\r\nBcc: evil " + "A" * 300,
                      "cluster": "Cluster-01\n", "vcpu": 4, "ram_gb": 16})
    flat = json.dumps(res)
    check("Anlegen + Freitext-Härtung", st == 200 and "\\r" not in flat
          and "Bcc" in flat and len(res[-1]["name"]) <= 120
          and "\n" not in res[-1]["cluster"])

    st, res2, _ = req("POST", "/api/reservations",
                      {"name": "Storno-Test", "cluster": "Cluster-01",
                       "vcpu": 1, "ram_gb": 1})
    sid = next(r["id"] for r in res2 if r["name"] == "Storno-Test")
    req("POST", f"/api/reservations/{sid}/cancel", {})
    st, _, _ = req("POST", f"/api/reservations/{sid}/approve", {})
    check("Stornierter Antrag nicht mehr genehmigbar", st == 404)

    print("== API-Token: Schreibrechte ==")
    st, tok, _ = req("POST", "/api/tokens", {"name": "Smoke-Writer"})
    raw_token = tok.get("token") or tok.get("raw") or ""
    check("Token erzeugt", st == 200 and raw_token.startswith("kapa_"))
    st, tl, _ = req("GET", "/api/tokens")
    tid = next(iter(tl))
    auth = {"Authorization": "Bearer " + raw_token}
    st, _, _ = req("POST", "/api/v1/reservations", {"name": "x"}, headers=auth)
    check("Schreiben ohne Recht -> 403", st == 403)
    st, _, _ = req("PUT", f"/api/tokens/{tid}",
                   {"write_res": True, "write_approve": True})
    check("Rechte per Klick setzen", st == 200)
    st, made, _ = req("POST", "/api/v1/reservations",
                      {"name": "Smoke API", "cluster": "Cluster-02",
                       "vcpu": 2, "ram_gb": 8, "abteilung": "Team Netzwerk"},
                      headers=auth)
    rid = made.get("reservation", {}).get("id", "")
    check("API-Anlage 201 + Kapa-ID", st == 201 and rid.startswith("KAPA-"))
    st, a1, _ = req("POST", f"/api/v1/reservations/{rid}/approve",
                    {"comment": "ok"}, headers=auth)
    st2, a2, _ = req("POST", f"/api/v1/reservations/{rid}/approve", headers=auth)
    check("Zweistufige API-Freigabe", st == 200 and st2 == 200
          and a2["reservation"]["approved"] is True
          and a2["reservation"]["approved_by"] == "api:Smoke-Writer")
    st, _, _ = req("POST", f"/api/v1/reservations/{rid}/reject", headers=auth)
    check("Entschieden -> reject 404", st == 404)

    print("== CSV / Sprache / OpenAPI ==")
    st, csv_de, _ = req("GET", "/api/v1/reservations?format=csv", raw=True)
    st2, csv_en, _ = req("GET", "/api/v1/reservations?format=csv&lang=en", raw=True)
    check("Reservierungs-CSV DE/EN",
          csv_de.decode().startswith("id;name;change") and "gueltig_bis" in csv_de.decode()
          and "valid_until" in csv_en.decode() and "approved" in csv_en.decode())
    st, cap, _ = req("GET", "/api/v1/data?format=csv&lang=en", raw=True)
    lines = cap.decode().splitlines()
    h = lines[0].split(";")
    row = dict(zip(h, lines[1].split(";")))
    eff = int(row["vcpu_free"]) - int(row["reserved_vcpu"]) - int(row["tanzu_vcpu"])
    check("Kapazitäts-CSV effektiv-frei-Rechnung",
          "vcpu_free_effective" in h and eff == int(row["vcpu_free_effective"]))
    st, spec_de, _ = req("GET", "/api/v1/openapi.json")
    st2, spec_en, _ = req("GET", "/api/v1/openapi.json",
                          headers={"Accept-Language": "en-US"})
    check("OpenAPI DE-Default + EN per Header",
          "Kapazitätsplanung" in spec_de["info"]["title"]
          and "Capacity Planning" in spec_en["info"]["title"])
    st, docs, _ = req("GET", "/api/v1/docs", raw=True)
    check("API-Doku-Seite", st == 200 and b"openapi.json" in docs)

    print("== Mail-Regeln / Vorlage / Ankündigung / Prefs ==")
    st, n, _ = req("PUT", "/api/notify",
                   {"role": {"reviewer": {"team_turn": True, "reminder": True}},
                    "team_email": {"Team Netzwerk": "n@x.de"},
                    "reminder_days": 99,
                    "template_subject": "Smoke {{name}}"})
    check("Notify: reminder_days-Clamp + Vorlage",
          n["notify"]["reminder_days"] == 30
          and n["notify"]["template_subject"] == "Smoke {{name}}"
          and n["notify"]["role"]["reviewer"]["reminder"] is True)
    st, pv, _ = req("PUT", "/api/mail-preview", {"template_subject": "S {{cluster}}"})
    check("Mail-Vorschau", pv.get("subject") == "S Cluster-03")
    st, an, _ = req("PUT", "/api/announce",
                    {"active": True, "title": "Smoke", "text": "Hallo Welt"})
    st2, page2, _ = req("GET", "/", raw=True)
    check("Ankündigung aktiv -> injiziert",
          an["announce"]["active"] is True
          and b'"title": "Smoke"' in page2)
    st, _, _ = req("PUT", "/api/announce", {"active": False, "title": "Smoke",
                                            "text": "Hallo Welt"})
    st, page3, _ = req("GET", "/", raw=True)
    check("Ankündigung inaktiv -> null", b"let ANNOUNCE = null;" in page3)
    st, p, _ = req("PUT", "/api/prefs",
                   {"cols": {"ktable": {"3": True}}, "announce_seen": "abc123",
                    "theme": "light"})
    check("Prefs-Roundtrip (cols/seen/theme)",
          p.get("theme") == "light" and p.get("announce_seen") == "abc123"
          and p.get("cols", {}).get("ktable", {}).get("3") is True)
    st, p2, _ = req("PUT", "/api/prefs", {"cols": {}, "theme": "neon"})
    check("Ungültiges Theme wird verworfen", "theme" not in p2)

    print("== INI-Wächter ==")
    bad = os.path.join(DATA, "bad.ini")
    with open(bad, "w") as f:
        f.write("[kapa]\ncpu-factor = 6\n[quelle:X]\nurl = https://x\nuser = u\nport = 8888\n")
    r = subprocess.run([sys.executable, APP, "--config", bad, "--sample", "--serve"],
                       capture_output=True, text=True, timeout=30)
    check("Verrutschter Schlüssel in [quelle:] -> Fehler",
          r.returncode != 0 and "gehören aber vermutlich nach [kapa]" in r.stderr)

finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(DATA, ignore_errors=True)

print(f"\n{PASS} ok, {len(FAIL)} fehlgeschlagen"
      + (": " + ", ".join(FAIL) if FAIL else " — alles grün ✅"))
sys.exit(1 if FAIL else 0)
