import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from make_examples import make_family_workbook, make_lab_workbook  # noqa: E402

from term_assistant.admin import editorial, ops  # noqa: E402
from term_assistant.admin.app import create_app  # noqa: E402
from term_assistant.admin.settings import Editorial, Namespace, Settings, Target  # noqa: E402
from term_assistant.admin.store import Release, Workspace  # noqa: E402
from term_assistant.importer.mapping import ColumnRole  # noqa: E402
from term_assistant.importer.package import read_package  # noqa: E402
from term_assistant.importer.plan import plan_import  # noqa: E402
from term_assistant.importer.registry import PropertyRegistry  # noqa: E402

BASE = "http://example.org/fhir"
NS = Namespace(key="ex", label="Exempel", base_url=BASE, oid_root="1.2.752.999", publisher="Exempelmyndigheten",
               package_prefix="se.example")


def _settings(**editorial_kw) -> Settings:
    return Settings(namespaces=[NS], editorial=Editorial(**editorial_kw))


@pytest.fixture
def family_project(tmp_path):
    ws = Workspace(tmp_path / "ws")
    path = make_family_workbook(tmp_path / "familj.xlsx")
    plan, sheets = plan_import(path, BASE, PropertyRegistry.default())
    project = ops.create_project(ws, plan.proposals[0], sheets, "KV EX", "anna", namespace=NS,
                                 classification="intern")
    return ws, project


@pytest.fixture
def lab_project(tmp_path):
    ws = Workspace(tmp_path / "ws")
    path = make_lab_workbook(tmp_path / "lab.xlsx")
    plan, sheets = plan_import(path, BASE, PropertyRegistry.default())
    project = ops.create_project(ws, plan.proposals[1], sheets, "Labbgrupper", "anna", namespace=NS,
                                 classification="öppen")
    return ws, project


def _msg(html: str) -> str:
    m = re.search(r'class="msg">([^<]*)', html)
    return m.group(1) if m else ""


def _form(html: str) -> dict:
    """Plocka ut ett begreppsformulärs nuvarande värden."""
    form = dict(re.findall(r'name="(c\d+)" value="([^"]*)"', html))
    form.update(dict(re.findall(r'name="(c\d+)"[^>]*>([^<]*)</textarea>', html)))
    form.update({k: "on" for k in re.findall(r'name="(c\d+)" checked', html)})
    return form


# -- projekt och kolumner ------------------------------------------------------------------


def test_project_folds_text_catalogue_into_columns(family_project):
    ws, project = family_project
    data = ws.data(project.id)
    tpl = project.template
    assert tpl.text_catalogue is None
    # Källans dokumentägare vinner över namnrymdens förvalda utgivare.
    assert (tpl.namespace, tpl.classification) == ("ex", "intern")
    assert tpl.codesystem.publisher == "Exempelmyndigheten, enhet för kodverk"
    texts = [c for c in tpl.columns if c.source_column in ("Rubriktexter", "Hjälptexter")]
    assert [c.designation.use_code for c in texts] == ["RBK", "HLP"]
    annat = ops.find_row(data["KV_EX_0001"], tpl, "ANNAT")
    assert annat["Hjälptexter"] == "Ange vilket underlag som avses."
    assert "ID rubriktext" not in annat  # härledda värden lagras inte
    rbk = next(c for c in tpl.columns if c.source_column == "ID rubriktext")
    assert ops.cell(tpl, rbk, annat, "KV_EX_0001") == "KV_EX_0001.ANNAT.RBK"
    assert any("SAKNAS" in q.text for q in project.questions)


def test_change_column_role_without_migrating_rows(family_project):
    ws, project = family_project
    data = ws.data(project.id)
    tpl = project.template
    i = next(i for i, c in enumerate(tpl.columns) if c.source_column == "Hjälptexter")
    new_tpl = ops.update_column(tpl, i, {"role": "definition", "label": "Hjälptext", "visible": "on",
                                         "editable": "on"}, PropertyRegistry.default())
    project.template = new_tpl
    resources, _ = ops.build(project, data)
    annat = next(c for c in resources[0]["concept"] if c["code"] == "ANNAT")
    assert annat["definition"] == "Ange vilket underlag som avses."
    assert all(d["use"]["code"] != "HLP" for d in annat["designation"])
    assert any(g.key == "textroll" for g in new_tpl.generated_codesystems)


