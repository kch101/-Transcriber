"""Экспорт расшифровки в TXT, DOCX и PDF.

payload = {
  "title": str,
  "timestamps": bool,
  "speakers": {"SPEAKER_00": "Анна", ...},
  "blocks": [{"speaker", "start", "text"}, ...],
}
"""
import io
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

FORMATS = {"txt": "text/plain; charset=utf-8",
           "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           "pdf": "application/pdf"}


def fmt_time(sec) -> str:
    if sec is None:
        return ""
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _rows(p):
    names = p.get("speakers") or {}
    for b in p.get("blocks", []):
        text = (b.get("text") or "").strip()
        if not text:
            continue
        name = names.get(b.get("speaker")) or b.get("speaker") or "Спикер"
        ts = fmt_time(b.get("start")) if p.get("timestamps", True) else ""
        yield name, ts, text


def _single_speaker(p) -> bool:
    return len({b.get("speaker") for b in p.get("blocks", [])}) <= 1


def to_txt(p) -> bytes:
    out = [p.get("title") or "Расшифровка", ""]
    single = _single_speaker(p)
    for name, ts, text in _rows(p):
        head = " · ".join(x for x in ([] if single else [name]) + ([ts] if ts else []) if x)
        if head:
            out.append(f"[{head}]")
        out.append(text)
        out.append("")
    return "\n".join(out).encode("utf-8")


def to_docx(p) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Arial"
    st.font.size = Pt(11)
    doc.add_heading(p.get("title") or "Расшифровка", level=1)
    meta = doc.add_paragraph(datetime.now().strftime("%d.%m.%Y"))
    meta.runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    single = _single_speaker(p)
    for name, ts, text in _rows(p):
        if not single or ts:
            head = doc.add_paragraph()
            head.paragraph_format.space_before = Pt(10)
            head.paragraph_format.space_after = Pt(2)
            head.paragraph_format.keep_with_next = True
            if not single:
                r = head.add_run(name)
                r.bold = True
                r.font.color.rgb = RGBColor(0xB0, 0x5A, 0x10)
            if ts:
                r = head.add_run(("   " if not single else "") + ts)
                r.font.size = Pt(9)
                r.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
        para = doc.add_paragraph(text)
        para.paragraph_format.space_after = Pt(4)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


_FONT_CANDIDATES = [
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
]
_fonts_ready = False


def _register_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for reg, bold in _FONT_CANDIDATES:
        if Path(reg).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont("TxtRegular", reg))
            pdfmetrics.registerFont(TTFont("TxtBold", bold))
            _fonts_ready = True
            return
    raise RuntimeError("Не найден шрифт с кириллицей для PDF (Arial)")


def to_pdf(p) -> bytes:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    _register_fonts()
    title = p.get("title") or "Расшифровка"
    s_title = ParagraphStyle("t", fontName="TxtBold", fontSize=18, leading=22, spaceAfter=2)
    s_meta = ParagraphStyle("m", fontName="TxtRegular", fontSize=9, textColor=HexColor("#888888"), spaceAfter=10)
    s_head = ParagraphStyle("h", fontName="TxtBold", fontSize=10.5, leading=14, spaceBefore=9, spaceAfter=1,
                            textColor=HexColor("#B05A10"), keepWithNext=True)
    s_body = ParagraphStyle("b", fontName="TxtRegular", fontSize=10.5, leading=15, spaceAfter=3)

    story = [Paragraph(escape(title), s_title), Paragraph(datetime.now().strftime("%d.%m.%Y"), s_meta)]
    single = _single_speaker(p)
    for name, ts, text in _rows(p):
        parts = []
        if not single:
            parts.append(escape(name))
        if ts:
            parts.append(f'<font name="TxtRegular" size="8.5" color="#999999">{ts}</font>')
        if parts:
            story.append(Paragraph("&nbsp;&nbsp;&nbsp;".join(parts), s_head))
        story.append(Paragraph(escape(text), s_body))
    if len(story) == 2:
        story.append(Spacer(1, 1))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm, title=title, author="Расшифровка")
    doc.build(story)
    return buf.getvalue()


def render(fmt: str, payload: dict) -> bytes:
    return {"txt": to_txt, "docx": to_docx, "pdf": to_pdf}[fmt](payload)
