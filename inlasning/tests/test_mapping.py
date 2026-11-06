"""Mappningsprojekt (ConceptMap): skapa, redigera, kontrollera, släppa och ta in förslag från terminologitjänsten."""

import re

import pytest
from starlette.testclient import TestClient

from term_inlasning.admin.app import create_app
from term_inlasning.admin.settings import Editorial, Namespace, Settings
from term_inlasning.importer import conceptmap
from term_inlasning.importer.mapping import CodeSystemMeta, MappingSpec

NO = "http://hl7.no/fhir/CodeSystem/civil"
NO_VS = "http://hl7.no/fhir/ValueSet/civil"
V3 = "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus"
CODES = {NO: {"gift": "Gift", "ugift": "Ugift", "uoppgitt": "Uoppgitt"},
         V3: {"M": "Married", "S": "Never Married", "D": "Divorced"}}
PROPOSAL = {"resourceType": "ConceptMap", "id": "NoCivil", "url": "http://example.org/fhir/ConceptMap/no-civil",
            "version": "0.1.0", "name": "NoCivil", "title": "Norska civilstånd till v3", "status": "draft",
            "meta": {"tag": [{"system": "urn:term-assistant:arbetsflode", "code": "forslag"}]},
            "sourceUri": NO_VS,
            "group": [{"source": NO, "target": V3, "element": [
                {"code": "gift", "display": "Gift", "target": [{"code": "M", "display": "Married",
                                                                "equivalence": "equivalent"}]},
                {"code": "uoppgitt", "target": [{"equivalence": "unmatched", "comment": "Saknas i v3"}]}]}]}
VS_PROPOSAL = {"resourceType": "ValueSet", "id": "Urval", "url": "http://example.org/fhir/ValueSet/urval",
               "version": "0.1.0", "name": "Urval", "title": "Urval civilstånd", "status": "draft",
               "compose": {"include": [{"system": V3, "concept": [{"code": "M", "display": "Married"}]}]}}


class FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def read_canonical(self, rtype, url, version=None):
        return {"url": url} if (rtype, url) in {("ValueSet", NO_VS), ("CodeSystem", NO), ("CodeSystem", V3)} else None

    async def expand(self, url=None, valueset=None, filter=None, count=50, offset=0):
        system = V3 if valueset and valueset["compose"]["include"][0]["system"] == V3 else NO
        hits = [{"system": system, "code": c, "display": d} for c, d in CODES[system].items()
                if not filter or filter.lower() in (c + d).lower()]
        return {"contains": hits, "total": len(hits)}

    async def validate_code(self, code, system=None, **kw):
        display = CODES.get(system, {}).get(code)
        return {"result": display is not None, "display": display}

    async def search(self, rtype, count=20, **kw):
        return {"ConceptMap": [PROPOSAL], "ValueSet": [VS_PROPOSAL]}.get(rtype, [])

    async def read(self, rtype, rid):
        return {"NoCivil": PROPOSAL, "Urval": VS_PROPOSAL}.get(rid)


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    Settings(namespaces=[Namespace(key="ex", label="Exempel", base_url="http://example.org/fhir",
                                   publisher="Exempelorganisationen")],
             editorial=Editorial(review_manual_edits=False)).save(root)
    return TestClient(create_app(root, tx_client_factory=FakeTx), follow_redirects=True)


def _msg(html: str) -> str:
    m = re.search(r'class="msg">([^<]*)', html)
    return m.group(1) if m else ""


def test_relations_build_r4_and_r5():
    tpl = conceptmap.template(CodeSystemMeta(url="http://ex.org/cm", name="Cm"), MappingSpec(
        source_system=NO, target_system=V3, source_valueset=NO_VS))
    rows = [{"Nr": "M1", "Källkod": "gift", "Relation": "likvärdig", "Målkod": "M", "Måltext": "Married"},
            {"Nr": "M2", "Källkod": "gift", "Relation": "målet är bredare", "Målkod": "S"},
            {"Nr": "M3", "Källkod": "uoppgitt", "Relation": "ingen motsvarighet", "Kommentar": "Saknas"},
            {"Nr": "M4", "Källkod": "ugift"}]
    cm, warnings = conceptmap.build_conceptmap(tpl, rows, "R4")
    gift, okand = cm["group"][0]["element"]
    assert [t["equivalence"] for t in gift["target"]] == ["equivalent", "wider"]
    assert okand["target"] == [{"equivalence": "unmatched", "comment": "Saknas"}]
    assert cm["sourceUri"] == NO_VS and any("ugift" in w for w in warnings)   # omappad källkod
    r5, _ = conceptmap.build_conceptmap(tpl, rows, "R5")
    gift, okand = r5["group"][0]["element"]
    assert gift["target"][1]["relationship"] == "source-is-narrower-than-target" and okand.get("noMap") is True
    assert conceptmap.row_problems(tpl, {"Relation": "målet är smalare", "Målkod": "M"}) == [
        "Relationen 'målet är smalare' ska motiveras med en kommentar."]
    assert conceptmap.relation_key("source-is-broader-than-target") == "narrower"


