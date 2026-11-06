"""Webbappen för administrationsvyn (Starlette + Jinja2, serverrenderad).

Starta:  term-inlasning admin --workspace ./arbetsyta

Vyn har ingen egen inloggning. Kör den bakom en proxy som sköter inloggning
och skickar användarnamnet i ``X-Remote-User``. Namnet hamnar i loggen och
används för fyra ögon (granskning och godkännande av releaser).

Arbetsytans inställningar (namnrymder, informationsklasser, publiceringsmål,
redaktionella regler) ligger i ``<arbetsyta>/settings.json``; se
``settings.py``. Publicering via FHIR-API kräver dessutom ``TX_PUBLISH=1``.
"""

from __future__ import annotations

import hmac
import json
import re
import time
import os
from pathlib import Path
from urllib.parse import quote, urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..config import ServerConfig, fhir_version
from ..importer.mapping import ColumnRole
from ..importer.plan import ImportPlan, plan_import
from ..importer.registry import PropertyRegistry
from ..importer import conceptmap, lifecycle, metadata, supplement
from ..importer.replacements import RELATIONS
from ..importer.suggest import MappingProposal
from ..importer.workbook import read_workbook
from . import auth, editorial, ops
from .settings import Settings
from .store import Workspace

HERE = Path(__file__).parent


def dev_users() -> list[str]:
    """Testläge utan inloggningsproxy: TERM_DEV_USERS="anna,bertil" ger ett användarbyte i sidhuvudet."""
    return [u.strip() for u in os.environ.get("TERM_DEV_USERS", "").split(",") if u.strip()]


def author(request: Request) -> str:
    """Den inloggade användaren (sätts av inloggningen i create_app). Användarhuvuden godtas bara i
    proxyläge och bara från en betrodd proxy – se auth.py."""
    ident = getattr(request.state, "identity", None)
    return ident.username if ident is not None and ident.username else "okänd"


class SecurityMiddleware:
    """Säkerhetshuvuden och skydd mot förfalskade formulär (CSRF): en POST med ett Origin eller Referer
    från en annan webbplats avvisas. Sessionscookien är dessutom SameSite=Lax."""

    def __init__(self, app, allowed_origin: str | None = None):
        self.app = app
        self.allowed_origin = allowed_origin.rstrip("/") if allowed_origin else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            origin = headers.get("origin") or headers.get("referer")
            if origin and origin != "null":
                from urllib.parse import urlsplit
                parts = urlsplit(origin)
                got = f"{parts.scheme}://{parts.netloc}"
                own = {f"{scope.get('scheme', 'http')}://{headers.get('host', '')}"}
                if self.allowed_origin:
                    own.add(self.allowed_origin)
                if got not in own:
                    response = HTMLResponse("Begäran kom från en annan webbplats och avvisades.", status_code=403)
                    return await response(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                extra = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                         (b"referrer-policy", b"same-origin"),
                         (b"content-security-policy", b"frame-ancestors 'none'")]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)
        await self.app(scope, receive, send_with_headers)


