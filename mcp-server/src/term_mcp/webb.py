"""Webbgränssnitt för att instruera MCP-servern – för demo.

    term-mcp-webb                      # http://127.0.0.1:8001, terminologitjänst från TX_BASE_URL
    term-mcp-webb --host 0.0.0.0       # i Codespaces (låt porten vara privat)

Varje samtal har en egen MCP-server (underprocess, stdio) och en egen dialog med
Claude. Stegen – Claudes text, verktygsanrop och verktygssvar – strömmas till
sidan medan de sker. Förslag som skapats på terminologitjänsten listas bredvid
samtalet. MCP-servern skriver ingenting till terminologitjänsten: urval, mappningar
och översättningar blir utkast som hämtas här som FHIR-Bundle eller FHIR-paket (.tgz)
och tas in i administrationsverktyget för granskning. Som demo-klienten pratar
webbappen bara med MCP-servern, aldrig direkt med terminologitjänsten.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import drafts
from .demo import EXAMPLES, LOCAL_TX, Session, connect, read_drafts, server_parameters

STATIC = Path(__file__).parent / "static"


def render_markdown(text: str) -> str:
    """Claudes svar (markdown) som HTML. HTML i texten visas som text, inte som kod."""
    import markdown
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists"])
    # Ingen rå HTML: block och inline-taggar tolkas inte utan visas som text (och kodsnuttar escapas bara en gång).
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    md.treeprocessors.register(_SafeLinks(md), "safe-links", 0)
    return md.convert(text)


def _safe_url(url: str) -> bool:
    from urllib.parse import urlsplit
    scheme = urlsplit(url.strip()).scheme.lower()
    return scheme in ("", "http", "https", "mailto")


class _SafeLinks:
    """Ta bort länkar och bilder med t.ex. javascript:-adresser ur Claudes markdown."""

    def __init__(self, md):
        self.md = md

    def run(self, root):
        for element in root.iter():
            for attribute in ("href", "src"):
                value = element.get(attribute)
                if value is not None and not _safe_url(value):
                    del element.attrib[attribute]
        return None


def port_url(port: int, default_host: str = "localhost") -> str:
    """Adress till en annan tjänst på samma maskin – i Codespaces den vidarebefordrade porten."""
    name, domain = os.environ.get("CODESPACE_NAME"), os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    if name and domain:
        return f"https://{name}-{port}.{domain}"
    return f"http://{default_host}:{port}"


class Conversation:
    """Ett samtal: en MCP-session och en Claude-dialog som lever i en egen bakgrundsuppgift."""

    def __init__(self, tx_url: str, anthropic_factory: Callable[[], Any] | None):
        self.id = uuid.uuid4().hex
        self.tx_url = tx_url
        self.anthropic_factory = anthropic_factory
        self.jobs: asyncio.Queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.error: str | None = None
        self.busy = False
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async with connect(server_parameters(self.tx_url)) as mcp:
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


def create_app(tx_url: str | None = None,
               anthropic_factory: Callable[[], Any] | None = None, admin_url: str | None = None):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
    from starlette.routing import Route

    tx_url = (tx_url or os.environ.get("TX_BASE_URL") or LOCAL_TX).rstrip("/")
    admin_url = (admin_url or os.environ.get("TERM_ADMIN_URL") or port_url(8000)).rstrip("/")
    conversations: dict[str, Conversation] = {}

    def get(request: Request) -> Conversation | None:
        return conversations.get(request.path_params["cid"])

    async def index(request: Request):
        return FileResponse(STATIC / "webb.html")

    async def config(request: Request):
        return JSONResponse({"tx": tx_url, "admin": admin_url, "examples": EXAMPLES,
                             "apiKey": bool(os.environ.get("ANTHROPIC_API_KEY")) or anthropic_factory is not None})

    async def new_conversation(request: Request):
        conv = Conversation(tx_url, anthropic_factory)
        conversations[conv.id] = conv
        await conv.ready.wait()
        if conv.error:
            conversations.pop(conv.id)
            return JSONResponse({"error": f"MCP-servern startade inte: {conv.error}"}, status_code=500)
        tools = await conv.submit(lambda: conv.mcp.list_tools())
        return JSONResponse({"id": conv.id, "tools": [t.name for t in tools.tools]})

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

    async def draft_list(request: Request):
        conv = get(request)
        if conv is None:
            return JSONResponse([], status_code=404)
        try:
            return JSONResponse(await conv.call_tool("list_drafts", {}))
        except Exception as exc:
            return JSONResponse({"error": _friendly(exc)}, status_code=502)

    async def draft_download(request: Request):
        """Utkasten som fil: utkast.json (FHIR-Bundle), utkast.tgz (FHIR-paket) eller en enskild resurs."""
        conv = get(request)
        if conv is None:
            return JSONResponse({"error": "Samtalet finns inte – ladda om sidan."}, status_code=404)
        resources = await conv.submit(lambda: read_drafts(conv.mcp))
        rtype, rid = request.path_params.get("rtype"), request.path_params.get("rid")
        if request.path_params.get("format", "json") not in ("json", "tgz"):
            return JSONResponse({"error": "Formatet ska vara json eller tgz."}, status_code=404)
        if rtype:
            resources = [r for r in resources if r["resourceType"] == rtype and r["id"] == rid]
        if not resources:
            return JSONResponse({"error": "Inga utkast att hämta."}, status_code=404)
        stamp = time.strftime("%Y%m%d-%H%M")
        if request.path_params.get("format") == "tgz":
            content, media, name = drafts.package(resources), "application/gzip", f"utkast-{stamp}.tgz"
        elif rtype:
            content = json.dumps(resources[0], ensure_ascii=False, indent=2).encode()
            media, name = "application/fhir+json", f"{rtype}-{rid}.json"
        else:
            content = json.dumps(drafts.bundle(resources), ensure_ascii=False, indent=2).encode()
            media, name = "application/fhir+json", f"utkast-{stamp}.json"
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})

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
        Route("/api/samtal/{cid}/utkast", draft_list),
        Route("/api/samtal/{cid}/utkast.{format:str}", draft_download),
        Route("/api/samtal/{cid}/utkast/{rtype}/{rid}.json", draft_download),
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
    args = parser.parse_args(argv)
    import uvicorn
    app = create_app(args.tx)
    print(f"Webbgränssnittet: http://{args.host}:{args.port}  (terminologitjänst: "
          f"{(args.tx or os.environ.get('TX_BASE_URL') or LOCAL_TX)})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

