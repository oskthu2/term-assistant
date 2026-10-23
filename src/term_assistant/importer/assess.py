"""Bedömning av en hel fil innan inläsning – "kvalitetsgrinden".

Varje blad klassas som en av:

=================  ============================================================
``codesystem``     eget kodverk (en kod per rad, egna koder)
``subset``         urval ur ett befintligt kodsystem (NPU, SNOMED CT …) -> ValueSet
``reference``      stor referenslista ur ett befintligt kodsystem, som andra blad
                   är urval ur (används för kontroll, publiceras inte)
``text_catalogue`` texter som nycklas på kodverk + kod i andra blad
``documentation``  inledning, revisionshistorik, regler (läses för metadata)
``helper``         hjälplistor, t.ex. Ja/Nej för datavalidering
``unusable``       uppfyller inte minimikraven -> avvisas med tips
=================  ============================================================

Bedömningen ger dessutom:

* **familjer** – blad med samma struktur som delar en mall
* **relationer** – blad vars koder är ett urval av ett annat blads koder,
  med koder som saknas i det större bladet (ofta skrivfel)
* **katalog** – metadata per kodverk ur dokumentationsblad (OID, ägare, titel)
* **historik** och **regler** (t.ex. "koder får inte tas bort")
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .external import ExternalDetection, detect
from .mapping import ReleasePolicy
from .profiling import ColumnProfile, profile_sheet
from .registry import normalize
from .suggest import ROLE_ALIASES, ColumnRole, _role_alias_score, sheet_source
from .workbook import Sheet, read_workbook

MINIMUM_REQUIREMENTS = [
    "En rad per begrepp.",
    "En kod per rad, i en egen kolumn (t.ex. 'Kod'). Koden ska vara unik inom kodverket och stabil över tid.",
    "En benämning (klartext) per kod, i en egen kolumn.",
    "En rubrikrad som namnger kolumnerna.",
    "Ett kodverk per flik, med kodverkets namn i fliknamnet eller på en rad ovanför tabellen "
    "(t.ex. 'Kodverk: KV_X').",
]

_DOC_NAMES = ("inledning", "om ", "om kodverk", "info", "instruktion", "lasanvisning", "readme", "revision",
              "historik", "forsattsblad", "beskrivning", "anvisning")
_HELPER_NAMES = ("datablad", "data", "lista", "listor", "lookup", "validering", "hjalp", "val")
_HELPER_VALUES = {"ja", "nej", "x", "true", "false", "sant", "falskt", "1", "0"}
_MISSING = {"ej faststallt", "saknas", "-", "tbd", "n a", "na", "ej angivet", "ej klart"}
_OWNER_LABELS = {"dokumentagare", "agare", "forvaltare", "utgivare", "publisher", "ansvarig"}


class SheetKind(str, Enum):
    CODESYSTEM = "codesystem"
    SUBSET = "subset"
    REFERENCE = "reference"
    TEXT_CATALOGUE = "text_catalogue"
    DOCUMENTATION = "documentation"
    HELPER = "helper"
    UNUSABLE = "unusable"


class Verdict(str, Enum):
    ACCEPT = "godkänd"
    WARN = "godkänd med anmärkningar"
    SUPPORT = "stöddata"
    REJECT = "avvisad"


class SheetAssessment(BaseModel):
    sheet: str
    kind: SheetKind
    verdict: Verdict
    rows: int
    code_column: str | None = None
    display_column: str | None = None
    reasons: list[str] = []
    warnings: list[str] = []
    tips: list[str] = []
    external: ExternalDetection | None = None
    family: str | None = None


class Relation(BaseModel):
    subset: str
    superset: str
    overlap: float
    missing: list[str] = []
    hints: list[str] = []


class CatalogueEntry(BaseModel):
    name: str
    identifier: str | None = None
    publisher: str | None = None
    title: str | None = None
    raw: dict[str, Any] = {}


class TextCatalogueCandidate(BaseModel):
    sheet: str
    key_column: str
    text_columns: list[str]
    flag_columns: list[str] = []
    targets: list[str]


class WorkbookAssessment(BaseModel):
    file: str
    format: str
    verdict: Verdict
    sheets: list[SheetAssessment]
    families: dict[str, list[str]] = {}
    relations: list[Relation] = []
    catalogue: dict[str, CatalogueEntry] = {}
    history: list[dict[str, Any]] = []
    publisher: str | None = None
    policy: ReleasePolicy = ReleasePolicy()
    documentation: dict[str, str] = {}
    text_catalogues: list[TextCatalogueCandidate] = []
    tips: list[str] = []

    def sheet(self, name: str) -> SheetAssessment | None:
        return next((s for s in self.sheets if s.sheet == name), None)


# -- klassning av ett blad ------------------------------------------------------


def _is_code_like(p: ColumnProfile, external: ExternalDetection | None) -> bool:
    if external is not None:
        return True
    if p.inferred_type in ("code", "integer"):
        return True
    return _role_alias_score(p.header, ColumnRole.CODE) > 0 and p.avg_length <= 40


def _code_column(profiles: list[ColumnProfile], sheet: Sheet) -> tuple[ColumnProfile | None, ExternalDetection | None]:
    best, best_ext, best_key = None, None, None
    for p in profiles:
        if p.fill_ratio < 0.9 or p.unique_ratio < 0.85:
            continue
        ext = detect(sheet.column(p.header))
        if not _is_code_like(p, ext):
            continue
        key = (_role_alias_score(p.header, ColumnRole.CODE) > 0, ext is not None, p.unique_ratio, -p.avg_length)
        if best_key is None or key > best_key:
            best, best_ext, best_key = p, ext, key
    return best, best_ext


def _display_column(profiles: list[ColumnProfile], code: ColumnProfile) -> ColumnProfile | None:
    texts = [p for p in profiles if p.header != code.header and p.inferred_type not in ("marker", "boolean")
             and p.fill_ratio > 0.5]
    return max(texts, key=lambda p: (_role_alias_score(p.header, ColumnRole.DISPLAY), p.inferred_type == "text",
                                     p.fill_ratio), default=None)


def _prose_ratio(sheet: Sheet) -> float:
    cells = [str(v) for r in sheet.raw for v in r if v is not None]
    if not cells:
        return 0.0
    return sum(len(c) for c in cells if len(c) > 120) / max(1, sum(len(c) for c in cells))


def assess_sheet(sheet: Sheet, codesystem_names: set[str]) -> SheetAssessment:
    name = normalize(sheet.name)
    a = SheetAssessment(sheet=sheet.name, kind=SheetKind.UNUSABLE, verdict=Verdict.REJECT, rows=sheet.total_rows)
    profiles = profile_sheet(sheet) if sheet.rows else []

    # Textkatalog: en kolumn vars värden är "<annat kodverk>.<kod>".
    for p in profiles:
        values = [str(v) for v in sheet.column(p.header) if v]
        targets = {n for n in codesystem_names for v in values if v.startswith(n + ".")}
        hits = sum(1 for v in values if any(v.startswith(n + ".") for n in codesystem_names))
        if values and hits / len(values) > 0.8 and sheet.name not in targets:
            a.kind, a.verdict, a.code_column = SheetKind.TEXT_CATALOGUE, Verdict.SUPPORT, p.header
            a.reasons.append(f"Kolumnen '{p.header}' pekar på koder i {len(targets)} andra kodverk "
                             f"(t.ex. '{values[0]}'); texterna blir designations på de begreppen.")
            return a

    if any(name.startswith(n.strip()) for n in _DOC_NAMES) or _prose_ratio(sheet) > 0.5:
        a.kind, a.verdict = SheetKind.DOCUMENTATION, Verdict.SUPPORT
        a.reasons.append("Bladet innehåller dokumentation (löptext, historik eller förteckningar).")
        return a

    values = {normalize(str(v)) for r in sheet.rows for v in r.values() if v is not None}
    if sheet.total_rows <= 10 and len(sheet.headers) <= 2 and (
            any(name.startswith(n) for n in _HELPER_NAMES) or values <= _HELPER_VALUES):
        a.kind, a.verdict = SheetKind.HELPER, Verdict.SUPPORT
        a.reasons.append(f"Hjälplista ({', '.join(sorted(values))}), troligen för datavalidering i Excel.")
        return a

    if not profiles:
        a.reasons.append("Bladet innehåller inga datarader.")
        a.tips = list(MINIMUM_REQUIREMENTS)
        return a

    code, external = _code_column(profiles, sheet)
    if code is None:
        a.reasons.append(_no_code_reason(profiles, sheet))
        a.tips = list(MINIMUM_REQUIREMENTS) + _rejection_tips(sheet, profiles)
        return a

    a.code_column, a.external = code.header, external
    display = _display_column(profiles, code)
    a.display_column = display.header if display else None
    if external:
        a.kind = SheetKind.SUBSET
        a.reasons.append("Koderna kommer från " + " och ".join(f"{s.name} ({s.count})" for s in external.systems)
                         + " – bladet är ett urval ur befintliga kodsystem och blir ett ValueSet.")
        for anomaly in external.anomalies:
            a.warnings.append(f"'{anomaly.code}' {anomaly.problem}.")
    else:
        a.kind = SheetKind.CODESYSTEM
        a.reasons.append(f"Egna koder i kolumnen '{code.header}'.")

    codes = [str(v).strip() for v in sheet.column(code.header) if v is not None]
    dups = [c for c, n in Counter(codes).items() if n > 1]
    if dups:
        a.warnings.append(f"{len(dups)} koder förekommer på flera rader, t.ex. {', '.join(dups[:5])}.")
    if display is None:
        a.warnings.append("Ingen kolumn med benämning hittades.")
    elif display.fill_ratio < 1:
        a.warnings.append(f"{sheet.total_rows - display.filled} rader saknar benämning i '{display.header}'.")
    if sheet.source_format == "pdf":
        a.tips.append("Filen är en PDF-utskrift; kolumngränser och flikindelning har gissats. "
                      "Lämna hellre originalet i Excel eller CSV.")
    a.verdict = Verdict.WARN if a.warnings else Verdict.ACCEPT
    return a


def _no_code_reason(profiles: list[ColumnProfile], sheet: Sheet) -> str:
    if len(profiles) == 1:
        sample = profiles[0].samples[0] if profiles[0].samples else ""
        return (f"Ingen kodkolumn: bladet har bara en kolumn med fritext (t.ex. '{sample[:60]}'). "
                "Varje rad måste ha en kod.")
    uniq = [p for p in profiles if p.unique_ratio >= 0.85 and p.fill_ratio >= 0.9]
    if not uniq:
        return "Ingen kolumn är unik och ifylld på varje rad, så ingen kolumn kan vara kod."
    return ("Ingen kolumn ser ut som koder (" + ", ".join(f"'{p.header}'" for p in uniq[:3])
            + " innehåller fritext).")


def _rejection_tips(sheet: Sheet, profiles: list[ColumnProfile]) -> list[str]:
    tips = []
    if sheet.header_row == 0:
        tips.append("Bladet saknar rubrikrad; lägg till t.ex. 'Kod' och 'Benämning' överst.")
    if len(profiles) == 1:
        tips.append("Listan ser ut som enbart benämningar. Koder måste fastställas av den som förvaltar "
                    "kodverket innan import – verktyget hittar inte på koder.")
    if sheet.source_format == "pdf":
        tips.append("Lämna originalet i Excel eller CSV i stället för PDF.")
    return tips


# -- hela arbetsboken -------------------------------------------------------------


def assess_workbook(path: str | Path, sheets: list[Sheet] | None = None) -> tuple[WorkbookAssessment, list[Sheet]]:
    path = Path(path)
    sheets = sheets if sheets is not None else read_workbook(path)
    fmt = path.suffix.lower().lstrip(".")
    names = {sheet_source(s).name for s in sheets} | {s.name for s in sheets}
    assessments = [assess_sheet(s, names - {s.name, sheet_source(s).name}) for s in sheets]
    wa = WorkbookAssessment(file=path.name, format=fmt, verdict=Verdict.REJECT, sheets=assessments)

    by_name = {s.name: s for s in sheets}
    _families(wa, by_name)
    _relations(wa, by_name)
    code_sheets = {a.sheet for a in wa.sheets if a.kind == SheetKind.CODESYSTEM}
    cs_names = {sheet_source(by_name[n]).name for n in code_sheets}
    for a in wa.sheets:
        if a.kind == SheetKind.DOCUMENTATION:
            _read_documentation(wa, by_name[a.sheet], cs_names)
        elif a.kind == SheetKind.TEXT_CATALOGUE:
            wa.text_catalogues.append(_text_catalogue(by_name[a.sheet], a.code_column, cs_names))

    no_oid = {name: next((v for k, v in e.raw.items() if "oid" in normalize(k) or "uuid" in normalize(k)), None)
              for name, e in wa.catalogue.items() if e.identifier is None}
    no_oid = {k: v for k, v in no_oid.items() if v is not None}
    if no_oid:
        wa.tips.append(f"Identifierare (OID) saknas för {len(no_oid)} kodverk i katalogen "
                       f"(t.ex. {next(iter(no_oid))}: '{next(iter(no_oid.values()))}').")

    usable = [a for a in wa.sheets if a.verdict in (Verdict.ACCEPT, Verdict.WARN)]
    if not usable:
        wa.verdict = Verdict.REJECT
        wa.tips = list(dict.fromkeys(wa.tips + _workbook_rejection_tips(wa, sheets)))
    elif any(a.verdict == Verdict.WARN for a in usable) or any(r.missing for r in wa.relations):
        wa.verdict = Verdict.WARN
    else:
        wa.verdict = Verdict.ACCEPT
    return wa, sheets


def _workbook_rejection_tips(wa: WorkbookAssessment, sheets: list[Sheet]) -> list[str]:
    tips = ["Filen innehåller inget blad som uppfyller minimikraven för ett kodverk:"]
    tips += [f"  • {r}" for r in MINIMUM_REQUIREMENTS]
    rejected = [a for a in wa.sheets if a.verdict == Verdict.REJECT]
    if len(rejected) > 1 and all(len(s.headers) == 1 for s in sheets if s.name in {a.sheet for a in rejected}):
        tips.append(f"Filen innehåller {len(rejected)} separata listor ({', '.join(a.sheet for a in rejected)}). "
                    "Om de är olika kodverk: lägg dem i varsin flik med namn och kodkolumn.")
    if wa.format == "pdf":
        tips.append("Lämna originalet i Excel eller CSV i stället för PDF.")
    return tips


def _families(wa: WorkbookAssessment, sheets: dict[str, Sheet]) -> None:
    groups: dict[tuple, list[str]] = {}
    for a in wa.sheets:
        if a.kind == SheetKind.CODESYSTEM:
            key = tuple(normalize(h) for h in sheets[a.sheet].headers)
            groups.setdefault(key, []).append(a.sheet)
    from .suggest import family_prefix
    for members in groups.values():
        if len(members) >= 2:
            fam = family_prefix([sheet_source(sheets[m]).name for m in members])
            wa.families[fam] = members
            for m in members:
                wa.sheet(m).family = fam


def _relations(wa: WorkbookAssessment, sheets: dict[str, Sheet]) -> None:
    coded = [a for a in wa.sheets if a.code_column and a.kind in (SheetKind.SUBSET, SheetKind.CODESYSTEM)]
    codes = {a.sheet: {str(v).strip() for v in sheets[a.sheet].column(a.code_column) if v} for a in coded}
    pairs = {a.sheet: [(str(r.get(a.code_column)).strip(), str(r.get(a.display_column) or ""))
                       for r in sheets[a.sheet].rows] for a in coded if a.display_column}
    for a in coded:
        for b in coded:
            if a.sheet == b.sheet or len(codes[a.sheet]) > len(codes[b.sheet]):
                continue
            if len(codes[a.sheet]) == len(codes[b.sheet]) and a.sheet > b.sheet:
                continue
            overlap = len(codes[a.sheet] & codes[b.sheet]) / max(1, len(codes[a.sheet]))
            if overlap < 0.6:
                continue
            missing = sorted(codes[a.sheet] - codes[b.sheet])
            hints = []
            by_display = {_norm_display(d): c for c, d in pairs.get(b.sheet, []) if d}
            own = {c: d for c, d in pairs.get(a.sheet, [])}
            for code in missing:
                d = own.get(code)
                if d and _norm_display(d) in by_display:
                    hints.append(f"'{code}' ({d}) saknas i '{b.sheet}', där '{d}' har koden "
                                 f"'{by_display[_norm_display(d)]}'.")
            wa.relations.append(Relation(subset=a.sheet, superset=b.sheet, overlap=round(overlap, 3),
                                         missing=missing[:25], hints=hints))
    # Ett stort urval som andra urval ingår i är en referenslista.
    for r in wa.relations:
        sup = wa.sheet(r.superset)
        if sup.kind == SheetKind.SUBSET and sup.rows >= 500 and r.overlap >= 0.6:
            sup.kind = SheetKind.REFERENCE
            note = (f"Referenslista: andra blad är urval ur detta ({r.subset} till {r.overlap:.0%}). "
                    "Används för kontroll av urvalen; publiceras normalt inte själv.")
            if note not in sup.reasons:
                sup.reasons.append(note)


def _norm_display(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize(text))


# -- dokumentationsblad -----------------------------------------------------------


def _read_documentation(wa: WorkbookAssessment, sheet: Sheet, cs_names: set[str]) -> None:
    raw = sheet.raw
    texts = [str(v) for r in raw for v in r if isinstance(v, str)]
    prose = [t for t in texts if len(t) > 120]
    if prose:
        wa.documentation[sheet.name] = "\n\n".join(prose)
        _policy_from_text(wa, " ".join(prose))

    # Ägare: en etikett ("Dokumentägare") följd av värdet på nästa rad.
    for i, r in enumerate(raw):
        cells = [v for v in r if v is not None]
        if len(cells) == 1 and normalize(str(cells[0])) in _OWNER_LABELS:
            nxt = next((row for row in raw[i + 1:i + 3] if any(v is not None for v in row)), None)
            if nxt:
                wa.publisher = str(next(v for v in nxt if v is not None))

    # Revisionshistorik: tabell med Version + Datum.
    for i, r in enumerate(raw):
        norm = [normalize(str(v)) if v is not None else "" for v in r]
        if "version" in norm and any(n in ("datum", "date") for n in norm):
            for row in raw[i + 1:]:
                if not any(v is not None for v in row):
                    break
                entry = {str(r[j]): row[j] for j in range(len(r)) if r[j] is not None and j < len(row)
                         and row[j] is not None}
                if entry:
                    wa.history.append(entry)
            break

    # Katalog: rader vars celler innehåller namn på kodverken i filen.
    first = next((i for i, r in enumerate(raw) if any(str(v) in cs_names for v in r if v is not None)), None)
    if first is None:
        return
    header = next((raw[j] for j in range(first - 1, -1, -1)
                   if sum(v is not None for v in raw[j]) >= 2), None)
    if header is None:
        return
    for row in raw[first:]:
        name = next((str(v) for v in row if v is not None and str(v) in cs_names), None)
        if name is None:
            if not any(v is not None for v in row):
                continue
            break
        rec = {str(header[j]): row[j] for j in range(min(len(header), len(row))) if header[j] is not None}
        entry = CatalogueEntry(name=name, raw=rec)
        for label, value in rec.items():
            n = normalize(label)
            if value is None:
                continue
            v = str(value).strip()
            if normalize(v) in _MISSING:
                continue
            if "oid" in n or "uuid" in n or "identifierare" in n:
                entry.identifier = _identifier(v)
            elif any(w in n for w in ("agare", "forvaltare", "publisher", "utgivare")):
                entry.publisher = v
            elif any(w in n for w in ("innehall", "avser", "titel", "title", "beskrivning")):
                entry.title = v
        wa.catalogue[name] = entry


def _identifier(value: str) -> str:
    if re.fullmatch(r"\d+(\.\d+)+", value):
        return f"urn:oid:{value}"
    if re.fullmatch(r"[0-9a-fA-F-]{36}", value):
        return f"urn:uuid:{value.lower()}"
    return value


_REMOVAL = re.compile(r"[^.]*\b(inte|ej|aldrig|får inte)\b[^.]*\b(tas bort|ta bort|tagits bort|raderas|strykas)\b[^.]*\.",
                      re.IGNORECASE)


def _policy_from_text(wa: WorkbookAssessment, text: str) -> None:
    m = _REMOVAL.search(text)
    if m:
        wa.policy.allow_code_removal = False
        wa.policy.rationale = m.group(0).strip()


def _text_catalogue(sheet: Sheet, key_column: str, cs_names: set[str]) -> TextCatalogueCandidate:
    profiles = profile_sheet(sheet)
    texts = [p.header for p in profiles if p.header != key_column and p.inferred_type == "text"]
    flags = [p.header for p in profiles if p.header != key_column and p.inferred_type == "boolean"]
    keys = [str(v) for v in sheet.column(key_column) if v]
    targets = sorted({n for n in cs_names for k in keys if k.startswith(n + ".")})
    return TextCatalogueCandidate(sheet=sheet.name, key_column=key_column, text_columns=texts,
                                  flag_columns=flags, targets=targets)


def match_label(label: str, candidates: list[str], cutoff: float = 0.7) -> str | None:
    """Bästa fuzzy-träff bland etiketter (normaliserat)."""
    norm = {normalize(c): c for c in candidates}
    hit = difflib.get_close_matches(normalize(label), list(norm), n=1, cutoff=cutoff)
    return norm[hit[0]] if hit else None


__all__ = ["assess_workbook", "assess_sheet", "WorkbookAssessment", "SheetAssessment", "SheetKind", "Verdict",
           "MINIMUM_REQUIREMENTS", "ROLE_ALIASES", "match_label"]
