"""Bounded OOXML structure extraction; no macros, links, or embedded files execute."""
from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
LIMIT = 64 * 1024 * 1024


def extract_docx(path):
    with zipfile.ZipFile(path) as package:
        info = package.getinfo("word/document.xml")
        if info.file_size > LIMIT:
            raise ValueError("DOCX XML exceeds the extraction limit.")
        xml = package.read(info)
        declarations = xml.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
            raise ValueError("DOCX XML entity declarations are unsupported.")
        root = ElementTree.fromstring(xml)
        names = set(package.namelist())

    def text(element):
        if element.tag in {W + "del", W + "moveFrom"}:
            return ""
        if element.tag == W + "t":
            return element.text or ""
        if element.tag == W + "tab":
            return "\t"
        if element.tag in {W + "br", W + "cr"}:
            return "\n"
        return "".join(text(child) for child in element)

    def blocks(element):
        for child in element:
            if child.tag in {W + "p", W + "tbl"}:
                yield child
            elif child.tag not in {W + "del", W + "moveFrom", W + "sectPr"}:
                yield from blocks(child)

    sections, parts, structure = [], [], []
    title, level = "Document body", None
    position = 0

    def flush():
        nonlocal parts, structure, position
        if parts:
            sections.append({"page": None, "section": title, "text": "\n".join(parts),
                             "heading_level": level, "blocks": structure})
        parts, structure, position = [], [], 0

    body = root.find(W + "body")
    for block in blocks(body if body is not None else root):
        if block.tag == W + "p":
            value = text(block)
            style = block.find(f"{W}pPr/{W}pStyle")
            outline = block.find(f"{W}pPr/{W}outlineLvl")
            heading = re.fullmatch(r"heading\s*([1-6])", style.get(W + "val", ""), re.I) if style is not None else None
            heading_level = int(heading.group(1)) if heading else None
            if outline is not None and outline.get(W + "val", "").isdigit():
                heading_level = min(6, int(outline.get(W + "val")) + 1)
            if heading_level and value.strip():
                flush()
                title, level = value.strip(), heading_level
            kind = "heading" if heading_level else "paragraph"
        else:
            rows = ["\t".join("\n".join(text(p) for p in cell.findall(W + "p"))
                               for cell in row.findall(W + "tc")) for row in block.findall(W + "tr")]
            value, kind = "\n".join(rows), "table"
        start = position
        position += len(value) + 1
        parts.append(value)
        structure.append({"kind": kind, "start": start, "end": start + len(value)})
    flush()
    warnings = ["DOCX headings, paragraphs and table rows preserve document order; page numbers are unavailable. Import a PDF for page citations."]
    if any(name.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes", "word/media/")) for name in names):
        warnings.append("DOCX contains images, headers, footers, or notes not extracted; inspect the original for additional evidence.")
    return sections, warnings, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
