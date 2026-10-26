"""Livscykel och förvaltning: fasta mål i stället för homonyma egenskaper, och metadata om kodverket."""

from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from term_assistant.admin import ops
from term_assistant.admin.store import SheetData
from term_assistant.importer import lifecycle, metadata
from term_assistant.importer.build import build_all
from term_assistant.importer.mapping import ColumnRole
from term_assistant.importer.plan import plan_import
from term_assistant.importer.registry import PropertyRegistry
from term_assistant.importer.workbook import read_workbook

EXAMPLES = Path(__file__).parent.parent / "examples"
BASE = "http://example.org/fhir"


def _own_codesystem(tmp_path):
    import sys
    sys.path.insert(0, str(EXAMPLES))
    from make_examples import make_own_codesystem_workbook
    path = make_own_codesystem_workbook(tmp_path / "eget.xlsx")
    plan, sheets = plan_import(path, BASE, PropertyRegistry.default())
    return plan, sheets


def test_lifecycle_columns_get_fixed_targets_not_properties(tmp_path):
    plan, sheets = _own_codesystem(tmp_path)
    proposal = plan.proposals[0]
    roles = {c.source_column: (c.role, c.registry_key) for c in proposal.choices}
    assert roles["Tillagd"] == (ColumnRole.LIFECYCLE, "lifecycle:effectiveDate")
    assert roles["Borttagetdatum"] == (ColumnRole.LIFECYCLE, "lifecycle:retirementDate")
    assert roles["Status"] == (ColumnRole.LIFECYCLE, "lifecycle:status")
    assert roles["Ersätts av"] == (ColumnRole.LIFECYCLE, "lifecycle:replacedBy")
    assert roles["Senast ändrad"] == (ColumnRole.LIFECYCLE, "lifecycle:changed")
    assert roles["Ändringsorsak"] == (ColumnRole.LIFECYCLE, "lifecycle:changeNote")
    assert not proposal.new_properties and proposal.template.properties == []

    cs = build_all(proposal.template, sheets)[0][0]
    by_code = {c["code"]: c for c in cs["concept"]}
    tel = {p["code"]: list(p.values())[1] for p in by_code["TEL"]["property"]}
    assert tel == {"status": "retired", "inactive": True, "effectiveDate": "2019-01-01",
                   "retirementDate": "2023-12-31"}
    assert by_code["TEL"]["extension"][0]["valueCoding"]["code"] == "DIST"
    assert {"code": "status", "valueCode": "deprecated"} in by_code["GRUPP"]["property"]
    # Förvaltningsuppgifter (senast ändrad, ändringsorsak) exporteras inte.
    assert {p["code"] for p in cs["property"]} == {"status", "inactive", "effectiveDate", "retirementDate"}
    assert all(p["uri"].startswith("http://hl7.org/fhir/concept-properties#") for p in cs["property"])


def test_rules_derive_consistent_state_and_flag_contradictions():
    cols = {"status": "Status", "retirementDate": "Giltig t.o.m.", "active": "Aktiv"}
    on = date(2025, 1, 1)
    assert lifecycle.row_state(cols, {"Giltig t.o.m.": "2024-06-30"}, on).status == "retired"
    assert lifecycle.row_state(cols, {"Giltig t.o.m.": "2026-06-30"}, on).status == "active"   # framtida
    assert lifecycle.row_state(cols, {"Aktiv": "Nej"}, on).status == "retired"
    assert lifecycle.row_state(cols, {"Status": "Gällande"}, on).status == "active"
    st = lifecycle.row_state(cols, {"Status": "Aktiv", "Giltig t.o.m.": "2024-06-30"}, on)
    assert st.problems and "motsäger" in st.problems[0]
    # Värdena avgör syskonmålet: "Avregistrerad" med datum är ett borttagsdatum.
    assert lifecycle.refine_key("inactive", ["2024-01-01", None, "2023-05-01"]) == "retirementDate"
    assert lifecycle.refine_key("retirementDate", ["x", None, "x"]) == "inactive"
    assert lifecycle.parse_date("31.12.2023") == "2023-12-31"
    # R4 och R5 har olika namn på utfasningsdatumet.
    row = {"Utfasad": "2024-01-01"}
    r4 = lifecycle.concept_elements({"deprecationDate": "Utfasad"}, row, "http://x", "R4", on)[0]
    r5 = lifecycle.concept_elements({"deprecationDate": "Utfasad"}, row, "http://x", "R5", on)[0]
    assert {"code": "deprecated", "valueDateTime": "2024-01-01"} in r4
    assert {"code": "deprecationDate", "valueDateTime": "2024-01-01"} in r5


