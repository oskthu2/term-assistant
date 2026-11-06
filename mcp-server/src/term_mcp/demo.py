"""Demo-klient: instruera MCP-servern på svenska, Claude väljer verktygen.

Klienten startar MCP-servern som en underprocess (stdio), hämtar dess verktyg
och låter Claude anropa dem i en loop tills uppgiften är löst. Allt går genom
MCP – klienten pratar aldrig själv med terminologitjänsten.

    term-mcp-demo "Vilka civilstånd finns i HL7 Sveriges värdemängd?"
    term-mcp-demo                              # dialog, flera instruktioner i rad
    term-mcp-demo --spara utkast.tgz "Föreslå en mappning från norska civilstånd till HL7 v3 MaritalStatus"
    term-mcp-demo --verktyg                    # lista verktygen (ingen API-nyckel behövs)
    term-mcp-demo --anropa lookup_code '{"system": "...", "code": "M"}'

Terminologitjänst: --tx URL, annars TX_BASE_URL, annars den lokala HAPI-servern
(http://localhost:8080/fhir). Claude-anrop kräver ANTHROPIC_API_KEY (eller en
inloggad profil via `ant auth login`).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODEL = "claude-opus-5-5"
LOCAL_TX = "http://localhost:8080/fhir"

SYSTEM = """\
Du är en terminologiassistent. Du arbetar mot en FHIR-terminologitjänst enbart
genom de verktyg du har fått. Hitta aldrig på koder, URL:er eller displaytexter:
slå upp, sök eller validera dem med verktygen och säg tydligt när något inte
finns på tjänsten. Börja med att hitta rätt kodsystem eller värdemängd (canonical
URL) innan du slår upp enskilda koder.

Förslag (urval, mappningar, översättningar) bygger du på kodernas egna texter och
definitioner som du hämtat med verktygen. Redovisa förslaget som en tabell innan
du gör utkastet, motivera relationer som inte är likvärdiga och peka ut koder som
saknar motsvarighet. Utkast får canonical URL:er under http://example.org/fhir/
om användaren inte anger något annat. Inget skrivs till terminologitjänsten:
utkasten hämtas som FHIR-Bundle eller FHIR-paket och tas in i
administrationsverktyget, där de granskas innan de släpps. Säg exakt vilket utkast
som skapades (canonical URL, antal koder). Svara på svenska, kort och sakligt,
gärna med en tabell när du listar koder."""

EXAMPLES = [
    "Vilka svenska och norska kodsystem och värdemängder finns på tjänsten?",
    "Lista civilstånden i HL7 Sveriges värdemängd för civilstånd, med svensk text där den finns.",
    "Översätt SNOMED CT-koden 44054006 till ICD-10 med hjälp av en ConceptMap på tjänsten.",
    "Föreslå en mappning från norska civilstånd (no-basis-marital-status) till HL7 v3 MaritalStatus. "
    "Visa tabellen först och gör utkastet när jag säger till.",
    "Föreslå en svensk översättning av de norska familjerelationerna (no-basis-family-relation).",
    "Föreslå ett urval av civilstånd för ett vårdregister: gift, ogift, skild och änka/änkling.",
]


def server_parameters(tx_url: str) -> StdioServerParameters:
    """MCP-servern i samma Python-miljö, med vald terminologitjänst."""
    env = {**os.environ, "TX_BASE_URL": tx_url}
    return StdioServerParameters(command=sys.executable, args=["-m", "term_mcp.server"], env=env)


async def read_drafts(session: ClientSession) -> list[dict]:
    """Utkasten i MCP-serverns minne, via MCP-resursen utkast://bundle."""
    result = await session.read_resource("utkast://bundle")
    bundle = json.loads(result.contents[0].text)
    return [e["resource"] for e in bundle.get("entry", [])]


