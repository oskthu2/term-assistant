"""Datamodellen för inläsningsmallen.

Samma mall används i två steg:

1. **Inläsning** – den beskriver hur varje kolumn i Excel-arket ska tolkas
   och översättas till ett FHIR CodeSystem.
2. **Administration** – den blir specifikationen för den skräddarsydda
   administrationsvyn: vilka kolumner som visas, i vilken ordning, med
   vilken widget, och vilken FHIR-egenskap varje kolumn står för. Att byta
   roll på en kolumn eller lägga till en ny kolumn i vyn är en ändring av
   mallen, inte av koden.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ColumnRole(str, Enum):
    CODE = "code"                # CodeSystem.concept.code
    DISPLAY = "display"          # CodeSystem.concept.display
    DEFINITION = "definition"    # CodeSystem.concept.definition
    DESIGNATION = "designation"  # CodeSystem.concept.designation (synonym, översättning ...)
    PROPERTY = "property"        # CodeSystem.concept.property -> CodeSystem.property.code
    PARENT = "parent"            # Hierarki: concept.property 'parent'
    IGNORE = "ignore"            # Läses inte in


PropertyType = Literal["code", "Coding", "string", "integer", "boolean", "dateTime", "decimal"]


class DesignationTarget(BaseModel):
    """Vart en designation-kolumn hamnar."""

    language: str | None = Field(None, description="BCP-47, t.ex. 'sv' eller 'en'")
    use_system: str | None = Field(None, description="Coding.system för designation.use")
    use_code: str | None = Field(None, description="Coding.code för designation.use, t.ex. 900000000000013009 (synonym)")
    use_display: str | None = None


class UiHints(BaseModel):
    """Presentationsinställningar för administrationsvyn."""

    label: str
    order: int = 0
    visible: bool = True
    editable: bool = True
    widget: Literal["text", "textarea", "select", "checkbox", "date", "number", "concept-picker"] = "text"
    options: list[str] | None = Field(None, description="Tillåtna värden för select-widget")


class ColumnMapping(BaseModel):
    source_column: str = Field(description="Kolumnrubrik i källfilen (eller namn på en ny kolumn i vyn)")
    role: ColumnRole
    property_code: str | None = Field(None, description="CodeSystem.property.code när role=property")
    designation: DesignationTarget | None = Field(None, description="När role=designation")
    confidence: float = Field(1.0, ge=0, le=1)
    rationale: str = ""
    ui: UiHints | None = None


class PropertyDefinition(BaseModel):
    """Motsvarar CodeSystem.property."""

    code: str
    uri: str | None = None
    type: PropertyType = "string"
    description: str | None = None


class CodeSystemMeta(BaseModel):
    url: str
    name: str
    title: str | None = None
    version: str = "0.1.0"
    publisher: str | None = None
    language: str = "sv"
    case_sensitive: bool = True
    hierarchy_meaning: Literal["grouped-by", "is-a", "part-of", "classified-with"] | None = None


class SourceInfo(BaseModel):
    file: str | None = None
    sheet: str | None = None
    header_row: int = 1


class ImportTemplate(BaseModel):
    """Inläsningsmall och tillika specifikation för administrationsvyn."""

    template_version: int = 1
    codesystem: CodeSystemMeta
    source: SourceInfo = SourceInfo()
    columns: list[ColumnMapping]
    properties: list[PropertyDefinition] = []

    def column(self, role: ColumnRole) -> ColumnMapping | None:
        return next((c for c in self.columns if c.role == role), None)

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
        used = [c.property_code for c in self.columns if c.role == ColumnRole.PROPERTY]
        for dup in {p for p in used if used.count(p) > 1}:
            problems.append(f"Flera kolumner mappas mot samma egenskap '{dup}'.")
        return problems
