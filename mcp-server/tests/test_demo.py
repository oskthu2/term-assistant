"""Demo-klienten hela vägen: fejkad Claude-API -> tool runner -> riktig MCP-server (underprocess) -> fejkad FHIR-server."""

import asyncio
import io
import json
import httpx2
import pytest

pytest.importorskip("anthropic")

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient  # noqa: E402

from term_mcp.demo import Session, call_tool, connect, list_tools, server_parameters  # noqa: E402

from .conftest import FakeFhir  # noqa: E402

def _message(content, stop_reason, n):
    return {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}}


def test_claude_drives_mcp_tools(fhir):
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append((request.headers.get("anthropic-beta"), body))
        last = body["messages"][-1]
        if isinstance(last["content"], list) and last["content"][0].get("type") == "tool_result":
            text = json.dumps(last["content"][0]["content"], ensure_ascii=False)
            answer = "Värdemängden är SEBaseMaritalStatusVS." if "SEBaseMaritalStatusVS" in text else "Hittade inget."
            return httpx2.Response(200, json=_message([{"type": "text", "text": answer}], "end_turn", 2))
        return httpx2.Response(200, json=_message([
            {"type": "text", "text": "Jag söker."},
            {"type": "tool_use", "id": "toolu_1", "name": "list_valuesets", "input": {"query": "marital"}}],
            "tool_use", 1))

    claude = AsyncAnthropic(api_key="test", http_client=DefaultAsyncHttpxClient(
        transport=httpx2.MockTransport(handler)))
    out = io.StringIO()

    async def run():
        async with connect(server_parameters(fhir, allow_write=False)) as mcp_session:
            dialog = Session(mcp_session, claude, out=out)
            answer = await dialog.ask("Vilken värdemängd för civilstånd finns?")
            return answer, dialog.history

    answer, history = asyncio.run(run())
    assert answer == "Värdemängden är SEBaseMaritalStatusVS."
    assert "→ list_valuesets(query=marital)" in out.getvalue()
    beta, first = seen[0]
    assert first["model"] == "claude-opus-5-5" and "server-side-fallback-2026-07-01" in beta
    assert first["fallbacks"] == "default" and first["output_config"] == {"effort": "medium"}
    names = {t["name"] for t in first["tools"]}
    assert {"lookup_code", "expand_valueset", "translate_code"} <= names
    assert "propose_conceptmap" not in names                  # förslagsverktygen är av som standard
    assert any(p.startswith("/fhir/ValueSet") for p in FakeFhir.requests)
    # Hela dialogen sparas för nästa instruktion: fråga, verktygsanrop, verktygssvar, svar.
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]


def test_tools_and_direct_call_without_claude(fhir, capsys):
    names = asyncio.run(list_tools(server_parameters(fhir, allow_write=True)))
    assert {"propose_valueset", "propose_conceptmap", "propose_translation", "withdraw_proposal"} <= set(names)
    assert "upsert_resource" not in names                       # inget generellt skrivverktyg
    data = asyncio.run(call_tool(server_parameters(fhir, allow_write=False), "server_info", {}))
    assert data["base_url"] == fhir and data["write_enabled"] is False
