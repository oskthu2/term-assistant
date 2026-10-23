"""Webbappen för administrationsvyn (Starlette + Jinja2, serverrenderad).

Starta:  term-assistant admin --workspace ./arbetsyta

Vyn har ingen egen inloggning. Kör den bakom en proxy som sköter inloggning
och skickar användarnamnet i ``X-Remote-User``. Namnet hamnar i loggen och
används för fyra ögon (granskning och godkännande av releaser).

Arbetsytans inställningar (namnrymder, informationsklasser, publiceringsmål,
redaktionella regler) ligger i ``<arbetsyta>/settings.json``; se
``settings.py``. Publicering via FHIR-API kräver dessutom ``TX_PUBLISH=1``.
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

from ..config import ServerConfig, fhir_version
from ..importer.mapping import ColumnRole
from ..importer.plan import ImportPlan, plan_import
from ..importer.registry import PropertyRegistry
from ..importer.replacements import RELATIONS
from ..importer.suggest import MappingProposal
from ..importer.workbook import read_workbook
from . import editorial, ops
from .settings import Settings
from .store import Workspace

HERE = Path(__file__).parent


def author(request: Request) -> str:
    return request.headers.get("x-remote-user") or request.headers.get("x-forwarded-user") or "okänd"


def create_app(workspace: str | Path, registry_path: str | Path | None = None,
               base_url: str = "http://example.org/fhir", tx_client_factory=None, ai_client_factory=None
               ) -> Starlette:
    """``tx_client_factory``/``ai_client_factory`` gör det möjligt att byta klienter i test."""
    ws = Workspace(workspace)
    reg_path = Path(registry_path) if registry_path else ws.root / "registry.json"
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    env.globals.update(ROLE_LABELS=ops.ROLE_LABELS, ColumnRole=ColumnRole, quote=quote, RELATIONS=RELATIONS,
                       tx_url=lambda: ServerConfig.from_env().base_url, fhir_version=fhir_version,
                       publish_enabled=lambda: os.environ.get("TX_PUBLISH") == "1",
                       validator_enabled=lambda: bool(os.environ.get("FHIR_VALIDATOR_JAR")),
                       code_count=lambda r: len(ops.resource_codes(r)))

    def settings() -> Settings:
        return Settings.load(ws.root)

    def registry() -> PropertyRegistry:
        return PropertyRegistry.load(reg_path)

    def tx_client(base: str | None = None):
        if tx_client_factory:
            return tx_client_factory()
        from dataclasses import replace

        from ..fhir.client import TerminologyClient
        config = ServerConfig.from_env()
        return TerminologyClient(replace(config, base_url=base.rstrip("/")) if base else config)

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("msg", request.query_params.get("msg"))
        if "project" in ctx and "data_all" not in ctx:
            ctx["data_all"] = ctx.get("data") if isinstance(ctx.get("data"), dict) else ws.data(ctx["project"].id)
        if "project" in ctx:
            ctx.setdefault("open_proposals", sum(p.status == "öppen" for p in ws.proposals(ctx["project"].id)))
        ctx.setdefault("errors", [])
        ctx.setdefault("settings", settings())
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

    def sheet_url(project, sheet, code=None) -> str:
        return f"/p/{project.id}/s/{quote(sheet)}" + (f"/c/{quote(str(code))}" if code is not None else "")

    # -- start, inställningar och inläsning ----------------------------------------------------

    async def index(request: Request):
        return render(request, "index.html", projects=ws.projects())

    async def settings_page(request: Request):
        return render(request, "settings.html", path=ws.root / "settings.json",
                      settings_json=settings().model_dump_json(indent=2))

    async def import_form(request: Request):
        return render(request, "import.html", base_url=base_url)

    async def import_upload(request: Request):
        form = await request.form()
        upload = form.get("file")
        cfg = settings()
        if upload is None or not getattr(upload, "filename", ""):
            return render(request, "import.html", status=400, base_url=base_url, errors=["Välj en fil."])
        ns = cfg.namespace(form.get("namespace"))
        if ns is not None:
            base = ns.base_url
        elif cfg.allow_custom_namespace and (form.get("base_url") or "").strip():
            base = form["base_url"].strip()
        else:
            return render(request, "import.html", status=400, base_url=base_url, errors=["Välj en namnrymd."])
        uid, path = ws.save_upload(upload.filename, await upload.read())
        try:
            plan, _ = plan_import(path, base, registry(), language=(form.get("language") or "sv").strip())
        except Exception as exc:  # trasig eller okänd fil: visa felet i stället för att krascha
            return render(request, "import.html", status=400, base_url=base_url,
                          errors=[f"Filen kunde inte läsas ({type(exc).__name__}: {exc}). "
                                  "Kontrollera att det är en Excel-, CSV- eller PDF-fil."])
        choices = {"namespace": ns.key if ns else None, "classification": form.get("classification")}
        ws.upload_cache(uid).write_text(json.dumps({"plan": json.loads(plan.model_dump_json()), **choices}),
                                        encoding="utf-8")
        return redirect(f"/import/{uid}")

    def cached_plan(uid: str) -> tuple[ImportPlan | None, dict]:
        try:
            raw = json.loads(ws.upload_cache(uid).read_text(encoding="utf-8"))
        except (KeyError, FileNotFoundError):
            return None, {}
        return ImportPlan.model_validate(raw["plan"]), raw

    async def import_plan(request: Request):
        plan, choices = cached_plan(request.path_params["uid"])
        if plan is None:
            return not_found(request, "Inläsningen")
        return render(request, "import_plan.html", uid=request.path_params["uid"], plan=plan,
                      assessment=plan.assessment, choices=choices)

    async def import_create(request: Request):
        uid, i = request.path_params["uid"], request.path_params["i"]
        plan, choices = cached_plan(uid)
        if plan is None or i >= len(plan.proposals):
            return not_found(request, "Förslaget")
        form = await request.form()
        cfg = settings()
        proposal: MappingProposal = plan.proposals[i]
        title = (form.get("title") or proposal.template.codesystem.title or proposal.template.codesystem.name).strip()
        sheets = read_workbook(ws.upload_path(uid))
        project = ops.create_project(ws, proposal, sheets, title, author(request),
                                     namespace=cfg.namespace(choices.get("namespace")),
                                     classification=cfg.classification(choices.get("classification")).key)
        return redirect(f"/p/{project.id}", "Projektet är skapat. Gå igenom de öppna frågorna.")

    # -- projekt ---------------------------------------------------------------------------------

    async def project_page(request: Request):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        return render(request, "project.html", project=project, data=data, releases=ws.releases(project.id),
                      tpl=project.template, log=ws.read_log(project.id, 30))

    async def project_meta(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        form = await request.form()
        cfg = settings()
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
        if form.get("namespace") is not None:
            tpl.namespace = form.get("namespace") or None
        if form.get("classification"):
            tpl.classification = cfg.classification(form.get("classification")).key
        tpl.license_note = (form.get("license_note") or "").strip() or None
        tpl.policy.allow_code_removal = bool(form.get("allow_code_removal"))
        tpl.policy.allow_display_change = bool(form.get("allow_display_change"))
        ws.save(project, author=author(request), action="Metadata ändrad",
                detail={"informationsklass": tpl.classification, "namnrymd": tpl.namespace})
        return redirect(f"/p/{project.id}", "Sparat.")

    async def project_oids(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        try:
            assigned = ops.assign_oids(ws, project, settings())
        except ops.OpError as exc:
            return redirect(f"/p/{project.id}", "; ".join(exc.messages))
        ws.save(project, author=author(request), action="OID tilldelade", detail={"oid": assigned})
        return redirect(f"/p/{project.id}", f"{len(assigned)} OID tilldelade." if assigned else "Alla har OID.")

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

    # -- tabell och begrepp -------------------------------------------------------------------------

    async def sheet_page(request: Request):
        project, data = load(request.path_params["pid"])
        sheet = request.path_params["sheet"]
        if project is None or sheet not in data:
            return not_found(request)
        tpl = project.template
        pending = {p.code for p in ws.proposals(project.id) if p.status == "öppen" and p.sheet == sheet}
        return render(request, "sheet.html", project=project, tpl=tpl, sheet=sheet, data=data[sheet],
                      columns=ops.visible_columns(tpl), cell=ops.cell, code_col=ops.code_column(tpl),
                      released=ops.released_codes(ws, project.id, sheet), data_all=data,
                      flags=project.flags.get(sheet, {}), pending=pending)

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
        codes = [r.get(ops.code_column(tpl)) for r in data[sheet].rows]
        proposals = [p for p in ws.proposals(project.id) if p.sheet == sheet and p.code == (str(code) if code else None)
                     and not is_new]
        return render(request, "concept.html", status=status, project=project, tpl=tpl, sheet=sheet, row=row,
                      is_new=is_new, columns=ops.visible_columns(tpl), cell=ops.cell, widget=ops.widget,
                      errors=errors or [], released=str(code) in released, deletable=deletable,
                      delete_reason=reason, parents=codes, flag=project.flags.get(sheet, {}).get(str(code)),
                      proposals=proposals)

    async def concept_save(request: Request):
        project, data = load(request.path_params["pid"])
        sheet = request.path_params["sheet"]
        if project is None or sheet not in data:
            return not_found(request)
        code = request.path_params.get("code")
        tpl = project.template
        form = dict(await request.form())
        if settings().editorial.review_manual_edits:
            try:
                p = editorial.propose_edit(ws, project, data, sheet, code, form, author(request),
                                           (form.get("rationale") or "").strip())
            except ops.OpError as exc:
                row = ops.find_row(data[sheet], tpl, code) if code else {h: None for h in data[sheet].headers}
                return _concept(request, project, data, sheet, row, code is None, exc.messages, 400)
            return redirect(sheet_url(project, sheet), f"Förslag {p.id} är skickat för granskning.")
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
            return redirect(sheet_url(project, sheet), f"Begreppet {new_code} är tillagt.")
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
        return redirect(sheet_url(project, sheet), "Sparat." if changes else "Inga ändringar.")

    def _form_row(tpl, form):
        return {c.source_column: form.get(f"c{i}") for i, c in enumerate(tpl.columns)}

    async def concept_retire(request: Request):
        project, data = load(request.path_params["pid"])
        sheet, code = request.path_params["sheet"], request.path_params["code"]
        row = ops.find_row(data[sheet], project.template, code) if project and sheet in data else None
        if row is None:
            return not_found(request, "Begreppet")
        form = await request.form()
        replaced_by = (form.get("replaced_by") or "").strip() or None
        relation, comment = form.get("relation") or "equivalent", (form.get("comment") or "").strip()
        if settings().editorial.review_manual_edits:
            p = editorial.propose_retire(ws, project, sheet, code, author(request), replaced_by, relation, comment)
            return redirect(sheet_url(project, sheet), f"Förslag {p.id} om avveckling är skickat för granskning.")
        try:
            project.template = ops.retire(project.template, data, sheet, row, replaced_by, relation, comment)
        except ops.OpError as exc:
            return _concept(request, project, data, sheet, row, False, exc.messages, 400)
        ws.save(project, data, author=author(request), action=f"Begrepp {code} avvecklat i {sheet}",
                detail={"ersätts av": replaced_by, "relation": relation if replaced_by else None,
                        "kommentar": comment or None})
        return redirect(sheet_url(project, sheet),
                        f"{code} är avvecklat" + (f" och ersätts av {replaced_by}." if replaced_by else "."))

    async def concept_delete(request: Request):
        project, data = load(request.path_params["pid"])
        sheet, code = request.path_params["sheet"], request.path_params["code"]
        row = ops.find_row(data[sheet], project.template, code) if project and sheet in data else None
        if row is None:
            return not_found(request, "Begreppet")
        ok, reason = ops.can_delete(project.template, ops.released_codes(ws, project.id, sheet), code)
        if not ok:
            return redirect(sheet_url(project, sheet, code), reason)
        data[sheet].rows.remove(row)
        ws.save(project, data, author=author(request), action=f"Begrepp {code} borttaget ur {sheet}",
                detail={"rad": row})
        return redirect(sheet_url(project, sheet), f"{code} är borttaget.")

    async def concept_replace(request: Request):
        """Urval: byt en flaggad kod mot en ersättare."""
        project, data = load(request.path_params["pid"])
        sheet, code = request.path_params["sheet"], request.path_params["code"]
        row = ops.find_row(data[sheet], project.template, code) if project and sheet in data else None
        if row is None:
            return not_found(request, "Koden")
        form = await request.form()
        new_code = (form.get("new_code") or "").strip()
        try:
            ops.replace_code(project.template, data[sheet], row, new_code, form.get("display"))
        except ops.OpError as exc:
            return _concept(request, project, data, sheet, row, False, exc.messages, 400)
        project.flags.get(sheet, {}).pop(code, None)
        ws.save(project, data, author=author(request), action=f"Kod {code} ersatt med {new_code} i urvalet {sheet}",
                detail={"källa": form.get("source")})
        return redirect(sheet_url(project, sheet), f"{code} är ersatt med {new_code}.")

    # -- redaktionell kedja -------------------------------------------------------------------------

    async def proposals_page(request: Request):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        items = sorted(ws.proposals(project.id), key=lambda p: (p.status != "öppen", p.created), reverse=False)
        return render(request, "proposals.html", project=project, data=data, proposals=items)

    async def proposal_review(request: Request):
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        form = await request.form()
        accept = request.path_params["decision"] == "accept"
        try:
            p = editorial.review(ws, project, data, request.path_params["id"], author(request), accept,
                                 (form.get("comment") or "").strip(), settings())
        except ops.OpError as exc:
            return redirect(f"/p/{project.id}/proposals", "; ".join(exc.messages))
        return redirect(f"/p/{project.id}/proposals", f"Förslag {p.id}: {p.status}.")

    async def ai_review(request: Request):
        project, data = load(request.path_params["pid"])
        sheet = request.path_params["sheet"]
        if project is None or sheet not in data:
            return not_found(request)
        try:
            client = ai_client_factory() if ai_client_factory else None
            made = editorial.ai_review(ws, project, data, sheet, author(request), client=client)
        except Exception as exc:  # saknade nycklar, nätverk, vägran …
            msg = "; ".join(exc.messages) if isinstance(exc, ops.OpError) else f"{type(exc).__name__}: {exc}"
            return redirect(sheet_url(project, sheet), f"AI-granskningen misslyckades: {msg}")
        return redirect(f"/p/{project.id}/proposals", f"AI-granskningen gav {len(made)} förslag att granska.")

    # -- kolumner (mallen) ------------------------------------------------------------------------

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
        ws.save(project, author=author(request),
                action=f"Kolumnen '{project.template.columns[i].source_column}' ändrad",
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

    # -- releaser -------------------------------------------------------------------------------

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
                                               author(request), (form.get("notes") or "").strip(), settings())
        except ops.OpError as exc:
            return await releases_page(request, exc.messages, 400)
        return redirect(f"/p/{project.id}/releases/{rel.version}", f"Releasekandidat {rel.version} är skapad.")

    def _release(project, version):
        try:
            return ws.release(project.id, version) if project else None
        except KeyError:
            return None

    async def release_page(request: Request):
        project, _ = load(request.path_params["pid"])
        rel = _release(project, request.path_params["version"])
        if rel is None:
            return not_found(request, "Releasen")
        cfg = settings()
        targets = [(t, cfg.allowed(project.template.classification, t)) for t in cfg.targets]
        return render(request, "release.html", project=project, rel=rel, latest=ws.latest_release(project.id),
                      targets=targets, classification=cfg.classification(project.template.classification))

    async def release_approve(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        version = request.path_params["version"]
        try:
            rel = ops.approve_release(ws, project, version, author(request), settings())
        except (ops.OpError, KeyError) as exc:
            msg = "; ".join(exc.messages) if isinstance(exc, ops.OpError) else "Releasen finns inte."
            return redirect(f"/p/{project.id}/releases/{version}", msg)
        return redirect(f"/p/{project.id}/releases/{rel.version}", f"Release {rel.version} är godkänd.")

    async def release_publish(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        version = request.path_params["version"]
        form = await request.form()
        cfg = settings()
        target = cfg.target(form.get("target") or "")
        if target is None:
            return redirect(f"/p/{project.id}/releases/{version}", "Välj ett mål.")
        if target.kind == "fhir-api" and os.environ.get("TX_PUBLISH") != "1":
            return redirect(f"/p/{project.id}/releases/{version}", "Publicering via API är avstängd (TX_PUBLISH=1).")
        try:
            if target.kind == "fhir-api":
                async with tx_client(target.base_url) as client:
                    rel = await ops.publish_release(ws, project, version, target.key, author(request), cfg, client)
            else:
                rel = await ops.publish_release(ws, project, version, target.key, author(request), cfg)
        except ops.OpError as exc:
            return redirect(f"/p/{project.id}/releases/{version}", "; ".join(exc.messages))
        return redirect(f"/p/{project.id}/releases/{version}",
                        "Publicerad." if rel.published else "Publiceringen misslyckades delvis – se loggen nedan.")

    def _bundle(resources):
        return {"resourceType": "Bundle", "type": "collection",
                "entry": [{"fullUrl": r["url"], "resource": r} for r in resources]}

    def _download(data: bytes | str, filename: str, media_type: str) -> Response:
        return Response(data, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    def _json_download(data, filename):
        return _download(json.dumps(data, ensure_ascii=False, indent=2), filename, "application/fhir+json")

    async def release_hl7(request: Request):
        """Kör inbyggd kontroll + HL7-validatorn på releasens paket (tar 10–60 s)."""
        from starlette.concurrency import run_in_threadpool

        from ..importer.validate import validate_package_file
        project, _ = load(request.path_params["pid"])
        rel = _release(project, request.path_params["version"])
        if rel is None:
            return not_found(request, "Releasen")
        form = await request.form()
        tx = ServerConfig.from_env().base_url if form.get("tx") == "server" else "n/a"
        _, data = ops.release_package(ws, project, rel.version, settings())
        try:
            result = await run_in_threadpool(validate_package_file, data, fhir_version(), None, tx)
        except Exception as exc:  # saknad Java/jar, tidsgräns …
            return redirect(f"/p/{project.id}/releases/{rel.version}", f"Valideringen kunde inte köras: {exc}")
        from .store import now
        rel.hl7_validation = {**result, "tx": tx, "time": now(), "by": author(request)}
        ws.save_release(project.id, rel)
        ws.log(project.id, author(request), f"Paketet {rel.version} validerat",
               {"fel": len(result.get("errors", [])) + len(result["builtin_errors"]),
                "varningar": len(result.get("warnings", []))})
        return redirect(f"/p/{project.id}/releases/{rel.version}", "Valideringen är klar.")

    async def release_bundle(request: Request):
        project, _ = load(request.path_params["pid"])
        rel = _release(project, request.path_params["version"])
        if rel is None:
            return not_found(request, "Releasen")
        return _json_download(_bundle(rel.resources), f"{project.id}-{rel.version}.json")

    async def release_tgz(request: Request):
        project, _ = load(request.path_params["pid"])
        rel = _release(project, request.path_params["version"])
        if rel is None:
            return not_found(request, "Releasen")
        filename, data = ops.release_package(ws, project, rel.version, settings())
        ws.log(project.id, author(request), f"Paket {filename} hämtat",
               {"informationsklass": project.template.classification})
        return _download(data, filename, "application/gzip")

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

    async def check_page(request: Request):
        """Urval: flagga koder som behöver hanteras (lokala kodverk + terminologitjänsten)."""
        project, data = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        use_server = (await request.form()).get("server") != "nej"
        try:
            if use_server:
                async with tx_client() as client:
                    result = await ops.check_subset(ws, project, data, client)
            else:
                result = await ops.check_subset(ws, project, data)
        except ops.OpError as exc:
            return redirect(f"/p/{project.id}", "; ".join(exc.messages))
        ws.save(project, author=author(request), action="Urvalet kontrollerat",
                detail={"flaggade": result["flags"], **result["summary"]})
        sheet = next(iter(data))
        msg = f"{result['flags']} koder behöver hanteras."
        if result["server_error"]:
            msg += f" Terminologitjänsten kunde inte nås: {result['server_error']}"
        return redirect(sheet_url(project, sheet), msg)

    async def log_page(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        return render(request, "log.html", project=project, log=ws.read_log(project.id, 1000))

    async def health(request: Request):
        return JSONResponse({"status": "ok", "projects": len(ws.projects()), "fhirVersion": fhir_version()})

    routes = [
        Route("/", index),
        Route("/health", health),
        Route("/settings", settings_page),
        Route("/import", import_form, methods=["GET"]),
        Route("/import", import_upload, methods=["POST"]),
        Route("/import/{uid}", import_plan),
        Route("/import/{uid}/create/{i:int}", import_create, methods=["POST"]),
        Route("/p/{pid}", project_page),
        Route("/p/{pid}/meta", project_meta, methods=["POST"]),
        Route("/p/{pid}/oids", project_oids, methods=["POST"]),
        Route("/p/{pid}/questions/{i:int}", toggle_question, methods=["POST"]),
        Route("/p/{pid}/proposals", proposals_page),
        Route("/p/{pid}/proposals/{id}/{decision:str}", proposal_review, methods=["POST"]),
        Route("/p/{pid}/columns", columns_page, methods=["GET"]),
        Route("/p/{pid}/columns/new", column_add, methods=["POST"]),
        Route("/p/{pid}/columns/{i:int}", column_save, methods=["POST"]),
        Route("/p/{pid}/releases", releases_page, methods=["GET"]),
        Route("/p/{pid}/releases", release_create, methods=["POST"]),
        Route("/p/{pid}/releases/{version}", release_page),
        Route("/p/{pid}/releases/{version}/approve", release_approve, methods=["POST"]),
        Route("/p/{pid}/releases/{version}/publish", release_publish, methods=["POST"]),
        Route("/p/{pid}/releases/{version}/bundle.json", release_bundle),
        Route("/p/{pid}/releases/{version}/validate", release_hl7, methods=["POST"]),
        Route("/p/{pid}/releases/{version}/package.tgz", release_tgz),
        Route("/p/{pid}/preview.json", preview_bundle),
        Route("/p/{pid}/template.json", template_json),
        Route("/p/{pid}/check", check_page, methods=["POST"]),
        Route("/p/{pid}/log", log_page),
        Route("/p/{pid}/s/{sheet}", sheet_page),
        Route("/p/{pid}/s/{sheet}/ai", ai_review, methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/new", concept_form, methods=["GET"]),
        Route("/p/{pid}/s/{sheet}/new", concept_save, methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}", concept_form, methods=["GET"]),
        Route("/p/{pid}/s/{sheet}/c/{code}", concept_save, methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}/retire", concept_retire, methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}/delete", concept_delete, methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}/replace", concept_replace, methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    return Starlette(routes=routes)


def run(workspace: str, host: str = "127.0.0.1", port: int = 8000, base_url: str = "http://example.org/fhir",
        registry_path: str | None = None) -> None:
    import uvicorn
    uvicorn.run(create_app(workspace, registry_path, base_url), host=host, port=port)
