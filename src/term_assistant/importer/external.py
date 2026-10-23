"""Igenkänning av koder från kända, externa kodsystem.

Om kodkolumnen innehåller t.ex. NPU-, SNOMED CT- eller LOINC-koder är bladet
inte ett eget kodverk utan ett *urval* ur ett befintligt. Det ska då bli ett
ValueSet och inte ett CodeSystem.

Mönstren används också för att hitta koder som nästan stämmer (t.ex.
``NPU0384`` med en siffra för lite). Sådana koder är nästan alltid skrivfel.

URI:erna som är markerade ``verified=False`` är inte bekräftade. Den
kanoniska URL:en ska slås upp på den terminologitjänst ni använder
(``resolve_system_uris``), eller anges i mallen.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_ok(number: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def loinc_ok(code: str) -> bool:
    body, check = code.split("-")
    digits = body.zfill(7)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d // 10 + d % 10
    return (10 - total % 10) % 10 == int(check)


@dataclass(frozen=True)
class ExternalSystem:
    key: str
    name: str
    uri: str | None
    pattern: re.Pattern
    near: re.Pattern | None = None          # "ser ut som men stämmer inte"
    check: Callable[[str], bool] | None = None
    expected: str = ""
    search_terms: tuple[str, ...] = ()       # för uppslag på terminologitjänsten
    verified: bool = True


SYSTEMS: list[ExternalSystem] = [
    # Bekräftad på tx-nordics.fhir.org (Ontoserver), version "INT 2026-09-30". SWE-koderna finns inte där.
    ExternalSystem("npu", "NPU (Nomenclature for Properties and Units)", "http://npu-terminology.org",
                   re.compile(r"^NPU\d{5}$"), re.compile(r"^NPU\s*\d+$", re.I), expected="NPU + 5 siffror",
                   search_terms=("NPU",)),
    # I Ineras NPU-lista (Ontoserver) ingår SWE-koderna i samma kodsystem som NPU. Den internationella
    # NPU-versionen på tx-nordics saknar dem, så valideringen beror på vilken version servern har.
    ExternalSystem("swe-npu", "Svenska NPU-tillägg (SWE-koder)", "http://npu-terminology.org",
                   re.compile(r"^SWE\d{5}$"), re.compile(r"^SWE\s*\d+$", re.I), expected="SWE + 5 siffror",
                   search_terms=("NPU",)),
    ExternalSystem("loinc", "LOINC", "http://loinc.org", re.compile(r"^\d{1,7}-\d$"),
                   check=loinc_ok, expected="siffror-kontrollsiffra"),
    ExternalSystem("snomed", "SNOMED CT", "http://snomed.info/sct", re.compile(r"^\d{6,18}$"),
                   check=verhoeff_ok, expected="6–18 siffror med giltig kontrollsiffra"),
    ExternalSystem("atc", "ATC", "http://www.whocc.no/atc", re.compile(r"^[A-Z]\d{2}[A-Z]{2}\d{2}$"),
                   re.compile(r"^[A-Z]\d{2}[A-Z]{1,2}\d{0,2}$"), expected="t.ex. N02BE01"),
    ExternalSystem("icd10", "ICD-10(-SE)", "http://hl7.org/fhir/sid/icd-10",
                   re.compile(r"^[A-TV-Z]\d{2}(\.\d{1,2})?[A-Z]?$"), expected="t.ex. J18.9",
                   search_terms=("ICD-10",), verified=False),
    ExternalSystem("kva", "KVÅ (Klassifikation av vårdåtgärder)", None, re.compile(r"^[A-Z]{2}\d{3}$"),
                   expected="2 bokstäver + 3 siffror", search_terms=("KVÅ", "vårdåtgärder"), verified=False),
]


class SystemMatch(BaseModel):
    key: str
    name: str
    uri: str | None
    verified: bool
    count: int
    share: float


class CodeAnomaly(BaseModel):
    code: str
    system: str
    problem: str


class ExternalDetection(BaseModel):
    systems: list[SystemMatch]        # system som täcker kolumnens värden
    coverage: float                   # andel värden som matchar något system
    anomalies: list[CodeAnomaly]      # nästan-träffar och felaktiga kontrollsiffror
    unmatched: list[str]


def classify_code(code: str) -> tuple[ExternalSystem | None, CodeAnomaly | None]:
    code = code.strip()
    for system in SYSTEMS:
        if system.pattern.match(code):
            if system.check and not system.check(code):
                return None, CodeAnomaly(code=code, system=system.key,
                                         problem=f"ser ut som {system.name} men kontrollsiffran stämmer inte")
            return system, None
    for system in SYSTEMS:
        if system.near and system.near.match(code):
            return None, CodeAnomaly(code=code, system=system.key,
                                     problem=f"ser ut som {system.name} men har fel format (förväntat {system.expected})")
    return None, None


def detect(values: list) -> ExternalDetection | None:
    """Avgör om en kolumns värden är koder ur ett eller flera externa kodsystem."""
    codes = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if not codes:
        return None
    counts: Counter[str] = Counter()
    anomalies, unmatched = [], []
    for code in codes:
        system, anomaly = classify_code(code)
        if system:
            counts[system.key] += 1
        elif anomaly:
            anomalies.append(anomaly)
        else:
            unmatched.append(code)
    # Generiska mönster (SNOMED = bara siffror, KVÅ = 2 bokstäver + 3 siffror) ger
    # lätt falska träffar på lokala koder. Kräv därför att de dominerar kolumnen.
    matched = sum(counts.values())
    if not matched or matched / len(codes) < 0.6:
        return None
    by_key = {s.key: s for s in SYSTEMS}
    systems = [SystemMatch(key=k, name=by_key[k].name, uri=by_key[k].uri, verified=by_key[k].verified,
                           count=n, share=round(n / len(codes), 3))
               for k, n in counts.most_common()]
    return ExternalDetection(systems=systems, coverage=round((matched + len(anomalies)) / len(codes), 3),
                             anomalies=anomalies, unmatched=unmatched[:20])


async def resolve_system_uris(client, keys: list[str]) -> dict[str, list[dict]]:
    """Leta efter kodsystemen på terminologitjänsten. Returnerar kandidater per nyckel."""
    by_key = {s.key: s for s in SYSTEMS}
    found: dict[str, list[dict]] = {}
    for key in keys:
        system = by_key[key]
        hits: dict[str, dict] = {}
        for term in system.search_terms or (system.name,):
            for cs in await client.search("CodeSystem", count=20, **{"title:contains": term}):
                text = " ".join(str(cs.get(k, "")) for k in ("title", "name", "url"))
                # Hela ord: "NPU" ska inte träffa "INPUT".
                if re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![a-z])", text, re.I) and cs.get("url") \
                        and not cs["url"].endswith("/property"):
                    hits[cs["url"]] = {k: cs.get(k) for k in ("url", "title", "version")}
        found[key] = list(hits.values())
    return found
