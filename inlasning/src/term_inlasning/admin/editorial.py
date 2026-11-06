"""Den redaktionella kedjan: förslag -> granskning -> införande -> release.

Varje ändring kan gå som ett *förslag* som en annan person granskar:

* **AI-förslag** går alltid den vägen. Claude läser ett blad och föreslår
  rättelser (stavning, saknade definitioner, synonymer, översättningar,
  misstänkta dubbletter). Varje förslag sparas med proveniens: modell,
  tidpunkt, ett kontrollvärde för underlaget och motivering.
* **Manuella ändringar** går samma väg om arbetsytans inställning
  ``editorial.review_manual_edits`` är på. Annars införs de direkt och loggas.

En granskare godkänner eller avvisar. Ett godkänt förslag införs bara om
raden fortfarande har de värden förslaget utgick från; annars blir förslaget
*inaktuellt* och måste göras om. Godkända förslag följer med i releasens
paket som FHIR Provenance.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..importer.mapping import ColumnRole
from . import ops
from .settings import Settings
from .store import Project, Proposal, SheetData, Workspace, now

AI_MODEL = "claude-opus-5-5"


def _new_id() -> str:
    return secrets.token_hex(4)


def _editable_columns(project: Project) -> dict[str, Any]:
    tpl = project.template
    return {c.source_column: c for c in tpl.columns
            if c.role not in (ColumnRole.CODE, ColumnRole.DERIVED, ColumnRole.IGNORE)
            and (c.ui is None or (c.ui.visible and c.ui.editable))}


# -- skapa förslag -----------------------------------------------------------------------


def propose_edit(ws: Workspace, project: Project, data: dict[str, SheetData], sheet: str, code: str | None,
                 form: dict, author: str, rationale: str = "") -> Proposal:
    """Gör ett förslag av ett formulär (validerat som om det skulle sparas direkt)."""
    tpl = project.template
    is_new = code is None
    original = ops.find_row(data[sheet], tpl, code) if code else {h: None for h in data[sheet].headers}
    if original is None:
        raise ops.OpError(f"Begreppet {code} finns inte.")
    trial = dict(original)
    # model_construct: utan validering, så att raderna förblir samma objekt (identitet används vid dubblettkontroll)
    staging = SheetData.model_construct(headers=data[sheet].headers,
                                        rows=[trial if r is original else r for r in data[sheet].rows])
    changes = ops.update_row(tpl, staging, trial, form, is_new=is_new)
    if not changes:
        raise ops.OpError("Inga ändringar att föreslå.")
    proposal = Proposal(id=_new_id(), sheet=sheet, code=None if is_new else code,
                        action="create" if is_new else "update",
                        changes={k: v[1] for k, v in changes.items()}, before={k: v[0] for k, v in changes.items()},
                        author=author, rationale=rationale)
    _append(ws, project, proposal, author)
    return proposal


def propose_retire(ws: Workspace, project: Project, sheet: str, code: str, author: str, replaced_by: str | None,
                   relation: str, comment: str) -> Proposal:
    proposal = Proposal(id=_new_id(), sheet=sheet, code=code, action="retire", author=author, rationale=comment,
                        extra={"replaced_by": replaced_by or None, "relation": relation, "comment": comment})
    _append(ws, project, proposal, author)
    return proposal


def _append(ws: Workspace, project: Project, proposal: Proposal, author: str) -> None:
    items = ws.proposals(project.id)
    items.append(proposal)
    ws.save_proposals(project.id, items)
    ws.log(project.id, author, f"Förslag {proposal.id} ({proposal.origin}): {proposal.action} "
                               f"{proposal.sheet}/{proposal.code or 'nytt'}",
           {"ändringar": proposal.changes or proposal.extra})


# -- granskning ------------------------------------------------------------------------------


def review(ws: Workspace, project: Project, data: dict[str, SheetData], proposal_id: str, reviewer: str,
           accept: bool, comment: str = "", settings: Settings | None = None) -> Proposal:
    settings = settings or Settings()
    items = ws.proposals(project.id)
    proposal = next((p for p in items if p.id == proposal_id), None)
    if proposal is None:
        raise ops.OpError("Förslaget finns inte.")
    if proposal.status != "öppen":
        raise ops.OpError(f"Förslaget är redan {proposal.status}.")
    if reviewer in ("", "okänd"):
        raise ops.OpError("Granskning kräver inloggning: granskarens namn saknas (X-Remote-User).")
    if proposal.origin == "manuell" and reviewer == proposal.author:
        raise ops.OpError("Ett förslag kan inte granskas av den som skrev det.")

    proposal.reviewer, proposal.reviewed, proposal.review_comment = reviewer, now(), comment.strip()
    if not accept:
        proposal.status = "avvisad"
    else:
        try:
            _apply(project, data, proposal)
            proposal.status = "godkänd"
        except _Stale as exc:
            proposal.status = "inaktuell"
            proposal.review_comment = (proposal.review_comment + " " if proposal.review_comment else "") + str(exc)
    ws.save_proposals(project.id, items)
    ws.save(project, data if proposal.status == "godkänd" else None, author=reviewer,
            action=f"Förslag {proposal.id} {proposal.status}", detail={"kommentar": proposal.review_comment}
            if proposal.review_comment else None)
    return proposal


class _Stale(Exception):
    pass


def _apply(project: Project, data: dict[str, SheetData], p: Proposal) -> None:
    tpl = project.template
    sheet = data.get(p.sheet)
    if sheet is None:
        raise _Stale("Bladet finns inte längre.")
    code_col = ops.code_column(tpl)
    if p.action == "create":
        code = p.changes.get(code_col)
        if ops.find_row(sheet, tpl, str(code)):
            raise _Stale(f"Koden {code} har lagts till sedan förslaget skrevs.")
        sheet.rows.append({h: None for h in sheet.headers} | p.changes)
        return
    row = ops.find_row(sheet, tpl, p.code)
    if row is None:
        raise _Stale(f"Begreppet {p.code} finns inte längre.")
    if p.action == "retire":
        project.template = ops.retire(tpl, data, p.sheet, row, p.extra.get("replaced_by"),
                                      p.extra.get("relation") or "equivalent", p.extra.get("comment") or "")
        return
    for col, before in p.before.items():
        current = row.get(col)
        if isinstance(before, bool) or isinstance(current, bool):
            same = ops.is_true(current) == ops.is_true(before)
        else:
            same = (current or None) == (before or None)
        if not same:
            raise _Stale(f"'{col}' har ändrats sedan förslaget skrevs (nu: {current!r}).")
    for col, value in p.changes.items():
        if col not in sheet.headers:
            sheet.headers.append(col)
        row[col] = value


# -- AI-förslag ------------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Du är redaktör för ett svenskt kodverk som förvaltas i FHIR. Du får kodverkets
kolumner (med vad varje kolumn står för) och dess rader. Föreslå konkreta
förbättringar av enskilda celler:

- stavfel och inkonsekvent skrivsätt i benämningar
- definitioner som saknas eller är otydliga (bara där underlaget räcker)
- synonymer eller översättningar som saknas i kolumner avsedda för dem
- misstänkta dubbletter (samma betydelse, olika koder) – föreslå inget värde,
  beskriv i rationale och sätt kind=dubblett

Ändra aldrig koder. Föreslå bara värden för de kolumner som listas som
redigerbara, och bara där du är rimligt säker. Hellre få och bra förslag än
många. Motivera varje förslag kort på svenska."""


