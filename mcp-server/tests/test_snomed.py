"""SNOMED CT mot Ontoserver: standardedition, ECL, kompakta uppslag, OAuth2-token och ECL-utkast."""

import asyncio
import json

import httpx2
import pytest

from term_mcp import drafts as proposals
from term_mcp.config import SNOMED, ServerConfig, snomed_edition
from term_mcp.fhir.client import TerminologyClient, TerminologyError
from term_mcp.server import _compact_snomed

SE = "http://snomed.info/sct/45991000052106"


def run(coro):
    return asyncio.run(coro)


def test_edition_parsing():
    assert snomed_edition("se") == snomed_edition("45991000052106") == SE
    assert snomed_edition(f"{SE}/version/20251130") == f"{SE}/version/20251130"
    assert snomed_edition("") is None
    with pytest.raises(ValueError):
        snomed_edition("svenska")


def _client(handler, **cfg):
    return TerminologyClient(ServerConfig(base_url="https://tx.example.org/fhir", **cfg),
                             transport=httpx2.MockTransport(handler))


def test_edition_is_sent_for_snomed_only():
    seen = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        seen.append((request.url.path, dict(request.url.params.multi_items()), body))
        if request.url.path.endswith("$expand"):
            return httpx2.Response(200, json={"resourceType": "ValueSet", "expansion": {"total": 0, "contains": []}})
        return httpx2.Response(200, json={"resourceType": "Parameters", "parameter": []})

    tx = _client(handler, snomed_edition=SE)
    run(tx.lookup(SNOMED, "73211009"))
    run(tx.lookup("http://loinc.org", "8867-4"))
    run(tx.lookup(SNOMED, "73211009", version=f"{SNOMED}/900000000000207008"))   # uttrycklig version vinner
    run(tx.validate_code("44054006", system=SNOMED))
    run(tx.validate_code("44054006", valueset="http://ex.org/vs"))
    run(tx.subsumes(SNOMED, "73211009", "44054006"))
    run(tx.expand(url=f"{SNOMED}?fhir_vs=isa/73211009"))
    run(tx.ecl("<< 73211009", filter="typ 2"))
    lookup, loinc, explicit, validate_cs, validate_vs, subsumes, expand, ecl = seen
    assert lookup[1]["version"] == SE and "version" not in loinc[1]
    assert explicit[1]["version"] == f"{SNOMED}/900000000000207008"
    assert validate_cs[1]["version"] == SE and validate_vs[1]["system-version"] == f"{SNOMED}|{SE}"
    assert subsumes[1]["version"] == SE and expand[1]["system-version"] == f"{SNOMED}|{SE}"
    params = {p["name"]: p for p in ecl[2]["parameter"]}
    include = params["valueSet"]["resource"]["compose"]["include"][0]
    assert include == {"system": SNOMED, "filter": [{"property": "constraint", "op": "=", "value": "<< 73211009"}]}
    assert params["system-version"]["valueCanonical"] == f"{SNOMED}|{SE}" and params["filter"]["valueString"] == "typ 2"
    # utan edition skickas ingen version alls
    seen.clear()
    run(_client(handler).lookup(SNOMED, "73211009"))
    assert "version" not in seen[0][1]


def test_oauth_client_credentials_token_is_cached_and_refreshed():
    calls = {"token": 0, "auth": []}

    def handler(request):
        if request.url.path == "/token":
            calls["token"] += 1
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            assert form["grant_type"] == "client_credentials" and form["client_id"] == "mcp"
            return httpx2.Response(200, json={"access_token": f"t{calls['token']}", "expires_in": 3600})
        calls["auth"].append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer t1" and len(calls["auth"]) == 3:
            return httpx2.Response(401)  # tredje anropet: token återkallad
        return httpx2.Response(200, json={"resourceType": "Parameters", "parameter": []})

    tx = _client(handler, token_url="https://idp.example.org/token", client_id="mcp", client_secret="hemligt")
    for _ in range(3):
        run(tx.lookup("http://x", "A"))
    assert calls["token"] == 2 and calls["auth"] == ["Bearer t1", "Bearer t1", "Bearer t1", "Bearer t2"]

    def refused(request):
        return httpx2.Response(401, text="invalid_client")
    with pytest.raises(TerminologyError, match="Ingen åtkomsttoken"):
        run(_client(refused, token_url="https://idp.example.org/token", client_id="mcp").lookup("http://x", "A"))


