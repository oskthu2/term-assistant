"""Paketbeskrivningen (``paket.json``): vad ett terminologipaket ska innehålla.

Exempel::

    {
      "name": "se.exempel.terminologi.nordisk",
      "title": "Nordiskt startpaket",
      "description": "Kodverk och värdemängder från tx-nordics för test och utveckling.",
      "fhirVersion": "4.0.1",
      "firstVersion": "1.0.0",
      "maxConcepts": 5000,
      "classification": {"system": "http://terminology.hl7.org/CodeSystem/v3-Confidentiality",
                         "code": "U", "display": "unrestricted"},
      "sources": [
        {"type": "fhir", "base": "https://tx-nordics.fhir.org/fhir", "include": [
          {"resourceType": "ValueSet", "url": "http://hl7.se/fhir/ig/base/ValueSet/SEBaseMaritalStatusVS",
           "withCodeSystems": true},
          {"resourceType": "ConceptMap", "url": "https://hl7.se/fhir/ConceptMap/snomed-to-icd10-diabetes-sample"}
        ]},
        {"type": "packages", "path": "../inlasning/arbetsyta/paket", "optional": true}
      ]
    }

Källor:

``fhir``      hämtar resurser via canonical URL (och ev. version) från en FHIR-server.
              ``withCodeSystems`` på ett ValueSet tar med de kodsystem det bygger på,
              om de finns på servern och inte är större än ``maxConcepts``.
``packages``  tar med resurserna i FHIR-paket (``.tgz``) i en katalog, t.ex. de
              releaser inläsningstjänsten publicerar. Senaste versionen per paket.

Relativa sökvägar räknas från ``paket.json``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RESOURCE_TYPES = ("CodeSystem", "ValueSet", "ConceptMap", "NamingSystem")
FHIR_VERSIONS = {"4.0.1": "hl7.fhir.r4.core", "5.0.0": "hl7.fhir.r5.core"}


class ConfigError(ValueError):
    pass


@dataclass
class Include:
    resource_type: str
    url: str
    version: str | None = None
    with_code_systems: bool = False
    allow_large: bool = False


@dataclass
class Source:
    type: str
    base: str | None = None
    include: list[Include] = field(default_factory=list)
    path: Path | None = None
    optional: bool = False


@dataclass
class PackageConfig:
    name: str
    title: str
    description: str
    fhir_version: str = "4.0.1"
    first_version: str = "1.0.0"
    max_concepts: int = 5000
    classification: dict | None = None
    author: str = ""
    sources: list[Source] = field(default_factory=list)


def load_config(path: str | Path) -> PackageConfig:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Kan inte läsa {path}: {exc}") from exc
    return parse_config(raw, path.parent)


def parse_config(raw: dict[str, Any], root: Path = Path(".")) -> PackageConfig:
    errors = []
    name = raw.get("name", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]*[a-z0-9]", name or ""):
        errors.append("name ska vara ett paketnamn med gemener, siffror, punkt och bindestreck, t.ex. "
                      "se.exempel.terminologi")
    fhir_version = raw.get("fhirVersion", "4.0.1")
    if fhir_version not in FHIR_VERSIONS:
        errors.append(f"fhirVersion ska vara en av {', '.join(FHIR_VERSIONS)}")
    first = raw.get("firstVersion", "1.0.0")
    if not re.fullmatch(r"\d+\.\d+\.\d+", first):
        errors.append("firstVersion ska ha formen X.Y.Z")
    sources = []
    for i, s in enumerate(raw.get("sources") or []):
        kind = s.get("type")
        if kind == "fhir":
            if not s.get("base"):
                errors.append(f"sources[{i}]: base saknas")
            includes = []
            for j, inc in enumerate(s.get("include") or []):
                if inc.get("resourceType") not in RESOURCE_TYPES or not inc.get("url"):
                    errors.append(f"sources[{i}].include[{j}]: resourceType ({'/'.join(RESOURCE_TYPES)}) "
                                  "och url krävs")
                    continue
                includes.append(Include(inc["resourceType"], inc["url"], inc.get("version"),
                                        bool(inc.get("withCodeSystems")), bool(inc.get("allowLarge"))))
            sources.append(Source("fhir", base=(s.get("base") or "").rstrip("/"), include=includes))
        elif kind == "packages":
            if not s.get("path"):
                errors.append(f"sources[{i}]: path saknas")
                continue
            sources.append(Source("packages", path=(root / s["path"]).resolve(), optional=bool(s.get("optional"))))
        else:
            errors.append(f"sources[{i}]: okänd typ '{kind}' (fhir eller packages)")
    if not sources:
        errors.append("minst en källa (sources) krävs")
    if errors:
        raise ConfigError("; ".join(errors))
    return PackageConfig(name=name, title=raw.get("title") or name, description=raw.get("description") or "",
                         fhir_version=fhir_version, first_version=first,
                         max_concepts=int(raw.get("maxConcepts", 5000)), classification=raw.get("classification"),
                         author=raw.get("author", ""), sources=sources)
