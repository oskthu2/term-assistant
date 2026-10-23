import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from make_examples import make_family_workbook, make_lab_workbook, make_list_pdf  # noqa: E402

from term_assistant.importer.assess import SheetKind, Verdict, assess_workbook  # noqa: E402
from term_assistant.importer.build import build_all  # noqa: E402
from term_assistant.importer.external import classify_code, detect  # noqa: E402
from term_assistant.importer.mapping import ColumnRole  # noqa: E402
from term_assistant.importer.plan import plan_import  # noqa: E402
from term_assistant.importer.registry import PropertyRegistry  # noqa: E402
from term_assistant.importer.release import check_policy, diff  # noqa: E402
from term_assistant.importer.subset import validate_valueset_codes  # noqa: E402
from term_assistant.importer.workbook import read_workbook  # noqa: E402

BASE = "http://example.org/fhir"


@pytest.fixture
def family(tmp_path):
    return make_family_workbook(tmp_path / "familj.xlsx")


@pytest.fixture
def lab(tmp_path):
    return make_lab_workbook(tmp_path / "lab.xlsx")


# -- kvalitetsgrind ---------------------------------------------------------------


def test_list_without_codes_is_rejected_with_tips(tmp_path):
    pytest.importorskip("reportlab")
    pytest.importorskip("pdfplumber")
    wa, sheets = assess_workbook(make_list_pdf(tmp_path / "lista.pdf"))
    assert wa.verdict == Verdict.REJECT
    assert [s.kind for s in wa.sheets] == [SheetKind.UNUSABLE] * 3
    assert "Ingen kodkolumn" in wa.sheets[0].reasons[0]
    tips = "\n".join(wa.tips)
    assert "En kod per rad" in tips
    assert "3 separata listor" in tips
    assert "Excel eller CSV" in tips
    plan, _ = plan_import(tmp_path / "lista.pdf", BASE, PropertyRegistry.default(), sheets=sheets)
    assert plan.proposals == []


def test_lab_subsets_reference_and_typo(lab):
    wa, _ = assess_workbook(lab)
    kinds = {s.sheet: s.kind for s in wa.sheets}
    assert kinds == {"Urval": SheetKind.SUBSET, "Grupperad": SheetKind.SUBSET, "Nomenklatur": SheetKind.REFERENCE}
    urval = wa.sheet("Urval")
    assert {s.key for s in urval.external.systems} == {"npu", "swe-npu"}
    assert any("NPU1059" in w for w in urval.warnings)
    hints = [h for r in wa.relations for h in r.hints]
    assert any("'NPU1059'" in h and "'NPU10599'" in h for h in hints)
    # Rubriken ovanför tabellen blir inte en datarad i referenslistan.
    ref = next(s for s in read_workbook(lab) if s.name == "Nomenklatur")
    assert ref.headers == ["NPU/SWE-kod", "Trivialnamn"] and ref.preamble == ["Nomenklatur Kemi"]


def test_lab_plan_builds_grouped_valuesets(lab):
    plan, sheets = plan_import(lab, BASE, PropertyRegistry.default())
    names = [p.template.codesystem.name for p in plan.proposals]
    assert names == ["Urval", "Grupperad"]  # referenslistan publiceras inte
    tpl = plan.proposals[1].template
    assert tpl.kind == "valueset"
    assert {c.source_column: c.role for c in tpl.columns} == {
        "Kortnamn": ColumnRole.DISPLAY, "Kod": ColumnRole.CODE, "Kategori": ColumnRole.GROUP}
    assert tpl.validate_template() == []
    # SWE-koderna ingår i NPU-kodsystemet (som i Ineras NPU-lista).
    assert tpl.external_systems == {"npu": "http://npu-terminology.org", "swe-npu": "http://npu-terminology.org"}
    resources, warnings = build_all(tpl, sheets)
    assert resources[0]["url"] == f"{BASE}/ValueSet/Grupperad"
    groups = {r["title"].split(" – ")[-1] for r in resources[1:]}
    assert groups == {"Övrigt", "Hematologi", "Kemiska rutinanalyser", "Koagulation", "Urinanalyser"}
    systems = {inc["system"] for inc in resources[0]["compose"]["include"]}
    assert systems == {"http://npu-terminology.org"}
    assert any(c["code"] == "SWE05003" for c in resources[0]["compose"]["include"][0]["concept"])
    first = plan.proposals[0]
    assert any("NPU1059" in q for q in first.questions)
    tpl0 = first.template
    _, warnings0 = build_all(tpl0, sheets)
    assert any("NPU1059" in w and "utelämnad" in w for w in warnings0)


# -- familj av flikar ---------------------------------------------------------------


def test_family_assessment(family):
    wa, _ = assess_workbook(family)
    assert wa.verdict == Verdict.ACCEPT
    kinds = {s.sheet: s.kind for s in wa.sheets}
    assert kinds["Inledning"] == kinds["Om kodverk"] == SheetKind.DOCUMENTATION
    assert kinds["datablad"] == SheetKind.HELPER
    assert kinds["Standardtexter"] == SheetKind.TEXT_CATALOGUE
    assert wa.families == {"KV_EX": ["KV_EX_0001", "KV_EX_0002", "KV_EX_0003"]}
    assert wa.catalogue["KV_EX_0001"].identifier == "urn:oid:1.2.752.999.1.1"
    assert wa.catalogue["KV_EX_0002"].identifier is None
    assert wa.publisher == "Exempelmyndigheten, enhet för kodverk"
    assert [h["Version"] for h in wa.history] == ["1.0", "1.1"]
    assert wa.policy.allow_code_removal is False


