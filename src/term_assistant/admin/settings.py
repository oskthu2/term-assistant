"""Inställningar för en arbetsyta (``<arbetsyta>/settings.json``).

* **Namnrymder** – väljs vid inläsning. De bestämmer bas-URL och URL-mönster
  för canonical URL:er, OID-rot (och om OID krävs), förvald utgivare och
  paketnamn. Releaser spärras om resurserna inte följer namnrymden.
* **Informationsklasser** – följer med projektet och märks på varje resurs
  (``meta.security``, v3-Confidentiality) och i paketets package.json.
* **Mål** – vart paket och resurser får publiceras. Varje mål har en högsta
  tillåten informationsklass; ett paket i en högre klass spärras.
* **Redaktionella regler** – om manuella ändringar ska granskas och om
  releaser kräver fyra ögon (godkännare ≠ den som skapade kandidaten).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

CONFIDENTIALITY = "http://terminology.hl7.org/CodeSystem/v3-Confidentiality"
# Kodsystemets egna displaytexter – validatorn underkänner andra (t.ex. svenska) etiketter.
CONFIDENTIALITY_DISPLAY = {"U": "unrestricted", "L": "low", "M": "moderate", "N": "normal", "R": "restricted",
                           "V": "very restricted"}


class Namespace(BaseModel):
    key: str
    label: str
    base_url: str
    url_pattern: str | None = Field(None, description="Reguljärt uttryck; standard: <bas>/(CodeSystem|ValueSet|"
                                                      "ConceptMap)/<namn>")
    oid_root: str | None = None
    require_oid: bool = False
    publisher: str | None = None
    package_prefix: str | None = Field(None, description="t.ex. 'se.example.kodverk'")

    def pattern(self) -> re.Pattern:
        if self.url_pattern:
            return re.compile(self.url_pattern)
        return re.compile(rf"^{re.escape(self.base_url.rstrip('/'))}/(CodeSystem|ValueSet|ConceptMap)/"
                          r"[A-Za-z0-9._\-]+$")

    def package_name(self, project_id: str) -> str:
        prefix = self.package_prefix
        if not prefix:
            host = re.sub(r"^https?://", "", self.base_url).split("/")[0].split(":")[0]
            prefix = ".".join(reversed(host.split(".")))
        name = f"{prefix}.{project_id}".lower()
        return re.sub(r"[^a-z0-9.\-]", "-", name)


class Classification(BaseModel):
    key: str
    label: str
    confidentiality: str = Field(description="Kod i v3-Confidentiality: U, L, M, N, R, V")
    rank: int
    description: str = ""


class Target(BaseModel):
    key: str
    label: str
    kind: Literal["fhir-api", "package-dir"]
    base_url: str | None = Field(None, description="För fhir-api; tomt = TX_BASE_URL")
    directory: str | None = Field(None, description="För package-dir: katalog som inläsningen läser paket från")
    max_classification: str = "öppen"


class Editorial(BaseModel):
    review_manual_edits: bool = False
    four_eyes_release: bool = True
    block_rc_with_open_proposals: bool = True


class Settings(BaseModel):
    namespaces: list[Namespace] = [
        Namespace(key="exempel", label="Exempel (ändra i settings.json)", base_url="http://example.org/fhir",
                  publisher="Exempelorganisationen"),
    ]
    allow_custom_namespace: bool = True
    classifications: list[Classification] = [
        Classification(key="öppen", label="Öppen", confidentiality="U", rank=0,
                       description="Får spridas fritt, t.ex. till publika testservrar."),
        Classification(key="intern", label="Intern", confidentiality="N", rank=1,
                       description="Får spridas inom vård och omsorg och till avtalade parter."),
        Classification(key="begränsad", label="Begränsad", confidentiality="R", rank=2,
                       description="Får bara spridas till namngivna mottagare (t.ex. licensvillkor)."),
    ]
    default_classification: str = "intern"
    targets: list[Target] = [
        Target(key="tx", label="Terminologitjänsten (TX_BASE_URL)", kind="fhir-api", max_classification="intern"),
    ]
    editorial: Editorial = Editorial()

    @classmethod
    def load(cls, root: str | Path) -> "Settings":
        path = Path(root) / "settings.json"
        if path.exists():
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        return cls()

    def save(self, root: str | Path) -> None:
        (Path(root) / "settings.json").write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def namespace(self, key: str | None) -> Namespace | None:
        return next((n for n in self.namespaces if n.key == key), None)

    def classification(self, key: str | None) -> Classification:
        hit = next((c for c in self.classifications if c.key == key), None)
        return hit or next(c for c in self.classifications if c.key == self.default_classification)

    def target(self, key: str) -> Target | None:
        return next((t for t in self.targets if t.key == key), None)

    def allowed(self, classification: str | None, target: Target) -> bool:
        return self.classification(classification).rank <= self.classification(target.max_classification).rank

    def security_label(self, classification: str | None) -> dict:
        c = self.classification(classification)
        return {"system": CONFIDENTIALITY, "code": c.confidentiality,
                "display": CONFIDENTIALITY_DISPLAY.get(c.confidentiality, c.confidentiality)}
