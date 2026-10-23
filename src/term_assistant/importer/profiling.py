"""Statistisk profilering av kolumner, som underlag för mappningsförslag."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from pydantic import BaseModel

from .workbook import Sheet

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,30}$")
_BOOL = {"ja", "nej", "j", "n", "yes", "no", "true", "false", "sant", "falskt", "x", "1", "0"}


class ColumnProfile(BaseModel):
    header: str
    filled: int
    fill_ratio: float
    unique_ratio: float
    distinct: int
    inferred_type: str           # integer | decimal | boolean | date | code | text
    avg_length: float
    samples: list[str]
    top_values: list[str]        # vanligaste värden (för låg kardinalitet -> select)
    subset_of: str | None = None  # värdena finns i denna kolumn (typiskt förälder -> kod)


def profile_sheet(sheet: Sheet) -> list[ColumnProfile]:
    profiles = [profile_column(h, [r.get(h) for r in sheet.rows]) for h in sheet.headers]
    # Hitta kolumner vars värden är en delmängd av en annan, unik kolumn (hierarki).
    value_sets = {p.header: {str(r.get(p.header)) for r in sheet.rows if r.get(p.header) is not None}
                  for p in profiles}
    unique_cols = [p.header for p in profiles if p.unique_ratio > 0.98 and p.fill_ratio > 0.95]
    for p in profiles:
        if p.header in unique_cols or not value_sets[p.header]:
            continue
        for other in unique_cols:
            if value_sets[p.header] <= value_sets[other]:
                p.subset_of = other
                break
    return profiles


def profile_column(header: str, values: list[Any]) -> ColumnProfile:
    present = [v for v in values if v is not None and str(v).strip() != ""]
    texts = [str(v) for v in present]
    counts = Counter(texts)
    n = len(values) or 1
    return ColumnProfile(
        header=header,
        filled=len(present),
        fill_ratio=round(len(present) / n, 3),
        unique_ratio=round(len(counts) / len(present), 3) if present else 0.0,
        distinct=len(counts),
        inferred_type=_infer_type(present),
        avg_length=round(sum(map(len, texts)) / len(texts), 1) if texts else 0.0,
        samples=list(dict.fromkeys(texts))[:5],
        top_values=[v for v, _ in counts.most_common(8)] if len(counts) <= 12 else [],
    )


def _infer_type(values: list[Any]) -> str:
    if not values:
        return "text"
    def share(pred) -> float:
        return sum(1 for v in values if pred(v)) / len(values)
    if share(lambda v: isinstance(v, bool) or str(v).strip().lower() in _BOOL) > 0.95 \
            and len({str(v).lower() for v in values}) <= 2:
        return "boolean"
    if share(lambda v: isinstance(v, int) and not isinstance(v, bool)) > 0.95:
        return "integer"
    if share(lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)) > 0.95:
        return "decimal"
    if share(lambda v: bool(_DATE.match(str(v)))) > 0.95:
        return "date"
    if share(lambda v: bool(_CODE.match(str(v))) and " " not in str(v)) > 0.95:
        return "code"
    return "text"
