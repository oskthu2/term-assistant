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
            {"type": "tool_use", "id": "toolu_1", "name": "list_valuesets", "input": {"query": "marital"}}],
            "tool_use", 1))
    return AsyncAnthropic(api_key="test", http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))


def events(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.split("\n\n") if line.startswith("data: ")]


def test_conversation_streams_steps_and_lists_proposals(fhir):
    with TestClient(create_app(fhir, anthropic_factory=fake_claude, admin_url="http://admin")) as client:
        cfg = client.get("/api/config").json()
        assert cfg["tx"] == fhir and cfg["admin"] == "http://admin" and cfg["examples"]
        assert "Terminologiassistent" in client.get("/").text

        conv = client.post("/api/samtal", json={"forslag": True}).json()
        assert conv["forslag"] and "propose_conceptmap" in conv["tools"]
        read_only = client.post("/api/samtal", json={"forslag": False}).json()
        assert "propose_conceptmap" not in read_only["tools"]

        steps = events(client.post(f"/api/samtal/{conv['id']}/fraga", json={"instruktion": "Vilka civilstånd finns?"}))
        kinds = [e["type"] for e in steps]
        assert kinds == ["tool_use", "tool_result", "text", "done"]
        assert steps[0]["name"] == "list_valuesets" and "SEBaseMaritalStatusVS" in steps[1]["text"]
        assert "<table>" in steps[2]["html"] and "<script>" not in steps[2]["html"]   # markdown, men ingen HTML

        assert client.post(f"/api/samtal/{conv['id']}/fraga", json={"instruktion": " "}).status_code == 400
        assert client.post("/api/samtal/finns-inte/fraga", json={"instruktion": "x"}).status_code == 404
        assert isinstance(client.get(f"/api/samtal/{conv['id']}/forslag").json(), list)
        assert client.post(f"/api/samtal/{conv['id']}/nollstall").json() == {"ok": True}


def test_markdown_is_escaped():
    assert render_markdown("**a** <b>x</b>") == "<p><strong>a</strong> &lt;b&gt;x&lt;/b&gt;</p>"
