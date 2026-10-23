import re
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from make_examples import make_family_workbook, make_lab_workbook  # noqa: E402

from term_assistant.admin import ops  # noqa: E402
from term_assistant.admin.app import create_app  # noqa: E402
from term_assistant.admin.store import Workspace  # noqa: E402
from term_assistant.importer.mapping import ColumnRole  # noqa: E402
from term_assistant.importer.plan import plan_import  # noqa: E402
from term_assistant.importer.registry import PropertyRegistry  # noqa: E402

BASE = "http://example.org/fhir"


@pytest.fixture
def family_project(tmp_path):
    ws = Workspace(tmp_path / "ws")
    path = make_family_workbook(tmp_path / "familj.xlsx")
    plan, sheets = plan_import(path, BASE, PropertyRegistry.default())
    project = ops.create_project(ws, plan.proposals[0], sheets, "KV EX", "testare")
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


# -- ops ------------------------------------------------------------------------------


def test_project_folds_text_catalogue_into_columns(family_project):
    ws, project = family_project
    data = ws.data(project.id)
    tpl = project.template
    assert tpl.text_catalogue is None
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
    reg = PropertyRegistry.default()
    # Hjälptexten ska i stället vara en definition – samma rader, ny betydelse.
    new_tpl = ops.update_column(tpl, i, {"role": "definition", "label": "Hjälptext", "visible": "on",
                                         "editable": "on"}, reg)
    project.template = new_tpl
    resources, _ = ops.build(project, data)
    annat = next(c for c in resources[0]["concept"] if c["code"] == "ANNAT")
    assert annat["definition"] == "Ange vilket underlag som avses."
    assert all(d["use"]["code"] != "HLP" for d in annat["designation"])
    # Textrollen HLP används inte längre men RBK gör det – textrollkodverket finns kvar.
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
    col = tpl.columns[-1]
    assert (col.role, col.property_code, col.ui.widget) == (ColumnRole.PROPERTY, "comment", "text")
    assert "Kommentar" in data["KV_EX_0001"].headers
    tpl = ops.add_column(tpl, data, {"label": "Utlåtande D", "role": "context", "context_group": "anvandsAv"}, reg)
    gen = tpl.generated("anvandsAv")
    assert [c.code for c in gen.concepts][-1] == "UTLATANDE_D"


def test_release_flow_with_policy(family_project):
    ws, project = family_project
    data = ws.data(project.id)
    rc = ops.create_release_candidate(ws, project, data, "1.1.0", "testare", "Första")
    assert rc.version == "1.1.0-rc.1" and rc.resources[0]["version"] == "1.1.0-rc.1"
    rel = ops.approve_release(ws, project, rc.version, "testare")
    assert rel.version == "1.1.0" and rel.resources[0]["status"] == "active"
    with pytest.raises(ops.OpError, match="redan släppt"):
        ops.create_release_candidate(ws, project, data, "1.1.0", "testare")
    # Policyn förbjuder borttag av släppta koder.
    released = ops.released_codes(ws, project.id, "KV_EX_0001")
    assert ops.can_delete(project.template, released, "TELEFON")[0] is False
    data["KV_EX_0001"].rows = [r for r in data["KV_EX_0001"].rows if r["Kod"] != "TELEFON"]
    with pytest.raises(ops.OpError, match="TELEFON"):
        ops.create_release_candidate(ws, project, data, "1.2.0", "testare")
    # Avveckling i stället: kolumnen 'Inaktiv' läggs till och koden markeras.
    data = ws.data(project.id)
    row = ops.find_row(data["KV_EX_0001"], project.template, "TELEFON")
    project.template = ops.retire(project.template, data["KV_EX_0001"], row)
    rc2 = ops.create_release_candidate(ws, project, data, "1.2.0", "testare")
    d = next(x for x in rc2.diffs if x["name"] == "KV_EX_0001")
    assert d["changed"] == [{"code": "TELEFON", "fields": ["property"]}] and not d["removed"]


# -- webbappen -------------------------------------------------------------------------