def test_create_edit_check_and_release_mapping(client):
    r = client.post("/mappning/ny", data={"title": "Norska civilstånd", "source": NO_VS, "target": V3,
                                          "namespace": "ex", "fill": "on",
                                          "description": "Civilstånd från norska system till HL7 v3."})
    assert "3 källkoder att mappa" in _msg(r.text) and "Ny mappning" in r.text
    pid = r.url.path.split("/")[2]
    # Kodsökning i målkodsystemet för kodfältet.
    assert client.get(f"/p/{pid}/sok?q=divorced").json() == [{"code": "D", "display": "Divorced"}]
    form = client.get(f"/p/{pid}/s/Mappningar/c/M1").text
    assert 'data-sok="mal"' in form and "likvärdig" in form
    # c0 Nr, c1 Källkod, c2 Källtext, c3 Relation, c4 Målkod, c5 Måltext, c6 Kommentar
    r = client.post(f"/p/{pid}/s/Mappningar/c/M1", data={"c1": "gift", "c2": "Gift", "c3": "målet är smalare",
                                                           "c4": "M", "c5": "Married"})
    assert r.status_code == 400 and "motiveras" in r.text
    client.post(f"/p/{pid}/s/Mappningar/c/M1", data={"c1": "gift", "c2": "Gift", "c3": "likvärdig", "c4": "M",
                                                      "c5": "Gift (fel text)"})
    r = client.post(f"/p/{pid}/s/Mappningar/new", data={"c1": "ugift", "c3": "likvärdig", "c4": "XX"})
    assert r.status_code == 200
    r = client.post(f"/p/{pid}/check")
    assert "2 rader behöver hanteras" in _msg(r.text)            # okänd målkod XX och avvikande text
    r = client.post(f"/p/{pid}/releases", data={"version": "1.0.0", "notes": "Första"})
    assert r.url.path.endswith("/releases/1.0.0-rc.1"), r.text[:500]
    cm = client.get(f"/p/{pid}/releases/1.0.0-rc.1/bundle.json").json()["entry"][0]["resource"]
    assert cm["resourceType"] == "ConceptMap" and cm["url"] == "http://example.org/fhir/ConceptMap/Norska_civilstand"
    assert [e["code"] for e in cm["group"][0]["element"]] == ["gift", "ugift"]


def test_take_in_proposals_from_terminology_server(client):
    page = client.get("/tx-forslag").text
    assert "Norska civilstånd till v3" in page and "Urval civilstånd" in page
    r = client.post("/tx-forslag/ta-in", data={"type": "ConceptMap", "id": "NoCivil", "namespace": "ex"})
    assert "taget in som projekt" in _msg(r.text) and "mappning → ConceptMap" in r.text
    pid = r.url.path.split("/")[2]
    sheet = client.get(f"/p/{pid}/s/Mappningar").text
    assert "ingen motsvarighet" in sheet and "Saknas i v3" in sheet
    assert "taget in" in client.get("/tx-forslag").text
    r = client.post("/tx-forslag/ta-in", data={"type": "ValueSet", "id": "Urval", "namespace": "ex"})
    assert "urval → ValueSet" in r.text
    vs_pid = r.url.path.split("/")[2]
    vs = client.get(f"/p/{vs_pid}/preview.json").json()["entry"][0]["resource"]
    assert vs["compose"]["include"][0]["concept"][0]["code"] == "M"


def test_take_in_ecl_valueset_uses_expansion_snapshot(tmp_path):
    from term_inlasning.admin import ops
    from term_inlasning.admin.store import Workspace
    sct = "http://snomed.info/sct"
    vs = {"resourceType": "ValueSet", "id": "Diabetes", "url": "http://example.org/fhir/ValueSet/diabetes",
          "status": "draft", "title": "Diabetes",
          "compose": {"include": [{"system": sct, "filter": [
              {"property": "constraint", "op": "=", "value": "<< 73211009"}]}]},
          "expansion": {"timestamp": "2026-11-06T18:00:00+00:00", "total": 2,
                        "parameter": [{"name": "used-codesystem",
                                       "valueUri": f"{sct}|{sct}/45991000052106/version/20251130"}],
                        "contains": [{"system": sct, "code": "44054006", "display": "diabetes mellitus typ 2"},
                                     {"system": sct, "code": "46635009", "display": "diabetes mellitus typ 1"}]}}
    ws = Workspace(tmp_path / "ws")
    project = ops.project_from_resource(ws, vs, "Diabetes", "anna")
    rows = ws.data(project.id)["Urval"].rows
    assert [r["Kod"] for r in rows] == ["44054006", "46635009"] and rows[0]["Kodsystem"] == sct
    assert any("constraint = << 73211009" in q.text and "45991000052106" in q.text for q in project.questions)
    partial = vs | {"id": "Stor", "expansion": vs["expansion"] | {"total": 5000}}
    with pytest.raises(ops.OpError, match="fullständig"):
        ops.project_from_resource(ws, partial, "Stor", "anna")
