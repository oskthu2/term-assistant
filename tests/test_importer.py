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
    assert roles["Giltig från"] == (ColumnRole.PROPERTY, "effectiveDate")
    assert roles["Kommentar till begrepp"] == (ColumnRole.PROPERTY, "comment")
    assert roles["Tom"][0] == ColumnRole.IGNORE
    # "Aktiv?" matchar inget i registret -> ny boolesk egenskap, flaggad som osäker
    assert roles["Aktiv?"][0] == ColumnRole.PROPERTY
    assert any(p.code == "aktiv" and p.type == "boolean" for p in proposal.new_properties)
    assert any("Aktiv?" in q for q in proposal.questions)
    assert proposal.template.validate_template() == []


def test_registry_converges_after_learning(workbook):
    registry = PropertyRegistry.default()
    registry.learn("Aktiv?", "inactive")  # terminologen bestämmer: mappa mot befintlig egenskap
    sheet = read_workbook(workbook)[0]
    proposal = propose_mapping(sheet, registry, META)
    choice = next(c for c in proposal.choices if c.source_column == "Aktiv?")
    assert choice.registry_key == "inactive"
    assert choice.confidence >= 0.95
    assert not proposal.new_properties


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
    assert props["aktiv"]["valueBoolean"] is True
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
    assert by_col["Aktiv?"].registry_key == "status"        # AI:ns beslut gäller
    assert by_col["Kod"].role == ColumnRole.CODE            # heuristiken fyller luckor
    assert [c.source_column for c in refined.choices] == sheet.headers
    assert refined.questions == ai.questions
