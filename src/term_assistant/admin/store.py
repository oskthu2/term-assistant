"""Lagring av projekt på disk, i JSON-filer som går att versionshantera.

    <arbetsyta>/
      uploads/<id>/<fil>               uppladdade källfiler + cachad importplan
      projects/<id>/project.json       metadata, mall, öppna frågor
      projects/<id>/data.json          raderna per blad (arbetskopian)
      projects/<id>/releases/<v>.json  releasekandidater och releaser
      projects/<id>/log.jsonl          ändringslogg (vem, när, vad)

Arbetskopian lagras som *rader*, precis som i kalkylarket. Mallen översätter
raderna till FHIR först när en releasekandidat skapas. Därför kräver det
ingen datamigrering att byta vad en kolumn står för.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..importer.mapping import ImportTemplate


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SheetData(BaseModel):
    headers: list[str]
    rows: list[dict[str, Any]] = []


class Question(BaseModel):
    text: str
    resolved: bool = False
    note: str = ""


class Flag(BaseModel):
    """En kod i ett urval som behöver hanteras (inaktiv, okänd, ersatt …)."""

    status: str
    message: str = ""
    server_display: str | None = None
    suggestions: list[dict[str, Any]] = []   # [{code, display, source}]
    checked: str = Field(default_factory=lambda: now())


class Proposal(BaseModel):
    """Ett steg i den redaktionella kedjan: förslag -> granskning -> införd/avvisad."""

    id: str
    sheet: str
    code: str | None = None
    action: Literal["update", "create", "retire", "replace-code"] = "update"
    changes: dict[str, Any] = {}
    before: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    author: str
    origin: Literal["manuell", "ai"] = "manuell"
    rationale: str = ""
    provenance: dict[str, Any] | None = None
    status: Literal["öppen", "godkänd", "avvisad", "inaktuell"] = "öppen"
    created: str = Field(default_factory=lambda: now())
    reviewer: str | None = None
    reviewed: str | None = None
    review_comment: str = ""


class Project(BaseModel):
    id: str
    title: str
    created: str = Field(default_factory=now)
    updated: str = Field(default_factory=now)
    source_file: str | None = None
    template: ImportTemplate
    questions: list[Question] = []
    flags: dict[str, dict[str, Flag]] = Field(default_factory=dict, description="Blad -> kod -> flagga")


class Release(BaseModel):
    version: str
    status: Literal["rc", "release"]
    created: str = Field(default_factory=now)
    author: str = "okänd"
    notes: str = ""
    resources: list[dict[str, Any]]
    diffs: list[dict[str, Any]] = []
    warnings: list[str] = []
    based_on: str | None = None
    codes: dict[str, list[str]] = Field(default_factory=dict, description="Blad -> koder i releasen")
    published: str | None = None
    hl7_validation: dict[str, Any] | None = None
    publish_log: list[str] = []


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(str.maketrans("åäöé", "aaoe"))).strip("-")
    return s[:40] or "projekt"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "projects").mkdir(parents=True, exist_ok=True)
        (self.root / "uploads").mkdir(parents=True, exist_ok=True)

    def _pdir(self, pid: str) -> Path:
        if not _SAFE.match(pid):
            raise KeyError(pid)
        return self.root / "projects" / pid

    # -- uppladdningar ----------------------------------------------------------

    def save_upload(self, filename: str, content: bytes) -> tuple[str, Path]:
        uid = secrets.token_hex(6)
        name = re.sub(r"[^\w.\-]+", "_", Path(filename).name) or "fil"
        path = self.root / "uploads" / uid / name
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        return uid, path

    def upload_path(self, uid: str) -> Path:
        if not _SAFE.match(uid):
            raise KeyError(uid)
        folder = self.root / "uploads" / uid
        files = [p for p in folder.iterdir() if not p.name.startswith(".")] if folder.exists() else []
        if not files:
            raise KeyError(uid)
        return files[0]

    def upload_cache(self, uid: str) -> Path:
        return self.upload_path(uid).parent / ".plan.json"

    # -- projekt ------------------------------------------------------------------

    def projects(self) -> list[Project]:
        out = []
        for d in sorted((self.root / "projects").iterdir()):
            if (d / "project.json").exists():
                out.append(Project.model_validate_json((d / "project.json").read_text(encoding="utf-8")))
        return sorted(out, key=lambda p: p.updated, reverse=True)

    def new_id(self, title: str) -> str:
        base = slug(title)
        pid, n = base, 2
        while self._pdir(pid).exists():
            pid, n = f"{base}-{n}", n + 1
        return pid

    def get(self, pid: str) -> Project:
        path = self._pdir(pid) / "project.json"
        if not path.exists():
            raise KeyError(pid)
        return Project.model_validate_json(path.read_text(encoding="utf-8"))

    def data(self, pid: str) -> dict[str, SheetData]:
        raw = json.loads((self._pdir(pid) / "data.json").read_text(encoding="utf-8"))
        return {name: SheetData.model_validate(s) for name, s in raw.items()}

    def save(self, project: Project, data: dict[str, SheetData] | None = None, *,
             author: str = "okänd", action: str = "", detail: dict | None = None) -> None:
        project.updated = now()
        d = self._pdir(project.id)
        _atomic_write(d / "project.json", project.model_dump_json(indent=2, exclude_none=True))
        if data is not None:
            _atomic_write(d / "data.json", json.dumps({k: v.model_dump() for k, v in data.items()},
                                                      ensure_ascii=False, indent=1, default=str))
        if action:
            self.log(project.id, author, action, detail)

    # -- redaktionella förslag ------------------------------------------------------

    def proposals(self, pid: str) -> list[Proposal]:
        path = self._pdir(pid) / "proposals.json"
        if not path.exists():
            return []
        return [Proposal.model_validate(p) for p in json.loads(path.read_text(encoding="utf-8"))]

    def save_proposals(self, pid: str, proposals: list[Proposal]) -> None:
        _atomic_write(self._pdir(pid) / "proposals.json",
                      json.dumps([p.model_dump(exclude_none=True) for p in proposals], ensure_ascii=False,
                                 indent=1, default=str))

    # -- logg ---------------------------------------------------------------------

    def log(self, pid: str, author: str, action: str, detail: dict | None = None) -> None:
        entry = {"time": now(), "author": author, "action": action, **({"detail": detail} if detail else {})}
        with open(self._pdir(pid) / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def read_log(self, pid: str, limit: int = 200) -> list[dict]:
        path = self._pdir(pid) / "log.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in reversed(lines[-limit:])]

    # -- releaser -----------------------------------------------------------------

    def releases(self, pid: str) -> list[Release]:
        folder = self._pdir(pid) / "releases"
        if not folder.exists():
            return []
        items = [Release.model_validate_json(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")]
        return sorted(items, key=lambda r: r.created, reverse=True)

    def release(self, pid: str, version: str) -> Release:
        if not _SAFE.match(version):
            raise KeyError(version)
        path = self._pdir(pid) / "releases" / f"{version}.json"
        if not path.exists():
            raise KeyError(version)
        return Release.model_validate_json(path.read_text(encoding="utf-8"))

    def save_release(self, pid: str, rel: Release) -> None:
        _atomic_write(self._pdir(pid) / "releases" / f"{rel.version}.json",
                      rel.model_dump_json(indent=1, exclude_none=True))

    def latest_release(self, pid: str) -> Release | None:
        return next((r for r in self.releases(pid) if r.status == "release"), None)