def test_compact_snomed_lookup():
    raw = {"code": "73211009", "display": "diabetes mellitus", "version": f"{SE}/version/20251130",
           "property": [{"code": "parent", "value": "362969004"}, {"code": "child", "value": "44054006"},
                        {"code": "child", "value": "46635009"}, {"code": "inactive", "value": False}],
           "designation": [
               {"language": "en", "use": {"display": "Synonym"}, "value": "DM - Diabetes mellitus"},
               {"language": "en", "use": {"code": "display"}, "value": "Diabetes mellitus"},
               {"language": "en-x-sctlang-90000000-00005090-07", "use": {"code": "preferredForLanguage"},
                "value": "Diabetes mellitus"},
               {"language": "sv", "use": {"display": "Synonym"}, "value": "diabetes"},
               {"language": "sv-x-sctlang-46011000-052107", "use": {"code": "preferredForLanguage"},
                "value": "diabetes mellitus"}]}
    out = _compact_snomed(raw)
    assert out["parents"] == ["362969004"] and out["children_count"] == 2 and out["properties"] == {"inactive": False}
    assert out["terms"] == [{"language": "en", "value": "DM - Diabetes mellitus", "use": "Synonym"},
                            {"language": "en", "value": "Diabetes mellitus", "use": "föredragen"},
                            {"language": "sv", "value": "diabetes", "use": "Synonym"},
                            {"language": "sv", "value": "diabetes mellitus", "use": "föredragen"}]


class Server:
    """Fejkad FHIR-server: ECL-expansion (bara på lässervern) och lagring av resurser."""

    def __init__(self, name, ecl=None, existing=None):
        self.config = ServerConfig(base_url=f"https://{name}.example.org/fhir")
        self.ecl_result = ecl
        self.store = {(r["resourceType"], r["id"]): r for r in existing or []}
        self.puts = []

    async def ecl(self, expression, count=50, **kw):
        if self.ecl_result is None:
            raise TerminologyError("400 ValueSet/$expand: Invalid ECL")
        return self.ecl_result

    async def validate_code(self, code, system=None, **kw):
        return {"result": True, "display": code}

    async def search(self, rtype, count=20, url=None, **kw):
        return [r for (t, _), r in self.store.items() if t == rtype and (url is None or r.get("url") == url)]

    async def read(self, rtype, rid):
        return self.store.get((rtype, rid))

    async def put_resource(self, r):
        self.puts.append(r)
        self.store[(r["resourceType"], r["id"])] = r
        return r


EXPANSION = {"total": 2, "contains": [
    {"system": SNOMED, "code": "44054006", "display": "diabetes mellitus typ 2", "version": f"{SE}/version/20251130"},
    {"system": SNOMED, "code": "46635009", "display": "diabetes mellitus typ 1", "version": f"{SE}/version/20251130"}]}


def test_ecl_valueset_draft_with_snapshot():
    onto = Server("onto", ecl=EXPANSION)
    r = run(proposals.valueset(onto, "http://example.org/fhir/ValueSet/diabetes", "Diabetes", "Diabetes", [],
                               ecl="<< 73211009"))
    assert r["created"] and r["ecl_concepts"] == 2 and r["snapshot"] and not onto.puts
    vs = r["resource"]
    assert vs["compose"]["include"] == [{"system": SNOMED, "filter": [
        {"property": "constraint", "op": "=", "value": "<< 73211009"}]}]
    assert [c["code"] for c in vs["expansion"]["contains"]] == ["44054006", "46635009"]
    assert vs["expansion"]["parameter"] == [{"name": "used-codesystem", "valueUri": f"{SNOMED}|{SE}/version/20251130"}]
    with pytest.raises(proposals.ProposalError, match="godtogs inte"):
        run(proposals.valueset(Server("onto"), "http://x/vs", "X", "X", [], ecl="<<< 7321"))
    with pytest.raises(proposals.ProposalError, match="inga begrepp"):
        run(proposals.valueset(Server("onto", ecl={"total": 0, "contains": []}), "http://x/vs2", "X2", "X", [],
                               ecl="<< 1"))
    with pytest.raises(proposals.ProposalError, match="koder"):
        run(proposals.valueset(onto, "http://x/vs3", "X3", "X", []))