def test_add_column_prefers_existing_properties(family_project):
    ws, project = family_project
    data = ws.data(project.id)
    reg = PropertyRegistry.default()
    with pytest.raises(ops.OpError, match="finns redan"):
        ops.add_column(project.template, data, {"label": "Kommentar", "role": "property", "property": "__new__",
                                                "new_property_code": "comment"}, reg)
    tpl = ops.add_column(project.template, data, {"label": "Kommentar", "role": "property", "property": "comment"},
                         reg)
    assert (tpl.columns[-1].role, tpl.columns[-1].property_code) == (ColumnRole.PROPERTY, "comment")
    tpl = ops.add_column(tpl, data, {"label": "Utlåtande D", "role": "context", "context_group": "anvandsAv"}, reg)
    assert [c.code for c in tpl.generated("anvandsAv").concepts][-1] == "UTLATANDE_D"


# -- namnrymd och identifierare ---------------------------------------------------------------


def test_namespace_and_oid_rules(family_project):
    ws, project = family_project
    data = ws.data(project.id)
    strict = Settings(namespaces=[NS.model_copy(update={"require_oid": True})])
    with pytest.raises(ops.OpError) as exc:
        ops.create_release_candidate(ws, project, data, "1.1.0", "anna", settings=strict)
    assert any("OID saknas" in m for m in exc.value.messages)
    assigned = ops.assign_oids(ws, project, strict)
    assert assigned == ["KV_EX_0002 = urn:oid:1.2.752.999.2", "KV_EX_0003 = urn:oid:1.2.752.999.3"]  # .1 upptagen
    project.template.codesystem.url = "http://annan.example/fhir/CodeSystem"
    for s in project.template.source.sheets:
        s.url = f"http://annan.example/fhir/CodeSystem/{s.name}"
    with pytest.raises(ops.OpError) as exc:
        ops.create_release_candidate(ws, project, data, "1.1.0", "anna", settings=strict)
    assert any("följer inte namnrymden" in m for m in exc.value.messages)


# -- releaser, paket och klassning --------------------------------------------------------------


def test_release_flow_four_eyes_and_policy(family_project):
    ws, project = family_project
    data = ws.data(project.id)
    cfg = _settings()
    rc = ops.create_release_candidate(ws, project, data, "1.1.0", "anna", "Första", cfg)
    assert rc.version == "1.1.0-rc.1" and rc.resources[0]["version"] == "1.1.0-rc.1"
    assert rc.resources[0]["meta"]["security"][0]["code"] == "N"  # intern
    with pytest.raises(ops.OpError, match="Fyra ögon"):
        ops.approve_release(ws, project, rc.version, "anna", cfg)
    rel = ops.approve_release(ws, project, rc.version, "bertil", cfg)
    assert rel.version == "1.1.0" and rel.resources[0]["status"] == "active"
    released = ops.released_codes(ws, project.id, "KV_EX_0001")
    assert ops.can_delete(project.template, released, "TELEFON")[0] is False
    data["KV_EX_0001"].rows = [r for r in data["KV_EX_0001"].rows if r["Kod"] != "TELEFON"]
    with pytest.raises(ops.OpError, match="TELEFON"):
        ops.create_release_candidate(ws, project, data, "1.2.0", "anna", settings=cfg)


