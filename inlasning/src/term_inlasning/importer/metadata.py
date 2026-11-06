"""Metadata om kodverket (eller urvalet) – vad som fångas, varifrån och när det krävs.

Ett eget kodverk som ska bli ett CodeSystem behöver mer än koderna: namn,
titel, version, utgivare, beskrivning, kontaktpunkt, OID, licens. Sådant
står sällan på ett ställe i Excel-filen. Inläsningen letar därför på flera
ställen, i prioritetsordning:

1. nyckel–värde-rader ovanför tabellen ("Kodverk: KV_X", "Ägare: …")
2. dokumentationsflikar ("Om kodverket", "Inledning") med etikett + värde
   på samma rad, eller etikett på en rad och värde på nästa
3. bedömningens katalog och revisionshistorik (OID, ägare, senaste version)
4. filens dokumentegenskaper (titel, beskrivning)
5. fliknamnet (namn och titel, sista utväg)

Varje värde sparas med sin källa (``CodeSystemMeta.sources``) och visas i
inläsningsförslaget, där det kan rättas innan projektet skapas. Vissa fält
krävs redan vid inläsning, andra först före release (Shareable-profilerna
kräver t.ex. utgivare och beskrivning).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .mapping import ImportTemplate
from .registry import normalize
from .workbook import Sheet


@dataclass(frozen=True)
class MetaField:
    key: str
    label: str
    required: str | None      # "import" | "release" | None (rekommenderas)
    aliases: tuple[str, ...]
    help: str = ""


FIELDS: list[MetaField] = [
    MetaField("name", "Namn (maskinläsbart)", "import",
              ("kodverk", "kodverksnamn", "kodverk id", "kortnamn", "namn", "name", "urval", "vardemangd"),
              "Blir CodeSystem.name/ValueSet.name, t.ex. KV_EXEMPEL"),
    MetaField("title", "Titel", "import",
              ("titel", "title", "avser", "rubrik", "kodverkets namn", "fullstandigt namn", "benamning"),
              "Läsbar titel"),
    MetaField("url", "Canonical URL", "import", (), "Från vald namnrymd; bas-URL för en familj av kodverk"),
    MetaField("version", "Version", "import", ("version", "versionsnummer", "utgava", "aktuell version"),
              "X.Y.Z – höjs vid varje release"),
    MetaField("publisher", "Utgivare", "release",
              ("utgivare", "publisher", "forvaltare", "agare", "dokumentagare", "ansvarig", "ansvarig organisation",
               "forvaltningsansvarig", "kodverksagare", "forvaltande organisation")),
    MetaField("description", "Beskrivning", "release",
              ("beskrivning", "description", "omfattning", "innehall", "sammanfattning", "om kodverket")),
    MetaField("contact", "Kontakt", None,
              ("kontakt", "kontaktperson", "kontaktuppgifter", "e post", "epost", "mejl", "mail", "contact",
               "funktionsbrevlada", "support"), "E-post eller namn; rekommenderas före release"),
    MetaField("identifier", "OID / identifierare", None,
              ("oid", "identifierare", "identifier", "uuid", "kodverks oid", "kodverksidentitet")),
    MetaField("purpose", "Syfte", None,
              ("syfte", "purpose", "anvandning", "anvandningsomrade", "tillampning", "avsedd anvandning")),
    MetaField("copyright", "Upphovsrätt / licens", None,
              ("copyright", "upphovsratt", "licens", "license", "villkor", "anvandarvillkor", "licensvillkor")),
]
BY_KEY = {f.key: f for f in FIELDS}
_LABEL_MAX = 40
_HEADER_WORDS = {"datum", "date", "andring", "andringar", "signatur", "forfattare", "kommentar"}


def _match(label: str) -> MetaField | None:
    n = normalize(label)
    if not n or len(n) > _LABEL_MAX:
        return None
    return next((f for f in FIELDS if n in f.aliases), None)


def key_values(raw: list[tuple]) -> list[tuple[str, str, str]]:
    """(fält, värde, etikett) ur rader: 'Etikett: värde', 'Etikett | värde' eller etikett över värde."""
    out = []
    for i, row in enumerate(raw):
        cells = [v for v in row if v is not None and str(v).strip()]
        if not cells or not isinstance(cells[0], str):
            continue
        label = cells[0].strip()
        inline = None
        if ":" in label and not label.endswith(":") and len(cells) == 1:  # "Version: 1.2" i en cell
            label, inline = (part.strip() for part in label.split(":", 1))
        field = _match(label.rstrip(":"))
        if field is None:
            continue
        # En tabellrubrik ("Version | Datum | Beskrivning") är ingen nyckel–värde-rad.
        if sum(isinstance(c, str) and (_match(c) is not None or normalize(c) in _HEADER_WORDS)
               for c in cells[1:]) >= 1 and len(cells) >= 3:
            continue
        if inline:
            out.append((field.key, inline, label))
        elif len(cells) >= 2:
            out.append((field.key, " ".join(str(c).strip() for c in cells[1:]), label))
        else:  # etikett på en rad, värdet på nästa icke-tomma rad
            nxt = next((r for r in raw[i + 1:i + 3] if any(v is not None for v in r)), None)
            if nxt:
                vals = [str(v).strip() for v in nxt if v is not None and str(v).strip()]
                if vals and _match(vals[0]) is None:
                    out.append((field.key, " ".join(vals), label))
    return out


def document_properties(path: str | Path) -> dict[str, str]:
    """Titel och beskrivning ur Excel-filens dokumentegenskaper (inte författare – personuppgift)."""
    path = Path(path)
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return {}
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True)
        props = wb.properties
        wb.close()
    except Exception:  # skadad fil, lösenord …
        return {}
    out = {}
    if props.title and props.title.strip():
        out["title"] = props.title.strip()
    desc = (props.description or props.subject or "").strip()
    if desc:
        out["description"] = desc
    return out


def normalize_version(value: str) -> str:
    """'1.2' -> '1.2.0', 'v3' -> '3.0.0'; annat lämnas orört (och flaggas vid kontrollen)."""
    v = str(value).strip().lstrip("vV").strip()
    if re.fullmatch(r"\d+(\.\d+){0,2}", v):
        return ".".join((v.split(".") + ["0", "0"])[:3])
    return str(value).strip()


def harvest(template: ImportTemplate, sheets: list[Sheet], documentation: list[Sheet],
            path: str | Path | None = None, assessment=None) -> list[str]:
    """Fyll i mallens metadata ur filen, utan att skriva över det som redan är satt från en bättre källa.
    Returnerar frågor om det som saknas."""
    meta = template.codesystem
    sources = meta.sources
    family = len(template.source.sheets) > 1
    found: dict[str, tuple[str, str]] = {}

    def offer(key: str, value, source: str) -> None:
        if value is None or not str(value).strip() or key in found:
            return
        if family and key in ("name", "title", "identifier"):
            return  # per kodverk i familjen (SheetSource), inte för hela projektet
        found[key] = (str(value).strip(), source)

    first = sheets[0] if sheets else None
    if first is not None:
        for k, v in first.metadata.items():
            field = _match(k)
            if field:
                offer(field.key, v, f"rad ovanför tabellen i '{first.name}' ({k})")
        for key, value, label in key_values([(line,) for line in first.preamble]):
            offer(key, value, f"rad ovanför tabellen i '{first.name}' ({label})")
    for doc in documentation:
        for key, value, label in key_values(doc.raw):
            offer(key, value, f"fliken '{doc.name}' ({label})")
    if assessment is not None:
        entry = assessment.catalogue.get(meta.name) if not family else None
        if entry:
            offer("identifier", entry.identifier, "katalogen i dokumentationen")
            offer("publisher", entry.publisher, "katalogen i dokumentationen")
            offer("title", entry.title, "katalogen i dokumentationen")
        offer("publisher", assessment.publisher, "dokumentägare i dokumentationen")
        if assessment.history:
            latest = assessment.history[-1]
            offer("version", latest.get("Version") or latest.get("version"), "revisionshistoriken")
    if path is not None:
        for key, value in document_properties(path).items():
            offer(key, value, "filens dokumentegenskaper")

    for key, (value, source) in found.items():
        if key == "version":
            value = normalize_version(value)
        current = getattr(meta, key)
        if current == value:
            sources.setdefault(key, source)
            continue
        if key in ("name", "title"):
            # Namn/titel ur rubrikraderna är redan satta; skriv bara över det som kommer från fliknamnet.
            if current and current != value and not (first and current in (first.name, _vs_name(first.name))):
                continue
        elif key != "version" and current:  # filens version ersätter standardvärdet
            continue
        setattr(meta, key, value)
        sources[key] = source
    for key in ("name", "title"):
        sources.setdefault(key, "fliknamnet" if not family else "familjens gemensamma prefix")
    if template.namespace is None and "url" not in sources:
        sources["url"] = "vald namnrymd"
    if "version" not in sources:
        sources["version"] = "standard (första version)"

    questions = []
    missing_release = [BY_KEY[k].label.lower() for k in ("publisher", "description") if not getattr(meta, k)]
    if missing_release:
        questions.append("Metadata saknas i filen och behövs före release: " + ", ".join(missing_release) + ".")
    return questions


def _vs_name(sheet_name: str) -> str:
    return re.sub(r"\W+", "_", sheet_name).strip("_")


def import_errors(template: ImportTemplate) -> list[str]:
    """Det som måste finnas för att projektet ska kunna skapas."""
    meta, errors = template.codesystem, []
    for key in ("name", "title", "url", "version"):
        if not (getattr(meta, key) or "").strip():
            errors.append(f"{BY_KEY[key].label} måste anges.")
    if meta.url and not re.match(r"^(https?://\S+|urn:(oid|uuid):\S+)$", meta.url):
        errors.append(f"Canonical URL '{meta.url}' ska vara en absolut http(s)-URL.")
    if meta.version and not re.fullmatch(r"\d+\.\d+\.\d+", meta.version):
        errors.append(f"Versionen '{meta.version}' ska ha formen X.Y.Z, t.ex. 1.0.0.")
    if meta.identifier and not re.fullmatch(r"(urn:oid:)?\d+(\.\d+)+|urn:uuid:[0-9a-fA-F-]{36}", meta.identifier):
        errors.append(f"Identifieraren '{meta.identifier}' är varken en OID eller en urn:uuid.")
    return errors


def release_errors(template: ImportTemplate, required: list[str]) -> tuple[list[str], list[str]]:
    """(fel, varningar) för metadata före en releasekandidat."""
    meta, errors, warnings = template.codesystem, [], []
    sheets = template.source.sheets if len(template.source.sheets) > 1 else []
    for key in required:
        value = getattr(meta, key, None)
        if key == "publisher" and sheets and all(s.publisher for s in sheets):
            value = value or "per kodverk"
        if not value:
            errors.append(f"Metadata saknas: {BY_KEY[key].label.lower() if key in BY_KEY else key} "
                          "(ange under Metadata).")
    for key in ("contact", "copyright"):
        if key not in required and not getattr(meta, key):
            warnings.append(f"Metadata: {BY_KEY[key].label.lower()} saknas (rekommenderas).")
    if template.kind == "codesystem" and "identifier" not in required:
        no_oid = [s.name for s in sheets if not s.identifier] if sheets else ([] if meta.identifier else [meta.name])
        if no_oid:
            warnings.append(f"OID saknas för {', '.join(no_oid[:4])}{' …' if len(no_oid) > 4 else ''}.")
    return errors, warnings


def apply_form(template: ImportTemplate, form) -> None:
    """Skriv formulärfälten ``meta_<fält>`` till mallen; ändrade värden får källan 'angivet vid inläsning'."""
    meta = template.codesystem
    for f in FIELDS:
        name = f"meta_{f.key}"
        if name not in form:
            continue
        value = (form.get(name) or "").strip() or None
        if f.key == "version" and value:
            value = normalize_version(value)
        if value != getattr(meta, f.key):
            setattr(meta, f.key, value)
            meta.sources[f.key] = "angivet vid inläsning"
