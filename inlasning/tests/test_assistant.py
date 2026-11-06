"""AI-assistenten i projektet: fejkad Claude-API, en riktig MCP-underprocess (fejkad server) och förslag i
den redaktionella kedjan som kontrolleras mot terminologitjänsten innan de sparas."""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
anthropic = pytest.importorskip("anthropic")
import httpx2  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from term_inlasning.admin import assistant  # noqa: E402
from term_inlasning.admin.app import create_app  # noqa: E402
from term_inlasning.admin.settings import Editorial, Namespace, Settings  # noqa: E402
from term_inlasning.admin.store import Workspace  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_mapping import NO_VS, V3, FakeTx, _msg  # noqa: E402

FAKE_MCP = [sys.executable, str(Path(__file__).with_name("fake_mcp_server.py"))]


def _message(content, stop_reason, n):
    return {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}}


class FakeClaude:
    """Spelar en assistent: läser ofullständiga rader, söker i MCP-servern, föreslår ändringar, sammanfattar."""

    def __init__(self):
        self.requests: list[dict] = []

    def __call__(self):
        return anthropic.AsyncAnthropic(api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(self.handler)))

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        last = body["messages"][-1]["content"]
        answered = {b.get("tool_use_id") for b in last if isinstance(b, dict)} if isinstance(last, list) else set()
        if "t3" in answered:
            return httpx2.Response(200, json=_message([{"type": "text", "text":
                "Ett förslag skapades (M1 → **M**). M2 och M3 behöver en människa."}], "end_turn", 3))
        if "t1" in answered:
            return httpx2.Response(200, json=_message([{"type": "tool_use", "id": "t3", "name": "propose_changes",
                "input": {"changes": [
                    {"row": "M1", "values": {"Relation": "likvärdig", "Målkod": "M"}, "rationale": "Gift = Married"},
                    {"row": "M2", "values": {"Relation": "likvärdig", "Målkod": "X"}, "rationale": "gissning"},
                    {"row": "M3", "values": {"Relation": "närliggande", "Målkod": "S"}, "rationale": "nära"},
                    {"row": "M9", "values": {"Målkod": "M"}},
                    {"row": "M1", "values": {"Nr": "Z"}}]}}], "tool_use", 2))
        return httpx2.Response(200, json=_message([
            {"type": "text", "text": "Jag läser raderna som saknar mål."},
            {"type": "tool_use", "id": "t1", "name": "project_rows", "input": {"filter": "ofullständiga"}},
            {"type": "tool_use", "id": "t2", "name": "search_concepts", "input": {"system": V3, "text": "married"}}],
            "tool_use", 1))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("TERM_DEV_USERS", "anna,bertil")
    root = tmp_path / "ws"
    root.mkdir()
    Settings(namespaces=[Namespace(key="ex", label="Exempel", base_url="http://example.org/fhir",
                                   publisher="Exempelorganisationen")],
             editorial=Editorial(review_manual_edits=False)).save(root)
    claude = FakeClaude()
    client = TestClient(create_app(root, tx_client_factory=FakeTx, assistant_client_factory=claude,
                                   mcp_command=FAKE_MCP), follow_redirects=True)
    client.cookies.set("dev_user", "anna")
    r = client.post("/mappning/ny", data={"title": "Norska civilstånd", "source": NO_VS, "target": V3,
                                          "namespace": "ex", "fill": "on", "description": "Civilstånd."})
    return client, claude, root, r.url.path.split("/")[2]


def events(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.split("\n\n") if line.startswith("data: ")]


def test_assistant_proposes_checked_changes_for_review(setup):
    client, claude, root, pid = setup
    page = client.get(f"/p/{pid}/assistent").text
    assert "AI-assistent" in page and "Föreslå mål för de rader som saknar målkod." in page

    steps = events(client.post(f"/p/{pid}/assistent/fraga", json={"instruktion": "Föreslå mål för omappade rader"}))
    kinds = [e["type"] for e in steps]
    assert kinds == ["text", "tool_use", "tool_use", "tool_result", "tool_result", "tool_use", "tool_result",
                     "text", "done"], steps
    rows, search = steps[3], steps[4]
    assert json.loads(rows["text"])["totalt"] == 3 and "Married" in search["text"]
    result = json.loads(steps[6]["text"])
    reasons = {s["row"]: s["orsak"] for s in result["hoppade_över"]}
    assert result["skapade"] == 1
    assert "målkoden X finns inte" in reasons["M2"] and "motiveras" in reasons["M3"]
    assert reasons["M9"] == "raden finns inte" and "Nr" in [s for s in result["hoppade_över"] if s["row"] == "M1"][0]["orsak"]
    assert steps[-1]["created"] == 1 and "<strong>M</strong>" in steps[-2]["html"]

    # Claude fick projektets sammanhang, MCP-serverns läsande verktyg och projektets egna verktyg – inga utkastverktyg.
    first = claude.requests[0]
    names = {t["name"] for t in first["tools"]}
    assert {"project_rows", "propose_changes", "search_concepts", "lookup_code"} <= names
    assert "draft_valueset" not in names
    assert first["messages"][0]["content"].startswith("Projektet:") and V3 in first["messages"][0]["content"]

    # Förslaget ligger i kedjan med proveniens – raden är oförändrad tills någon godkänner.
    ws = Workspace(root)
    [proposal] = ws.proposals(pid)
    assert proposal.origin == "ai" and proposal.author == "anna" and proposal.changes == {"Relation": "likvärdig", "Målkod": "M", "Måltext": "Married"}
    prov = proposal.provenance
    assert prov["instruction"] == "Föreslå mål för omappade rader" and prov["kind"] == "assistent"
    assert prov["mcp_server"] == {"base_url": "https://tx.example.org/fhir", "software": "Fake 1.0",
                                  "snomed_edition": "http://snomed.info/sct/45991000052106"}
    assert prov["tools"] == {"project_rows": 1, "search_concepts": 1, "propose_changes": 1}
    assert prov["verified"] == [{"system": V3, "code": "M", "version": None, "display": "Married"}]
    assert next(r for r in ws.data(pid)["Mappningar"].rows if r["Nr"] == "M1").get("Målkod") is None
    r = client.post(f"/p/{pid}/proposals/{proposal.id}/accept")
    assert "godkänd" in _msg(r.text)
    row = next(r for r in Workspace(root).data(pid)["Mappningar"].rows if r["Nr"] == "M1")
    assert (row["Målkod"], row["Måltext"], row["Relation"]) == ("M", "Married", "likvärdig")

    # Följdfråga: dialogen fortsätter; samma förslag skapas inte två gånger.
    steps = events(client.post(f"/p/{pid}/assistent/fraga", json={"instruktion": "Gör om det"}))
    assert claude.requests[-3]["messages"][0]["content"].startswith("Projektet:")      # historiken följer med
    assert claude.requests[-3]["messages"][-1]["content"] == "Gör om det"
    assert "Dialogen fortsätter" in client.get(f"/p/{pid}/assistent").text
    client.post(f"/p/{pid}/assistent/ny")
    assert "Dialogen fortsätter" not in client.get(f"/p/{pid}/assistent").text