def create_app(workspace: str | Path, registry_path: str | Path | None = None,
               base_url: str = "http://example.org/fhir", tx_client_factory=None, ai_client_factory=None,
               oidc_transport=None) -> Starlette:
    """``tx_client_factory``/``ai_client_factory``/``oidc_transport`` gör det möjligt att byta klienter i test."""
    ws = Workspace(workspace)
    auth_cfg = Settings.load(ws.root).auth  # inloggningsläget gäller tills verktyget startas om
    auth_warnings = auth.check_config(auth_cfg)
    roles_store = auth.RoleStore(ws.root)
    oidc_client = auth.OidcClient(auth_cfg.oidc, os.environ.get("TERM_OIDC_CLIENT_SECRET"), oidc_transport) \
        if auth_cfg.mode == "oidc" else None
    reg_path = Path(registry_path) if registry_path else ws.root / "registry.json"
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    env.globals.update(ROLE_LABELS=ops.ROLE_LABELS, is_retired=ops.is_retired, LIFECYCLE=lifecycle.FIELDS,
                       META_FIELDS=metadata.FIELDS, RELATION_LABELS=conceptmap.LABELS,
                       KIND_LABELS={"codesystem": "kodverk → CodeSystem", "valueset": "urval → ValueSet",
                                    "conceptmap": "mappning → ConceptMap",
                                    "supplement": "översättning → CodeSystem-supplement"}, ColumnRole=ColumnRole, quote=quote, RELATIONS=RELATIONS,
                       tx_url=lambda: ServerConfig.from_env().base_url, fhir_version=fhir_version,
                       publish_enabled=lambda: os.environ.get("TX_PUBLISH") == "1",
                       validator_enabled=lambda: bool(os.environ.get("FHIR_VALIDATOR_JAR")),
                       code_count=lambda r: len(ops.resource_codes(r)),
                       dev_users=lambda: dev_users() if auth_cfg.mode == "demo" else [], auth_mode=auth_cfg.mode,
                       ROLES=auth.ROLES)

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
        ident = identity(request)
        return HTMLResponse(env.get_template(name).render(request=request, user=author(request), ident=ident,
                                                          can=ident.has, **ctx), status_code=status)

    # -- inloggning och behörighet -----------------------------------------------------------

    def identity(request: Request) -> auth.Identity:
        cached = getattr(request.state, "identity", None)
        if cached is not None:
            return cached
        current = settings().auth  # rolltilldelningar läses om vid varje begäran
        ident = auth.ANONYMOUS
        if auth_cfg.mode == "demo":
            cookie = request.cookies.get("dev_user")
            name = cookie if cookie and cookie in dev_users() else "okänd"
            ident = auth.Identity(username=name, roles=auth.resolve_roles(current, roles_store, name, [], demo=True))
        elif auth_cfg.mode == "proxy":
            found = auth.proxy_identity(request.headers, request.client.host if request.client else None, auth_cfg)
            if found is not None:
                found.roles = auth.resolve_roles(current, roles_store, found.username, found.groups)
                roles_store.seen(found)
                ident = found
        else:
            user = request.session.get("user")
            fresh = time.time() - request.session.get("login_at", 0) < auth_cfg.session_minutes * 60
            if user and fresh:
                ident = auth.Identity(username=user["username"], name=user.get("name"), email=user.get("email"),
                                      groups=user.get("groups", []), source="oidc",
                                      roles=auth.resolve_roles(current, roles_store, user["username"],
                                                               user.get("groups", [])))
        request.state.identity = ident
        return ident

    def need(role: str, endpoint):
        """Rutt som kräver en roll. Kontrollen görs på servern – knappar som döljs i vyn är bara bekvämlighet."""
        async def guarded(request: Request):
            ident = identity(request)
            if not ident.authenticated:
                if auth_cfg.mode == "oidc":
                    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                    return RedirectResponse(f"/auth/login?{urlencode({'next': target})}", status_code=303)
                return render(request, "forbidden.html", status=401, needed=role, reason="inloggning")
            if not ident.has(role):
                return render(request, "forbidden.html", status=403, needed=role, reason="roll")
            return await endpoint(request)
        return guarded

    def public_base(request: Request) -> str:
        if auth_cfg.oidc and auth_cfg.oidc.public_url:
            return auth_cfg.oidc.public_url.rstrip("/")
        return str(request.base_url).rstrip("/")

    async def login(request: Request):
        if oidc_client is None:
            return RedirectResponse("/", status_code=303)
        next_url = request.query_params.get("next") or "/"
        if not next_url.startswith("/") or next_url.startswith("//"):  # ingen öppen omdirigering
            next_url = "/"
        try:
            url, pending = await oidc_client.authorization_url(public_base(request) + "/auth/callback")
        except Exception as exc:  # IdP:n nås inte
            return render(request, "error.html", status=502, message=f"Inloggningstjänsten kunde inte nås ({exc}).")
        request.session["pending"] = pending | {"next": next_url}
        return RedirectResponse(url, status_code=303)

    async def callback(request: Request):
        if oidc_client is None:
            return RedirectResponse("/", status_code=303)
        pending = request.session.pop("pending", None)
        params = request.query_params
        if not pending:
            return render(request, "error.html", status=400, message="Inloggningen har gått ut – försök igen.")
        if params.get("error"):
            return render(request, "error.html", status=401,
                          message=f"Inloggningen avbröts ({params.get('error')}).")
        if not hmac.compare_digest(params.get("state", ""), pending["state"]) or \
                time.time() - pending["started"] > 600:
            return render(request, "error.html", status=400, message="Inloggningen kunde inte verifieras (state).")
        try:
            tokens = await oidc_client.exchange(params.get("code", ""), pending)
            claims = await oidc_client.validate(tokens["id_token"], pending["nonce"])
        except auth.AuthError as exc:
            return render(request, "error.html", status=401, message=str(exc))
        user = auth.identity_from_claims(claims, auth_cfg.oidc, settings().auth)
        request.session.clear()  # ny session vid inloggning
        request.session.update(user=user, login_at=int(time.time()), id_token=tokens["id_token"])
        roles_store.seen(auth.Identity(username=user["username"], name=user.get("name"), email=user.get("email"),
                                       groups=user["groups"], source="oidc"))
        return RedirectResponse(pending.get("next") or "/", status_code=303)

    async def logout(request: Request):
        id_token = request.session.get("id_token") if auth_cfg.mode == "oidc" else None
        if auth_cfg.mode == "oidc":
            request.session.clear()
        if oidc_client is not None:
            try:
                url = await oidc_client.logout_url(id_token, public_base(request) + "/auth/utloggad")
            except Exception:  # noqa: BLE001 – lokal utloggning räcker
                url = None
            if url:
                return RedirectResponse(url, status_code=303)
        return RedirectResponse("/auth/utloggad", status_code=303)

    async def logged_out(request: Request):
        return render(request, "error.html", title="Utloggad", message="Du är utloggad.")

    async def users_page(request: Request, errors=None, status=200):
        data = roles_store.all()
        current = settings().auth
        names = sorted(set(data["seen"]) | set(data["users"]) | set(dev_users() if auth_cfg.mode == "demo" else []))
        rows = []
        for name in names:
            seen = data["seen"].get(name, {})
            from_idp = sorted({r for g in seen.get("groups", []) for r in current.role_mapping.get(g, [])}
                              | set(current.default_roles))
            rows.append({"username": name, "name": seen.get("name"), "email": seen.get("email"),
                         "groups": seen.get("groups", []), "last_seen": seen.get("last_seen"),
                         "local": data["users"].get(name, []), "from_idp": from_idp})
        return render(request, "users.html", status=status, rows=rows, mapping=current.role_mapping,
                      default_roles=current.default_roles, log=roles_store.log(), errors=errors or [],
                      warnings=auth_warnings, username_claim=auth_cfg.oidc.username_claim if auth_cfg.oidc else None)

    async def users_save(request: Request):
        form = await request.form()
        username = (form.get("username") or "").strip()
        if not username:
            return await users_page(request, ["Ange användarnamnet."], 400)
        roles = {r for r in auth.ROLES if form.get(f"r_{r}")}
        me = author(request)
        if username == me and "administratör" in roles_store.assigned(me) and "administratör" not in roles:
            return await users_page(request, ["Du kan inte ta bort din egen administratörsroll."], 400)
        roles_store.assign(username, roles, by=me)
        return redirect("/anvandare", f"Rollerna för {username} är sparade.")

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
            reg = registry()
            plan, sheets = plan_import(path, base, reg, language=(form.get("language") or "sv").strip())
            if form.get("ai") and plan.proposals:
                from ..importer.ai import refine_with_claude
                by_name = {s.name: s for s in sheets}
                try:
                    plan.proposals = [refine_with_claude(p, by_name[p.template.source.sheet], reg,
                                                         p.template.codesystem,
                                                         client=ai_client_factory() if ai_client_factory else None)
                                      for p in plan.proposals]
                    plan.assessment.tips.append("Förslagen är granskade av Claude (se motiveringarna).")
                except Exception as exc:  # saknad nyckel, nätverk …
                    plan.assessment.tips.append(f"AI-granskningen kunde inte köras ({type(exc).__name__}: {exc}); "
                                                "förslagen bygger på heuristik.")
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
        return _plan_page(request, request.path_params["uid"], plan, choices)

    def _plan_page(request, uid, plan, choices, errors=None, status=200, selected=None):
        reg = registry()
        targets = []
        for n, p in enumerate(plan.proposals):
            options = ops.target_options(p.template, reg)
            values = {v for _, opts in options for v, _ in opts}
            current = {c.source_column: (selected or {}).get((n, j)) or ops.current_target(c)
                       for j, c in enumerate(p.template.columns)}
            extra = [(v, f"{c.role.value} (som föreslaget)") for c in p.template.columns
                     for v in [ops.current_target(c)] if v not in values]
            if extra:
                options = options + [("Nuvarande", list(dict.fromkeys(extra)))]
            samples = {pr.header: ", ".join(pr.samples[:3]) for pr in p.profiles}
            targets.append({"options": options, "current": current, "samples": samples})
        return render(request, "import_plan.html", status=status, uid=uid, plan=plan, assessment=plan.assessment,
                      choices=choices, targets=targets, errors=errors or [])

    async def import_create(request: Request):
        uid, i = request.path_params["uid"], request.path_params["i"]
        plan, choices = cached_plan(uid)
        if plan is None or i >= len(plan.proposals):
            return not_found(request, "Förslaget")
        form = await request.form()
        cfg = settings()
        proposal: MappingProposal = plan.proposals[i]
        title = (form.get("title") or proposal.template.codesystem.title or proposal.template.codesystem.name).strip()
        reg = registry()
        selected = {}
        metadata.apply_form(proposal.template, form)
        try:
            for j, col in enumerate(list(proposal.template.columns)):
                value = form.get(f"col_{j}")
                selected[(i, j)] = value
                if value and value != ops.current_target(col):
                    proposal.template = ops.apply_target(proposal.template, j, value, reg)
            problems = metadata.import_errors(proposal.template)
            if problems:
                raise ops.OpError(problems)
        except ops.OpError as exc:
            return _plan_page(request, uid, plan, choices, exc.messages, 400, selected)
        sheets = read_workbook(ws.upload_path(uid))
        project = ops.create_project(ws, proposal, sheets, title, author(request),
                                     namespace=cfg.namespace(choices.get("namespace")),
                                     classification=cfg.classification(choices.get("classification")).key)
        learned = ops.learn_from_template(project.template, reg)
        reg.save(reg_path)
        ws.log(project.id, author(request), "Registret lärde sig mappningarna", {"alias": learned})
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
        for field in ("title", "publisher", "description", "purpose", "language", "contact", "copyright",
                      "identifier"):
            if f"cs_{field}" not in form and field == "identifier":
                continue
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
        if project.template.kind == "supplement":
            try:
                async with tx_client() as client:
                    result = await ops.check_translation(project, data, client)
            except Exception as exc:
                return redirect(f"/p/{project.id}", f"Terminologitjänsten kunde inte nås: {exc}")
            ws.save(project, author=author(request), action="Översättningen kontrollerad",
                    detail={"flaggade": result["flags"], "nya koder": len(result["new_codes"])})
            msg = (f"{result['codes']} koder i kodsystemet, {result['untranslated']} rader saknar översättning, "
                   f"{result['flags']} rader behöver hanteras.")
            if result["new_codes"]:
                msg += f" {len(result['new_codes'])} nya koder i kodsystemet – hämta dem från projektets översikt."
            return redirect(sheet_url(project, supplement.SHEET), msg)
        if project.template.kind == "conceptmap":
            try:
                async with tx_client() as client:
                    result = await ops.check_mapping(project, data, client)
            except Exception as exc:
                return redirect(f"/p/{project.id}", f"Terminologitjänsten kunde inte nås: {exc}")
            ws.save(project, author=author(request), action="Mappningen kontrollerad",
                    detail={"flaggade": result["flags"], "omappade": len(result["unmapped"])})
            msg = f"{result['checked']} koder kontrollerade, {result['flags']} rader behöver hanteras."
            if result["unmapped"]:
                msg += (f" {len(result['unmapped'])} källkoder saknar mappning: "
                        + ", ".join(result["unmapped"][:8]) + (" …" if len(result["unmapped"]) > 8 else ""))
            return redirect(sheet_url(project, conceptmap.SHEET), msg)
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

    # -- mappningar och förslag från terminologitjänsten -----------------------------------------

    PROPOSAL_TAG = "urn:term-assistant:arbetsflode|forslag"

    async def mapping_new(request: Request, errors=None, form=None, status=200):
        return render(request, "mapping_new.html", status=status, errors=errors or [], form=form or {},
                      tx=ServerConfig.from_env().base_url)

    async def mapping_create(request: Request):
        form = dict(await request.form())
        cfg = settings()
        errors = []
        title = (form.get("title") or "").strip()
        source, target = (form.get("source") or "").strip(), (form.get("target") or "").strip()
        if not title:
            errors.append("Ange ett namn på mappningen.")
        if not source or not target:
            errors.append("Ange källa (ValueSet eller CodeSystem) och målkodsystem.")
        ns = cfg.namespace(form.get("namespace"))
        if errors:
            return await mapping_new(request, errors, form, 400)
        source_system, source_vs, codes = source, None, []
        try:
            async with tx_client() as client:
                vs = await client.read_canonical("ValueSet", source)
                if vs is not None:  # källan är en värdemängd: mappa dess koder
                    source_vs = source
                    expansion = await client.expand(url=source, count=2000)
                    systems = {c.get("system") for c in expansion["contains"]}
                    if len(systems) != 1:
                        raise ops.OpError(f"Källvärdemängden innehåller {len(systems)} kodsystem – ett "
                                          "mappningsprojekt har ett källkodsystem. Ange kodsystemet i stället.")
                    source_system = systems.pop()
                    codes = expansion["contains"]
                elif await client.read_canonical("CodeSystem", source) is not None:
                    implicit = {"resourceType": "ValueSet", "compose": {"include": [{"system": source}]}}
                    codes = (await client.expand(valueset=implicit, count=2000))["contains"]
                else:
                    raise ops.OpError(f"{source} finns varken som ValueSet eller CodeSystem på terminologitjänsten.")
                if await client.read_canonical("CodeSystem", target) is None:
                    raise ops.OpError(f"Målkodsystemet {target} finns inte på terminologitjänsten.")
        except ops.OpError as exc:
            return await mapping_new(request, exc.messages, form, 400)
        except Exception as exc:  # nätverk m.m.
            return await mapping_new(request, [f"Terminologitjänsten kunde inte nås: {exc}"], form, 502)
        from ..importer.mapping import CodeSystemMeta, MappingSpec
        from ..importer.transform import _ascii
        name = re.sub(r"[^A-Za-z0-9]+", "_", _ascii(title)).strip("_") or "Mappning"
        base = (ns.base_url if ns else "http://example.org/fhir").rstrip("/")
        meta = CodeSystemMeta(url=f"{base}/ConceptMap/{name}", name=name, title=title,
                              description=(form.get("description") or "").strip() or None,
                              sources={"url": "vald namnrymd"})
        spec = MappingSpec(source_system=source_system, target_system=target, source_valueset=source_vs,
                           target_valueset=(form.get("target_valueset") or "").strip() or None,
                           origin="skapad i administrationsverktyget")
        tpl = conceptmap.template(meta, spec)
        rows = ops.source_rows(tpl, codes) if form.get("fill") else []
        project = ops.create_mapping_project(ws, title, meta, spec, rows, author(request), namespace=ns,
                                             classification=cfg.classification(form.get("classification")).key)
        return redirect(sheet_url(project, conceptmap.SHEET),
                        f"Mappningsprojektet är skapat med {len(rows)} källkoder att mappa.")

    async def translation_new(request: Request, errors=None, form=None, status=200):
        return render(request, "translation_new.html", status=status, errors=errors or [], form=form or {},
                      tx=ServerConfig.from_env().base_url)

    async def translation_create(request: Request):
        form = dict(await request.form())
        cfg = settings()
        codesystem = (form.get("codesystem") or "").strip()
        language = (form.get("language") or "").strip()
        errors = []
        if not codesystem:
            errors.append("Ange kodsystemet som ska översättas.")
        if not supplement.LANGUAGE.match(language):
            errors.append(f"'{language}' är ingen språkkod (BCP-47, t.ex. sv, en, nb).")
        if errors:
            return await translation_new(request, errors, form, 400)
        try:
            async with tx_client() as client:
                base = await client.read_canonical("CodeSystem", codesystem)
                if base is None:
                    raise ops.OpError(f"Kodsystemet {codesystem} finns inte på terminologitjänsten.")
                codes = await ops.codesystem_codes(client, codesystem)
        except ops.OpError as exc:
            return await translation_new(request, exc.messages, form, 400)
        except Exception as exc:  # nätverk m.m.
            return await translation_new(request, [f"Terminologitjänsten kunde inte nås: {exc}"], form, 502)
        from ..importer.mapping import CodeSystemMeta, TranslationSpec
        from ..importer.transform import _ascii
        base_title = base.get("title") or base.get("name") or codesystem
        title = (form.get("title") or "").strip() or f"{base_title} ({language})"
        name = re.sub(r"[^A-Za-z0-9]+", "_", _ascii(f"{base.get('name') or base_title}_{language}")).strip("_")
        ns = cfg.namespace(form.get("namespace"))
        url_base = (ns.base_url if ns else "http://example.org/fhir").rstrip("/")
        meta = CodeSystemMeta(url=f"{url_base}/CodeSystem/{name}", name=name, title=title, language=language,
                              description=(form.get("description") or "").strip() or None,
                              case_sensitive=base.get("caseSensitive", True), sources={"url": "vald namnrymd"})
        spec = TranslationSpec(codesystem=codesystem, codesystem_title=base_title,
                               origin="skapad i administrationsverktyget")
        tpl = supplement.template(meta, spec)
        project = ops.create_translation_project(ws, title, meta, spec, supplement.rows(tpl, codes), author(request),
                                                 namespace=ns,
                                                 classification=cfg.classification(form.get("classification")).key)
        return redirect(sheet_url(project, supplement.SHEET), f"Översättningsprojektet är skapat med {len(codes)} koder.")

    async def fetch_new_codes(request: Request):
        project, data = load(request.path_params["pid"])
        if project is None or project.template.kind != "supplement":
            return not_found(request, "Översättningsprojektet")
        try:
            async with tx_client() as client:
                added = await ops.add_missing_codes(project, data, client)
        except Exception as exc:
            return redirect(f"/p/{project.id}", f"Terminologitjänsten kunde inte nås: {exc}")
        if added:
            ws.save(project, data, author=author(request), action="Nya koder hämtade från kodsystemet",
                    detail={"koder": added})
        return redirect(sheet_url(project, supplement.SHEET),
                        f"{len(added)} nya koder lades till." if added else "Inga nya koder i kodsystemet.")

    async def tx_proposals(request: Request):
        """Förslag (urval, mappningar, översättningar) som MCP-servern skapat på terminologitjänsten."""
        found, error = [], None
        try:
            async with tx_client() as client:
                for rtype in ("ConceptMap", "ValueSet", "CodeSystem"):
                    for r in await client.search(rtype, count=100, _tag=PROPOSAL_TAG, _summary=None):
                        if r.get("status") == "draft":  # en släppt resurs är aldrig ett förslag
                            found.append(r)
        except Exception as exc:
            error = f"Terminologitjänsten kunde inte nås: {exc}"
        taken = {p.source_file for p in ws.projects() if p.source_file}
        return render(request, "tx_proposals.html", proposals=found, error=error, taken=taken,
                      tx=ServerConfig.from_env().base_url, take_url="/tx-forslag/ta-in", file=None)

    async def _translation_base(resource: dict) -> list[dict] | None:
        """Alla koder i kodsystemet som en översättning gäller, så att även oöversatta koder får en rad."""
        if resource.get("content") != "supplement" or not resource.get("supplements"):
            return None
        try:
            async with tx_client() as client:
                return await ops.codesystem_codes(client, resource["supplements"].split("|")[0])
        except Exception:  # noqa: BLE001 – då blir det bara de översatta koderna
            return None

    async def proposal_file_upload(request: Request):
        """Ta in en FHIR-Bundle eller ett FHIR-paket (.tgz), t.ex. utkast hämtade från MCP-klienten."""
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            return redirect("/tx-forslag", "Välj en fil (FHIR-Bundle .json eller FHIR-paket .tgz).")
        content = await upload.read()
        try:
            resources = ops.resources_from_fhir_file(content)
        except ops.OpError as exc:
            return redirect("/tx-forslag", "; ".join(exc.messages))
        uid, _ = ws.save_upload(upload.filename, content)
        return redirect(f"/forslag/fil/{uid}", f"{len(resources)} resurser i {upload.filename}.")

    def _file_resources(uid: str) -> tuple[Path, list[dict]]:
        path = ws.upload_path(uid)
        return path, ops.resources_from_fhir_file(path.read_bytes())

    async def proposal_file_page(request: Request):
        try:
            path, resources = _file_resources(request.path_params["uid"])
        except (KeyError, ops.OpError):
            return render(request, "error.html", status=404, message="Filen finns inte.")
        rows = [r | {"_index": i, "_reason": ops.can_take_in(r)} for i, r in enumerate(resources)]
        taken = {p.source_file for p in ws.projects() if p.source_file}
        return render(request, "tx_proposals.html", proposals=rows, error=None, taken=taken, tx=None,
                      take_url=f"/forslag/fil/{request.path_params['uid']}/ta-in", file=path.name)

    async def proposal_file_take(request: Request):
        uid = request.path_params["uid"]
        form = await request.form()
        cfg = settings()
        try:
            _, resources = _file_resources(uid)
            index = int(form.get("index", "-1"))
            if not 0 <= index < len(resources):
                raise ops.OpError("Resursen finns inte i filen.")
            resource = resources[index]
            reason = ops.can_take_in(resource)
            if reason:
                raise ops.OpError(reason)
            resource.setdefault("id", resource.get("name"))
            title = (form.get("title") or resource.get("title") or resource.get("name") or resource["id"]).strip()
            project = ops.project_from_resource(ws, resource, title, author(request),
                                                namespace=cfg.namespace(form.get("namespace")),
                                                classification=cfg.classification(form.get("classification")).key,
                                                base_codes=await _translation_base(resource))
        except KeyError:
            return render(request, "error.html", status=404, message="Filen finns inte.")
        except (ops.OpError, ValueError) as exc:
            messages = exc.messages if isinstance(exc, ops.OpError) else [str(exc)]
            return redirect(f"/forslag/fil/{uid}", "; ".join(messages))
        return redirect(f"/p/{project.id}", "Utkastet är taget in som projekt. Granska det innan release.")

    async def tx_proposal_take(request: Request):
        form = await request.form()
        rtype, rid = form.get("type") or "", form.get("id") or ""
        cfg = settings()
        try:
            async with tx_client() as client:
                resource = await client.read(rtype, rid)
            if resource is None:
                raise ops.OpError(f"{rtype}/{rid} finns inte längre på terminologitjänsten.")
            title = (form.get("title") or resource.get("title") or resource.get("name") or rid).strip()
            base_codes = await _translation_base(resource)
            project = ops.project_from_resource(ws, resource, title, author(request),
                                                namespace=cfg.namespace(form.get("namespace")),
                                                classification=cfg.classification(form.get("classification")).key,
                                                base_codes=base_codes)
        except ops.OpError as exc:
            return redirect("/tx-forslag", "; ".join(exc.messages))
        return redirect(f"/p/{project.id}", "Förslaget är taget in som projekt. Granska det innan release.")

    async def code_search(request: Request):
        """Sök koder i projektets käll- eller målkodsystem (för kodfälten i mappningsprojekt)."""
        project, _ = load(request.path_params["pid"])
        if project is None or project.template.kind != "conceptmap":
            return JSONResponse([], status_code=404)
        spec = project.template.mapping
        system = spec.target_system if request.query_params.get("sida") != "kalla" else spec.source_system
        scope = spec.target_valueset if request.query_params.get("sida") != "kalla" else spec.source_valueset
        q = (request.query_params.get("q") or "").strip()
        try:
            async with tx_client() as client:
                if scope:
                    result = await client.expand(url=scope, filter=q or None, count=30)
                else:
                    implicit = {"resourceType": "ValueSet", "compose": {"include": [{"system": system}]}}
                    result = await client.expand(valueset=implicit, filter=q or None, count=30)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        hits = [{"code": c["code"], "display": c.get("display")} for c in result["contains"]]
        if q and not any(h["code"] == q for h in hits):  # exakt kod som inte matchar textfiltret
            try:
                async with tx_client() as client:
                    exact = await client.validate_code(q, system=system)
                if exact.get("result"):
                    hits.insert(0, {"code": q, "display": exact.get("display")})
            except Exception:  # noqa: BLE001 – sökningen fungerar ändå
                pass
        return JSONResponse(hits)

    async def log_page(request: Request):
        project, _ = load(request.path_params["pid"])
        if project is None:
            return not_found(request, "Projektet")
        return render(request, "log.html", project=project, log=ws.read_log(project.id, 1000))

    async def dev_user(request: Request):
        form = await request.form()
        user = form.get("user") or ""
        back = request.headers.get("referer") or "/"
        response = RedirectResponse(back, status_code=303)
        if auth_cfg.mode == "demo" and user in dev_users():
            response.set_cookie("dev_user", user, httponly=True, samesite="lax")
        return response

    async def health(request: Request):
        return JSONResponse({"status": "ok", "projects": len(ws.projects()), "fhirVersion": fhir_version()})

    routes = [
        Route("/", need("läsare", index)),
        Route("/health", health),
        Route("/dev/user", dev_user, methods=["POST"]),
        Route("/auth/login", login),
        Route("/auth/callback", callback),
        Route("/auth/logout", logout),
        Route("/auth/utloggad", logged_out),
        Route("/anvandare", need("administratör", users_page), methods=["GET"]),
        Route("/anvandare", need("administratör", users_save), methods=["POST"]),
        Route("/settings", need("administratör", settings_page)),
        Route("/import", need("redaktör", import_form), methods=["GET"]),
        Route("/import", need("redaktör", import_upload), methods=["POST"]),
        Route("/import/{uid}", need("redaktör", import_plan)),
        Route("/import/{uid}/create/{i:int}", need("redaktör", import_create), methods=["POST"]),
        Route("/mappning/ny", need("redaktör", mapping_new), methods=["GET"]),
        Route("/mappning/ny", need("redaktör", mapping_create), methods=["POST"]),
        Route("/oversattning/ny", need("redaktör", translation_new), methods=["GET"]),
        Route("/oversattning/ny", need("redaktör", translation_create), methods=["POST"]),
        Route("/p/{pid}/hamta-koder", need("redaktör", fetch_new_codes), methods=["POST"]),
        Route("/tx-forslag", need("läsare", tx_proposals), methods=["GET"]),
        Route("/tx-forslag/ta-in", need("redaktör", tx_proposal_take), methods=["POST"]),
        Route("/forslag/fil", need("redaktör", proposal_file_upload), methods=["POST"]),
        Route("/forslag/fil/{uid}", need("redaktör", proposal_file_page), methods=["GET"]),
        Route("/forslag/fil/{uid}/ta-in", need("redaktör", proposal_file_take), methods=["POST"]),
        Route("/p/{pid}/sok", need("läsare", code_search)),
        Route("/p/{pid}", need("läsare", project_page)),
        Route("/p/{pid}/meta", need("redaktör", project_meta), methods=["POST"]),
        Route("/p/{pid}/oids", need("redaktör", project_oids), methods=["POST"]),
        Route("/p/{pid}/questions/{i:int}", need("redaktör", toggle_question), methods=["POST"]),
        Route("/p/{pid}/proposals", need("läsare", proposals_page)),
        Route("/p/{pid}/proposals/{id}/{decision:str}", need("granskare", proposal_review), methods=["POST"]),
        Route("/p/{pid}/columns", need("läsare", columns_page), methods=["GET"]),
        Route("/p/{pid}/columns/new", need("redaktör", column_add), methods=["POST"]),
        Route("/p/{pid}/columns/{i:int}", need("redaktör", column_save), methods=["POST"]),
        Route("/p/{pid}/releases", need("läsare", releases_page), methods=["GET"]),
        Route("/p/{pid}/releases", need("redaktör", release_create), methods=["POST"]),
        Route("/p/{pid}/releases/{version}", need("läsare", release_page)),
        Route("/p/{pid}/releases/{version}/approve", need("granskare", release_approve), methods=["POST"]),
        Route("/p/{pid}/releases/{version}/publish", need("publicerare", release_publish), methods=["POST"]),
        Route("/p/{pid}/releases/{version}/bundle.json", need("läsare", release_bundle)),
        Route("/p/{pid}/releases/{version}/validate", need("redaktör", release_hl7), methods=["POST"]),
        Route("/p/{pid}/releases/{version}/package.tgz", need("läsare", release_tgz)),
        Route("/p/{pid}/preview.json", need("läsare", preview_bundle)),
        Route("/p/{pid}/template.json", need("läsare", template_json)),
        Route("/p/{pid}/check", need("redaktör", check_page), methods=["POST"]),
        Route("/p/{pid}/log", need("läsare", log_page)),
        Route("/p/{pid}/s/{sheet}", need("läsare", sheet_page)),
        Route("/p/{pid}/s/{sheet}/ai", need("redaktör", ai_review), methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/new", need("läsare", concept_form), methods=["GET"]),
        Route("/p/{pid}/s/{sheet}/new", need("redaktör", concept_save), methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}", need("läsare", concept_form), methods=["GET"]),
        Route("/p/{pid}/s/{sheet}/c/{code}", need("redaktör", concept_save), methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}/retire", need("redaktör", concept_retire), methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}/delete", need("redaktör", concept_delete), methods=["POST"]),
        Route("/p/{pid}/s/{sheet}/c/{code}/replace", need("redaktör", concept_replace), methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    middleware = [Middleware(SecurityMiddleware, allowed_origin=auth_cfg.oidc.public_url
                             if auth_cfg.oidc and auth_cfg.oidc.public_url else None)]
    if auth_cfg.mode == "oidc":
        public = auth_cfg.oidc.public_url or ""
        middleware.append(Middleware(SessionMiddleware, secret_key=os.environ["TERM_SESSION_SECRET"],
                                     session_cookie="term_session", max_age=auth_cfg.session_minutes * 60,
                                     same_site="lax", https_only=public.startswith("https://")))
    return Starlette(routes=routes, middleware=middleware)


def run(workspace: str, host: str = "127.0.0.1", port: int = 8000, base_url: str = "http://example.org/fhir",
        registry_path: str | None = None) -> None:
    import uvicorn
    app = create_app(workspace, registry_path, base_url)
    mode = Settings.load(Path(workspace)).auth.mode
    print(f"Inloggningsläge: {mode}")
    for warning in auth.check_config(Settings.load(Path(workspace)).auth):
        print(f"VARNING: {warning}")
    uvicorn.run(app, host=host, port=port, proxy_headers=False)  # klientadressen ska vara proxyns egen
