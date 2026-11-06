"""Webbgränssnitt för att instruera MCP-servern – för demo.

    term-mcp-webb                      # http://127.0.0.1:8001, terminologitjänst från TX_BASE_URL
    term-mcp-webb --host 0.0.0.0       # i Codespaces (låt porten vara privat)

Varje samtal har en egen MCP-server (underprocess, stdio) och en egen dialog med
Claude. Stegen – Claudes text, verktygsanrop och verktygssvar – strömmas till
sidan medan de sker. Förslag som skapats på terminologitjänsten listas bredvid
samtalet, med länk till resursen och till administrationsverktyget där de granskas.
Som demo-klienten pratar webbappen bara med MCP-servern, aldrig direkt med
terminologitjänsten (utom för att visa en resurs som JSON).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from .demo import EXAMPLES, LOCAL_TX, Session, connect, server_parameters

STATIC = Path(__file__).parent / "static"


def render_markdown(text: str) -> str:
    """Claudes svar (markdown) som HTML. HTML i texten visas som text, inte som kod."""
    import markdown
    safe = text.replace("&", "&amp;").replace("<", "&lt;")
    return markdown.markdown(safe, extensions=["tables", "fenced_code", "sane_lists"])


def port_url(port: int, default_host: str = "localhost") -> str:
    """Adress till en annan tjänst på samma maskin – i Codespaces den vidarebefordrade porten."""
    name, domain = os.environ.get("CODESPACE_NAME"), os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    if name and domain:
        return f"https://{name}-{port}.{domain}"
    return f"http://{default_host}:{port}"


class Conversation:
    """Ett samtal: en MCP-session och en Claude-dialog som lever i en egen bakgrundsuppgift."""

    def __init__(self, tx_url: str, allow_write: bool, anthropic_factory: Callable[[], Any] | None):
        self.id = uuid.uuid4().hex
        self.tx_url, self.allow_write = tx_url, allow_write
        self.anthropic_factory = anthropic_factory
        self.jobs: asyncio.Queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.error: str | None = None
        self.busy = False
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async with connect(server_parameters(self.tx_url, self.allow_write)) as mcp:
                self.mcp = mcp
                self.session: Session | None = None
                self.ready.set()
                while (job := await self.jobs.get()) is not None:
                    await job()
        except Exception as exc:  # MCP-servern startade inte e.d.
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.ready.set()

    async def submit(self, fn) -> Any:
        """Kör ``fn`` i samtalets uppgift (där MCP-sessionen lever) och vänta på resultatet."""
        await self.ready.wait()
        if self.error:
            raise RuntimeError(self.error)
        done: asyncio.Future = asyncio.get_running_loop().create_future()

        async def job():
            try:
                done.set_result(await fn())
            except Exception as exc:
                done.set_exception(exc)
        await self.jobs.put(job)
        return await done

    def dialog(self) -> Session:
        if self.session is None:
            client = self.anthropic_factory() if self.anthropic_factory else None
            self.session = Session(self.mcp, client)
        return self.session

    async def call_tool(self, name: str, arguments: dict) -> Any:
        async def call():
            result = await self.mcp.call_tool(name, arguments)
            if result.structured_content is not None:
                data = result.structured_content
                return data.get("result", data) if isinstance(data, dict) else data
            text = " ".join(getattr(c, "text", "") for c in result.content)
            with contextlib.suppress(ValueError):
                return json.loads(text)
            return text
        return await self.submit(call)

    async def close(self) -> None:
        await self.jobs.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.task, 10)


def create_app(tx_url: str | None = None, allow_write: bool = True,
               anthropic_factory: Callable[[], Any] | None = None, admin_url: str | None = None):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, StreamingResponse
    from starlette.routing import Route

    tx_url = (tx_url or os.environ.get("TX_BASE_URL") or LOCAL_TX).rstrip("/")
    admin_url = (admin_url or os.environ.get("TERM_ADMIN_URL") or port_url(8000)).rstrip("/")
    conversations: dict[str, Conversation] = {}

    def get(request: Request) -> Conversation | None:
        return conversations.get(request.path_params["cid"])

    async def index(request: Request):
        return FileResponse(STATIC / "webb.html")

    async def config(request: Request):
        return JSONResponse({"tx": tx_url, "admin": admin_url, "examples": EXAMPLES, "writeAllowed": allow_write,
                             "apiKey": bool(os.environ.get("ANTHROPIC_API_KEY")) or anthropic_factory is not None})

    async def new_conversation(request: Request):
        body = await request.json() if await request.body() else {}
        conv = Conversation(tx_url, allow_write and bool(body.get("forslag", True)), anthropic_factory)
        conversations[conv.id] = conv
        await conv.ready.wait()
        if conv.error:
            conversations.pop(conv.id)
            return JSONResponse({"error": f"MCP-servern startade inte: {conv.error}"}, status_code=500)
        tools = await conv.submit(lambda: conv.mcp.list_tools())
        return JSONResponse({"id": conv.id, "forslag": conv.allow_write, "tools": [t.name for t in tools.tools]})

    async def ask(request: Request):
        conv = get(request)
        if conv is None:
            return JSONResponse({"error": "Samtalet finns inte – ladda om sidan."}, status_code=404)
        if conv.busy:
            return JSONResponse({"error": "Vänta tills föregående instruktion är klar."}, status_code=409)
        instruction = ((await request.json()).get("instruktion") or "").strip()
        if not instruction:
            return JSONResponse({"error": "Skriv en instruktion."}, status_code=400)
        events: asyncio.Queue = asyncio.Queue()

        def on_event(kind: str, data: dict) -> None:
            if kind in ("text", "done") and data.get("text" if kind == "text" else "answer"):
                data = data | {"html": render_markdown(data["text" if kind == "text" else "answer"])}
            events.put_nowait({"type": kind, **data})

        async def run():
            conv.busy = True
            try:
                dialog = conv.dialog()
                dialog.on_event = on_event
                await dialog.ask(instruction)
            except Exception as exc:
                events.put_nowait({"type": "error", "text": _friendly(exc)})
            finally:
                conv.busy = False
                events.put_nowait(None)

        asyncio.create_task(conv.submit(run))

        async def stream():
            while (event := await events.get()) is not None:
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def reset(request: Request):
        conv = get(request)
        if conv is not None and conv.session is not None:
            conv.session.reset()
        return JSONResponse({"ok": True})

    async def proposals(request: Request):
        conv = get(request)
        if conv is None:
            return JSONResponse([], status_code=404)
        try:
            return JSONResponse(await conv.call_tool("list_proposals", {}))
        except Exception as exc:
            return JSONResponse({"error": _friendly(exc)}, status_code=502)

    async def resource(request: Request):
        """Visa en resurs på terminologitjänsten som JSON (fungerar även via Codespaces portvidarebefordran)."""
        import httpx2
        rtype, rid = request.path_params["rtype"], request.path_params["rid"]
        if rtype not in ("CodeSystem", "ValueSet", "ConceptMap"):
            return JSONResponse({"error": "okänd resurstyp"}, status_code=400)
        async with httpx2.AsyncClient(timeout=30) as http:
            r = await http.get(f"{tx_url}/{rtype}/{rid}", headers={"Accept": "application/fhir+json"})
        return JSONResponse(r.json(), status_code=r.status_code)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        for conv in list(conversations.values()):  # stäng MCP-servrarna (underprocesserna)
            await conv.close()

    routes = [
        Route("/", index),
        Route("/api/config", config),
        Route("/api/samtal", new_conversation, methods=["POST"]),
        Route("/api/samtal/{cid}/fraga", ask, methods=["POST"]),
        Route("/api/samtal/{cid}/nollstall", reset, methods=["POST"]),
        Route("/api/samtal/{cid}/forslag", proposals),
        Route("/api/resurs/{rtype}/{rid}", resource),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def _friendly(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if "api_key" in text.lower() or "authentication" in text.lower() or "auth" in type(exc).__name__.lower():
        return ("Claude kunde inte anropas – ANTHROPIC_API_KEY saknas eller är ogiltig. Lägg nyckeln som "
                f"Codespaces-hemlighet och starta om. ({text})")
    return text


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="term-mcp-webb", description="Webbgränssnitt för att instruera MCP-servern")
    parser.add_argument("--tx", default=None, help=f"Terminologitjänst (standard TX_BASE_URL eller {LOCAL_TX})")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--utan-forslag", action="store_true", help="Stäng av förslagsverktygen helt")
    args = parser.parse_args(argv)
    import uvicorn
    app = create_app(args.tx, allow_write=not args.utan_forslag)
    print(f"Webbgränssnittet: http://{args.host}:{args.port}  (terminologitjänst: "
          f"{(args.tx or os.environ.get('TX_BASE_URL') or LOCAL_TX)})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

