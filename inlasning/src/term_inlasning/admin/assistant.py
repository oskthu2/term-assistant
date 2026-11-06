"""AI-assistenten i ett projekt: Claude arbetar med projektets rader och MCP-serverns verktyg.

Assistenten får en instruktion ("föreslå mål för de omappade raderna", "översätt de koder
som saknar översättning") och arbetar i en loop med två sorters verktyg:

* **MCP-serverns läsande verktyg** – uppslag, sökning, ECL, validering och översättning mot
  terminologitjänsten. MCP-servern startas som underprocess (``TERM_MCP_COMMAND``, standard
  ``term-mcp``) och pekas mot samma terminologitjänst som verktyget. Ingen kod delas: det
  enda gränssnittet är MCP. Utkastverktygen används inte här.
* **Projektets egna verktyg** – ``project_rows`` läser raderna och ``propose_changes``
  lägger ändringsförslag i den redaktionella kedjan.

Inget ändras direkt. Varje förslag blir ett AI-förslag (``origin = ai``) med proveniens
och granskas under *Förslag* som alla andra. Förslagen kontrolleras dessutom här, oberoende
av modellen: kolumnen måste vara redigerbar, en ny målkod valideras mot terminologitjänsten
och en mappningsrad måste uppfylla samma regler som vid manuell redigering.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shlex
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from ..importer import conceptmap
from ..importer.mapping import ColumnRole
from . import editorial, ops
from .store import Project, Proposal, SheetData, Workspace, now

MODEL = editorial.AI_MODEL
MAX_ROWS = 100
# MCP-serverns läsande verktyg. Utkastverktygen (draft_*) hör hemma i fristående klienter.
MCP_TOOLS = {"server_info", "list_codesystems", "list_valuesets", "list_conceptmaps", "find_codesystem",
             "lookup_code", "validate_code", "expand_valueset", "search_concepts", "snomed_ecl",
             "concept_hierarchy", "subsumes", "translate_code", "validate_valueset"}

EXAMPLES = {
    "conceptmap": ["Föreslå mål för de rader som saknar målkod.",
                   "Granska relationerna och föreslå kommentarer där målet är smalare eller närliggande.",
                   "Kontrollera att målkoderna fortfarande är aktiva och föreslå ersättare för inaktiva."],
    "supplement": ["Översätt de koder som saknar översättning.",
                   "Granska översättningarna och föreslå rättelser där texten avviker från originalet."],
    "valueset": ["Kontrollera urvalets koder mot terminologitjänsten och sammanfatta vad som behöver göras."],
    "codesystem": ["Föreslå definitioner där de saknas, utifrån benämningarna.",
                   "Leta efter stavfel och inkonsekvent skrivsätt i benämningarna."],
}

SYSTEM = """\
Du är redaktörens assistent i ett projekt i ett administrationsverktyg för kodverk,
urval, mappningar och översättningar i FHIR. Du arbetar mot en terminologitjänst
genom MCP-verktygen och med projektets rader genom project_rows och propose_changes.

Regler:
- Hitta aldrig på koder eller texter från kodsystem: slå upp, sök eller validera dem
  med verktygen. För SNOMED CT: sök med snomed_ecl och avgränsa till rätt hierarki.
- Du ändrar ingenting själv. propose_changes lägger förslag som en människa granskar.
  Föreslå bara där du är rimligt säker, och motivera varje förslag kort på svenska.
- Mappningar: relationen är en av likvärdig, målet är bredare, målet är smalare,
  närliggande eller ingen motsvarighet. "Målet är smalare" och "närliggande" kräver
  en kommentar. Ingen motsvarighet har ingen målkod. Fyll i måltexten från tjänsten.
- Översättningar: översätt från kodsystemets egen text och definition, till projektets
  språk, kort och i samma stil som övriga översättningar.
