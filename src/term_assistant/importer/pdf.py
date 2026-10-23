"""Läsning av PDF-utskrifter av kalkylark.

Kolumner återskapas från ordens x-positioner. En rubrikrad (fetstil, eller
rubriklika ord som "Kod"/"Benämning") startar en ny tabell. Det motsvarar en
ny flik i det ursprungliga kalkylarket. Sidor utan egen rubrik fortsätter
föregående tabell. Sidor som saknar rubrik helt, och inte kan fortsätta någon
tabell, blir en egen rubriklös lista ("Sida N").

PDF är ett nödläge. Kolumngränserna gissas, så bedömningen tipsar alltid om
att hellre lämna Excel eller CSV.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .registry import normalize
from .workbook import Sheet, parse_rows

_HEADER_WORDS = {"kod", "code", "benamning", "namn", "term", "klartext", "grupp", "kategori", "analys",
                 "kortnamn", "trivialnamn", "beskrivning", "definition", "id", "display", "status"}


@dataclass
class _Cell:
    x0: float
    x1: float
    text: str
    bold: bool


def _lines(page) -> list[list[_Cell]]:
    words = page.extract_words(extra_attrs=["fontname", "size"])
    words.sort(key=lambda w: (round(w["top"]), w["x0"]))
    lines: list[list[dict]] = []
    for w in words:
        if lines and abs(lines[-1][0]["top"] - w["top"]) < 2.5:
            lines[-1].append(w)
        else:
            lines.append([w])
    out = []
    for line in lines:
        line.sort(key=lambda w: w["x0"])
        cells: list[_Cell] = []
        for w in line:
            gap = w["x0"] - cells[-1].x1 if cells else None
            if cells and gap is not None and gap < w["size"] * 0.6:
                cells[-1].text += " " + w["text"]
                cells[-1].x1 = w["x1"]
                cells[-1].bold = cells[-1].bold and "Bold" in w["fontname"]
            else:
                cells.append(_Cell(w["x0"], w["x1"], w["text"], "Bold" in w["fontname"]))
        out.append(cells)
    return out


def _is_header(cells: list[_Cell]) -> bool:
    if len(cells) < 2:
        return False
    if all(c.bold for c in cells):
        return True
    words = {normalize(t) for c in cells for t in c.text.replace("/", " ").split()}
    return len(words & _HEADER_WORDS) >= 1 and all(len(c.text) <= 30 for c in cells)


def _assign(cells: list[_Cell], columns: list[float]) -> tuple:
    row: list[str | None] = [None] * len(columns)
    for c in cells:
        idx = max((i for i, x in enumerate(columns) if x <= c.x0 + 3), default=0)
        row[idx] = c.text if row[idx] is None else f"{row[idx]} {c.text}"
    return tuple(row)


def read_pdf(path: str | Path, max_rows: int | None = None) -> list[Sheet]:
    import pdfplumber

    tables: list[dict] = []  # {"name", "columns", "raw"}
    current: dict | None = None
    with pdfplumber.open(str(path)) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            lines = _lines(page)
            page_started = False
            for i, cells in enumerate(lines):
                if _is_header(cells):
                    # En ensam rad direkt före rubriken är tabellens titel ("Nomenklatur Kemi").
                    prev = lines[i - 1] if i > 0 else None
                    title = prev[0].text if prev and len(prev) == 1 else f"Tabell {len(tables) + 1}"
                    current = {"name": title, "columns": [c.x0 for c in cells],
                               "raw": [tuple(c.text for c in cells)], "page": pno}
                    tables.append(current)
                    page_started = True
                    continue
                nxt = lines[i + 1] if i + 1 < len(lines) else None
                if len(cells) == 1 and nxt is not None and _is_header(nxt):
                    continue  # titelrad, hanteras av rubriken ovan
                if current is None or (not page_started and current.get("headerless")):
                    # Rubriklös sida: egen lista per sida.
                    current = {"name": f"Sida {pno}", "columns": [cells[0].x0], "raw": [],
                               "page": pno, "headerless": True}
                    tables.append(current)
                page_started = True
                current["raw"].append(_assign(cells, current["columns"]))

    sheets = []
    for t in tables:
        sheet = parse_rows(t["name"], t["raw"], max_rows) if t["raw"] else None
        if sheet is not None:
            sheet.source_format = "pdf"
            sheets.append(sheet)
    return sheets
