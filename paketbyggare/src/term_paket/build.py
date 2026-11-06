"""Bygg ett versionerat paket – bara när innehållet har ändrats.

Versionen räknas fram genom att jämföra med föregående paket (semver):

==========  ==================================================================
major       en resurs har tagits bort (mottagare kan ha byggt på den)
minor       en resurs har tillkommit, eller en resurs har fått ny version
patch       en resurs har ändrats utan att dess version ändrats
oförändrat  inget nytt paket byggs
==========  ==================================================================

Jämförelsen nycklas på resurstyp + canonical URL. Serverns lagringsmetadata
(``meta.lastUpdated`` m.m.) räknas inte som ändring.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import PackageConfig
from .package import Package, assign_ids, manifest, resource_hash, write_package
from .sources import Collected


@dataclass
class Changes:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    new_version: list[str] = field(default_factory=list)   # "url: 1.0.0 -> 1.1.0"
    changed: list[str] = field(default_factory=list)

    @property
    def bump(self) -> str | None:
        if self.removed:
            return "major"
        if self.added or self.new_version:
            return "minor"
        if self.changed:
            return "patch"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"bump": self.bump, "added": self.added, "removed": self.removed, "newVersion": self.new_version,
                "changed": self.changed}


@dataclass
class BuildResult:
    changed: bool
    version: str
    previous: str | None
    changes: Changes
    file: Path | None = None
    notes_file: Path | None = None
    package: Package | None = None


def _key(r: dict) -> str:
    return f"{r['resourceType']} {r.get('url')}"


def compare(old: list[dict], new: list[dict]) -> Changes:
    old_by, new_by = {_key(r): r for r in old}, {_key(r): r for r in new}
    ch = Changes()
    ch.added = sorted(set(new_by) - set(old_by))
    ch.removed = sorted(set(old_by) - set(new_by))
    for key in sorted(set(old_by) & set(new_by)):
        a, b = old_by[key], new_by[key]
        if a.get("version") != b.get("version"):
            ch.new_version.append(f"{key}: {a.get('version')} -> {b.get('version')}")
        elif resource_hash(_without_id(a)) != resource_hash(_without_id(b)):
            ch.changed.append(key)
    return ch


def _without_id(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in ("id", "text")}


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split("-")[0].split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def build(config: PackageConfig, collected: Collected, out_dir: str | Path, previous: Package | None = None,
          force: bool = False, today: str | None = None) -> BuildResult:
    out_dir = Path(out_dir)
    resources = [dict(r) for r in collected.resources]
    assign_ids(resources)
    if config.classification:
        stricter = stricter_than(resources, config.classification)
        if stricter:
            raise ClassificationError(
                f"{len(stricter)} resurser har striktare informationsklass än paketets "
                f"({config.classification.get('code')}) och får inte spridas med det: " + "; ".join(stricter[:5])
                + ". Höj paketets classification eller ta bort källan.")
        _label(resources, config.classification)  # före jämförelsen, så att märkningen inte räknas som en ändring
    if previous is None:
        changes = Changes(added=sorted(_key(r) for r in resources))
        version, changed = config.first_version, True
    else:
        changes = compare(previous.resources, resources)
        part = changes.bump or ("patch" if force else None)
        changed = part is not None
        version = bump(previous.version, part) if part else previous.version
    if not changed:
        return BuildResult(False, version, previous.version if previous else None, changes)

    date = today or time.strftime("%Y%m%d%H%M%S", time.gmtime())
    notes = release_notes(config, version, previous.version if previous else None, changes, collected)
    pkg = Package(
        manifest(config.name, version, config.fhir_version, title=config.title, description=config.description,
                 date=date, author=config.author, classification=config.classification),
        resources,
        {"changes.json": {"version": version, "previous": previous.version if previous else None,
                          **changes.as_dict(), "sources": collected.sources},
         "CHANGES.md": notes},
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    data = write_package(pkg)
    target = out_dir / f"{config.name}-{version}.tgz"
    target.write_bytes(data)
    notes_file = out_dir / f"{config.name}-{version}.md"
    notes_file.write_text(notes, encoding="utf-8")
    _update_feed(out_dir, config, version, target, data, changes, date)
    return BuildResult(True, version, previous.version if previous else None, changes, target, notes_file, pkg)


CONFIDENTIALITY = "http://terminology.hl7.org/CodeSystem/v3-Confidentiality"
_RANK = {"U": 0, "L": 1, "M": 2, "N": 3, "R": 4, "V": 5}


class ClassificationError(ValueError):
    """En resurs är mer skyddsvärd än paketet den skulle spridas med."""


def stricter_than(resources: list[dict], classification: dict) -> list[str]:
    """Resurser vars v3-Confidentiality-märkning är striktare än paketets. Märkningen sänks aldrig tyst."""
    if classification.get("system") != CONFIDENTIALITY:
        return []
    limit = _RANK.get(classification.get("code"), 0)
    out = []
    for r in resources:
        for label in r.get("meta", {}).get("security", []):
            if label.get("system") == CONFIDENTIALITY and _RANK.get(label.get("code"), 0) > limit:
                out.append(f"{r['resourceType']} {r.get('url')} ({label.get('code')})")
    return out


def _label(resources: list[dict], classification: dict) -> None:
    """Informationsklassen i meta.security, så att den följer med när en resurs plockas ur paketet."""
    for r in resources:
        meta = r.setdefault("meta", {})
        labels = [s for s in meta.get("security", []) if s.get("system") != classification.get("system")]
        meta["security"] = labels + [classification]


def release_notes(config: PackageConfig, version: str, previous: str | None, changes: Changes,
                  collected: Collected) -> str:
    lines = [f"# {config.title} {version}", ""]
    lines.append(f"Föregående version: {previous}." if previous else "Första versionen.")
    if changes.bump:
        lines.append(f"Ändringsnivå: {changes.bump}.")
    lines.append("")
    for title, items in (("Tillagda", changes.added), ("Borttagna", changes.removed),
                         ("Ny version", changes.new_version), ("Ändrade", changes.changed)):
        if items:
            lines += [f"## {title}", *[f"- {i}" for i in items], ""]
    lines += ["## Källor", *[f"- {s}" for s in collected.sources], ""]
    skipped = [n for n in collected.notes if not n.startswith(("Hämtad", "Paket "))]
    if skipped:
        lines += ["## Anmärkningar", *[f"- {n}" for n in skipped], ""]
    return "\n".join(lines)


def _update_feed(out_dir: Path, config: PackageConfig, version: str, path: Path, data: bytes, changes: Changes,
                 date: str) -> None:
    """``<namn>.feed.json``: alla byggda versioner av paketet, senaste först."""
    import hashlib
    feed_path = out_dir / f"{config.name}.feed.json"
    try:
        feed = json.loads(feed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        feed = {"name": config.name, "title": config.title, "versions": []}
    feed["versions"] = [v for v in feed.get("versions", []) if v.get("version") != version]
    feed["versions"].insert(0, {"version": version, "date": date, "file": path.name,
                                "sha256": hashlib.sha256(data).hexdigest(), "bump": changes.bump,
                                "added": len(changes.added), "removed": len(changes.removed),
                                "changed": len(changes.changed) + len(changes.new_version)})
    feed_path.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
