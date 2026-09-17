"""Real document generation for Astra multimodal output.

Generates actual PDF, DOCX, XLSX, and PPTX files from content text using
pure Python stdlib (zipfile + XML for Office formats, raw bytes for PDF).
No external libraries required.

This is a tool module — it never calls a provider, never bypasses the Gateway.
The AI generates the content through the normal Provider path; this module
creates the actual file from that content.
"""
from __future__ import annotations

import zipfile
from io import BytesIO


def generate_pdf(content: str, title: str = "Document") -> bytes:
    """Generate a minimal but valid PDF from text content."""
    lines = content.replace('\r\n', '\n').split('\n')

    def _esc(s):
        return s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')

    text_objects = []
    y = 750
    for line in lines:
        if y < 50:
            break
        text_objects.append(f"BT /F1 11 Tf 50 {y} Td ({_esc(line[:120])}) Tj ET")
        y -= 14

    stream = "\n".join(text_objects)
    stream_bytes = stream.encode("latin-1", errors="replace")

    objects = []
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    objects.append(b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                   b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n")
    objects.append(b"4 0 obj\n<< /Length " + str(len(stream_bytes)).encode() +
                   b" >>\nstream\n" + stream_bytes + b"\nendstream\nendobj\n")
    objects.append(b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")

    buf = BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = []
    for obj in objects:
        offsets.append(buf.tell())
        buf.write(obj)

    xref_pos = buf.tell()
    buf.write(b"xref\n")
    buf.write(f"0 {len(objects) + 1}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode())

    buf.write(b"trailer\n")
    buf.write(f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode())
    buf.write(b"startxref\n")
    buf.write(f"{xref_pos}\n".encode())
    buf.write(b"%%EOF\n")

    return buf.getvalue()


def generate_docx(content: str, title: str = "Document") -> bytes:
    """Generate a minimal but valid DOCX from text content."""
    from xml.sax.saxutils import escape
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>')
        zf.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type='
            '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            ' Target="word/document.xml"/>'
            '</Relationships>')
        zf.writestr("word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        paragraphs = []
        for line in content.split('\n'):
            paragraphs.append(
                f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>')
        body = "".join(paragraphs)
        zf.writestr("word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def generate_xlsx(content: str, title: str = "Sheet1") -> bytes:
    """Generate a minimal but valid XLSX from text/CSV content."""
    from xml.sax.saxutils import escape
    rows = []
    for line in content.strip().split('\n'):
        cells = [c.strip() for c in line.split(',')]
        rows.append(cells)

    sheet_rows = []
    for ri, row in enumerate(rows, 1):
        cells_xml = []
        for ci, val in enumerate(row):
            col_letter = chr(65 + min(ci, 25))
            ref = f"{col_letter}{ri}"
            try:
                float(val)
                cells_xml.append(f'<c r="{ref}"><v>{escape(val)}</v></c>')
            except (ValueError, TypeError):
                cells_xml.append(
                    f'<c r="{ref}" t="inlineStr"><is><t>{escape(val)}</t></is></c>')
        sheet_rows.append(f'<row r="{ri}">{"".join(cells_xml)}</row>')

    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>')
        zf.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type='
            '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            ' Target="xl/workbook.xml"/>'
            '</Relationships>')
        zf.writestr("xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type='
            '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"'
            ' Target="worksheets/sheet1.xml"/>'
            '</Relationships>')
        zf.writestr("xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
    return buf.getvalue()


def generate_pptx(content: str, title: str = "Presentation") -> bytes:
    """Generate a minimal but valid PPTX from text content."""
    from xml.sax.saxutils import escape
    slides_content = content.split('\n\n') if '\n\n' in content else [content]

    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        slide_overrides = ""
        slide_rels = ""
        for i in range(len(slides_content)):
            slide_overrides += (
                f'<Override PartName="/ppt/slides/slide{i + 1}.xml" ContentType='
                f'"application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>')
            slide_rels += (
                f'<Relationship Id="rId{i + 1}" Type='
                f'"http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"'
                f' Target="slides/slide{i + 1}.xml"/>')

        zf.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/ppt/presentation.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
            f'{slide_overrides}</Types>')
        zf.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type='
            '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            ' Target="ppt/presentation.xml"/>'
            '</Relationships>')

        slide_list = "".join(
            f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>'
            for i in range(len(slides_content)))
        zf.writestr("ppt/presentation.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<p:sldIdLst>{slide_list}</p:sldIdLst></p:presentation>')
        zf.writestr("ppt/_rels/presentation.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{slide_rels}</Relationships>')

        for i, slide_text in enumerate(slides_content):
            lines = slide_text.strip().split('\n')
            title_text = escape(lines[0] if lines else f"Slide {i + 1}")
            body_lines = [escape(l) for l in lines[1:]] if len(lines) > 1 else []
            body_paras = "".join(
                f'<a:p><a:r><a:rPr lang="en-US" sz="1800"/><a:t>{l}</a:t></a:r></a:p>'
                for l in body_lines) or '<a:p><a:endParaRPr lang="en-US"/></a:p>'

            zf.writestr(f"ppt/slides/slide{i + 1}.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<p:cSld><p:spTree>'
                '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
                '<p:grpSpPr/>'
                '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                '<p:spPr><a:xfrm><a:off x="457200" y="274638"/>'
                '<a:ext cx="8229600" cy="1143000"/></a:xfrm></p:spPr>'
                '<p:txBody><a:bodyPr/><a:p><a:r>'
                f'<a:rPr lang="en-US" sz="2800" b="1"/><a:t>{title_text}</a:t>'
                '</a:r></a:p></p:txBody></p:sp>'
                '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Body"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                '<p:spPr><a:xfrm><a:off x="457200" y="1600200"/>'
                '<a:ext cx="8229600" cy="4525963"/></a:xfrm></p:spPr>'
                f'<p:txBody><a:bodyPr/>{body_paras}</p:txBody></p:sp>'
                '</p:spTree></p:cSld></p:sld>')
            zf.writestr(f"ppt/slides/_rels/slide{i + 1}.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
    return buf.getvalue()