def test_retire_with_replacement_gives_conceptmap(family_project, monkeypatch):
    ws, project = family_project
    data = ws.data(project.id)
    row = ops.find_row(data["KV_EX_0001"], project.template, "TELEFON")
    with pytest.raises(ops.OpError, match="finns inte"):
        ops.retire(project.template, data, "KV_EX_0001", row, "FINNS_EJ")
    project.template = ops.retire(project.template, data, "KV_EX_0001", row, "UNDERSOKNING", "wider",
                                  "Telefon räknas som undersökning")
    resources, _ = ops.build(project, data)
    cs = resources[0]
    telefon = next(c for c in cs["concept"] if c["code"] == "TELEFON")
    props = {p["code"]: p for p in telefon["property"]}
    assert props["inactive"]["valueBoolean"] is True and props["replacedBy"]["valueCode"] == "UNDERSOKNING"
    cm = next(r for r in resources if r["resourceType"] == "ConceptMap")
    assert cm["url"] == f"{BASE}/ConceptMap/KV_EX_ERSATTNINGAR"
    target = cm["group"][0]["element"][0]["target"][0]
    assert (target["code"], target["equivalence"]) == ("UNDERSOKNING", "wider")
    monkeypatch.setenv("FHIR_VERSION", "R5")
    resources, _ = ops.build(project, data)
    target = next(r for r in resources if r["resourceType"] == "ConceptMap")["group"][0]["element"][0]["target"][0]
    assert target["relationship"] == "source-is-narrower-than-target" and "equivalence" not in target
    rc = ops.create_release_candidate(ws, project, data, "1.1.0", "anna", settings=_settings())
    assert any(d["resourceType"] == "ConceptMap" for d in rc.diffs)


def test_package_and_classification_targets(family_project, tmp_path):
    ws, project = family_project
    data = ws.data(project.id)
    drop = tmp_path / "inlasning"
    drop.mkdir()
    cfg = _settings()
    cfg.targets = [Target(key="publik", label="Publik testserver", kind="package-dir", directory=str(drop),
                          max_classification="öppen"),
                   Target(key="inera", label="Inläsning Inera", kind="package-dir", directory=str(drop),
                          max_classification="intern")]
    project.template.license_note = "Får spridas inom svensk vård."
    rc = ops.create_release_candidate(ws, project, data, "1.1.0", "anna", settings=cfg)
    ops.approve_release(ws, project, rc.version, "bertil", cfg)
    filename, blob = ops.release_package(ws, project, "1.1.0", cfg)
    assert filename == "se.example.kv-ex-1.1.0.tgz"
    pkg = read_package(blob)
    manifest = pkg["package/package.json"]
    assert manifest["fhirVersions"] == ["4.0.1"] and manifest["x-classification"]["key"] == "intern"
    assert manifest["dependencies"] == {"hl7.fhir.r4.core": "4.0.1"}
    index = pkg["package/.index.json"]["files"]
    assert {f["resourceType"] for f in index} == {"CodeSystem"}
    cs = pkg["package/CodeSystem-KV-EX-0001.json"]
    assert cs["meta"]["security"] == [{"system": "http://terminology.hl7.org/CodeSystem/v3-Confidentiality",
                                       "code": "N", "display": "normal"}]
    assert "svensk vård" in pkg["package/other/license.md"]
    assert pkg["package/other/provenance.json"]["entry"][-1]["resource"]["activity"]["text"].startswith("Release")
    with pytest.raises(ops.OpError, match="tar högst emot"):
        asyncio.run(ops.publish_release(ws, project, "1.1.0", "publik", "bertil", cfg))
    asyncio.run(ops.publish_release(ws, project, "1.1.0", "inera", "bertil", cfg))
    assert (drop / filename).exists()


# -- urval: flaggning och ersättning ----------------------------------------------------------------


