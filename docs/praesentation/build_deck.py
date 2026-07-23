#!/usr/bin/env python3
# Baut die Management-Praesentation (7 Folien, 16:9) fuer das
# VMware-Kapazitaets-Dashboard. Benoetigt python-pptx (Build-Werkzeug, NICHT
# Teil des Produkts). Screenshots werden aus SHOTS eingebettet.
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn
from PIL import Image
import sys, os

SHOTS = sys.argv[1] if len(sys.argv) > 1 else "screenshots"
OUT   = sys.argv[2] if len(sys.argv) > 2 else "VMware-Kapazitaetsplanung.pptx"

# --- Farbpalette (an das Produkt angelehnt) ---
BG     = RGBColor(0x0B, 0x12, 0x20)   # dunkler Hintergrund
CARD   = RGBColor(0x15, 0x1E, 0x33)   # Panel
LINE   = RGBColor(0x2A, 0x36, 0x52)   # Rahmen
WHITE  = RGBColor(0xF2, 0xF5, 0xFA)
MUTED  = RGBColor(0x93, 0xA0, 0xB5)
ACCENT = RGBColor(0x7C, 0x83, 0xFF)   # Indigo (wie die Buttons)
GREEN  = RGBColor(0x34, 0xD3, 0x99)
FONT   = "Segoe UI"

EMU_IN = 914400
prs = Presentation()
prs.slide_width  = Inches(13.333)
prs.slide_height = Inches(7.5)
SW, SH = prs.slide_width, prs.slide_height
BLANK = prs.slide_layouts[6]


def slide(bg=BG):
    s = prs.slides.add_slide(BLANK)
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = bg
    return s


def rect(s, x, y, w, h, fill=None, line=None, line_w=1.0, shape=MSO_SHAPE.RECTANGLE):
    sp = s.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if fill is None:
        sp.fill.background()
    else:
        sp.fill.solid(); sp.fill.fore_color.rgb = fill
    if line is None:
        sp.line.fill.background()
    else:
        sp.line.color.rgb = line; sp.line.width = Pt(line_w)
    sp.shadow.inherit = False
    return sp


