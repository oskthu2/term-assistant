"""Importplan för en hel arbetsbok: bedömning + en mall per kodverk/familj/urval.

Planen kombinerar bladens mallförslag med det som bedömningen hittat i
resten av filen:

* familjer av likadana blad -> en gemensam mall
* katalog i dokumentationsblad -> namn, titel, OID och ägare per kodverk
* revisionshistorik -> mallens historik och version
* regler i löptext -> mallens releasepolicy (t.ex. inga borttag)
* textkataloger -> designations med textroller som eget kodverk
* urval ur externa kodsystem -> ValueSet-mallar
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from .assess import SheetKind, Verdict, WorkbookAssessment, assess_workbook, match_label
from .mapping import CodeSystemMeta, ColumnRole, GeneratedCodeSystem, GeneratedConcept, TextCatalogue, TextColumn
from .registry import PropertyRegistry
from .suggest import MappingProposal, family_prefix, propose_mapping, sheet_source, to_constant
from .workbook import Sheet


class ImportPlan(BaseModel):
    assessment: WorkbookAssessment
    proposals: list[MappingProposal] = []

    @property
    def templates(self):
        return [p.template for p in self.proposals]


def plan_import(
    path: str | Path,
    base_url: str,
    registry: PropertyRegistry,
    sheet: str | None = None,
    language: str = "sv",
    sheets: list[Sheet] | None = None,
) -> tuple[ImportPlan, list[Sheet]]:
    assessment, sheets = assess_workbook(path, sheets)
    by_name = {s.name: s for s in sheets}
    base = base_url.rstrip("/")
    plan = ImportPlan(assessment=assessment)
    if assessment.verdict == Verdict.REJECT:
        return plan, sheets

    done: set[str] = set()
    for a in assessment.sheets:
        if a.sheet in done or a.verdict not in (Verdict.ACCEPT, Verdict.WARN):
            continue
        if a.kind == SheetKind.REFERENCE:
            continue
        members = assessment.families.get(a.family, [a.sheet]) if a.family else [a.sheet]
        if sheet and sheet not in members:
            continue
        done.update(members)
        family = [by_name[m] for m in members]
        first = family[0]

        if re.fullmatch(r"(Tabell|Sida|Blad|Sheet|Kolumn)\s*\d+", first.name):
            generic_name = first.name
        else:
            generic_name = None

        if a.kind == SheetKind.SUBSET:
            name = re.sub(r"\W+", "_", first.name).strip("_")
            meta = CodeSystemMeta(url=f"{base}/ValueSet/{name}", name=name, title=first.name, language=language,
                                  publisher=assessment.publisher)
            proposal = propose_mapping(first, registry, meta, source_file=str(path))
            if generic_name:
                proposal.questions.insert(0, f"'{generic_name}' saknar namn i källan – vad ska urvalet heta?")
            for rel in assessment.relations:
                if rel.subset == first.name:
                    proposal.questions += [f"Jämfört med '{rel.superset}': {h}" for h in rel.hints[:10]]
            plan.proposals.append(proposal)
            continue

        if len(family) > 1:
            prefix = a.family or family_prefix([sheet_source(s).name for s in family])
            meta = CodeSystemMeta(url=f"{base}/CodeSystem", name=prefix, title=prefix, language=language,
                                  publisher=assessment.publisher)
        else:
            src = sheet_source(first)
            meta = CodeSystemMeta(url=f"{base}/CodeSystem/{src.name}", name=src.name, title=src.title or src.name,
                                  language=language, publisher=assessment.publisher)
        proposal = propose_mapping(first, registry, meta, source_file=str(path),
                                   family=family if len(family) > 1 else None)
        _enrich(proposal, assessment, base, family)
        if generic_name:
            proposal.questions.insert(0, f"'{generic_name}' saknar namn i källan – vad ska kodverket heta?")
        plan.proposals.append(proposal)
    return plan, sheets


def _enrich(proposal: MappingProposal, wa: WorkbookAssessment, base: str, family: list[Sheet]) -> None:
    tpl = proposal.template
    # Katalog: OID, ägare och titel per kodverk.
    sources = tpl.source.sheets or []
    for src in sources:
        entry = wa.catalogue.get(src.name)
        if entry:
            src.identifier = src.identifier or entry.identifier
            src.publisher = src.publisher or entry.publisher
            src.title = src.title or entry.title
    missing_oid = [s.name for s in sources if wa.catalogue.get(s.name) and not s.identifier]
    if missing_oid:
        proposal.questions.append(f"OID saknas för {len(missing_oid)} kodverk ({', '.join(missing_oid[:4])}"
                                  f"{' …' if len(missing_oid) > 4 else ''}). Ska de tilldelas före publicering?")

    # Historik och version.
    if wa.history:
        tpl.history = wa.history
        latest = str(wa.history[-1].get("Version") or wa.history[-1].get("version") or "")
        if re.fullmatch(r"\d+(\.\d+){0,2}", latest):
            parts = (latest.split(".") + ["0", "0"])[:3]
            tpl.codesystem.version = ".".join(parts)

    # Releasepolicy ur löptext.
    if not wa.policy.allow_code_removal:
        tpl.policy = wa.policy.model_copy()
        proposal.questions.append(f"Källan säger att koder inte får tas bort (\"{wa.policy.rationale}\"). "
                                  "Releaser som tar bort koder spärras – stämmer det?")
    if wa.documentation:
        proposal.questions.append("Dokumentationsbladen (" + ", ".join(wa.documentation)
                                  + ") innehåller löptext. Ska något bli description/purpose i kodverken?")

    # Textkatalog som pekar på familjens kodverk.
    names = {s.name for s in sources} or {tpl.codesystem.name}
    for cat in wa.text_catalogues:
        if not set(cat.targets) & names or not cat.text_columns:
            continue
        derived = {c.source_column: c.pattern for c in tpl.columns if c.role == ColumnRole.DERIVED and c.pattern}
        concepts, texts = [], []
        for col in cat.text_columns:
            label = re.sub(r"^id\s+", "", col, flags=re.I)
            hit = match_label(label, [re.sub(r"^id\s+", "", d, flags=re.I) for d in derived])
            pattern = next((p for d, p in derived.items() if re.sub(r"^id\s+", "", d, flags=re.I) == hit), None)
            suffix = pattern.rsplit(".", 1)[-1] if pattern and "{" not in pattern.rsplit(".", 1)[-1] else None
            code = suffix or to_constant(col)
            concepts.append(GeneratedConcept(code=code, display=col, source_column=col))
            texts.append(TextColumn(column=col, use_code=code))
        prefix = family_prefix(sorted(names)) if len(names) > 1 else tpl.codesystem.name
        name = f"{prefix}_TEXTROLL"
        tpl.generated_codesystems.append(GeneratedCodeSystem(
            key="textroll", url=f"{base}/CodeSystem/{name}", name=name, title=f"Textroller ({prefix})",
            purpose="designation-use", concepts=concepts))
        tpl.text_catalogue = TextCatalogue(sheet=cat.sheet, key_column=cat.key_column, use_codesystem="textroll",
                                           texts=texts)
        if cat.flag_columns:
            proposal.questions.append(f"Kolumnerna {', '.join(cat.flag_columns)} i '{cat.sheet}' anger bara om en "
                                      "text finns och läses inte in – stämmer det?")