def test_check_subset_flags_with_local_and_server_sources(lab_project):
    ws, project = lab_project
    data = ws.data(project.id)
    # Ett lokalt förvaltat kodverk med samma URL som NPU, där NPU10007 är avvecklad och ersatt.
    local = ops.create_project(ws, *_tiny_cs_proposal(ws), "Lokal NPU", "anna")
    ws.save_release(local.id, Release(version="1.0.0", status="release", resources=[{
        "resourceType": "CodeSystem", "url": "http://npu-terminology.org", "version": "1.0.0", "concept": [
            {"code": "NPU10007", "display": "P-Analys 7", "property": [
                {"code": "inactive", "valueBoolean": True}, {"code": "replacedBy", "valueCode": "NPU10599"}]},
            {"code": "NPU10599", "display": "Pt-Kroppsvikt"}]}]))

    class Server:
        async def validate_code(self, code, system=None, **kw):
            return {"result": True, "display": "x"}

    result = asyncio.run(ops.check_subset(ws, project, data, Server()))
    sheet = next(iter(data))
    flags = project.flags[sheet]
    assert flags["NPU10007"].status == "inaktiv"
    assert flags["NPU10007"].suggestions[0]["code"] == "NPU10599"
    assert result["summary"]["lokala"] > 0
    # Okända koder (inte i det lokala kodverket) flaggas också.
    assert flags["NPU10000"].status == "okänd"
    row = ops.find_row(data[sheet], project.template, "NPU10007")
    ops.replace_code(project.template, data[sheet], row, "NPU10599")  # annan grupp: tillåtet
    with pytest.raises(ops.OpError, match="finns redan"):
        ops.replace_code(project.template, data[sheet], row, "NPU10001")  # finns i samma grupp
    other = next(r for r in data[sheet].rows if r["Kod"] == "NPU10010")
    ops.replace_code(project.template, data[sheet], other, "NPU10598")
    assert other["Kod"] == "NPU10598"


def _tiny_cs_proposal(ws):
    import tempfile
    from openpyxl import Workbook
    wb = Workbook()
    ws_ = wb.active
    ws_.title = "Lokal"
    ws_.append(["Kod", "Benämning"])
    ws_.append(["A", "Alfa"])
    ws_.append(["B", "Beta"])
    path = Path(tempfile.mkdtemp()) / "lokal.xlsx"
    wb.save(path)
    plan, sheets = plan_import(path, BASE, PropertyRegistry.default())
    return plan.proposals[0], sheets


def test_server_suggestions_from_lookup(lab_project):
    ws, project = lab_project
    data = ws.data(project.id)

    class Server:
        async def validate_code(self, code, system=None, **kw):
            return {"result": True, "display": "x", "inactive": code == "NPU10007"}

        async def lookup(self, system, code, version=None, properties=None):
            return {"property": [{"code": "replacedBy", "value": "NPU10599"}]}

        async def translate(self, code, system, conceptmap=None, target_system=None):
            return {"result": False}

    asyncio.run(ops.check_subset(ws, project, data, Server()))
    flag = project.flags[next(iter(data))]["NPU10007"]
    assert flag.status == "inaktiv" and flag.suggestions == [
        {"code": "NPU10599", "display": None, "source": "$lookup replacedBy"}]


# -- redaktionell kedja ---------------------------------------------------------------------------


