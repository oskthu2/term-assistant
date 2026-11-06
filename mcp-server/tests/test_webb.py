"""Webbgränssnittet: samtal med strömmade steg och förslagslistan, med fejkad Claude-API och FHIR-server."""

import json

import httpx2
import pytest

pytest.importorskip("anthropic")
pytest.importorskip("markdown")

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from term_mcp.webb import create_app, render_markdown  # noqa: E402

from .test_demo import _message  # noqa: E402


def fake_claude():
    def handler(request: httpx2.Request) -> httpx2.Response:
        last = json.loads(request.content)["messages"][-1]
        if isinstance(last["content"], list) and last["content"][0].get("type") == "tool_result":
            return httpx2.Response(200, json=_message([{"type": "text", "text":
                "| Kod | Text |\n|---|---|\n| M | Gift |\n\n<script>x</script>"}], "end_turn", 2))
        return httpx2.Response(200, json=_message([
            {"type": "tool_use", "id": "toolu_1", "name": "draft_valueset", "input": {
                "url": "http://example.org/fhir/ValueSet/civil", "name": "Civil", "title": "Civilstånd",
                "concepts": [{"system": "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus", "code": "M"}]}}],
            "tool_use", 1))
    return AsyncAnthropic(api_key="test", http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))


def events(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.split("\n\n") if line.startswith("data: ")]


def test_conversation_streams_steps_and_offers_drafts_for_download(fhir):
    with TestClient(create_app(fhir, anthropic_factory=fake_claude, admin_url="http://admin")) as client:
        cfg = client.get("/api/config").json()
        assert cfg["tx"] == fhir and cfg["admin"] == "http://admin" and cfg["examples"]
        page = client.get("/").text
        assert "Terminologiassistent" in page and "endast läsning" in page

        conv = client.post("/api/samtal").json()
        assert "draft_conceptmap" in conv["tools"] and not any(t.startswith("propose_") for t in conv["tools"])
        cid = conv["id"]
        assert client.get(f"/api/samtal/{cid}/utkast").json() == []
        assert client.get(f"/api/samtal/{cid}/utkast.json").status_code == 404

        steps = events(client.post(f"/api/samtal/{cid}/fraga", json={"instruktion": "Gör ett urval av civilstånd"}))
        assert [e["type"] for e in steps] == ["tool_use", "tool_result", "text", "done"]
        assert steps[0]["name"] == "draft_valueset" and "utkast://ValueSet/Civil" in steps[1]["text"]
        assert "<table>" in steps[2]["html"] and "<script>" not in steps[2]["html"]   # markdown, men ingen HTML

        listed = client.get(f"/api/samtal/{cid}/utkast").json()
        assert listed[0]["id"] == "Civil" and listed[0]["codes"] == 1
        r = client.get(f"/api/samtal/{cid}/utkast.json")
        assert "attachment" in r.headers["content-disposition"] and r.json()["entry"][0]["resource"]["id"] == "Civil"
        r = client.get(f"/api/samtal/{cid}/utkast.tgz")
        assert r.headers["content-type"] == "application/gzip" and r.content[:2] == b"\x1f\x8b"
        assert client.get(f"/api/samtal/{cid}/utkast/ValueSet/Civil.json").json()["url"].endswith("/civil")
        assert client.get(f"/api/samtal/{cid}/utkast.exe").status_code == 404

        assert client.post(f"/api/samtal/{cid}/fraga", json={"instruktion": " "}).status_code == 400
        assert client.post("/api/samtal/finns-inte/fraga", json={"instruktion": "x"}).status_code == 404
        assert client.post(f"/api/samtal/{cid}/nollstall").json() == {"ok": True}


def test_markdown_is_escaped():
    assert render_markdown("**a** <b>x</b>") == "<p><strong>a</strong> &lt;b&gt;x&lt;/b&gt;</p>"
    assert render_markdown("ECL `<< 44054006`") == "<p>ECL <code>&lt;&lt; 44054006</code></p>"
    assert "<script>" not in render_markdown("<script>alert(1)</script>\n\n<img src=x onerror=y>")
    assert "<a" not in render_markdown("<a href='javascript:x'>a</a>")
    assert "javascript" not in render_markdown("[klicka](javascript:alert(1)) ![b](javascript:x)")
    assert 'href="https://tx-nordics.fhir.org"' in render_markdown("[tx](https://tx-nordics.fhir.org)")