def test_family_sheet_structure(family):
    sheet = next(s for s in read_workbook(family) if s.name == "KV_EX_0001")
    assert sheet.metadata == {"Kodverk": "KV_EX_0001", "Avser": "Typ av underlag"}
    assert sheet.column_groups == {"Används av": ["Intyg A", "Intyg B", "Utlåtande C"]}
    assert sheet.headers[1] == "Klartext" and "textleveransen" in sheet.header_notes["Klartext"]
    assert len(sheet.rows) == 4  # fotnoten är ingen datarad


def test_family_plan_and_build(family):
    plan, sheets = plan_import(family, BASE, PropertyRegistry.default())
    assert len(plan.proposals) == 1
    proposal = plan.proposals[0]
    tpl = proposal.template
    roles = {c.source_column: (c.role, c.pattern or (c.context.code if c.context else None)) for c in tpl.columns}
    assert roles["Kod"][0] == ColumnRole.CODE and roles["Klartext"][0] == ColumnRole.DISPLAY
    assert roles["ID rubriktext"] == (ColumnRole.DERIVED, "{codesystem}.{code}.RBK")
    assert roles["Intyg A"] == (ColumnRole.CONTEXT, "INTYG_A")
    assert [s.name for s in tpl.source.sheets] == ["KV_EX_0001", "KV_EX_0002", "KV_EX_0003"]
    assert tpl.source.sheets[0].identifier == "urn:oid:1.2.752.999.1.1"
    assert tpl.codesystem.version == "1.1.0"
    assert tpl.policy.allow_code_removal is False
    assert {g.name for g in tpl.generated_codesystems} == {"KV_EX_ANVANDS_AV", "KV_EX_TEXTROLL"}
    assert [t.use_code for t in tpl.text_catalogue.texts] == ["RBK", "HLP"]
    assert any("OID saknas" in q for q in proposal.questions)

    resources, warnings = build_all(tpl, sheets)
    by_name = {r["name"]: r for r in resources}
    assert set(by_name) == {"KV_EX_0001", "KV_EX_0002", "KV_EX_0003", "KV_EX_ANVANDS_AV", "KV_EX_TEXTROLL"}
    cs = by_name["KV_EX_0001"]
    assert cs["identifier"][0]["value"] == "urn:oid:1.2.752.999.1.1"
    assert cs["title"] == "Typ av underlag" and cs["publisher"] == "Exempelmyndigheten"
    telefon = next(c for c in cs["concept"] if c["code"] == "TELEFON")
    uses = [d["use"]["code"] for d in telefon["designation"]]
    assert uses == ["INTYG_A"]  # bara kryssad kontext
    annat = next(c for c in cs["concept"] if c["code"] == "ANNAT")
    texts = {d["use"]["code"]: d["value"] for d in annat["designation"]
             if d["use"]["system"].endswith("TEXTROLL")}
    assert texts == {"RBK": "Annat", "HLP": "Ange vilket underlag som avses."}
    assert any("SAKNAS" in w for w in warnings)
    contexts = by_name["KV_EX_ANVANDS_AV"]
    assert [c["display"] for c in contexts["concept"]] == ["Intyg A", "Intyg B", "Utlåtande C"]


def test_release_policy_blocks_removed_codes(family):
    plan, sheets = plan_import(family, BASE, PropertyRegistry.default())
    tpl = plan.proposals[0].template
    resources, _ = build_all(tpl, sheets)
    old = resources[0]
    new = {**old, "concept": old["concept"][:-1]}
    errors = check_policy(diff(old, new), tpl.policy)
    assert errors and "ANNAT" in errors[0]


# -- externa kodsystem och validering ------------------------------------------------


def test_external_detection():
    assert classify_code("NPU03804")[0].key == "npu"
    assert classify_code("NPU0384")[1].problem.startswith("ser ut som NPU")
    assert classify_code("73211009")[0].key == "snomed"
    assert classify_code("73211008")[0] is None  # fel kontrollsiffra
    assert classify_code("8867-4")[0].key == "loinc"
    assert detect(["UNDERSOKNING", "TELEFON", "ANNAT"]) is None  # lokala koder är inget urval
    assert detect(["123456", "A", "B", "C", "D"]) is None


def test_validate_valueset_codes():
    class Stub:
        async def validate_code(self, code, system=None, **kw):
            if code == "BAD":
                return {"result": False, "message": "Okänd kod"}
            return {"result": True, "display": "X", "inactive": code == "OLD"}

    vs = {"compose": {"include": [{"system": "http://x", "concept": [{"code": "A"}, {"code": "BAD"}, {"code": "OLD"}]}]}}
    report = asyncio.run(validate_valueset_codes(Stub(), vs))
    assert report["summary"] == {"ok": 1, "invalid": 1, "inactive": 1}
    assert {p["code"] for p in report["problems"]} == {"BAD", "OLD"}