def test_editorial_chain_manual_and_ai(family_project):
    ws, project = family_project
    data = ws.data(project.id)
    cfg = _settings(review_manual_edits=True)
    page_form = {"c1": "Telefon- eller videokontakt", "c4": "on"}
    p = editorial.propose_edit(ws, project, data, "KV_EX_0001", "TELEFON", page_form, "anna", "Bredare")
    assert p.changes == {"Klartext": "Telefon- eller videokontakt"}
    assert ops.find_row(data["KV_EX_0001"], project.template, "TELEFON")["Klartext"] == "Telefonkontakt"
    with pytest.raises(ops.OpError, match="den som skrev"):
        editorial.review(ws, project, data, p.id, "anna", True, settings=cfg)
    with pytest.raises(ops.OpError, match="öppna förslag"):
        ops.create_release_candidate(ws, project, data, "1.1.0", "anna", settings=cfg)
    editorial.review(ws, project, data, p.id, "bertil", True, "Ok", cfg)
    assert ops.find_row(ws.data(project.id)["KV_EX_0001"], project.template, "TELEFON")["Klartext"] == \
        "Telefon- eller videokontakt"

    calls = []

    def parse(**kw):
        calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn", id="msg_1", model="claude-opus-5-5",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                               parsed_output=editorial.AiSuggestions(suggestions=[
                                   editorial.AiSuggestion(code="JOURNAL", column="Klartext", value="Journaluppgift",
                                                          kind="stavning", rationale="Singular som övriga"),
                                   editorial.AiSuggestion(code="JOURNAL", column="Kod", value="X", kind="övrigt",
                                                          rationale="Får inte ändra koder"),
                                   editorial.AiSuggestion(code="ANNAT", column="Hjälptexter", value=None,
                                                          kind="dubblett", rationale="Liknar UNDERSOKNING?")]))

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)))
    data = ws.data(project.id)
    made = editorial.ai_review(ws, project, data, "KV_EX_0001", "anna", client=client)
    assert calls[0]["model"] == "claude-opus-5-5" and calls[0]["output_format"] is editorial.AiSuggestions
    assert [(m.code, m.changes) for m in made] == [("JOURNAL", {"Klartext": "Journaluppgift"}), ("ANNAT", {})]
    assert made[0].origin == "ai" and made[0].provenance["input_sha256"]
    # AI-förslaget godkänns av en människa; samma person som startade granskningen får göra det.
    editorial.review(ws, project, data, made[0].id, "anna", True, settings=cfg)
    editorial.review(ws, project, data, made[1].id, "anna", False, "Ingen dubblett", cfg)
    data = ws.data(project.id)
    assert ops.find_row(data["KV_EX_0001"], project.template, "JOURNAL")["Klartext"] == "Journaluppgift"

    rc = ops.create_release_candidate(ws, project, data, "1.1.0", "anna", settings=cfg)
    ops.approve_release(ws, project, rc.version, "bertil", cfg)
    _, blob = ops.release_package(ws, project, "1.1.0", cfg)
    prov = read_package(blob)["package/other/provenance.json"]["entry"]
    who = [a["who"]["display"] for e in prov for a in e["resource"]["agent"]]
    assert "AI: claude-opus-5-5" in who and "bertil" in who


def test_stale_proposal_is_not_applied(family_project):
    ws, project = family_project
    data = ws.data(project.id)
    p = editorial.propose_edit(ws, project, data, "KV_EX_0001", "TELEFON", {"c1": "Ny text", "c4": "on"}, "anna")
    ops.find_row(data["KV_EX_0001"], project.template, "TELEFON")["Klartext"] = "Ändrad under tiden"
    ws.save(project, data)
    result = editorial.review(ws, project, ws.data(project.id), p.id, "bertil", True)
    assert result.status == "inaktuell" and "har ändrats" in result.review_comment


# -- webbappen ------------------------------------------------------------------------------------


def test_web_flow_family(tmp_path):
    root = tmp_path / "ws"
    Workspace(root)
    _settings().save(root)
    client = TestClient(create_app(root), follow_redirects=True)
    content = make_family_workbook(tmp_path / "f.xlsx").read_bytes()
    r = client.post("/import", files={"file": ("familj.xlsx", content)},
                    data={"namespace": "ex", "classification": "intern"})
    assert r.status_code == 200 and "Skapa projekt" in r.text and "familj KV_EX" in r.text
    uid = r.url.path.rsplit("/", 1)[1]
    anna, bertil = {"X-Remote-User": "anna"}, {"X-Remote-User": "bertil"}
    r = client.post(f"/import/{uid}/create/0", data={"title": "KV EX"}, headers=anna)
    pid = r.url.path.rsplit("/", 1)[1]
    assert "Öppna frågor" in r.text and "Intern" in r.text

    r = client.get(f"/p/{pid}/s/KV_EX_0001")
    assert "Används av" in r.text and "KV_EX_0001.ANNAT.RBK" in r.text and "AI-granskning" in r.text
    form = _form(client.get(f"/p/{pid}/s/KV_EX_0001/c/ANNAT").text)
    form["c1"] = "Annat underlag"
    r = client.post(f"/p/{pid}/s/KV_EX_0001/c/ANNAT", data=form, headers=anna)
    assert _msg(r.text) == "Sparat." and "Annat underlag" in r.text

    r = client.post(f"/p/{pid}/s/KV_EX_0001/c/TELEFON/retire",
                    data={"replaced_by": "UNDERSOKNING", "relation": "wider", "comment": ""}, headers=anna)
    assert "ersätts av UNDERSOKNING" in _msg(r.text)

    r = client.post(f"/p/{pid}/releases", data={"version": "1.1.0", "notes": "Första"}, headers=anna)
    assert r.url.path.endswith("/releases/1.1.0-rc.1"), r.text[:3000]
    r = client.post(f"/p/{pid}/releases/1.1.0-rc.1/approve", headers=anna)
    assert "Fyra ögon" in _msg(r.text)
    r = client.post(f"/p/{pid}/releases/1.1.0-rc.1/approve", headers=bertil)
    assert "Release 1.1.0 är godkänd." in r.text and "Ladda ner paket" in r.text
    r = client.get(f"/p/{pid}/releases/1.1.0/package.tgz")
    pkg = read_package(r.content)
    assert "package/ConceptMap-KV-EX-ERSATTNINGAR.json" in pkg

    r = client.post(f"/p/{pid}/s/KV_EX_0001/c/UNDERSOKNING/delete")
    assert "Avveckla den i stället" in r.text
    assert "bertil" in client.get(f"/p/{pid}/log").text
    assert client.get(f"/p/{pid}/proposals").status_code == 200
    assert "settings.json" in client.get("/settings").text