def text(s, x, y, w, h, runs, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
         space_after=6, line_spacing=1.0):
    """runs: Liste von Absaetzen; jeder Absatz = Liste von (txt, size, color, bold)."""
    tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame; tf.word_wrap = True; tf.vertical_anchor = anchor
    tf.margin_left = 0; tf.margin_right = 0; tf.margin_top = 0; tf.margin_bottom = 0
    for i, para in enumerate(runs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align; p.space_after = Pt(space_after); p.space_before = Pt(0)
        p.line_spacing = line_spacing
        for (txt, size, color, bold) in para:
            r = p.add_run(); r.text = txt
            r.font.size = Pt(size); r.font.bold = bold
            r.font.color.rgb = color; r.font.name = FONT
    return tb


def picture(s, path, box_x, box_y, box_w, box_h, border=LINE):
    """Bild proportional in eine Box einpassen und zentrieren, mit Rahmen."""
    iw, ih = Image.open(path).size
    scale = min(box_w * EMU_IN / iw, box_h * EMU_IN / ih)
    w = int(iw * scale); h = int(ih * scale)
    x = int(box_x * EMU_IN + (box_w * EMU_IN - w) / 2)
    y = int(box_y * EMU_IN + (box_h * EMU_IN - h) / 2)
    pic = s.shapes.add_picture(path, Emu(x), Emu(y), width=Emu(w), height=Emu(h))
    pic.line.color.rgb = border; pic.line.width = Pt(1.25)
    return pic


def footer(s, n):
    rect(s, 0, 7.36, 13.333, 0.14, fill=ACCENT)
    text(s, 11.7, 7.02, 1.5, 0.3,
         [[("%d / 7" % n, 10, MUTED, False)]], align=PP_ALIGN.RIGHT)
    text(s, 0.55, 7.02, 8, 0.3,
         [[("VMware Kapazitätsplanung", 10, MUTED, False)]])


def bullets(items):
    out = []
    for it in items:
        out.append([("▸  ", 14, ACCENT, True), (it, 14, WHITE, False)])
    return out


def highlight(n, kicker, title, sub, items, img, page):
    s = slide()
    # linke Textspalte
    text(s, 0.6, 0.85, 5.2, 0.4, [[(kicker, 12.5, ACCENT, True)]])
    text(s, 0.6, 1.28, 4.9, 1.7, [[(title, 30, WHITE, True)]], line_spacing=1.02)
    text(s, 0.6, 2.9, 4.9, 0.9, [[(sub, 14.5, MUTED, False)]], line_spacing=1.1)
    text(s, 0.6, 3.95, 4.9, 3.0, bullets(items), space_after=11, line_spacing=1.05)
    # rechte Bildspalte auf einer Karte
    rect(s, 5.5, 0.9, 7.35, 5.75, fill=CARD, line=LINE, line_w=1.0,
         shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    picture(s, os.path.join(SHOTS, img), 5.62, 1.02, 7.11, 5.51)
    footer(s, page)
    return s


# ---------------- Folie 1: Titel ----------------
s = slide()
rect(s, 0, 0, 0.28, 7.5, fill=ACCENT)
text(s, 0.9, 1.5, 11.5, 0.5, [[("KAPAZITÄTS-DASHBOARD FÜR VMWARE ARIA OPERATIONS",
                                14, ACCENT, True)]])
text(s, 0.86, 2.15, 11.6, 2.2,
     [[("Kapazität sehen.", 52, WHITE, True)],
      [("Planen. Freigeben.", 52, WHITE, True)]], line_spacing=1.0)
text(s, 0.9, 4.55, 10.8, 1.0,
     [[("Die gesamte virtuelle Landschaft auf einen Blick — in einem einzigen, "
        "abhängigkeitsfreien Werkzeug.", 19, MUTED, False)]], line_spacing=1.15)
rect(s, 0.92, 5.75, 6.4, 0.02, fill=LINE)
text(s, 0.9, 5.95, 11.8, 0.8,
     [[("Eine Python-Datei · keine Fremd-Abhängigkeiten · zweisprachig DE/EN · "
        "läuft auf jedem RHEL-Host · über 100 Releases", 13.5, MUTED, False)]],
     line_spacing=1.15)
footer(s, 1)

# ---------------- Folien 2–6: Top-5-Highlights ----------------
highlight(2, "HIGHLIGHT 1 · ÜBERBLICK",
          "Freie Kapazität auf einen Blick",
          "Sofort erkennen, wo Reserven sind — und wo es eng wird.",
          ["Freie vCPU, RAM & Storage je Cluster, farbcodiert nach Auslastung",
           "Ausfallreserve (N+1) und vSAN-Faktor bereits eingerechnet — keine Excel-Bastelei",
           "Ein Klick zum CSV-Export für Reports ans Management"],
          "01_kapazitaet.png", 2)

highlight(3, "HIGHLIGHT 2 · TIEFE",
          "Vom Cluster bis zur VM — ein Klick",
          "Volle Detailtiefe, ganz ohne vCenter-Login.",
          ["CPU, RAM, Storage, Netzwerk, Hosts & VMs je Cluster in einer Karte",
           "Tanzu-/Kubernetes-Reservierungen zählen automatisch mit",
           "Alle Zahlen mit derselben, nachvollziehbaren Rechenlogik"],
          "02_detail.png", 3)

highlight(4, "HIGHLIGHT 3 · GOVERNANCE",
          "Ressourcen mit klarem Freigabe-Prozess",
          "Nichts wird unbemerkt vergeben — alles ist nachvollziehbar.",
          ["Mehrstufiger Genehmigungs-Workflow je Team, inkl. Auto-Freigabe nach Schwellen",
           "Warnung, wenn eine Anfrage nicht mehr in den Cluster passt",
           "Lückenloses Audit-Log und automatische Mail-Benachrichtigungen"],
          "03_genehmigung.png", 4)

highlight(5, "HIGHLIGHT 4 · PROGNOSE",
          "Wohin wächst die Umgebung?",
          "Investitionsentscheidungen datenbasiert statt aus dem Bauch.",
          ["Trends aus bis zu 2 Jahren Historie: RAM/vCPU/Disk je VM, Auslastung, VM-Wachstum",
           "Frühzeitig erkennen, wann Erweiterungen fällig werden",
           "Selbst gezeichnete Charts — komplett offline, kein externer Dienst"],
          "05_statistik.png", 5)

highlight(6, "HIGHLIGHT 5 · REICHWEITE",
          "Alle Rechenzentren — auch die isolierten",
          "Eine Sicht für die ganze Landschaft, rollengerecht abgesichert.",
          ["Mehrere vROps-Quellen in einer Übersicht (Quellen-Badge je Cluster)",
           "Isolierte vCenter ohne Netz per Offline-Import (PowerCLI) einbinden",
           "Storage-Brücke: LUN-Erweiterungen direkt ans Storage-Team (API/CSV)"],
          "06_storage.png", 6)

# ---------------- Folie 7: Abschluss / Nutzenargumente ----------------
s = slide()
text(s, 0.6, 0.7, 12, 0.4, [[("FAZIT", 13, ACCENT, True)]])
text(s, 0.6, 1.12, 12, 1.0, [[("Warum das überzeugt", 34, WHITE, True)]])

cols = [
    ("Betrieb & Kosten", [
        "Eine einzige Python-Datei — kein Build, keine Datenbank, keine Fremd-Pakete",
        "Läuft auf jedem RHEL-Host; ein Update = eine Datei austauschen"]),
    ("Sicherheit & Compliance", [
        "AD-Anmeldung, rollenbasierte Sichtbarkeit, quellenbezogene Filter",
        "5 unabhängige Security-Scanner in der CI, geschützter main-Branch"]),
    ("Reichweite", [
        "Zweisprachig DE/EN, offline- und proxy-fähig",
        "Lesende API für Grafana, CMDB & Co."]),
    ("Pflege & Reife", [
        "Aktiv weiterentwickelt — über 100 Releases",
        "Reviewer-Handbuch, Architektur-Doku, Smoke-Tests inklusive"]),
]
positions = [(0.6, 2.35), (6.9, 2.35), (0.6, 4.55), (6.9, 4.55)]
for (title, items), (x, y) in zip(cols, positions):
    rect(s, x, y, 5.85, 1.95, fill=CARD, line=LINE, line_w=1.0,
         shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    text(s, x + 0.32, y + 0.22, 5.3, 0.4, [[(title, 16, ACCENT, True)]])
    text(s, x + 0.32, y + 0.72, 5.25, 1.1, bullets(items),
         space_after=7, line_spacing=1.03)

text(s, 0.6, 6.72, 12, 0.5,
     [[("Wenig Angriffsfläche. Kein Vendor-Lock-in. Sofort einsatzbereit.",
        16, GREEN, True)]])
footer(s, 7)

prs.save(OUT)
print("gespeichert:", OUT, "-", len(prs.slides._sldIdLst), "Folien")
