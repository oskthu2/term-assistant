from datetime import date

import pytest
from openpyxl import Workbook

from term_assistant.importer.mapping import CodeSystemMeta, ColumnRole
from term_assistant.importer.registry import PropertyRegistry, normalize, split_language
from term_assistant.importer.release import diff, release, release_candidate
from term_assistant.importer.suggest import propose_mapping
from term_assistant.importer.transform import build_codesystem
from term_assistant.importer.workbook import read_workbook

META = CodeSystemMeta(url="http://example.org/cs/test", name="Test")


@pytest.fixture
def workbook(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Blad1"
    ws.append(["Titelrad för kodverket"])
    ws.append([])
    ws.append(["Kod", "Benämning", "Överordnad", "Term (en)", "Synonymer", "Giltig från", "Aktiv?", "Kommentar till begrepp", "Tom"])
    ws.append(["A", "Alfa", None, "Alpha", None, date(2020, 1, 1), "Ja", None])
    ws.append(["A1", "Alfa ett", "A", "Alpha one", "A-ett;Första", date(2020, 1, 1), "Ja", "Notering"])
    ws.append(["A2", "Alfa två", "A", "Alpha two", None, date(2021, 1, 1), "Nej", None])
    path = tmp_path / "kodverk.xlsx"
    wb.save(path)
    return path


def test_normalize_and_language():
    assert normalize("  Giltig fr.o.m. ") == "giltig fr o m"
    assert normalize("Anmärkning") == "anmarkning"
    assert split_language("Benämning (en)") == ("benamning", "en")
    assert split_language("Term engelska") == ("term", "en")
    assert split_language("Svenska") == ("svenska", None)


def test_header_detection(workbook):
    sheet = read_workbook(workbook)[0]
    assert sheet.header_row == 3
    assert sheet.headers[0] == "Kod"
    assert sheet.total_rows == 3
    assert sheet.preamble == ["Titelrad för kodverket"]


def test_proposal_roles(workbook):
    sheet = read_workbook(workbook)[0]
    proposal = propose_mapping(sheet, PropertyRegistry.default(), META)
    roles = {c.source_column: (c.role, c.registry_key or c.new_property_code or c.language) for c in proposal.choices}
    assert roles["Kod"][0] == ColumnRole.CODE
    assert roles["Benämning"][0] == ColumnRole.DISPLAY
    assert roles["Överordnad"][0] == ColumnRole.PARENT
    assert roles["Term (en)"] == (ColumnRole.DESIGNATION, "en")
    assert roles["Synonymer"] == (ColumnRole.DESIGNATION, "designation:synonym")
    # Förvaltningskolumner får fasta livscykelmål – aldrig egna egenskaper.
    assert roles["Giltig från"] == (ColumnRole.LIFECYCLE, "lifecycle:effectiveDate")
    assert roles["Aktiv?"] == (ColumnRole.LIFECYCLE, "lifecycle:active")
    assert roles["Kommentar till begrepp"] == (ColumnRole.PROPERTY, "comment")
    assert roles["Tom"][0] == ColumnRole.IGNORE
    assert not proposal.new_properties
    assert proposal.template.validate_template() == []


def test_registry_converges_after_learning(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Kod", "Benämning", "Ur bruk sedan"])
    ws.append(["A", "Alfa", None])
    ws.append(["B", "Beta", date(2023, 5, 1)])
    path = tmp_path / "ny.xlsx"
    wb.save(path)
    sheet = read_workbook(path)[0]
    registry = PropertyRegistry.default()
    before = next(c for c in propose_mapping(sheet, registry, META).choices if c.source_column == "Ur bruk sedan")
    assert before.role == ColumnRole.PROPERTY and before.new_property_code  # okänd rubrik -> ny egenskap
    registry.learn("Ur bruk sedan", "lifecycle:retirementDate")  # terminologen bestämmer en gång
    proposal = propose_mapping(sheet, registry, META)
    choice = next(c for c in proposal.choices if c.source_column == "Ur bruk sedan")
    assert (choice.role, choice.registry_key) == (ColumnRole.LIFECYCLE, "lifecycle:retirementDate")
    assert choice.confidence >= 0.95
    assert not proposal.new_properties
    b = next(c for c in build_codesystem(proposal.template, sheet.rows)[0]["concept"] if c["code"] == "B")
    assert {p["code"]: list(p.values())[1] for p in b["property"]} == {
        "status": "retired", "inactive": True, "retirementDate": "2023-05-01"}


def test_old_registry_and_templates_migrate_lifecycle_properties(tmp_path):
    import json

    from term_assistant.importer.mapping import ImportTemplate
    old = {"entries": [{"kind": "property", "key": "inactive", "aliases": ["avslutad"], "usage_count": 3,
                        "property": {"code": "inactive", "type": "boolean"}}]}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(old))
    registry = PropertyRegistry.load(path)
    assert registry.get("inactive") is None
    assert "avslutad" in registry.get("lifecycle:inactive").aliases
    with pytest.raises(ValueError, match="livscykel"):
        from term_assistant.importer.mapping import PropertyDefinition
        registry.add_property(PropertyDefinition(code="retirementDate", type="dateTime"))
    tpl = ImportTemplate.model_validate({
        "codesystem": {"url": "http://x/cs", "name": "X"},
        "columns": [{"source_column": "Kod", "role": "code"},
                    {"source_column": "Ersätts av", "role": "property", "property_code": "replacedBy"}],
        "properties": [{"code": "replacedBy", "type": "code"}]})
    assert tpl.columns[1].role == ColumnRole.LIFECYCLE and tpl.columns[1].lifecycle == "replacedBy"
    assert tpl.properties == [] and tpl.validate_template() == []


def test_registry_rejects_duplicate_and_merges():
    registry = PropertyRegistry.default()
    with pytest.raises(ValueError):
        from term_assistant.importer.mapping import PropertyDefinition
        registry.add_property(PropertyDefinition(code="comment"))
    registry.harvest([{"url": "http://x", "property": [{"code": "anmarkning", "type": "string"}]}])
    assert ("comment", "anmarkning") in [(a, b) for a, b, _ in registry.near_duplicates(0.7)] or \
           ("anmarkning", "comment") in [(a, b) for a, b, _ in registry.near_duplicates(0.7)]
    kept = registry.merge("comment", "anmarkning")
    assert registry.get("anmarkning") is None
    assert "anmarkning" in kept.all_names()


def test_build_codesystem(workbook):
    sheet = read_workbook(workbook)[0]
    proposal = propose_mapping(sheet, PropertyRegistry.default(), META)
    cs, warnings = build_codesystem(proposal.template, sheet.rows)
    assert warnings == []
    assert cs["resourceType"] == "CodeSystem" and cs["count"] == 3
    assert cs["hierarchyMeaning"] == "is-a"
    a1 = next(c for c in cs["concept"] if c["code"] == "A1")
    assert a1["display"] == "Alfa ett"
    props = {p["code"]: p for p in a1["property"]}
    assert props["parent"]["valueCode"] == "A"
    assert props["effectiveDate"]["valueDateTime"] == "2020-01-01"
    assert props["inactive"]["valueBoolean"] is False          # "Aktiv? = Ja" inverteras till inactive
    a2 = {p["code"]: p for p in next(c for c in cs["concept"] if c["code"] == "A2")["property"]}
    assert a2["status"]["valueCode"] == "retired" and a2["inactive"]["valueBoolean"] is True
    declared = {p["code"]: p.get("uri") for p in cs["property"]}
    assert declared["inactive"] == "http://hl7.org/fhir/concept-properties#inactive"
    assert "aktiv" not in declared
    values = [d["value"] for d in a1["designation"]]
    assert values == ["Alpha one", "A-ett", "Första"]


def test_release_flow_and_diff(workbook):
    sheet = read_workbook(workbook)[0]
    proposal = propose_mapping(sheet, PropertyRegistry.default(), META)
    v1, _ = build_codesystem(proposal.template, sheet.rows)
    rc1 = release_candidate(v1, "1.0.0")
    rc2 = release_candidate(v1, "1.0.0", previous_rc=rc1["version"])
    assert (rc1["version"], rc2["version"]) == ("1.0.0-rc.1", "1.0.0-rc.2")
    released = release(rc2)
    assert released["version"] == "1.0.0" and released["status"] == "active"

    v2 = release_candidate(released, "1.1.0")
    v2["concept"] = [c for c in v2["concept"] if c["code"] != "A2"] + [{"code": "B", "display": "Beta"}]
    v2["concept"][0]["display"] = "Alfa (ändrad)"
    d = diff(released, v2)
    assert d["added"] == ["B"] and d["removed"] == ["A2"]
    assert d["changed"] == [{"code": "A", "fields": ["display"]}]


def test_release_retargets_references_to_the_candidate_version():
    cm = {"resourceType": "ConceptMap", "version": "2.2.0-rc.1", "status": "draft",
          "sourceCanonical": "http://x/vs|2.2.0-rc.1",
          "group": [{"source": "http://x/cs", "sourceVersion": "2.2.0-rc.1", "targetVersion": "2.2.0-rc.1",
                     "element": [{"code": "2.2.0-rc.1", "target": [{"code": "B"}]}]}]}
    released = release(cm)
    group = released["group"][0]
    assert (released["version"], group["sourceVersion"], group["targetVersion"]) == ("2.2.0",) * 3
    assert released["sourceCanonical"] == "http://x/vs|2.2.0"
    assert group["element"][0]["code"] == "2.2.0-rc.1"  # koder rörs inte


def test_negation_is_never_a_match():
    from term_assistant.importer.registry import is_negation
    assert is_negation("aktiv", "inaktiv")
    assert is_negation("valbar", "ej valbar")
    assert is_negation("giltig", "ogiltig")
    assert not is_negation("kommentar", "kommentarer")
    assert all(c.entry.key != "inactive" for c in PropertyRegistry.default().match("Aktiv"))


def test_ai_refinement_with_stub_client(workbook):
    from types import SimpleNamespace

    from term_assistant.importer.ai import AiMapping, refine_with_claude
    from term_assistant.importer.suggest import ColumnChoice

    sheet = read_workbook(workbook)[0]
    registry = PropertyRegistry.default()
    proposal = propose_mapping(sheet, registry, META)
    ai = AiMapping(
        columns=[ColumnChoice(source_column="Aktiv?", role=ColumnRole.PROPERTY, registry_key="status",
                              confidence=0.7, rationale="Ja/Nej om begreppet är aktivt"),
                 ColumnChoice(source_column="Finns inte", role=ColumnRole.IGNORE, confidence=1, rationale="-")],
        questions=["Ska 'Aktiv?' uttryckas som status eller inactive?"],
    )
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=ai)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)))
    refined = refine_with_claude(proposal, sheet, registry, META, client=client)
    assert calls[0]["output_format"] is AiMapping
    by_col = {c.source_column: c for c in refined.choices}
    assert "Finns inte" not in by_col                       # okända kolumner filtreras bort
    # AI:n föreslog egenskapen status – livscykelregeln rättar till rätt mål (Aktiv ja/nej).
    assert (by_col["Aktiv?"].role, by_col["Aktiv?"].registry_key) == (ColumnRole.LIFECYCLE, "lifecycle:active")
    assert by_col["Kod"].role == ColumnRole.CODE            # heuristiken fyller luckor
    assert [c.source_column for c in refined.choices] == sheet.headers
    assert refined.questions == ai.questions