class AiSuggestion(BaseModel):
    code: str
    column: str
    value: str | None = Field(None, description="Föreslaget nytt värde; tomt för dubbletter")
    kind: Literal["stavning", "definition", "synonym", "översättning", "dubblett", "övrigt"]
    rationale: str


class AiSuggestions(BaseModel):
    suggestions: list[AiSuggestion]


def ai_review(ws: Workspace, project: Project, data: dict[str, SheetData], sheet: str, author: str,
              client=None, max_rows: int = 200) -> list[Proposal]:
    """Låt Claude föreslå förbättringar av ett blad. Förslagen läggs i kedjan, oinförda."""
    tpl = project.template
    editable = _editable_columns(project)
    code_col = ops.code_column(tpl)
    rows = data[sheet].rows[:max_rows]
    columns = [{"kolumn": c.source_column, "står för": ops.ROLE_LABELS[c.role],
                "egenskap": c.property_code, "språk": c.designation.language if c.designation else None,
                "redigerbar": c.source_column in editable} for c in tpl.columns
               if c.role not in (ColumnRole.IGNORE, ColumnRole.DERIVED)]
    payload = {"kodverk": sheet, "språk": tpl.codesystem.language, "kolumner": columns,
               "rader": [{k: v for k, v in r.items() if not k.startswith("_") and v not in (None, "")}
                         for r in rows]}
    user = json.dumps(payload, ensure_ascii=False, default=str)
    digest = hashlib.sha256((SYSTEM_PROMPT + user).encode("utf-8")).hexdigest()

    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=AI_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "medium"},
        output_format=AiSuggestions,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise ops.OpError("AI-granskningen gav inget svar.")
    served_by = getattr(response, "model", None) or AI_MODEL
    usage = getattr(response, "usage", None)
    by_code = {str(r.get(code_col)): r for r in rows}
    proposals = []
    for s in response.parsed_output.suggestions:
        row = by_code.get(s.code)
        if row is None or (s.kind != "dubblett" and s.column not in editable):
            continue
        if s.kind != "dubblett" and (s.value or None) == (row.get(s.column) or None):
            continue
        provenance = {"model": served_by, "request_id": getattr(response, "id", None), "created": now(),
                      "input_sha256": digest, "kind": s.kind,
                      "usage": {"input_tokens": getattr(usage, "input_tokens", None),
                                "output_tokens": getattr(usage, "output_tokens", None)} if usage else None}
        changes = {} if s.kind == "dubblett" else {s.column: s.value}
        proposals.append(Proposal(id=_new_id(), sheet=sheet, code=s.code, action="update", changes=changes,
                                  before={s.column: row.get(s.column)} if changes else {},
                                  author=author, origin="ai", rationale=f"[{s.kind}] {s.rationale}",
                                  provenance=provenance))
    items = ws.proposals(project.id) + proposals
    ws.save_proposals(project.id, items)
    ws.log(project.id, author, f"AI-granskning av {sheet}: {len(proposals)} förslag",
           {"modell": served_by, "underlag_sha256": digest[:16]})
    return proposals
