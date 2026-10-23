"""AI-granskning av mappningsförslag med Claude.

Modellen får bladets rubriker, ett stickprov av rader, kolumnprofiler,
egenskapsregistret och heuristikens förslag. Den svarar med strukturerad
output (``ColumnChoice`` per kolumn) och måste i första hand välja bland
registrets poster – nya egenskaper accepteras bara med motivering.

Kräver paketet ``anthropic`` (``pip install term-assistant[ai]``) och
autentisering via ``ANTHROPIC_API_KEY`` eller ``ant auth login``.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from .mapping import CodeSystemMeta, SourceInfo
from .registry import PropertyRegistry
from .suggest import ColumnChoice, MappingProposal, build_template
from .workbook import Sheet

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """\
Du hjälper en terminolog att migrera kodverk från Excel till FHIR CodeSystem.
Du får ett blad (rubriker, stickprov, kolumnprofiler), ett egenskapsregister och
ett heuristiskt förslag. Bestäm för varje kolumn vilken roll den har:

- code: CodeSystem.concept.code (exakt en kolumn)
- display: CodeSystem.concept.display på kodverkets huvudspråk (högst en)
- definition: CodeSystem.concept.definition
- designation: synonymer, översättningar, fullständiga namn. Ange registry_key
  för designation-typen om den finns och language (BCP-47) om kolumnen är på
  ett annat språk.
- property: övriga egenskaper. Ange registry_key för en befintlig egenskap.
- parent: överordnad kod (hierarki)
- ignore: tomma kolumner, löpnummer, interna anteckningar utan värde för kodverket

Målet är att konvergera mot ett litet antal egenskaper som återanvänds mellan
kodverk. Välj därför en befintlig registerpost när betydelsen är densamma, även
om rubriken är formulerad annorlunda. Föreslå new_property_code (lowerCamelCase)
bara när ingen post passar, och förklara då varför i rationale. Kolumner som är
synonymer till varandra ska inte bli två olika egenskaper.

Sätt confidence lågt när du är osäker och lägg en konkret fråga till
terminologen i questions. Svara på svenska."""


class AiMapping(BaseModel):
    columns: list[ColumnChoice]
    questions: list[str]


def refine_with_claude(
    proposal: MappingProposal,
    sheet: Sheet,
    registry: PropertyRegistry,
    meta: CodeSystemMeta,
    source_file: str | None = None,
    sample_rows: int = 15,
    client=None,
) -> MappingProposal:
    import anthropic

    client = client or anthropic.Anthropic()
    payload = {
        "sheet": sheet.name,
        "preamble": sheet.preamble,
        "headers": sheet.headers,
        "sample_rows": sheet.sample(sample_rows),
        "total_rows": sheet.total_rows,
        "profiles": [p.model_dump(exclude_none=True) for p in proposal.profiles],
        "heuristic_proposal": [c.model_dump(exclude_none=True) for c in proposal.choices],
        "heuristic_questions": proposal.questions,
    }
    user = (
        "## Egenskapsregister\n" + registry.summary_for_prompt()
        + "\n\n## Blad\n```json\n" + json.dumps(payload, ensure_ascii=False, default=str, indent=1) + "\n```"
    )
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "medium"},
        output_format=AiMapping,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        proposal.questions.append("AI-granskningen gav inget svar; förslaget bygger enbart på heuristik.")
        return proposal

    ai = response.parsed_output
    known = set(sheet.headers)
    choices = [c for c in ai.columns if c.source_column in known]
    # Kolumner som modellen missat behåller heuristikens beslut.
    covered = {c.source_column for c in choices}
    choices += [c for c in proposal.choices if c.source_column not in covered]
    order = {h: i for i, h in enumerate(sheet.headers)}
    choices.sort(key=lambda c: order[c.source_column])
    # Ogiltiga registernycklar behandlas som förslag på ny egenskap.
    for c in choices:
        if c.registry_key and registry.get(c.registry_key) is None:
            c.new_property_code = c.new_property_code or c.registry_key
            c.registry_key = None

    template, new_props = build_template(
        choices, proposal.profiles, registry, meta,
        SourceInfo(file=source_file, sheet=sheet.name, header_row=sheet.header_row),
    )
    return proposal.model_copy(update={
        "template": template, "choices": choices, "questions": ai.questions, "new_properties": new_props,
    })