def test_web_flow_family(tmp_path):
    client = TestClient(create_app(tmp_path / "ws"), follow_redirects=True)
    content = make_family_workbook(tmp_path / "f.xlsx").read_bytes()
    r = client.post("/import", files={"file": ("familj.xlsx", content)}, data={"base_url": BASE})
    assert r.status_code == 200 and "Skapa projekt" in r.text and "familj KV_EX" in r.text
    uid = r.url.path.rsplit("/", 1)[1]
    r = client.post(f"/import/{uid}/create/0", data={"title": "KV EX"}, headers={"X-Remote-User": "anna"})
    pid = r.url.path.rsplit("/", 1)[1]
    assert "Öppna frågor" in r.text

    r = client.get(f"/p/{pid}/s/KV_EX_0001")
    assert "Används av" in r.text and "KV_EX_0001.ANNAT.RBK" in r.text

    page = client.get(f"/p/{pid}/s/KV_EX_0001/c/ANNAT").text
    form = _form(page)
    form["c1"] = "Annat underlag"
    r = client.post(f"/p/{pid}/s/KV_EX_0001/c/ANNAT", data=form, headers={"X-Remote-User": "anna"})
    assert _msg(r.text) == "Sparat." and "Annat underlag" in r.text

    r = client.post(f"/p/{pid}/s/KV_EX_0001/new", data={"c0": "ANNAT", "c1": "Dubblett"})
    assert r.status_code == 400 and "finns redan" in r.text

    r = client.post(f"/p/{pid}/releases", data={"version": "1.1.0", "notes": "Första"})
    assert r.url.path.endswith("/releases/1.1.0-rc.1")
    r = client.post(f"/p/{pid}/releases/1.1.0-rc.1/approve")
    assert r.url.path.endswith("/releases/1.1.0") and "Release 1.1.0 är godkänd." in r.text
    bundle = client.get(f"/p/{pid}/releases/1.1.0/bundle.json").json()
    names = [e["resource"]["name"] for e in bundle["entry"]]
    assert names[:3] == ["KV_EX_0001", "KV_EX_0002", "KV_EX_0003"]

    r = client.post(f"/p/{pid}/s/KV_EX_0001/c/TELEFON/delete")
    assert "Avveckla den i stället" in r.text
    log = client.get(f"/p/{pid}/log").text
    assert "anna" in log and "Begrepp ANNAT ändrat" in log

    r = client.post(f"/p/{pid}/columns/1", data={"role": "property", "property": "__new__",
                                                 "new_property_code": "1fel", "label": "Klartext"})
    assert r.status_code == 400 and "måste börja med" in r.text


def test_web_flow_lab_subset(tmp_path):
    client = TestClient(create_app(tmp_path / "ws"), follow_redirects=True)
    content = make_lab_workbook(tmp_path / "lab.xlsx").read_bytes()
    r = client.post("/import", files={"file": ("lab.xlsx", content)}, data={"base_url": BASE})
    assert "referenslista" not in r.text or "reference" in r.text
    uid = r.url.path.rsplit("/", 1)[1]
    r = client.post(f"/import/{uid}/create/1", data={"title": "Labbgrupper"})
    pid = r.url.path.rsplit("/", 1)[1]
    r = client.post(f"/p/{pid}/meta", data={"title": "Labbgrupper", "ext_npu": "http://npu-terminology.org",
                                             "ext_swe-npu": "http://example.org/fhir/CodeSystem/swe",
                                             "allow_code_removal": "on", "allow_display_change": "on"})
    assert _msg(r.text) == "Sparat."
    bundle = client.get(f"/p/{pid}/preview.json").json()
    titles = [e["resource"]["title"] for e in bundle["entry"]]
    assert titles[0] == "Grupperad" and "Grupperad – Hematologi" in titles


def test_upload_of_rejected_file_shows_tips(tmp_path):
    client = TestClient(create_app(tmp_path / "ws"), follow_redirects=True)
    r = client.post("/import", files={"file": ("lista.csv", "Läsa\nSkriva\nUppdatera\n".encode())},
                    data={"base_url": BASE})
    assert "avvisad" in r.text and "En kod per rad" in r.text and "Skapa projekt" not in r.text
