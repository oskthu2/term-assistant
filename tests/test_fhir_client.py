import asyncio
import json

import httpx2

from term_assistant.config import ServerConfig
from term_assistant.fhir.client import TerminologyClient, TerminologyError
from term_assistant.fhir.params import parameters_to_dict

LOOKUP = {
    "resourceType": "Parameters",
    "parameter": [
        {"name": "name", "valueString": "Test"},
        {"name": "display", "valueString": "Hjärtfrekvens"},
        {"name": "property", "part": [{"name": "code", "valueCode": "parent"}, {"name": "value", "valueCode": "P"}]},
        {"name": "property", "part": [{"name": "code", "valueCode": "child"}, {"name": "value", "valueCode": "C1"}]},
        {"name": "designation", "part": [{"name": "language", "valueCode": "en"}, {"name": "value", "valueString": "Heart rate"}]},
    ],
}


def test_parameters_to_dict():
    d = parameters_to_dict(LOOKUP)
    assert d["display"] == "Hjärtfrekvens"
    assert d["property"] == [{"code": "parent", "value": "P"}, {"code": "child", "value": "C1"}]
    assert d["designation"] == [{"language": "en", "value": "Heart rate"}]


def _client(handler):
    return TerminologyClient(ServerConfig(base_url="https://tx.example.org/fhir", token="secret"),
                             transport=httpx2.MockTransport(handler))


def test_hierarchy_and_headers():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["params"] = request.url.params.get_list("property")
        return httpx2.Response(200, json=LOOKUP)

    result = asyncio.run(_client(handler).hierarchy("http://x", "A"))
    assert result["parents"] == ["P"] and result["children"] == ["C1"]
    assert seen["auth"] == "Bearer secret"
    assert seen["params"] == ["parent", "child"]


def test_error_uses_operation_outcome():
    def handler(request):
        return httpx2.Response(404, json={"resourceType": "OperationOutcome",
                                          "issue": [{"diagnostics": "Okänt kodsystem"}]})

    try:
        asyncio.run(_client(handler).lookup("http://okänt", "X"))
    except TerminologyError as exc:
        assert "Okänt kodsystem" in str(exc)
    else:
        raise AssertionError("förväntade TerminologyError")


def test_expand_inline_valueset_posts_parameters():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json={"resourceType": "ValueSet",
                                          "expansion": {"total": 1, "contains": [{"code": "A", "display": "Alfa"}]}})

    vs = {"resourceType": "ValueSet", "compose": {"include": [{"system": "http://x"}]}}
    out = asyncio.run(_client(handler).expand(valueset=vs, filter="alf"))
    names = [p["name"] for p in captured["body"]["parameter"]]
    assert "valueSet" in names and "filter" in names
    assert out["contains"] == [{"code": "A", "display": "Alfa"}]
