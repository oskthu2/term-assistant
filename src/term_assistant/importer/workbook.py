"""Läsning av Excel-/CSV-filer: blad, rubrikrad och stickprov av rader."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


@dataclass
class Sheet:
    name: str
    header_row: int                     # 1-baserat radnummer för rubrikraden
    headers: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)
    total_rows: int = 0
    preamble: list[str] = field(default_factory=list)  # titelrader ovanför rubriken

    def sample(self, n: int = 10) -> list[dict[str, Any]]:
        return self.rows[:n]


def read_workbook(path: str | Path, sheet: str | None = None, max_rows: int | None = None) -> list[Sheet]:
    """Läs alla (eller ett namngivet) blad. ``max_rows`` begränsar antalet datarader."""
    path = Path(path)
    if path.suffix.lower() in (".csv", ".tsv", ".txt"):
        return [_read_csv(path, max_rows)]
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        names = [sheet] if sheet else wb.sheetnames
        return [s for s in (_read_sheet(name, list(wb[name].iter_rows(values_only=True)), max_rows)
                            for name in names) if s is not None]
    finally:
        wb.close()


def _read_csv(path: Path, max_rows: int | None) -> Sheet:
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    rows = [tuple(r) for r in csv.reader(text.splitlines(), dialect)]
    return _read_sheet(path.stem, rows, max_rows) or Sheet(path.stem, 1, [])


def _read_sheet(name: str, raw: list[tuple], max_rows: int | None) -> Sheet | None:
    raw = [r for r in raw]
    if not any(any(_clean(v) is not None for v in r) for r in raw):
        return None
    header_idx = detect_header_row(raw)
    header_cells = raw[header_idx]
    headers = _unique_headers(header_cells)
    preamble = [" ".join(str(v) for v in r if _clean(v) is not None) for r in raw[:header_idx]]
    rows = []
    for r in raw[header_idx + 1:]:
        values = [_clean(v) for v in r]
        if all(v is None for v in values):
            continue
        rows.append({h: (values[i] if i < len(values) else None) for i, h in enumerate(headers) if h})
    total = len(rows)
    if max_rows is not None:
        rows = rows[:max_rows]
    return Sheet(name=name, header_row=header_idx + 1, headers=[h for h in headers if h], rows=rows,
                 total_rows=total, preamble=[p for p in preamble if p])


def detect_header_row(raw: list[tuple], scan: int = 15) -> int:
    """Gissa rubrikraden: den första raden bland de översta där de flesta celler
    är korta texter och som har minst lika många ifyllda celler som raden under."""
    best_idx, best_score = 0, -1.0
    width = max((len(r) for r in raw[:scan]), default=0)
    for i, row in enumerate(raw[:scan]):
        cells = [_clean(v) for v in row]
        filled = [c for c in cells if c is not None]
        if len(filled) < 2:
            continue
        texts = [c for c in filled if isinstance(c, str) and len(c) <= 60]
        below = [_clean(v) for v in raw[i + 1]] if i + 1 < len(raw) else []
        below_filled = sum(c is not None for c in below)
        score = len(texts) / max(width, 1) + (0.3 if below_filled >= len(filled) * 0.5 else 0)
        score -= 0.02 * i  # föredra tidiga rader vid lika
        if len(set(texts)) < len(texts):
            score -= 0.2   # rubriker är oftast unika
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx


def _unique_headers(cells: tuple) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, cell in enumerate(cells):
        h = _clean(cell)
        h = str(h) if h is not None else ""
        if not h:
            out.append("")
            continue
        if h in seen:
            seen[h] += 1
            h = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
        out.append(h)
    return out


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
