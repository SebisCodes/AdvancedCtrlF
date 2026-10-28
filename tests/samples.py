"""Generates small sample files for every supported format.

Some samples are German on purpose: they cover umlauts, German word forms and
German/English language detection for OCR.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

CT = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def docx(path: Path) -> Path:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
    body = f"""<?xml version="1.0" encoding="UTF-8"?><w:document {w}><w:body>
<w:p><w:r><w:t>Der Arbeitsvertrag regelt die Kündigungsfrist.</w:t></w:r></w:p>
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
<w:p><w:r><w:t>Auf Seite zwei steht die Probezeit von sechs Monaten.</w:t></w:r></w:p>
<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Bild 1" descr="Organigramm der Abteilung"/></wp:inline></w:drawing></w:r></w:p>
<w:p><w:pPr><w:pStyle w:val="Caption"/></w:pPr><w:r><w:t>Abbildung 1: Aufbau der Abteilung</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Betrag</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>100 Euro</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>"""
    rels = f'<?xml version="1.0"?><Relationships xmlns="{RELS_NS}"><Relationship Id="rId1" ' \
           f'Type="{OD}/officeDocument" Target="word/document.xml"/></Relationships>'
    footer = f'<?xml version="1.0"?><w:ftr {w}><w:p><w:r><w:t>Vertraulich Fußzeile</w:t></w:r></w:p></w:ftr>'
    return _zip(path, {"[Content_Types].xml": CT, "_rels/.rels": rels, "word/document.xml": body,
                       "word/footer1.xml": footer})


def pptx(path: Path) -> Path:
    ns = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" ' \
         'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" ' \
         f'xmlns:r="{OD}"'

    def sp(text, x, y, cx, cy, ph=""):
        phx = f'<p:nvPr><p:ph type="{ph}"/></p:nvPr>' if ph else "<p:nvPr/>"
        return (f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="t"/><p:cNvSpPr/>{phx}</p:nvSpPr>'
                f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm></p:spPr>'
                f'<p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>')

    pic = ('<p:pic><p:nvPicPr><p:cNvPr id="5" name="Bild" descr="Diagramm Umsatzentwicklung"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
           '<p:spPr><a:xfrm><a:off x="1000000" y="1000000"/><a:ext cx="4000000" cy="3000000"/></a:xfrm></p:spPr></p:pic>')
    s1 = f'<?xml version="1.0"?><p:sld {ns}><p:cSld><p:spTree>{sp("Quartalsbericht", 0, 0, 9000000, 800000, "title")}' \
         f'{sp("Agenda und Überblick", 0, 1000000, 9000000, 3000000)}</p:spTree></p:cSld></p:sld>'
    s2 = f'<?xml version="1.0"?><p:sld {ns}><p:cSld><p:spTree>{pic}' \
         f'{sp("Abb. 3: Umsatz stieg um 12 Prozent", 1000000, 4100000, 4000000, 400000)}' \
         f'{sp("Fazit: Wachstum im Exportgeschäft", 6000000, 1000000, 3000000, 2000000)}</p:spTree></p:cSld></p:sld>'
    notes = f'<?xml version="1.0"?><p:notes {ns}><p:cSld><p:spTree>{sp("Sprechernotiz: Zahlen vorlesen", 0, 0, 1, 1)}</p:spTree></p:cSld></p:notes>'
    pres = f'<?xml version="1.0"?><p:presentation {ns}><p:sldIdLst><p:sldId id="256" r:id="rId2"/>' \
           f'<p:sldId id="257" r:id="rId3"/></p:sldIdLst></p:presentation>'
    prels = f'<?xml version="1.0"?><Relationships xmlns="{RELS_NS}">' \
            f'<Relationship Id="rId2" Type="{OD}/slide" Target="slides/slide1.xml"/>' \
            f'<Relationship Id="rId3" Type="{OD}/slide" Target="slides/slide2.xml"/></Relationships>'
    s1rels = f'<?xml version="1.0"?><Relationships xmlns="{RELS_NS}">' \
             f'<Relationship Id="rId9" Type="{OD}/notesSlide" Target="../notesSlides/notesSlide1.xml"/></Relationships>'
    rels = f'<?xml version="1.0"?><Relationships xmlns="{RELS_NS}"><Relationship Id="rId1" ' \
           f'Type="{OD}/officeDocument" Target="ppt/presentation.xml"/></Relationships>'
    return _zip(path, {"[Content_Types].xml": CT, "_rels/.rels": rels, "ppt/presentation.xml": pres,
                       "ppt/_rels/presentation.xml.rels": prels, "ppt/slides/slide1.xml": s1,
                       "ppt/slides/slide2.xml": s2, "ppt/slides/_rels/slide1.xml.rels": s1rels,
                       "ppt/notesSlides/notesSlide1.xml": notes})


def odt(path: Path) -> Path:
    content = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0">
<office:body><office:text>
<text:h>Protokoll der Sitzung</text:h>
<text:p>Beschluss<text:s/>über das Budget für Weiterbildung.</text:p>
<text:p><draw:frame><svg:title>Foto Teamevent</svg:title><svg:desc>Gruppenbild am See</svg:desc></draw:frame></text:p>
</office:text></office:body></office:document-content>"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", content)
    return path


def xlsx(path: Path) -> Path:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Tabelle"
    ws.append(["Name", "Ort", "Umsatz"])
    ws.append(["Bäckerei Nord", "Stadt A", 12000])
    ws.append(["Werkstatt Süd", "Stadt B", 8000])
    wb.save(path)
    return path


def pdf(path: Path, scanned: bool = True) -> Path:
    import pymupdf
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Die Kündigungsfrist beträgt drei Monate zum Quartalsende.", fontsize=11)
    p = doc.new_page()
    p.insert_text((72, 72), "Second page: the warranty period is twenty four months.", fontsize=11)
    if scanned:
        for txt in ("Die Rechnung wurde am Montag bezahlt und die Lieferung ist vollständig.",
                    "The invoice was paid on Monday and the delivery is complete."):
            tmp = pymupdf.open()
            tp = tmp.new_page(width=600, height=200)
            tp.insert_text((20, 100), txt, fontsize=14)
            pix = tp.get_pixmap(dpi=200)
            p = doc.new_page(width=600, height=200)
            p.insert_image(p.rect, stream=pix.tobytes("png"))
    doc.save(path)
    return path


def make_all(d: Path) -> dict[str, Path]:
    d.mkdir(parents=True, exist_ok=True)
    out = {
        "docx": docx(d / "vertrag.docx"),
        "pptx": pptx(d / "bericht.pptx"),
        "odt": odt(d / "protokoll.odt"),
        "xlsx": xlsx(d / "kunden.xlsx"),
        "pdf": pdf(d / "scan.pdf"),
    }
    (d / "liste.csv").write_text("Artikel;Preis;Lager\nSchraube;0,10;500\nDübel;0,05;1200\n", encoding="utf-8")
    # Sloppy but valid HTML: no </head>, no </p>, no <body> - must still be indexed
    (d / "sloppy.html").write_text("<!DOCTYPE html><html><head><meta charset=utf-8><title>Opening hours</title>\n"
                                   "<p>We are open on weekends\n<p>Closed on public holidays\n"
                                   "<table><tr><td>Mon<td>9-17</table>", encoding="utf-8")
    # Page whose content is built by JavaScript (only an empty container in the static HTML)
    (d / "app.html").write_text(
        "<title>Listing editor</title><div id='doc'></div>\n<script>\n"
        "const data = {headline: 'Listing headline text', text: `Description generated by a script`};\n"
        "function render(){ return '<div class=\"pin\" style=\"left:' + x + '%\" onclick=\"pinRemove(1)\">' +\n"
        "  '<h2>Floor plan</h2><button onclick=\"del()\">Remove page</button>'; }\n"
        "const cls = 'btnSave'; // don't index identifiers\n</script>", encoding="utf-8")
    (d / "seite.html").write_text("<html><head><title>Startseite</title><script>var x='geheim';</script></head>"
                                  "<body>\n<h1>Willkommen</h1>\n<p>Öffnungszeiten am Wochenende</p>\n"
                                  "<img src='a.png' alt='Logo oben links'>\n</body></html>", encoding="utf-8")
    (d / "daten.xml").write_text('<?xml version="1.0"?>\n<root>\n  <eintrag name="Konfiguration">\n'
                                 '    <wert>Zeitüberschreitung nach 30 Sekunden</wert>\n  </eintrag>\n</root>', encoding="utf-8")
    (d / "bomb.xml").write_text('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><r>&a;</r>', encoding="utf-8")
    (d / "notiz.txt").write_text("Zeile eins\nZeile zwei\n\nHier steht das Passwortrichtlinien-Dokument.\n", encoding="cp1252")
    (d / "brief.rtf").write_text(r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\f0 Sehr geehrte Damen und Herren,\par "
                                 r"die Lieferung erfolgt am Freitag. Gr\'fc\'dfe\par}", encoding="ascii")
    (d / "mail.eml").write_text("Subject: Angebot Migration\nFrom: sender@example.org\nTo: recipient@example.org\n"
                                "Content-Type: text/plain; charset=utf-8\n\nAnbei das Angebot für die Migration.\n",
                                encoding="utf-8")
    with zipfile.ZipFile(d / "archiv.zip", "w") as z:
        z.writestr("unterordner/innen.txt", "Inhalt aus dem Archiv: Archivinhalt Testtext")
        z.writestr("../evil.txt", "path traversal attempt")
    return out