def test_web_review_mode_creates_proposals(tmp_path):
    root = tmp_path / "ws"
    Workspace(root)
    _settings(review_manual_edits=True).save(root)
    client = TestClient(create_app(root), follow_redirects=True)
    content = make_family_workbook(tmp_path / "f.xlsx").read_bytes()
    uid = client.post("/import", files={"file": ("familj.xlsx", content)},
                      data={"namespace": "ex"}).url.path.rsplit("/", 1)[1]
    pid = client.post(f"/import/{uid}/create/0", data={"title": "KV"}).url.path.rsplit("/", 1)[1]
    form = _form(client.get(f"/p/{pid}/s/KV_EX_0001/c/ANNAT").text)
    form["c1"] = "Annat underlag"
    r = client.post(f"/p/{pid}/s/KV_EX_0001/c/ANNAT", data=form, headers={"X-Remote-User": "anna"})
    assert "skickat för granskning" in _msg(r.text) and "Annat underlag" not in r.text
    page = client.get(f"/p/{pid}/proposals").text
    pid_prop = re.search(r"/proposals/([0-9a-f]+)/accept", page).group(1)
    r = client.post(f"/p/{pid}/proposals/{pid_prop}/accept", headers={"X-Remote-User": "anna"})
    assert "den som skrev" in _msg(r.text)
    r = client.post(f"/p/{pid}/proposals/{pid_prop}/accept", headers={"X-Remote-User": "bertil"})
    assert "godkänd" in _msg(r.text)
    assert "Annat underlag" in client.get(f"/p/{pid}/s/KV_EX_0001").text


def test_upload_of_rejected_file_shows_tips(tmp_path):
    client = TestClient(create_app(tmp_path / "ws"), follow_redirects=True)
    r = client.post("/import", files={"file": ("lista.csv", "Läsa\nSkriva\nUppdatera\n".encode())},
                    data={"namespace": "exempel"})
    assert "avvisad" in r.text and "En kod per rad" in r.text and "Skapa projekt" not in r.text


def test_releases_page_with_conceptmap_in_working_copy(tmp_path):
    root = tmp_path / "ws"
    Workspace(root)
    _settings().save(root)
    client = TestClient(create_app(root), follow_redirects=True)
    content = make_family_workbook(tmp_path / "f.xlsx").read_bytes()
    uid = client.post("/import", files={"file": ("familj.xlsx", content)},
                      data={"namespace": "ex"}).url.path.rsplit("/", 1)[1]
    pid = client.post(f"/import/{uid}/create/0", data={"title": "KV"}).url.path.rsplit("/", 1)[1]
    client.post(f"/p/{pid}/s/KV_EX_0001/c/TELEFON/retire", data={"replaced_by": "UNDERSOKNING"})
    r = client.get(f"/p/{pid}/releases")
    assert r.status_code == 200 and "1 mappningar" in r.text
