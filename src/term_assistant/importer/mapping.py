"""Datamodellen för inläsningsmallen.

Samma mall används i två steg:

1. **Inläsning** – den beskriver hur varje kolumn i Excel-arket ska tolkas
   och översättas till FHIR (CodeSystem, eller ValueSet för urval ur ett
   befintligt kodsystem).
2. **Administration** – den blir specifikationen för den skräddarsydda
   administrationsvyn: vilka kolumner som visas, i vilken ordning, med
   vilken widget, och vilken FHIR-egenskap varje kolumn står för. Att byta
   roll på en kolumn eller lägga till en ny kolumn i vyn är en ändring av
   mallen, inte av koden.

En mall kan gälla en *familj* av blad med samma struktur (t.ex. elva flikar
KV_EX_0001, KV_EX_0002 … i samma arbetsbok). Varje blad blir då ett eget kodverk.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ColumnRole(str, Enum):
    CODE = "code"                # CodeSystem.concept.code
    DISPLAY = "display"          # CodeSystem.concept.display
    DEFINITION = "definition"    # CodeSystem.concept.definition
    DESIGNATION = "designation"  # CodeSystem.concept.designation (synonym, översättning …)
    PROPERTY = "property"        # CodeSystem.concept.property -> CodeSystem.property.code
    PARENT = "parent"            # Hierarki: concept.property 'parent'
    CONTEXT = "context"          # Markeringskolumn (x) under en grupprubrik, t.ex. "Används av"
    DERIVED = "derived"          # Värdet kan härledas ur koden, t.ex. "KV_X.{code}.RBK"
    GROUP = "group"              # Gruppering i ett urval -> ett ValueSet per grupp
    IGNORE = "ignore"            # Läses inte in


PropertyType = Literal["code", "Coding", "string", "integer", "boolean", "dateTime", "decimal"]


class DesignationTarget(BaseModel):
    """Vart en designation-kolumn hamnar."""

    language: str | None = Field(None, description="BCP-47, t.ex. 'sv' eller 'en'")
    use_system: str | None = Field(None, description="Coding.system för designation.use")
    use_code: str | None = Field(None, description="Coding.code för designation.use, t.ex. 900000000000013009 (synonym)")
    use_display: str | None = None


class ContextTarget(BaseModel):
    """En markeringskolumn som betyder "begreppet används i kontext X".

    ``mode='designation'``: en designation med use = (kontextkodverk, code) och
    värdet från ``text_column`` (standard: displaykolumnen). Det gör att texten
    kan skilja sig mellan kontexter.
    ``mode='property'``: en upprepad Coding-egenskap som pekar på kontexten.
    """

    codesystem: str = Field(description="Nyckel för det genererade kontextkodverket")
    code: str
    mode: Literal["designation", "property"] = "designation"
    text_column: str | None = None


class UiHints(BaseModel):
    """Presentationsinställningar för administrationsvyn."""

    label: str
    order: int = 0
    visible: bool = True
    editable: bool = True
    widget: Literal["text", "textarea", "select", "checkbox", "date", "number", "concept-picker"] = "text"
    options: list[str] | None = Field(None, description="Tillåtna värden för select-widget")
    group: str | None = Field(None, description="Grupprubrik i vyn, t.ex. 'Används av'")


class ColumnMapping(BaseModel):
    source_column: str = Field(description="Kolumnrubrik i källfilen (eller namn på en ny kolumn i vyn)")
    role: ColumnRole
    property_code: str | None = Field(None, description="CodeSystem.property.code när role=property")
    designation: DesignationTarget | None = Field(None, description="När role=designation")
    context: ContextTarget | None = Field(None, description="När role=context")
    pattern: str | None = Field(None, description="När role=derived, t.ex. '{codesystem}.{code}.RBK'")
    confidence: float = Field(1.0, ge=0, le=1)
    rationale: str = ""
    note: str | None = Field(None, description="Fotnot kopplad till kolumnen i källan")
    ui: UiHints | None = None


class PropertyDefinition(BaseModel):
    """Motsvarar CodeSystem.property."""

    code: str
    uri: str | None = None
    type: PropertyType = "string"
    description: str | None = None


class GeneratedConcept(BaseModel):
    code: str
    display: str
    source_column: str | None = None


class GeneratedCodeSystem(BaseModel):
    """Ett litet kodverk som skapas av kolumnetiketter.

    Används för användningskontexter ("Används av" -> intygstyper) och för
    textroller (rubriktext, hjälptext). Koderna kan användas som
    ``designation.use`` eller som Coding-egenskap.
    """

    key: str
    url: str
    name: str
    title: str
    purpose: Literal["usage-context", "designation-use"] = "usage-context"
    property_code: str | None = Field(None, description="Egenskapskod när kontexten uttrycks som property")
    concepts: list[GeneratedConcept]


class CodeSystemMeta(BaseModel):
    url: str
    name: str
    title: str | None = None
    version: str = "0.1.0"
    publisher: str | None = None
    language: str = "sv"
    case_sensitive: bool = True
    hierarchy_meaning: Literal["grouped-by", "is-a", "part-of", "classified-with"] | None = None
    description: str | None = None
    purpose: str | None = None


class SheetSource(BaseModel):
    """Ett blad i en familj och de metadata som hör till just det kodverket."""

    sheet: str
    name: str
    title: str | None = None
    url: str | None = None
    identifier: str | None = Field(None, description="OID/UUID, t.ex. 'urn:oid:1.2.752…'")
    publisher: str | None = None


class SourceInfo(BaseModel):
    file: str | None = None
    sheet: str | None = None
    header_row: int = 1
    sheets: list[SheetSource] = Field(default_factory=list, description="Alla blad i familjen")


class ReleasePolicy(BaseModel):
    allow_code_removal: bool = True
    allow_display_change: bool = True
    rationale: str = ""


class TextColumn(BaseModel):
    column: str
    use_code: str


class TextCatalogue(BaseModel):
    """Ett blad med texter som nycklas på kodverk + kod, t.ex. 'KV_X.KOD'."""

    sheet: str
    key_column: str
    key_pattern: str = "{codesystem}.{code}"
    use_codesystem: str = Field(description="Nyckel för det genererade textrollskodverket")
    texts: list[TextColumn]


class ImportTemplate(BaseModel):
    """Inläsningsmall och tillika specifikation för administrationsvyn."""

    template_version: int = 2
    kind: Literal["codesystem", "valueset"] = "codesystem"
    codesystem: CodeSystemMeta
    source: SourceInfo = SourceInfo()
    columns: list[ColumnMapping]
    properties: list[PropertyDefinition] = []
    generated_codesystems: list[GeneratedCodeSystem] = []
    external_systems: dict[str, str] = Field(
        default_factory=dict, description="För urval: extern systemnyckel -> canonical URL")
    text_catalogue: TextCatalogue | None = None
    policy: ReleasePolicy = ReleasePolicy()
    history: list[dict] = []

    def column(self, role: ColumnRole) -> ColumnMapping | None:
        return next((c for c in self.columns if c.role == role), None)

    def generated(self, key: str) -> GeneratedCodeSystem | None:
        return next((g for g in self.generated_codesystems if g.key == key), None)

    def validate_template(self) -> list[str]:
        """Returnera en lista med problem (tom lista = giltig mall)."""
        problems: list[str] = []
        codes = [c for c in self.columns if c.role == ColumnRole.CODE]
        if len(codes) != 1:
            problems.append(f"Mallen måste ha exakt en kodkolumn, har {len(codes)}.")
        if sum(c.role == ColumnRole.DISPLAY for c in self.columns) > 1:
            problems.append("Mallen får ha högst en display-kolumn; övriga bör vara designations.")
        known = {p.code for p in self.properties}
        for col in self.columns:
            if col.role == ColumnRole.PROPERTY and col.property_code not in known:
                problems.append(f"Kolumn '{col.source_column}' pekar på okänd egenskap '{col.property_code}'.")
            if col.role == ColumnRole.DESIGNATION and col.designation is None:
                problems.append(f"Kolumn '{col.source_column}' saknar designation-mål (språk/use).")
            if col.role == ColumnRole.CONTEXT:
                gen = self.generated(col.context.codesystem) if col.context else None
                if gen is None or col.context.code not in {c.code for c in gen.concepts}:
                    problems.append(f"Kolumn '{col.source_column}' pekar på en okänd kontext.")
            if col.role == ColumnRole.DERIVED and not col.pattern:
                problems.append(f"Kolumn '{col.source_column}' saknar härledningsmönster.")
        used = [c.property_code for c in self.columns if c.role == ColumnRole.PROPERTY]
        for dup in {p for p in used if used.count(p) > 1}:
            problems.append(f"Flera kolumner mappas mot samma egenskap '{dup}'.")
        if self.kind == "valueset":
            for key, uri in self.external_systems.items():
                if not uri or uri.startswith("urn:term-assistant:unresolved"):
                    problems.append(f"Canonical URL för kodsystemet '{key}' är inte angiven.")
        return problems