def test_selection_rows_leave_compose_when_removed(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Urval"
    ws.append(["Kod", "Benämning", "Tillagd", "Borttagen", "Ersätts av"])
    ws.append(["NPU03230", "P-Glukos", date(2020, 1, 1), None, None])
    ws.append(["NPU02319", "B-Hemoglobin", date(2020, 1, 1), date(2023, 1, 1), "NPU28309"])
    ws.append(["NPU01685", "P-Kalium", date(2099, 1, 1), None, None])
    path = tmp_path / "urval.xlsx"
    wb.save(path)
    plan, sheets = plan_import(path, BASE, PropertyRegistry.default())
    tpl = plan.proposals[0].template
    assert tpl.kind == "valueset"
    life = {c.source_column: c.lifecycle for c in tpl.columns if c.role == ColumnRole.LIFECYCLE}
    assert life == {"Tillagd": "effectiveDate", "Borttagen": "retirementDate", "Ersätts av": "replacedBy"}
    resources, warnings = build_all(tpl, sheets)
    codes = [c["code"] for inc in resources[0]["compose"]["include"] for c in inc.get("concept", [])]
    assert codes == ["NPU03230"]
    assert any("borttagen" in w and "NPU02319" in w for w in warnings)
    assert any("ännu inte giltiga" in w and "NPU01685" in w for w in warnings)


def test_new_property_for_lifecycle_column_is_refused(tmp_path):
    plan, _ = _own_codesystem(tmp_path)
    tpl = plan.proposals[0].template
    registry = PropertyRegistry.default()
    j = next(i for i, c in enumerate(tpl.columns) if c.source_column == "Tillagd")
    with pytest.raises(ops.OpError, match="förvaltningsinformation"):
        ops.apply_target(tpl, j, "prop:__new__", registry)
    # Vallistan erbjuder de fasta målen, med vad de blir i FHIR.
    groups = dict(ops.target_options(tpl, registry))
    life = dict(groups["Livscykel och förvaltning (fasta mål)"])
    assert "concept-properties#retirementDate" in life["life:retirementDate"]
    # Två kolumner kan inte stå för samma uppgift ...
    with pytest.raises(ops.OpError, match="samma livscykeluppgift"):
        ops.apply_target(tpl, j, "life:changed", registry)
    # ... men byte till ett ledigt mål går bra, och registret lär sig rubriken.
    new = ops.apply_target(tpl, j, "life:deprecationDate", registry)
    assert new.columns[j].lifecycle == "deprecationDate"
    ops.learn_from_template(new, registry)
    assert "tillagd" in registry.get("lifecycle:deprecationDate").aliases
    assert "tillagd" not in registry.get("lifecycle:effectiveDate").aliases


def test_admin_stamps_added_and_changed(tmp_path):
    plan, sheets = _own_codesystem(tmp_path)
    tpl = plan.proposals[0].template
    data = SheetData(headers=sheets[1].headers, rows=[dict(r) for r in sheets[1].rows])
    row = {}
    j = {c.source_column: i for i, c in enumerate(tpl.columns)}
    changes = ops.update_row(tpl, data, row, {f"c{j['Kod']}": "NY", f"c{j['Benämning']}": "Nytt besök"}, True)
    today = date.today().isoformat()
    assert row["Tillagd"] == today and row["Senast ändrad"] == today and "Tillagd" in changes
    with pytest.raises(ops.OpError, match="status"):
        ops.update_row(tpl, data, row, {f"c{j['Kod']}": "NY", f"c{j['Status']}": "kanske"}, False)


def test_metadata_is_harvested_with_sources(tmp_path):
    plan, _ = _own_codesystem(tmp_path)
    meta = plan.proposals[0].template.codesystem
    assert (meta.name, meta.title, meta.version) == ("KV_BESOKSTYP", "Typ av besök", "2.1.0")
    assert meta.publisher.startswith("Exempelregionen") and meta.contact.endswith("<terminologi@example.org>")
    assert meta.identifier == "1.2.752.999.7.1" and meta.copyright.startswith("Fri")
    assert meta.sources["description"] == "fliken 'Om kodverket' (Beskrivning)"
    cs = build_all(plan.proposals[0].template, read_workbook(tmp_path / "eget.xlsx"))[0][0]
    assert cs["identifier"][0]["value"] == "urn:oid:1.2.752.999.7.1"
    assert cs["contact"] == [{"name": "Terminologienheten",
                              "telecom": [{"system": "email", "value": "terminologi@example.org"}]}]


def test_metadata_in_header_rows_and_requirements(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Blad1"
    ws.append(["Kodverk:", "KV_TEST"])
    ws.append(["Version: 3"])
    ws.append(["Ägare:", "Testförvaltningen"])
    ws.append([])
    ws.append(["Kod", "Benämning"])
    ws.append(["A", "Alfa"])
    ws.append(["B", "Beta"])
    path = tmp_path / "huvud.xlsx"
    wb.save(path)
    plan, _ = plan_import(path, BASE, PropertyRegistry.default())
    proposal = plan.proposals[0]
    tpl = proposal.template
    assert (tpl.codesystem.name, tpl.codesystem.version, tpl.codesystem.publisher) == \
        ("KV_TEST", "3.0.0", "Testförvaltningen")
    assert any("beskrivning" in q for q in proposal.questions)
    errors, warnings = metadata.release_errors(tpl, ["publisher", "description"])
    assert errors == ["Metadata saknas: beskrivning (ange under Metadata)."]
    assert any("kontakt" in w for w in warnings) and any("OID" in w for w in warnings)
    metadata.apply_form(tpl, {"meta_version": "x.1", "meta_description": "Test."})
    assert metadata.import_errors(tpl) == ["Versionen 'x.1' ska ha formen X.Y.Z, t.ex. 1.0.0."]
    assert tpl.codesystem.sources["description"] == "angivet vid inläsning"
