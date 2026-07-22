# Sicherheitsrichtlinie / Security Policy

> 🇬🇧 English below.

## Schwachstelle melden

Bitte Sicherheitslücken **nicht** über öffentliche Issues melden, sondern
vertraulich über **GitHub → Security → „Report a vulnerability"**
([Private Vulnerability Reporting](https://github.com/marcobockelbrink/vmware/security/advisories/new)).
Wir bemühen uns um eine erste Rückmeldung innerhalb weniger Werktage.

## Automatische Sicherheitsprüfungen

Jeder Push auf **jeden Branch**, jeder Pull Request nach `main` sowie ein
wöchentlicher Lauf prüfen den Code mit mehreren unabhängigen, quelloffenen Scannern; die Ergebnisse landen im
Reiter **Security** dieses Repos:

| Scanner | Zweck |
|---|---|
| **CodeQL** | GitHubs SAST – Injection, Path-Traversal, unsichere Muster |
| **Bandit** | Python-Security-Linter (subprocess, Krypto, Temp-Dateien …) |
| **Semgrep** | Regelsätze für Python, Security-Audit und Secrets |
| **Trivy** | Schwachstellen, Fehlkonfigurationen und Secrets (Repo + Dockerfile) |
| **OpenSSF Scorecard** | Supply-Chain-/Repo-Hygiene mit Bewertung |
| **Dependabot** | aktuelle Action- und Container-Basis-Versionen |

## Konzeptionelle Härtung

Das Dashboard ist ein **einzelnes Python-Skript ohne Fremd-Abhängigkeiten**
(nur Standardbibliothek) — die Angriffsfläche über Dritt-Pakete entfällt damit.
Weitere Maßnahmen sind in der [Architektur-Doku](docs/ARCHITEKTUR.md#sicherheit-in-kürze)
zusammengefasst (u. a. parametrisierte SQL-Zugriffe, BER-kodiertes LDAP ohne
Filter-Injection, strikte Content-Security-Policy, `SameSite=Lax`-Sessions,
Escaping aller Fremddaten, Schutz gegen CSV-/Formel-Injection).

---

## Reporting a vulnerability

Please **do not** open public issues for security problems. Report them
confidentially via **GitHub → Security → “Report a vulnerability”**
([Private Vulnerability Reporting](https://github.com/marcobockelbrink/vmware/security/advisories/new)).
We aim to respond within a few business days.

## Automated security checks

Every push to any branch, every pull request to `main`, plus a weekly run, scan the code with
several independent open-source scanners (CodeQL, Bandit, Semgrep, Trivy,
OpenSSF Scorecard, Dependabot); results appear in the repository’s **Security**
tab. The app is a **single Python script with no third-party dependencies**
(standard library only), which removes the supply-chain attack surface via
external packages.