def save_drafts(resources: list[dict], path: str) -> str:
    """Spara utkasten som FHIR-paket (.tgz) eller FHIR-Bundle (annars JSON)."""
    from .drafts import bundle, package
    from pathlib import Path
    target = Path(path)
    if target.suffix in (".tgz", ".gz"):
        target.write_bytes(package(resources))
    else:
        target.write_text(json.dumps(bundle(resources), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


@asynccontextmanager
async def connect(params: StdioServerParameters) -> AsyncIterator[ClientSession]:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " …"


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _result_text(result: dict) -> str:
    content = result.get("content")
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


class Session:
    """En dialog med Claude över MCP-serverns verktyg. Historiken sparas mellan instruktionerna.

    Varje steg rapporteras som en händelse till ``on_event(typ, data)``: ``text``, ``tool_use``,
    ``tool_result`` och ``done``. Utan ``on_event`` skrivs stegen ut (terminalläget)."""

    def __init__(self, mcp_session: ClientSession, anthropic_client=None, model: str = MODEL,
                 out: TextIO = sys.stdout, on_event: Callable[[str, dict], Awaitable[None] | None] | None = None):
        if anthropic_client is None:
            from anthropic import AsyncAnthropic
            anthropic_client = AsyncAnthropic()
        self.mcp = mcp_session
        self.client = anthropic_client
        self.model = model
        self.out = out
        self.on_event = on_event or self._print
        self.history: list[dict] = []
        self._tools = None

    def _print(self, kind: str, data: dict) -> None:
        if kind == "text":
            print(data["text"], file=self.out)
        elif kind == "tool_use":
            args = ", ".join(f"{k}={_short(v, 60)}" for k, v in data["input"].items())
            print(f"  → {data['name']}({args})", file=self.out)
        elif kind == "tool_result":
            print(f"    {'✗' if data['is_error'] else '←'} {_short(data['text'])}", file=self.out)
        elif kind == "done" and data.get("note"):
            print(f"({data['note']})", file=self.out)

    async def _emit(self, kind: str, data: dict) -> None:
        result = self.on_event(kind, data)
        if inspect.isawaitable(result):
            await result

    async def tools(self):
        if self._tools is None:
            from anthropic.lib.tools.mcp import async_mcp_tool
            listed = await self.mcp.list_tools()
            self._tools = [async_mcp_tool(t, self.mcp) for t in listed.tools]
        return self._tools

    def reset(self) -> None:
        self.history = []

    async def ask(self, instruction: str) -> str:
        """Kör en instruktion till slut och returnera Claudes svar."""
        messages = [*self.history, {"role": "user", "content": instruction}]
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            tools=await self.tools(),
            messages=messages,
            output_config={"effort": "medium"},
            # Server-side fallback: om modellen avböjer tar en annan modell över i samma anrop.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            max_iterations=25,
        )
        final = None
        async for message in runner:
            final = message
            messages.append(message.to_param())
            names = {}
            for block in message.content:
                if block.type == "text" and block.text.strip():
                    await self._emit("text", {"text": block.text})
                elif block.type == "tool_use":
                    names[block.id] = block.name
                    await self._emit("tool_use", {"id": block.id, "name": block.name, "input": block.input})
            response = await runner.generate_tool_call_response()  # cachas; runnern kör inte verktygen igen
            if response is not None:
                messages.append(response)
                for result in response["content"]:
                    await self._emit("tool_result", {"id": result.get("tool_use_id"),
                                                     "name": names.get(result.get("tool_use_id")),
                                                     "is_error": bool(result.get("is_error")),
                                                     "text": _result_text(result)})
        answer = "".join(b.text for b in final.content if b.type == "text") if final else ""
        note = None
        if final is not None and final.stop_reason == "refusal":
            note = "Claude avböjde instruktionen."
        elif final is not None and final.stop_reason == "max_tokens":
            note = "Svaret blev avkortat – be om en kortare sammanfattning."
        self.history = messages
        await self._emit("done", {"answer": answer, "note": note})
        return answer


async def list_tools(params: StdioServerParameters, out: TextIO = sys.stdout) -> list[str]:
    async with connect(params) as session:
        tools = (await session.list_tools()).tools
        for t in tools:
            print(f"{t.name:24} {(t.description or '').splitlines()[0]}", file=out)
        return [t.name for t in tools]


async def call_tool(params: StdioServerParameters, name: str, arguments: dict, out: TextIO = sys.stdout) -> Any:
    """Anropa ett verktyg direkt, utan Claude."""
    async with connect(params) as session:
        result = await session.call_tool(name, arguments)
        data = result.structured_content
        if data is None:
            texts = [getattr(c, "text", str(c)) for c in result.content]
            data = [_maybe_json(t) for t in texts]
            data = data[0] if len(data) == 1 else data
        print(json.dumps(data, ensure_ascii=False, indent=2), file=out)
        return data


async def chat(params: StdioServerParameters, instruction: str | None, anthropic_client=None,
               save_to: str | None = None) -> None:
    async with connect(params) as session:
        dialog = Session(session, anthropic_client)
        try:
            await _dialog(dialog, instruction)
        finally:
            resources = await read_drafts(session)
            if resources and save_to:
                print(f"\n{len(resources)} utkast sparade i {save_drafts(resources, save_to)} – ta in filen i "
                      "administrationsverktyget (Förslag → Ta in fil).")
            elif resources:
                print(f"\n{len(resources)} utkast skapades men sparades inte. Använd --spara utkast.tgz.")


async def _dialog(dialog: Session, instruction: str | None) -> None:
    if instruction:
        await dialog.ask(instruction)
        return
    print("Skriv en instruktion (tom rad avslutar). Exempel:")
    for example in EXAMPLES:
        print(f"  • {example}")
    while True:
        try:
            line = await asyncio.to_thread(input, "\n> ")
        except EOFError:
            break
        if not line.strip() or line.strip().lower() in ("sluta", "exit", "quit"):
            break
        await dialog.ask(line)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="term-mcp-demo", description="Instruera MCP-servern på svenska via Claude")
    parser.add_argument("instruction", nargs="?", help="Instruktion; utan instruktion startar en dialog")
    parser.add_argument("--tx", default=os.environ.get("TX_BASE_URL") or LOCAL_TX,
                        help=f"Terminologitjänstens FHIR-bas (standard TX_BASE_URL eller {LOCAL_TX})")
    parser.add_argument("--spara", metavar="FIL",
                        help="Spara utkasten när dialogen är slut: .tgz ger ett FHIR-paket, annars en FHIR-Bundle (JSON)")
    parser.add_argument("--verktyg", action="store_true", help="Lista MCP-serverns verktyg och avsluta")
    parser.add_argument("--anropa", nargs="+", metavar=("VERKTYG", "JSON"),
                        help="Anropa ett verktyg direkt med argument som JSON, utan Claude")
    args = parser.parse_args(argv)
    params = server_parameters(args.tx)
    if args.verktyg:
        asyncio.run(list_tools(params))
    elif args.anropa:
        arguments = json.loads(args.anropa[1]) if len(args.anropa) > 1 else {}
        asyncio.run(call_tool(params, args.anropa[0], arguments))
    else:
        asyncio.run(chat(params, args.instruction, save_to=args.spara))


if __name__ == "__main__":
    main()
