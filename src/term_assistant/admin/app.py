"""Webbappen för administrationsvyn (Starlette + Jinja2, serverrenderad).

Starta:  term-assistant admin --workspace ./arbetsyta

Vyn har ingen egen inloggning. Kör den bakom en proxy som sköter inloggning
och skickar användarnamnet i ``X-Remote-User``; namnet hamnar i loggen.
Publicering till terminologitjänsten (``TX_BASE_URL``) är avstängd tills
``TX_PUBLISH=1`` är satt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote, urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..config import ServerConfig
from ..importer.mapping import ColumnRole
from ..importer.plan import ImportPlan, plan_import
from ..importer.registry import PropertyRegistry
from ..importer.suggest import MappingProposal
from ..importer.workbook import read_workbook
from . import ops
from .store import Workspace

HERE = Path(__file__).parent


def author(request: Request) -> str:
    return request.headers.get("x-remote-user") or request.headers.get("x-forwarded-user") or "okänd"


def create_app(workspace: str | Path, registry_path: str | Path | None = None,
               base_url: str = "http://example.org/fhir") -> Starlette:
    ws = Workspace(workspace)
    reg_path = Path(registry_path) if registry_path else ws.root / "registry.json"
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    env.globals.update(ROLE_LABELS=ops.ROLE_LABELS, ColumnRole=ColumnRole, quote=quote,
                       tx_url=lambda: ServerConfig.from_env().base_url,
                       publish_enabled=lambda: os.environ.get("TX_PUBLISH") == "1")

    def registry() -> PropertyRegistry:
        return PropertyRegistry.load(reg_path)

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("msg", request.query_params.get("msg"))
        if "project" in ctx and "data_all" not in ctx:
            ctx["data_all"] = ctx.get("data") if isinstance(ctx.get("data"), dict) else ws.data(ctx["project"].id)
        ctx.setdefault("errors", [])
        return HTMLResponse(env.get_template(name).render(request=request, user=author(request), **ctx),
                            status_code=status)

    def redirect(url: str, msg: str | None = None) -> RedirectResponse:
        return RedirectResponse(url + (f"?{urlencode({'msg': msg})}" if msg else ""), status_code=303)

    def load(pid: str):
        try:
            return ws.get(pid), ws.data(pid)
        except KeyError:
            return None, None

    def not_found(request: Request, what: str = "Sidan") -> HTMLResponse:
        return render(request, "error.html", status=404, message=f"{what} finns inte.")

    # -- start och inläsning ---------------------------------------------------------

    async def index(request: Request):
        return render(request, "index.html", projects=ws.projects())

    async def import_form(request: Request):
        return render(request, "import.html", base_url=base_url)

    async def import_upload(request: Request):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            return render(request, "import.html", status=400, base_url=base_url, errors=["Välj en fil."])
        uid, path = ws.save_upload(upload.filename, await upload.read())
        base = (form.get("base_url") or base_url).strip()
        try:
            plan, _ = plan_import(path, base, registry(), language=(form.get("language") or "sv").strip())
        except Exception as exc:  # trasig eller okänd fil: visa felet i stället för att krascha
            return render(request, "import.html", status=400, base_url=base_url,
                          errors=[f"Filen kunde inte läsas ({type(exc).__name__}: {exc}). "
                                  "Kontrollera att det är en Excel-, CSV- eller PDF-fil."])
        ws.upload_cache(uid).write_text(plan.model_dump_json(), encoding="utf-8")
        return redirect(f"/import/{uid}")

    def cached_plan(uid: str) -> ImportPlan | None:
        try:
            return ImportPlan.model_validate_json(ws.upload_cache(uid).read_text(encoding="utf-8"))
        except (KeyError, FileNotFoundError):
            return None

    async def import_plan(request: Request):
        plan = cached_plan(request.path_params["uid"])
        if plan is None:
            return not_found(request, "Inläsningen")
        return render(request, "import_plan.html", uid=request.path_params["uid"], plan=plan,
                      assessment=plan.assessment)

    async def import_create(request: Request):
        uid, i = request.path_params["uid"], request.path_params["i"]
        plan = cached_plan(uid)
        if plan is None or i >= len(plan.proposals):
            return not_found(request, "Förslaget")
        form = await request.form()
        proposal: MappingProposal = plan.proposals[i]
        title = (form.get("title") or proposal.template.codesystem.title or proposal.template.codesystem.name).strip()
        sheets = read_workbook(ws.upload_path(uid))
        project = ops.create_project(ws, proposal, sheets, title, author(request))
        return redirect(f"/p/{project.id}", "Projektet är skapat. Gå igenom de öppna frågorna.")

    # -- projekt -------------------------------------------------------------------------

    async def project_page(request: Request):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        releases = ws.releases(project.id)
        return render(request, "project.html", project=project, data=data, releases=releases,
                      tpl=project.template, log=ws.read_log(project.id, 30))

    async def project_meta(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        form = await request.form()
        tpl = project.template
        project.title = (form.get("title") or project.title).strip()
        for field in ("title", "publisher", "description", "purpose", "language"):
            value = (form.get(f"cs_{field}") or "").strip()
            setattr(tpl.codesystem, field, value or (tpl.codesystem.language if field == "language" else None))
        url = (form.get("cs_url") or "").strip()
        if url:
            tpl.codesystem.url = url
        for i, src in enumerate(tpl.source.sheets):
            for field in ("title", "url", "identifier", "publisher"):
                if f"s{i}_{field}" in form:
                    setattr(src, field, (form.get(f"s{i}_{field}") or "").strip() or None)
        for key in list(tpl.external_systems):
            if f"ext_{key}" in form:
                tpl.external_systems[key] = (form.get(f"ext_{key}") or "").strip()
        tpl.policy.allow_code_removal = bool(form.get("allow_code_removal"))
        tpl.policy.allow_display_change = bool(form.get("allow_display_change"))
        ws.save(project, author=author(request), action="Metadata ändrad")
        return redirect(f"/p/{project.id}", "Sparat.")

    async def toggle_question(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        i = request.path_params["i"]
        if 0 <= i < len(project.questions):
            form = await request.form()
            q = project.questions[i]
            q.resolved = not q.resolved
            q.note = (form.get("note") or q.note).strip()
            ws.save(project, author=author(request),
                    action=f"Fråga {'besvarad' if q.resolved else 'återöppnad'}", detail={"fråga": q.text})
        return redirect(f"/p/{project.id}#fragor")

    # -- tabell och begrepp ------------------------------------------------------------------

    async def sheet_page(request: Request):
        project, data = load(request.path_params["pid"])
        sheet = request.path_params["sheet"]
        if project is None or sheet not in data:
            return not_found(request)
        tpl = project.template
        return render(request, "sheet.html", project=project, tpl=tpl, sheet=sheet, data=data[sheet],
                      columns=ops.visible_columns(tpl), cell=ops.cell, code_col=ops.code_column(tpl),
                      released=ops.released_codes(ws, project.id, sheet), data_all=data)

    async def concept_form(request: Request):
        project, data = load(request.path_params["pid"])
        sheet = request.path_params["sheet"]
        if project is None or sheet not in data:
            return not_found(request)
        code = request.path_params.get("code")
        row = ops.find_row(data[sheet], project.template, code) if code else {}
        if row is None:
            return not_found(request, "Begreppet")
        return _concept(request, project, data, sheet, row, is_new=code is None)

    def _concept(request, project, data, sheet, row, is_new, errors=None, status=200):
        tpl = project.template
        released = ops.released_codes(ws, project.id, sheet)
        code = row.get(ops.code_column(tpl)) if row else None
        deletable, reason = ops.can_delete(tpl, released, str(code)) if code else (True, "")
        parents = [r.get(ops.code_column(tpl)) for r in data[sheet].rows]
        return render(request, "concept.html", status=status, project=project, tpl=tpl, sheet=sheet, row=row,
                      is_new=is_new, columns=ops.visible_columns(tpl), cell=ops.cell, widget=ops.widget,
                      errors=errors or [], released=str(code) in released, deletable=deletable,
                      delete_reason=reason, parents=parents)

    async def concept_save(request: Request):
        project, data = load(request.path_params["pid"])
        sheet = request.path_params["sheet"]
        if project is None or sheet not in data:
            return not_found(request)
        code = request.path_params.get("code")
        tpl = project.template
        form = dict(await request.form())
        if code is None:
            row: dict = {h: None for h in data[sheet].headers}
            try:
                changes = ops.update_row(tpl, data[sheet], row, form, is_new=True)
            except ops.OpError as exc:
                return _concept(request, project, data, sheet, {**row, **_form_row(tpl, form)}, True,
                                exc.messages, 400)
            data[sheet].rows.append(row)
            new_code = row[ops.code_column(tpl)]
            ws.save(project, data, author=author(request), action=f"Nytt begrepp {new_code} i {sheet}",
                    detail={k: v[1] for k, v in changes.items()})
            return redirect(f"/p/{project.id}/s/{quote(sheet)}", f"Begreppet {new_code} är tillagt.")
        row = ops.find_row(data[sheet], tpl, code)
        if row is None:
            return not_found(request, "Begreppet")
        try:
            changes = ops.update_row(tpl, data[sheet], row, form, is_new=False)
        except ops.OpError as exc:
            return _concept(request, project, data, sheet, row, False, exc.messages, 400)
        if changes:
            ws.save(project, data, author=author(request), action=f"Begrepp {code} ändrat i {sheet}",
                    detail={k: {"före": v[0], "efter": v[1]} for k, v in changes.items()})
        return redirect(f"/p/{project.id}/s/{quote(sheet)}", "Sparat." if changes else "Inga ändringar.")

    def _form_row(tpl, form):
        return {c.source_column: form.get(f"c{i}") for i, c in enumerate(tpl.columns)}

    async def concept_retire(request: Request):
        project, data = load(request.path_params["pid"])
        sheet, code = request.path_params["sheet"], request.path_params["code"]
        row = ops.find_row(data[sheet], project.template, code) if project and sheet in data else None
        if row is None:
            return not_found(request, "Begreppet")
        project.template = ops.retire(project.template, data[sheet], row)
        ws.save(project, data, author=author(request), action=f"Begrepp {code} avvecklat (inaktivt) i {sheet}")
        return redirect(f"/p/{project.id}/s/{quote(sheet)}", f"{code} är markerat som inaktivt.")

    async def concept_delete(request: Request):
        project, data = load(request.path_params["pid"])
        sheet, code = request.path_params["sheet"], request.path_params["code"]
        row = ops.find_row(data[sheet], project.template, code) if project and sheet in data else None
        if row is None:
            return not_found(request, "Begreppet")
        ok, reason = ops.can_delete(project.template, ops.released_codes(ws, project.id, sheet), code)
        if not ok:
            return redirect(f"/p/{project.id}/s/{quote(sheet)}/c/{quote(code)}", reason)
        data[sheet].rows.remove(row)
        ws.save(project, data, author=author(request), action=f"Begrepp {code} borttaget ur {sheet}",
                detail={"rad": row})
        return redirect(f"/p/{project.id}/s/{quote(sheet)}", f"{code} är borttaget.")

    # -- kolumner (mallen) -----------------------------------------------------------------------

    async def columns_page(request: Request, errors=None, status=200, form_index=None):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        reg = registry()
        tpl = project.template
        return render(request, "columns.html", status=status, project=project, tpl=tpl, errors=errors or [],
                      roles=ops.roles_for(tpl), properties=ops.property_choices(tpl, reg),
                      uses=ops.designation_uses(tpl, reg), types=ops.PROPERTY_TYPES, open_index=form_index,
                      contexts=[g for g in tpl.generated_codesystems if g.purpose == "usage-context"])

    async def column_save(request: Request):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        i = request.path_params["i"]
        if not 0 <= i < len(project.template.columns):
            return not_found(request, "Kolumnen")
        form = dict(await request.form())
        before = project.template.columns[i].model_dump(exclude_none=True, exclude={"ui"})
        try:
            project.template = ops.update_column(project.template, i, form, registry())
        except ops.OpError as exc:
            return await columns_page(request, exc.messages, 400, form_index=i)
        after = project.template.columns[i].model_dump(exclude_none=True, exclude={"ui"})
        ws.save(project, author=author(request), action=f"Kolumnen '{project.template.columns[i].source_column}' ändrad",
                detail={"före": before, "efter": after})
        return redirect(f"/p/{project.id}/columns", "Kolumnen är sparad.")

    async def column_add(request: Request):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        form = dict(await request.form())
        try:
            project.template = ops.add_column(project.template, data, form, registry())
        except ops.OpError as exc:
            return await columns_page(request, exc.messages, 400, form_index=-1)
        ws.save(project, data, author=author(request), action=f"Kolumnen '{form.get('label')}' tillagd",
                detail={"roll": form.get("role")})
        return redirect(f"/p/{project.id}/columns", "Kolumnen är tillagd.")

    # -- releaser -----------------------------------------------------------------------------

    async def releases_page(request: Request, errors=None, status=200):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        resources, warnings = ops.build(project, data)
        return render(request, "releases.html", status=status, project=project, releases=ws.releases(project.id),
                      suggested=ops.suggest_version(ws, project), warnings=warnings, errors=errors or [],
                      preview=resources)

    async def release_create(request: Request):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        form = await request.form()
        try:
            rel = ops.create_release_candidate(ws, project, data, (form.get("version") or "").strip(),
                                               author(request), (form.get("notes") or "").strip())
        except ops.OpError as exc:
            return await releases_page(request, exc.messages, 400)
        return redirect(f"/p/{project.id}/releases/{rel.version}", f"Releasekandidat {rel.version} är skapad.")

    async def release_page(request: Request):
        project, _ = load(request.path_params["pid"])
        try:
            rel = ws.release(project.id, request.path_params["version"]) if project else None
        except KeyError:
            rel = None
        if rel is None:
            return not_found(request, "Releasen")
        return render(request, "release.html", project=project, rel=rel,
                      latest=ws.latest_release(project.id))

    async def release_approve(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        try:
            rel = ops.approve_release(ws, project, request.path_params["version"], author(request))
        except (ops.OpError, KeyError) as exc:
            msg = "; ".join(exc.messages) if isinstance(exc, ops.OpError) else "Releasen finns inte."
            return redirect(f"/p/{project.id}/releases/{request.path_params['version']}", msg)
        return redirect(f"/p/{project.id}/releases/{rel.version}", f"Release {rel.version} är godkänd.")

    async def release_publish(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        version = request.path_params["version"]
        if os.environ.get("TX_PUBLISH") != "1":
            return redirect(f"/p/{project.id}/releases/{version}", "Publicering är avstängd (TX_PUBLISH=1 saknas).")
        from ..fhir.client import TerminologyClient
        async with TerminologyClient(ServerConfig.from_env()) as client:
            try:
                rel = await ops.publish_release(ws, project, version, client, author(request))
            except ops.OpError as exc:
                return redirect(f"/p/{project.id}/releases/{version}", "; ".join(exc.messages))
        ok = rel.published is not None
        return redirect(f"/p/{project.id}/releases/{version}",
                        "Publicerad." if ok else "Publiceringen misslyckades delvis – se loggen nedan.")

    def _bundle(resources):
        return {"resourceType": "Bundle", "type": "collection",
                "entry": [{"fullUrl": r["url"], "resource": r} for r in resources]}

    async def release_bundle(request: Request):
        project, _ = load(request.path_params["pid"])
        try:
            rel = ws.release(project.id, request.path_params["version"]) if project else None
        except KeyError:
            rel = None
        if rel is None:
            return not_found(request, "Releasen")
        return _json_download(_bundle(rel.resources), f"{project.id}-{rel.version}.json")

    async def preview_bundle(request: Request):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        resources, _ = ops.build(project, data)
        return _json_download(_bundle(resources), f"{project.id}-arbetskopia.json")

    async def template_json(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        return _json_download(project.template.model_dump(mode="json", exclude_none=True),
                              f"{project.id}-mall.json")

    def _json_download(data, filename):
        return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/fhir+json",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    async def validate_page(request: Request):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        from ..fhir.client import TerminologyClient
        from ..importer.subset import validate_valueset_codes
        resources, _ = ops.build(project, data)
        reports, error = [], None
        try:
            async with TerminologyClient(ServerConfig.from_env()) as client:
                # Det första ValueSet:et är hela urvalet; gruppernas ValueSet är delmängder av det.
                umbrella = next((r for r in resources if r["resourceType"] == "ValueSet"), None)
                if umbrella:
                    vs = {**umbrella, "compose": {"include": [i for i in umbrella["compose"]["include"]
                                                              if not i["system"].startswith("urn:term-assistant")]}}
                    reports.append((vs["name"], await validate_valueset_codes(client, vs)))
        except Exception as exc:  # nätverksfel m.m. visas på sidan
            error = str(exc)
        ws.log(project.id, author(request), "Urval validerat mot terminologitjänsten",
               {r[0]: r[1]["summary"] for r in reports})
        return render(request, "validate.html", project=project, reports=reports, error=error)

    async def log_page(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        return render(request, "log.html", project=project, log=ws.read_log(project.id, 1000))

    async def health(request: Request):
        return JSONResponse({"status": "ok", "projects": len(ws.projects())})

    routes = [
        Route("/", index),
        Route("/health", health),
        Route("/import", import_form, methods=["GET"]),
        Route("/import", import_upload, methods=["POST"]),
        Route("/import/{uid}", import_plan),
        Route("/import/{uid}/create/{i:int}", import_create, methods=["POST"]),
        Route("/p/{pid}", project_page),
        Route("/p/{pid}/meta", project_meta, methods=["POST"]),
        Route("/p/{pid}/questions/{i:int}", toggle_question, methods=["POST"]),
        Route("/p/{pid}/columns", columns_page, methods=["GET"]),
        Route("/p/{pid}/columns/new", column_add, methods=["POST"]),
        Route("/p/{pid}/columns/{i:int}", column_save, methods=["POST"]),
        Route("/p/{pid}/releases", releases_page, methods=["GET"]),
        Route("/p/{pid}/releases", release_create, methods=["POST"]),
        Route("/p/{pid}/releases/{version}", release_page),
        Route("/p/{pid}/releases/{version}/approve", release_approve, methods=["POST"]),
        Route("/p/{pid}/releases/{version}/publish", release_publish, methods=["POST"]),
        Route("/p/{pid}/releases/{version}/bundle.json", release_bundle),
        Route("/p/{pid}/preview.json", preview_bundle),
        Route("/p/{pid}/template.json", template_json),
        Route("/p/{pid}/validate", validate_page, methods=["POST"]),
        Route("/p/{pid}/log", log_page),
        Route("/p/{pid}/s/{sheet}", sheet_page),
        Route("/p/{pid}/s/{sheet}/new", concept_form, methods=["GET"]),
        Route("/p/{pid}/s/{sheet}/new", concept_save, methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}", concept_form, methods=["GET"]),
        Route("/p/{pid}/s/{sheet}/c/{code}", concept_save, methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}/retire", concept_retire, methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}/delete", concept_delete, methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    return Starlette(routes=routes)


def run(workspace: str, host: str = "127.0.0.1", port: int = 8000, base_url: str = "http://example.org/fhir",
        registry_path: str | None = None) -> None:
    import uvicorn
    uvicorn.run(create_app(workspace, registry_path, base_url), host=host, port=port)
