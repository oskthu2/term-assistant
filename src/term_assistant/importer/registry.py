"""Egenskapsregistret – mekanismen för att konvergera mot få egenskaper.

Varje "extrakolumn" i ett Excel-ark ska helst mappas mot en egenskap som
redan finns, snarare än att varje kodverk uppfinner sin egen. Registret
håller en begränsad uppsättning kanoniska mål:

* ``property``-poster motsvarar ``CodeSystem.property`` (code, uri, type)
* ``designation``-poster motsvarar en ``designation.use`` (+ ev. språk)

Varje post har alias (kolumnrubriker som tidigare bekräftats betyda samma
sak). När en användare bekräftar en mappning lär sig registret rubriken som
alias, så att nästa ark med samma rubrik mappas automatiskt. Registret kan
dessutom skördas från CodeSystem-resurser på terminologitjänsten, så att
egenskaper som redan används där återanvänds i stället för att dupliceras.
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .mapping import DesignationTarget, PropertyDefinition

CONCEPT_PROPERTIES = "http://hl7.org/fhir/concept-properties"
SNOMED = "http://snomed.info/sct"

# Ord i kolumnrubriker som anger språk, t.ex. "Term (en)" eller "Benämning engelska".
LANGUAGE_WORDS = {
    "sv": "sv", "svenska": "sv", "swedish": "sv",
    "en": "en", "engelska": "en", "english": "en",
    "no": "no", "nb": "nb", "norska": "nb", "norwegian": "nb",
    "da": "da", "danska": "da", "danish": "da",
    "fi": "fi", "finska": "fi", "finnish": "fi",
    "de": "de", "tyska": "de", "german": "de",
}


def normalize(text: str) -> str:
    """Normalisera en kolumnrubrik för jämförelse (skiftläge, accenter, skiljetecken)."""
    text = unicodedata.normalize("NFKD", text.strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_language(header: str) -> tuple[str, str | None]:
    """Dela upp en rubrik i (bas, språk), t.ex. 'Term (en)' -> ('term', 'en')."""
    tokens = normalize(header).split()
    language = None
    kept = []
    for tok in tokens:
        if language is None and tok in LANGUAGE_WORDS and len(tokens) > 1:
            language = LANGUAGE_WORDS[tok]
        else:
            kept.append(tok)
    return " ".join(kept), language


_NEGATIONS = ("in", "un", "non", "ej ", "icke ", "inte ", "not ", "o")


def is_negation(a: str, b: str) -> bool:
    """Sant om den ena (normaliserade) rubriken är negationen av den andra,
    t.ex. 'aktiv'/'inaktiv', 'ej valbar'/'valbar', 'giltig'/'ogiltig'."""
    a, b = a.strip(), b.strip()
    if not a or not b or a == b:
        return False
    for short, long_ in ((a, b), (b, a)):
        for prefix in _NEGATIONS:
            if long_ == prefix + short or long_ == f"{prefix.strip()} {short}":
                return True
    return False


class RegistryEntry(BaseModel):
    kind: Literal["property", "designation"]
    key: str = Field(description="Unik nyckel: property.code, eller 'designation:<use_code>'")
    property: PropertyDefinition | None = None
    designation: DesignationTarget | None = None
    aliases: list[str] = []
    source: str = "local"
    usage_count: int = 0

    def all_names(self) -> list[str]:
        names = list(self.aliases)
        if self.property:
            names.append(self.property.code)
            if self.property.description:
                names.append(self.property.description)
        if self.designation and self.designation.use_display:
            names.append(self.designation.use_display)
        return [normalize(n) for n in names if n]


class Candidate(BaseModel):
    entry: RegistryEntry
    score: float
    reason: str
    language: str | None = None


class PropertyRegistry(BaseModel):
    entries: list[RegistryEntry] = []

    # -- persistens -------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None) -> "PropertyRegistry":
        if path and Path(path).exists():
            return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
        return cls.default()

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")

    @classmethod
    def default(cls) -> "PropertyRegistry":
        return cls(entries=[e.model_copy(deep=True) for e in _SEED])

    # -- uppslag ----------------------------------------------------------

    def get(self, key: str) -> RegistryEntry | None:
        return next((e for e in self.entries if e.key == key), None)

    def properties(self) -> list[RegistryEntry]:
        return [e for e in self.entries if e.kind == "property"]

    def match(self, header: str, limit: int = 3, cutoff: float = 0.72) -> list[Candidate]:
        """Föreslå registerposter för en kolumnrubrik, bästa först."""
        base, language = split_language(header)
        full = normalize(header)
        candidates: list[Candidate] = []
        for entry in self.entries:
            best, reason = 0.0, ""
            for name in entry.all_names():
                if name in (full, base):
                    best, reason = 1.0, f"exakt alias '{name}'"
                    break
                if is_negation(name, full) or is_negation(name, base):
                    continue  # "aktiv" får aldrig matcha "inaktiv"
                ratio = max(difflib.SequenceMatcher(None, name, full).ratio(),
                            difflib.SequenceMatcher(None, name, base).ratio() if base else 0.0)
                # Delordsträff: "Kommentar till begrepp" ~ "kommentar"
                if name and (f" {name} " in f" {full} ") and len(name) >= 4:
                    ratio = max(ratio, 0.85)
                if ratio > best:
                    best, reason = ratio, f"liknar '{name}' ({ratio:.2f})"
            if best >= cutoff:
                # Lätt preferens för poster som använts mycket – konvergens.
                score = min(1.0, best + min(entry.usage_count, 10) * 0.005)
                candidates.append(Candidate(entry=entry, score=round(score, 3), reason=reason, language=language))
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:limit]

    # -- inlärning --------------------------------------------------------

    def learn(self, header: str, key: str) -> RegistryEntry:
        """Bekräfta att en rubrik betyder registerposten ``key``."""
        entry = self.get(key)
        if entry is None:
            raise KeyError(f"Okänd registerpost '{key}'")
        alias = split_language(header)[0] if entry.kind == "designation" else normalize(header)
        if alias and alias not in entry.all_names():
            entry.aliases.append(alias)
        entry.usage_count += 1
        return entry

    def add_property(self, definition: PropertyDefinition, aliases: list[str] | None = None,
                     source: str = "local") -> RegistryEntry:
        """Lägg till en ny egenskap. Avvisas om koden eller URI:n redan finns."""
        for entry in self.properties():
            if entry.property.code == definition.code or (definition.uri and entry.property.uri == definition.uri):
                raise ValueError(f"Egenskapen finns redan som '{entry.key}' – återanvänd den eller lägg till alias.")
        entry = RegistryEntry(kind="property", key=definition.code, property=definition,
                              aliases=[normalize(a) for a in aliases or []], source=source)
        self.entries.append(entry)
        return entry

    def merge(self, keep: str, drop: str) -> RegistryEntry:
        """Slå ihop två synonyma poster: ``drop`` blir alias till ``keep``."""
        kept, dropped = self.get(keep), self.get(drop)
        if kept is None or dropped is None:
            raise KeyError("Båda posterna måste finnas")
        if kept.kind != dropped.kind:
            raise ValueError("Kan inte slå ihop en property med en designation")
        for name in dropped.all_names():
            if name not in kept.all_names():
                kept.aliases.append(name)
        kept.usage_count += dropped.usage_count
        self.entries.remove(dropped)
        return kept

    def near_duplicates(self, threshold: float = 0.8) -> list[tuple[str, str, float]]:
        """Hitta poster som troligen är synonymer och borde slås ihop."""
        pairs = []
        for i, a in enumerate(self.entries):
            for b in self.entries[i + 1:]:
                if a.kind != b.kind:
                    continue
                if a.property and b.property and a.property.type != b.property.type:
                    continue
                # Olika uttryckliga URI:er betyder att någon redan definierat dem som skilda begrepp.
                if a.property and b.property and a.property.uri and b.property.uri:
                    continue
                if a.designation and b.designation and a.designation.use_code and b.designation.use_code:
                    continue
                score = max(
                    (difflib.SequenceMatcher(None, x, y).ratio() for x in a.all_names() for y in b.all_names()),
                    default=0.0,
                )
                if score >= threshold:
                    pairs.append((a.key, b.key, round(score, 3)))
        return sorted(pairs, key=lambda p: p[2], reverse=True)

    def harvest(self, codesystems: list[dict]) -> int:
        """Skörda CodeSystem.property från befintliga kodverk. Returnerar antal nya poster."""
        added = 0
        for cs in codesystems:
            for prop in cs.get("property", []):
                code, uri = prop.get("code"), prop.get("uri")
                existing = next(
                    (e for e in self.properties()
                     if (uri and e.property.uri == uri) or e.property.code == code),
                    None,
                )
                if existing:
                    if code and normalize(code) not in existing.all_names():
                        existing.aliases.append(normalize(code))
                    existing.usage_count += 1
                    continue
                self.entries.append(RegistryEntry(
                    kind="property", key=code,
                    property=PropertyDefinition(code=code, uri=uri, type=prop.get("type", "string"),
                                                description=prop.get("description")),
                    source=f"server:{cs.get('url', '?')}", usage_count=1,
                ))
                added += 1
        return added

    def summary_for_prompt(self) -> str:
        """Kompakt textversion av registret för LLM-prompten."""
        lines = []
        for e in self.entries:
            if e.property:
                p = e.property
                lines.append(f"- property key={e.key} type={p.type} uri={p.uri or '-'} "
                             f"aliases={', '.join(e.aliases[:8]) or '-'} uses={e.usage_count}")
            else:
                d = e.designation
                lines.append(f"- designation key={e.key} use={d.use_system}|{d.use_code} ({d.use_display}) "
                             f"aliases={', '.join(e.aliases[:8]) or '-'} uses={e.usage_count}")
        return "\n".join(lines)


def _prop(code: str, type_: str, description: str, aliases: list[str]) -> RegistryEntry:
    return RegistryEntry(
        kind="property", key=code, source="fhir-core",
        property=PropertyDefinition(code=code, uri=f"{CONCEPT_PROPERTIES}#{code}", type=type_, description=description),
        aliases=[normalize(a) for a in aliases],
    )


def _designation(key: str, code: str, display: str, aliases: list[str]) -> RegistryEntry:
    return RegistryEntry(
        kind="designation", key=f"designation:{key}", source="fhir-core",
        designation=DesignationTarget(use_system=SNOMED, use_code=code, use_display=display),
        aliases=[normalize(a) for a in aliases],
    )


_SEED: list[RegistryEntry] = [
    _prop("status", "code", "Status", ["status", "tillstånd", "begreppsstatus"]),
    _prop("inactive", "boolean", "Inactive", ["inaktiv", "inactive", "utgången", "avregistrerad", "ej aktiv"]),
    _prop("deprecated", "dateTime", "Deprecated", ["utfasad", "deprecated", "utfasningsdatum"]),
    _prop("notSelectable", "boolean", "Not Selectable",
          ["ej valbar", "not selectable", "abstrakt", "gruppering"]),
    _prop("effectiveDate", "dateTime", "Effective Date",
          ["giltig from", "giltig fr o m", "giltig fran", "gäller från", "startdatum", "effective date"]),
    _prop("retirementDate", "dateTime", "Retirement Date",
          ["giltig tom", "giltig t o m", "giltig till", "gäller till", "slutdatum", "retirement date"]),
    _prop("comment", "string", "Comment", ["kommentar", "anmärkning", "notering", "comment", "note", "not"]),
    _prop("itemWeight", "decimal", "Item Weight", ["vikt", "poäng", "score", "weight", "värde"]),
    _designation("synonym", "900000000000013009", "Synonym",
                 ["synonym", "synonymer", "alternativ term", "sökterm", "sokord", "andra namn"]),
    _designation("fsn", "900000000000003001", "Fully specified name",
                 ["fullständigt namn", "fsn", "fully specified name", "fullständig benämning"]),
]
