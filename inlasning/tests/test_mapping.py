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


def _tgz(files: dict[str, object]) -> bytes:
    import io
    import json as _json
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content if isinstance(content, bytes) else _json.dumps(content).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_take_in_drafts_from_bundle_and_package(client):
    import json as _json
    full_cs = {"resourceType": "CodeSystem", "id": "Eget", "url": "http://ex.org/cs", "content": "complete"}
    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"resource": PROPOSAL}, {"resource": VS_PROPOSAL}, {"resource": full_cs}]}
    r = client.post("/forslag/fil", files={"file": ("utkast.json", _json.dumps(bundle).encode())})
    assert "3 resurser i utkast.json" in _msg(r.text) and "Utkast i utkast.json" in r.text
    assert "Kodsystem tas in via Ny inläsning" in r.text                    # kan inte tas in härifrån
    file_url = r.url.path
    r = client.post(f"{file_url}/ta-in", data={"index": "0", "namespace": "ex"})
    assert "taget in som projekt" in _msg(r.text) and "mappning" in r.text
    page = client.get(file_url).text
    assert page.count("taget in") == 1                                       # samma ursprung känns igen
    r = client.post(f"{file_url}/ta-in", data={"index": "2", "namespace": "ex"})
    assert "Kodsystem tas in" in _msg(r.text)
    assert "finns inte" in _msg(client.post(f"{file_url}/ta-in", data={"index": "9"}).text)

    package = _tgz({"package/package.json": {"name": "utkast"}, "package/.index.json": {"files": []},
                    "package/ValueSet-Urval.json": VS_PROPOSAL, "package/other/readme.txt": b"x",
                    "package/trasig.json": b"{inte json"})
    r = client.post("/forslag/fil", files={"file": ("utkast.tgz", package)})
    assert "1 resurser i utkast.tgz" in _msg(r.text)
    r = client.post(f"{r.url.path}/ta-in", data={"index": "0", "namespace": "ex"})
    assert "taget in som projekt" in _msg(r.text) and "Married" in client.get(
        f"/p/{r.url.path.split('/')[2]}/s/Urval").text

    for bad, message in ((b"hej", "varken JSON"), (b'{"a": 1}', "ingen FHIR-resurs"),
                         (b"\x1f\x8bskrap", "inget läsbart FHIR-paket"),
                         (b'{"resourceType": "Bundle", "entry": []}', "inga FHIR-resurser")):
        assert message in _msg(client.post("/forslag/fil", files={"file": ("x.json", bad)}).text)


def test_package_limits(monkeypatch):
    from term_inlasning.admin import ops
    monkeypatch.setattr(ops, "FHIR_PACKAGE_TOTAL", 100)
    with pytest.raises(ops.OpError, match="för stort"):
        ops.resources_from_fhir_file(_tgz({"package/a.json": {"resourceType": "ValueSet", "pad": "x" * 200}}))
    monkeypatch.setattr(ops, "FHIR_PACKAGE_FILES", 2)
    with pytest.raises(ops.OpError, match="fler än 2"):
        ops.resources_from_fhir_file(_tgz({f"package/{i}.json": {"resourceType": "ValueSet"} for i in range(5)}))


def test_taken_in_draft_gets_origin_provenance_in_first_release_only(client, tmp_path):
    import json as _json
    from term_inlasning.admin import ops
    from term_inlasning.admin.store import Workspace
    draft = dict(PROPOSAL, publisher="Förslag från term-mcp (AI-stött, ej granskat)", date="2026-11-06",
                 description="Förslag: Norska civilstånd till v3. Motivering: Civilstånd från norska system.")
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": draft}]}
    r = client.post("/forslag/fil", files={"file": ("utkast.json", _json.dumps(bundle).encode())})
    r = client.post(f"{r.url.path}/ta-in", data={"index": "0", "namespace": "ex"})
    pid = r.url.path.split("/")[2]
    ws = Workspace(tmp_path / "ws")
    project = ws.get(pid)
    assert project.template.codesystem.description == "Civilstånd från norska system."   # märkningen är borta
    origin = project.origin
    assert origin["ai"] and origin["source"] == "fil utkast.json" and len(origin["sha256"]) == 64
    assert origin["url"] == PROPOSAL["url"] and origin["description"].startswith("Förslag:")
    client.post(f"/p/{pid}/releases", data={"version": "1.0.0", "notes": "Första"})
    rel = next(r for r in ws.releases(pid) if r.version == "1.0.0-rc.1")
    first = ops.provenance_bundle(ws, ws.get(pid), rel)
    intag = first["entry"][0]["resource"]
    assert intag["id"].startswith("intag-") and intag["activity"]["coding"][0]["code"] == "CREATE"
    assert intag["agent"][0]["who"]["reference"] == "#utkast" and intag["contained"][0]["resourceType"] == "Device"
    assert any(e["what"]["identifier"]["value"] == origin["sha256"] for e in intag["entity"])
    # Ursprunget hör till grundinnehållet: det följer med den första releasen men inte senare.
    from term_inlasning.admin.store import Release
    ws.save_release(pid, Release(version="1.0.0", status="release", resources=rel.resources, based_on=rel.version))
    later = Release(version="1.1.0", status="release", resources=rel.resources, created="9999-01-01T00:00:00+00:00")
    assert [e["resource"]["id"].split("-")[0] for e in ops.provenance_bundle(ws, ws.get(pid), later)["entry"]] \
        == ["release"]