- Arbeta med högst {max_rows} rader per anrop till propose_changes. Avsluta med en kort
  sammanfattning: hur många förslag, vad som återstår och vad granskaren bör titta på."""


class AssistantError(ops.OpError):
    pass


def mcp_command(override: list[str] | None = None) -> list[str]:
    if override:
        return override
    return shlex.split(os.environ.get("TERM_MCP_COMMAND", "term-mcp"))


def project_context(project: Project, data: dict[str, SheetData]) -> dict[str, Any]:
    """Det assistenten behöver veta om projektet (utan raderna, som hämtas med project_rows)."""
    tpl = project.template
    editable = editorial._editable_columns(project)
    context: dict[str, Any] = {
        "projekt": project.title, "typ": tpl.kind, "språk": tpl.codesystem.language,
        "canonical": tpl.codesystem.url, "blad": {name: len(sheet.rows) for name, sheet in data.items()},
        "kolumner": [{"kolumn": c.source_column, "står för": ops.ROLE_LABELS.get(c.role, c.role.value),
                      "redigerbar": c.source_column in editable}
                     for c in tpl.columns if c.role not in (ColumnRole.IGNORE, ColumnRole.DERIVED)],
        "nyckelkolumn": ops.code_column(tpl),
    }
    if tpl.mapping:
        context["mappning"] = {"källa": tpl.mapping.source_system, "mål": tpl.mapping.target_system,
                               "källvärdemängd": tpl.mapping.source_valueset,
                               "målvärdemängd": tpl.mapping.target_valueset,
                               "relationer": conceptmap.LABELS}
    if tpl.translation:
        context["översättning"] = {"kodsystem": tpl.translation.codesystem, "till språk": tpl.codesystem.language}
    return context


def _incomplete(project: Project, row: dict) -> bool:
    tpl = project.template
    if tpl.kind == "conceptmap":
        target = row.get(tpl.column(ColumnRole.MAP_TARGET).source_column)
        relation = row.get(tpl.column(ColumnRole.MAP_RELATION).source_column)
        try:
            unmatched = conceptmap.relation_key(relation) == "unmatched"
        except ValueError:
            unmatched = False
        return not target and not unmatched
    if tpl.kind == "supplement":
        return not row.get(tpl.column(ColumnRole.TRANSLATION).source_column)
    editable = editorial._editable_columns(project)
    return any(row.get(c) in (None, "") for c in editable)


class Run:
    """En instruktion: verktygen för projektet och förslagen som skapats."""

    def __init__(self, ws: Workspace, project: Project, data: dict[str, SheetData], author: str,
                 instruction: str, tx_client: Callable[[], Any]):
        self.ws, self.project, self.data, self.author = ws, project, data, author
        self.instruction, self.tx_client = instruction, tx_client
        self.created: list[Proposal] = []
        self.model = MODEL
        self.tools_used: list[str] = []        # verktygsanrop i ordning, fram till varje förslag
        self.mcp_server: dict[str, Any] = {}   # MCP-serverns server_info: terminologitjänst, edition, programvara
        from ..config import ServerConfig
        self.terminology_server = ServerConfig.from_env().base_url

    def provenance(self, verified: list[dict] | None = None) -> dict[str, Any]:
        """Underlaget för ett förslag: modell, instruktion, terminologitjänst, MCP-server, verktyg och de
        kodsystemversioner som målkoderna kontrollerades mot."""
        tools: dict[str, int] = {}
        for name in self.tools_used:
            tools[name] = tools.get(name, 0) + 1
        return {"model": self.model, "created": now(), "kind": "assistent", "instruction": self.instruction,
                "instruction_sha256": hashlib.sha256(self.instruction.encode()).hexdigest(),
                "terminology_server": self.terminology_server, "mcp_server": self.mcp_server or None,
                "tools": tools, "verified": verified or []}

    # -- projektets verktyg -------------------------------------------------------------

    def rows(self, sheet: str | None = None, filter: str = "alla", offset: int = 0, limit: int = 50) -> dict:
        sheet = sheet or next(iter(self.data))
        if sheet not in self.data:
            raise AssistantError(f"Bladet {sheet} finns inte. Blad: {', '.join(self.data)}.")
        rows = [r for r in self.data[sheet].rows if filter != "ofullständiga" or _incomplete(self.project, r)]
        page = rows[offset:offset + max(1, min(limit, MAX_ROWS))]
        return {"blad": sheet, "filter": filter, "totalt": len(rows), "offset": offset,
                "rader": [{k: v for k, v in r.items() if not k.startswith("_") and v not in (None, "")}
                          for r in page]}

    async def propose(self, changes: list[dict], sheet: str | None = None) -> dict:
        sheet = sheet or next(iter(self.data))
        if sheet not in self.data:
            raise AssistantError(f"Bladet {sheet} finns inte.")
        tpl = self.project.template
        editable = editorial._editable_columns(self.project)
        open_keys = {(p.sheet, p.code, json.dumps(p.changes, sort_keys=True, default=str))
                     for p in self.ws.proposals(self.project.id) if p.status == "öppen"}
        created, skipped, pending = [], [], []
        for change in changes[:MAX_ROWS]:
            key = str(change.get("row") or "").strip()
            values = {str(k): (None if v in ("", None) else str(v)) for k, v in (change.get("values") or {}).items()}
            row = ops.find_row(self.data[sheet], tpl, key) if key else None
            if row is None:
                skipped.append({"row": key, "orsak": "raden finns inte"})
                continue
            bad = [c for c in values if c not in editable]
            if bad:
                skipped.append({"row": key, "orsak": f"kolumnen går inte att ändra: {', '.join(bad)}"})
                continue
            values = {c: v for c, v in values.items() if (row.get(c) or None) != v}
            if not values:
                skipped.append({"row": key, "orsak": "inga ändringar"})
                continue
            pending.append((key, row, values, str(change.get("rationale") or "").strip()))
        verified: dict[str, list[dict]] = {}
        if tpl.kind == "conceptmap":
            pending, rejected, verified = await self._check_mappings(pending)
            skipped += rejected
        for key, row, values, rationale in pending:
            fingerprint = (sheet, key, json.dumps(values, sort_keys=True, default=str))
            if fingerprint in open_keys:
                skipped.append({"row": key, "orsak": "samma förslag är redan öppet"})
                continue
            proposal = Proposal(
                id=editorial._new_id(), sheet=sheet, code=key, action="update", changes=values,
                before={c: row.get(c) for c in values}, author=self.author, origin="ai",
                rationale=f"[assistent] {rationale or 'ingen motivering'}",
                provenance=self.provenance(verified.get(key)))
            created.append(proposal)
            open_keys.add(fingerprint)
        if created:
            items = self.ws.proposals(self.project.id) + created
            self.ws.save_proposals(self.project.id, items)
            self.ws.log(self.project.id, self.author, f"AI-assistenten: {len(created)} förslag",
                        {"instruktion": self.instruction[:300], "modell": self.model})
            self.created += created
        return {"skapade": len(created), "hoppade_över": skipped,
                "nästa": "Förslagen granskas under fliken Förslag." if created else None}

    async def _check_mappings(self, pending: list[tuple]) -> tuple[list[tuple], list[dict], dict[str, list[dict]]]:
        """Målkoder valideras mot terminologitjänsten och raden måste uppfylla mappningsreglerna."""
        tpl = self.project.template
        target_col = tpl.column(ColumnRole.MAP_TARGET).source_column
        display_col = tpl.column(ColumnRole.MAP_TARGET_DISPLAY).source_column
        relation_col = tpl.column(ColumnRole.MAP_RELATION).source_column
        ok, rejected, verified = [], [], {}
        codes = {v[target_col] for _, _, v, _ in pending if v.get(target_col)}
        found: dict[str, dict] = {}
        if codes:
            async with self.tx_client() as client:
                for code in codes:
                    try:
                        found[code] = await client.validate_code(code, system=tpl.mapping.target_system)
                    except Exception as exc:  # noqa: BLE001 – tjänsten svarar inte: förslaget stoppas
                        found[code] = {"result": False, "message": str(exc)}
        for key, row, values, rationale in pending:
            code = values.get(target_col)
            if code:
                result = found.get(code, {})
                if not result.get("result"):
                    rejected.append({"row": key, "orsak": f"målkoden {code} finns inte i {tpl.mapping.target_system}"
                                     + (f" ({result.get('message')})" if result.get("message") else "")})
                    continue
                if not values.get(display_col) and result.get("display"):
                    values[display_col] = result["display"]
                verified[key] = [{"system": tpl.mapping.target_system, "code": code,
                                  "version": result.get("version"), "display": result.get("display")}]
            if values.get(relation_col):
                try:
                    values[relation_col] = conceptmap.label(conceptmap.relation_key(values[relation_col]))
                except ValueError as exc:
                    rejected.append({"row": key, "orsak": str(exc)})
                    continue
            problems = conceptmap.row_problems(tpl, row | values)
            if problems:
                rejected.append({"row": key, "orsak": "; ".join(problems)})
                continue
            ok.append((key, row, values, rationale))
        return ok, rejected, verified


@asynccontextmanager
async def mcp_session(command: list[str]):
    """MCP-servern som underprocess, mot samma terminologitjänst som administrationsverktyget."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from ..config import ServerConfig
    env = {**os.environ, "TX_BASE_URL": ServerConfig.from_env().base_url}
    params = StdioServerParameters(command=command[0], args=command[1:], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _server_info(session) -> dict[str, Any]:
    """MCP-serverns egen uppgift om terminologitjänst, SNOMED CT-edition och programvara (för proveniensen)."""
    try:
        result = await session.call_tool("server_info", {})
        data = result.structured_content
        if data is None:
            data = json.loads(" ".join(getattr(c, "text", "") for c in result.content))
    except Exception:  # noqa: BLE001 – proveniensen får hellre sakna detta än att assistenten stoppas
        return {}
    data = data.get("result", data) if isinstance(data, dict) else {}
    software = data.get("software") or {}
    return {k: v for k, v in {"base_url": data.get("base_url"), "snomed_edition": data.get("snomed_edition"),
                              "software": " ".join(str(software.get(k)) for k in ("name", "version")
                                                   if software.get(k)) or None}.items() if v}


def _result_text(result: dict) -> str:
    content = result.get("content")
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


async def execute(run: Run, history: list[dict], client, command: list[str],
                  on_event: Callable[[str, dict], Awaitable[None] | None]) -> list[dict]:
    """Kör instruktionen till slut. Returnerar den nya historiken (för följdfrågor)."""
    from anthropic.lib.tools import beta_async_tool
    from anthropic.lib.tools.mcp import async_mcp_tool

    async def emit(kind: str, data: dict) -> None:
        result = on_event(kind, data)
        if inspect.isawaitable(result):
            await result

    @beta_async_tool(name="project_rows")
    async def project_rows(sheet: str | None = None, filter: str = "alla", offset: int = 0, limit: int = 50) -> str:
        """Läs projektets rader. filter: "alla" eller "ofullständiga" (t.ex. rader utan målkod eller
        utan översättning). Raderna identifieras med nyckelkolumnen."""
        return json.dumps(run.rows(sheet, filter, offset, limit), ensure_ascii=False, default=str)

    @beta_async_tool(name="propose_changes")
    async def propose_changes(changes: list[dict], sheet: str | None = None) -> str:
        """Lägg ändringsförslag i den redaktionella kedjan (inget ändras direkt).
        changes: [{"row": nyckel, "values": {kolumn: nytt värde}, "rationale": motivering}].
        Bara redigerbara kolumner. Svaret visar vilka förslag som skapades och vilka som stoppades."""
        return json.dumps(await run.propose(changes, sheet), ensure_ascii=False, default=str)

    try:
        async with mcp_session(command) as session:
            listed = await session.list_tools()
            tools = [async_mcp_tool(t, session) for t in listed.tools if t.name in MCP_TOOLS]
            if any(t.name == "server_info" for t in listed.tools):
                run.mcp_server = await _server_info(session)
            tools += [project_rows, propose_changes]
            context = json.dumps(project_context(run.project, run.data), ensure_ascii=False, default=str)
            first = not history
            content = (f"Projektet:\n{context}\n\nInstruktion: {run.instruction}" if first
                       else run.instruction)
            messages = [*history, {"role": "user", "content": content}]
            runner = client.beta.messages.tool_runner(
                model=MODEL, max_tokens=16000, system=SYSTEM.format(max_rows=MAX_ROWS), tools=tools,
                messages=messages, output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", max_iterations=30)
            final = None
            async for message in runner:
                final = message
                run.model = getattr(message, "model", None) or run.model
                messages.append(message.to_param())
                names = {}
                for block in message.content:
                    if block.type == "text" and block.text.strip():
                        await emit("text", {"text": block.text})
                    elif block.type == "tool_use":
                        names[block.id] = block.name
                        run.tools_used.append(block.name)
                        await emit("tool_use", {"id": block.id, "name": block.name, "input": block.input})
                response = await runner.generate_tool_call_response()
                if response is not None:
                    messages.append(response)
                    for result in response["content"]:
                        await emit("tool_result", {"id": result.get("tool_use_id"),
                                                   "name": names.get(result.get("tool_use_id")),
                                                   "is_error": bool(result.get("is_error")),
                                                   "text": _result_text(result)})
    except FileNotFoundError as exc:
        raise AssistantError(f"MCP-servern kunde inte startas ({' '.join(command)}). Installera term-mcp eller "
                             "sätt TERM_MCP_COMMAND.") from exc
    note = None
    if final is not None and final.stop_reason == "refusal":
        note = "Claude avböjde instruktionen."
    elif final is not None and final.stop_reason == "max_tokens":
        note = "Svaret blev avkortat."
    await emit("done", {"answer": "".join(b.text for b in final.content if b.type == "text") if final else "",
                        "note": note, "created": len(run.created)})
    return messages


def render_markdown(text: str) -> str:
    """Claudes svar som HTML, utan rå HTML och utan javascript:-länkar. Utan markdown-paketet: escapad text."""
    import html
    try:
        import markdown
    except ImportError:
        return f"<p>{html.escape(text)}</p>".replace("\n", "<br>")
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists"])
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")

    class SafeLinks:
        def run(self, root):
            from urllib.parse import urlsplit
            for element in root.iter():
                for attribute in ("href", "src"):
                    value = element.get(attribute)
                    if value is not None and urlsplit(value.strip()).scheme.lower() not in ("", "http", "https",
                                                                                           "mailto"):
                        del element.attrib[attribute]
    md.treeprocessors.register(SafeLinks(), "safe-links", 0)
    return md.convert(text)


class Conversations:
    """Dialoger per projekt och användare (i minnet: följdfrågor fungerar tills verktyget startas om)."""

    def __init__(self):
        self.history: dict[tuple[str, str], list[dict]] = {}
        self.busy: set[str] = set()
        self.lock = asyncio.Lock()

    def get(self, pid: str, user: str) -> list[dict]:
        return self.history.get((pid, user), [])

    def set(self, pid: str, user: str, messages: list[dict]) -> None:
        self.history[(pid, user)] = messages if len(messages) <= 60 else _trim(messages)

    def reset(self, pid: str, user: str) -> None:
        self.history.pop((pid, user), None)


def _trim(messages: list[dict]) -> list[dict]:
    """Behåll slutet av dialogen men börja på en användarfråga (inte mitt i ett verktygsanrop)."""
    tail = messages[-60:]
    for i, m in enumerate(tail):
        if m["role"] == "user" and isinstance(m["content"], str):
            return tail[i:]
    return []