def test_assistant_errors_and_access(setup, monkeypatch):
    client, _, root, pid = setup
    assert client.post(f"/p/{pid}/assistent/fraga", json={"instruktion": " "}).status_code == 400
    assert client.post("/p/finns-inte/assistent/fraga", json={"instruktion": "x"}).status_code == 404
    # MCP-servern saknas: ett begripligt fel, ingen krasch
    app = create_app(root, tx_client_factory=FakeTx, assistant_client_factory=FakeClaude(),
                     mcp_command=["finns-inte-term-mcp"])
    steps = events(TestClient(app).post(f"/p/{pid}/assistent/fraga", json={"instruktion": "x"}))
    assert steps[-1]["type"] == "error" and "MCP-servern kunde inte startas" in steps[-1]["text"]
    # en läsare ser varken fliken eller sidan
    monkeypatch.setenv("TERM_DEV_USERS", "lisa")
    from term_inlasning.admin import auth
    auth.RoleStore(root).assign("lisa", {"läsare"}, by="test")
    reader = TestClient(create_app(root, tx_client_factory=FakeTx))
    reader.cookies.set("dev_user", "lisa")
    assert "AI-assistent" not in reader.get(f"/p/{pid}").text
    assert reader.get(f"/p/{pid}/assistent").status_code == 403


def test_markdown_is_safe():
    pytest.importorskip("markdown")
    html = assistant.render_markdown("**a** <script>x</script> [l](javascript:alert(1)) `<< 1`")
    assert "<strong>a</strong>" in html and "<script>" not in html and "javascript" not in html
    assert "<code>&lt;&lt; 1</code>" in html


def test_assistant_proposal_becomes_structured_provenance(setup):
    from term_inlasning.admin import ops
    client, _, root, pid = setup
    events(client.post(f"/p/{pid}/assistent/fraga", json={"instruktion": "Föreslå mål för omappade rader"}))
    [proposal] = Workspace(root).proposals(pid)
    client.cookies.set("dev_user", "bertil")
    client.post(f"/p/{pid}/proposals/{proposal.id}/accept")
    client.cookies.set("dev_user", "anna")
    client.post(f"/p/{pid}/releases", data={"version": "1.0.0", "notes": "Första"})
    client.cookies.set("dev_user", "bertil")
    client.post(f"/p/{pid}/releases/1.0.0-rc.1/approve")
    ws = Workspace(root)
    bundle = ops.provenance_bundle(ws, ws.get(pid), ws.release(pid, "1.0.0"))
    prov, release = (e["resource"] for e in bundle["entry"])
    assert prov["contained"][0] == {"resourceType": "Device", "id": "ai", "manufacturer": "Anthropic",
                                    "deviceName": [{"name": "claude-opus-5-5", "type": "model-name"}],
                                    "type": {"text": "AI-modell (språkmodell)"}}
    roles = {a["type"]["coding"][0]["code"]: a["who"] for a in prov["agent"]}
    assert roles["author"]["reference"] == "#ai"
    assert roles["enterer"]["identifier"] == {"system": "urn:term-assistant:anvandare", "value": "anna"}
    assert roles["verifier"]["identifier"]["value"] == "bertil"
    assert prov["activity"]["coding"][0]["code"] == "UPDATE" and "AI-assistenten" in prov["activity"]["text"]
    shown = [e["what"]["display"] for e in prov["entity"]]
    assert "Instruktion: Föreslå mål för omappade rader" in shown
    assert any(d.startswith(f"{V3}|- kod M") for d in shown)
    assert "Verktygsanrop via MCP: project_rows, search_concepts, propose_changes" in shown
    assert not any("SNOMED" in d for d in shown)                     # SNOMED användes inte i den här mappningen
    assert prov["text"]["status"] == "generated" and "Instruktion" in prov["text"]["div"]
    assert release["activity"]["coding"][0] == {"system": "http://terminology.hl7.org/CodeSystem/v3-DocumentCompletion",
                                                "code": "LA", "display": "legally authenticated"}
    assert all(e["fullUrl"].startswith("urn:uuid:") for e in bundle["entry"])
    assert "extension" not in prov
