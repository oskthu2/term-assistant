"""Översättningsprojekt (CodeSystem-supplement): skapa, översätta, kontrollera, hämta nya koder, släppa
och ta in förslag från terminologitjänsten."""

import re

import pytest
from starlette.testclient import TestClient

from term_inlasning.admin.app import create_app
from term_inlasning.admin.settings import Editorial, Namespace, Settings
from term_inlasning.importer import supplement
from term_inlasning.importer.mapping import CodeSystemMeta, TranslationSpec

FAM = "http://hl7.no/fhir/CodeSystem/familie"
BASE = {"far": "Far", "mor": "Mor", "barn": "Barn"}
PROPOSAL = {"resourceType": "CodeSystem", "id": "Familie-sv", "url": "http://example.org/fhir/CodeSystem/Familie-sv",
            "version": "0.1.0", "name": "Familie_sv", "title": "Familierelasjon (sv)", "status": "draft",
            "description": "Förslag: Familierelasjon (sv).", "language": "sv", "content": "supplement",
            "supplements": FAM, "meta": {"tag": [{"system": "urn:term-assistant:arbetsflode", "code": "forslag"}]},
            "concept": [{"code": "far", "display": "far", "designation": [{"language": "sv", "value": "far"}]},
                        {"code": "syskon", "display": "syskon"}]}


class FakeTx:
    codes = dict(BASE)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def read_canonical(self, rtype, url, version=None):
        return {"url": url, "name": "Familie", "title": "Familierelasjon"} if url == FAM else None

    async def expand(self, url=None, valueset=None, filter=None, count=50, offset=0):
        return {"contains": [{"system": FAM, "code": c, "display": d} for c, d in FakeTx.codes.items()]}

    async def search(self, rtype, count=20, **kw):
        return [PROPOSAL] if rtype == "CodeSystem" else []

    async def read(self, rtype, rid):
        return PROPOSAL if rid == "Familie-sv" else None


@pytest.fixture
def client(tmp_path):
    FakeTx.codes = dict(BASE)
    root = tmp_path / "ws"
    root.mkdir()
    Settings(namespaces=[Namespace(key="ex", label="Exempel", base_url="http://example.org/fhir",
                                   publisher="Exempelorganisationen")],
             editorial=Editorial(review_manual_edits=False)).save(root)
    return TestClient(create_app(root, tx_client_factory=FakeTx), follow_redirects=True)


def _msg(html: str) -> str:
    m = re.search(r'class="msg">([^<]*)', html)
    return m.group(1) if m else ""


def test_supplement_resource():
    tpl = supplement.template(CodeSystemMeta(url="http://ex.org/cs/fam-sv", name="Fam_sv", language="sv"),
                              TranslationSpec(codesystem=FAM, codesystem_version="2.0.8"))
    cs, warnings = supplement.build_supplement(tpl, [{"Kod": "far", "Översättning": "far"}, {"Kod": "mor"}])
    assert (cs["content"], cs["supplements"], cs["language"], cs["count"]) == ("supplement", f"{FAM}|2.0.8", "sv", 1)
    assert cs["concept"] == [{"code": "far", "display": "far", "designation": [{"language": "sv", "value": "far"}]}]
    assert "1 av 2 koder saknar översättning" in warnings[0]
    assert supplement.translations_from_supplement(PROPOSAL) == {"far": "far", "syskon": "syskon"}


def test_create_translate_check_and_release(client):
    r = client.post("/oversattning/ny", data={"codesystem": FAM, "language": "svenska", "namespace": "ex"})
    assert r.status_code == 400 and "språkkod" in r.text
    r = client.post("/oversattning/ny", data={"codesystem": FAM, "language": "sv", "namespace": "ex",
                                              "description": "Svenska texter för familjerelationer."})
    assert "skapat med 3 koder" in _msg(r.text) and "0 av 3 översatta" in r.text
    pid = r.url.path.split("/")[2]
    # c0 Kod (låst), c1 Originaltext (låst), c2 Översättning, c3 Kommentar
    form = client.get(f"/p/{pid}/s/%C3%96vers%C3%A4ttningar/c/far").text
    assert 'name="c1" value="Far" readonly' in form
    client.post(f"/p/{pid}/s/%C3%96vers%C3%A4ttningar/c/far", data={"c2": "far"})
    client.post(f"/p/{pid}/s/%C3%96vers%C3%A4ttningar/c/mor", data={"c2": "mor"})
    FakeTx.codes = {"far": "Far (ny text)", "mor": "Mor", "barn": "Barn", "medmor": "Medmor"}
    r = client.post(f"/p/{pid}/check")
    assert "1 rader saknar översättning, 1 rader behöver hanteras" in _msg(r.text) and "1 nya koder" in _msg(r.text)
    r = client.post(f"/p/{pid}/hamta-koder")
    assert "1 nya koder lades till" in _msg(r.text) and "2 av 4 översatta" in r.text
    r = client.post(f"/p/{pid}/releases", data={"version": "1.0.0", "notes": "Första"})
    assert r.url.path.endswith("/releases/1.0.0-rc.1"), r.text[:300]
    cs = client.get(f"/p/{pid}/releases/1.0.0-rc.1/bundle.json").json()["entry"][0]["resource"]
    assert cs["url"] == "http://example.org/fhir/CodeSystem/Familie_sv" and cs["supplements"] == FAM
    assert [c["code"] for c in cs["concept"]] == ["far", "mor"] and cs["publisher"] == "Exempelorganisationen"


def test_take_in_translation_proposal(client):
    page = client.get("/tx-forslag").text
    assert "Familierelasjon (sv)" in page and "Översättningar granskas" not in page
    r = client.post("/tx-forslag/ta-in", data={"type": "CodeSystem", "id": "Familie-sv", "namespace": "ex"})
    assert "taget in som projekt" in _msg(r.text) and "översättning → CodeSystem-supplement" in r.text
    assert "syskon" in r.text                       # översatt kod som inte finns i kodsystemet blir en fråga
    pid = r.url.path.split("/")[2]
    sheet = client.get(f"/p/{pid}/s/%C3%96vers%C3%A4ttningar").text
    assert "2 av 4 översatta" in sheet               # alla kodsystemets koder + den okända
    assert "taget in" in client.get("/tx-forslag").text
