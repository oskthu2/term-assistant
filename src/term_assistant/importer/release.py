"""Releasehantering: releasekandidater, releaser och diff mellan versioner.

Livscykeln speglar CodeSystem.status:

    draft (arbetskopia) -> draft + version 'X.Y.Z-rc.N' (releasekandidat)
                        -> active + version 'X.Y.Z' (release)
"""

from __future__ import annotations

import copy
import re
from datetime import date
from typing import Any

_RC = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?:-rc\.(?P<n>\d+))?$")


def bump(version: str, part: str = "minor") -> str:
    m = _RC.match(version)
    if not m:
        raise ValueError(f"Versionen '{version}' följer inte X.Y.Z")
    major, minor, patch = (int(x) for x in m["base"].split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def release_candidate(codesystem: dict[str, Any], base_version: str, previous_rc: str | None = None) -> dict[str, Any]:
    """Skapa en releasekandidat, t.ex. 1.2.0-rc.1, rc.2 ..."""
    n = 1
    if previous_rc and (m := _RC.match(previous_rc)) and m["base"] == base_version and m["n"]:
        n = int(m["n"]) + 1
    rc = copy.deepcopy(codesystem)
    rc.update(version=f"{base_version}-rc.{n}", status="draft", date=date.today().isoformat())
    return rc


def release(candidate: dict[str, Any]) -> dict[str, Any]:
    """Gör en releasekandidat till release (status active, version utan -rc)."""
    m = _RC.match(candidate.get("version", ""))
    if not m:
        raise ValueError("Releasekandidaten saknar giltig version")
    released = copy.deepcopy(candidate)
    released.update(version=m["base"], status="active", date=date.today().isoformat(), experimental=False)
    return released


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Jämför två versioner av ett kodverk på begreppsnivå."""
    old_c = {c["code"]: c for c in _flatten(old.get("concept", []))}
    new_c = {c["code"]: c for c in _flatten(new.get("concept", []))}
    changed = []
    for code in sorted(old_c.keys() & new_c.keys()):
        fields = [f for f in ("display", "definition", "designation", "property")
                  if _norm(old_c[code].get(f)) != _norm(new_c[code].get(f))]
        if fields:
            changed.append({"code": code, "fields": fields})
    old_props = {p["code"] for p in old.get("property", [])}
    new_props = {p["code"] for p in new.get("property", [])}
    return {
        "from": old.get("version"),
        "to": new.get("version"),
        "added": sorted(new_c.keys() - old_c.keys()),
        "removed": sorted(old_c.keys() - new_c.keys()),
        "changed": changed,
        "properties_added": sorted(new_props - old_props),
        "properties_removed": sorted(old_props - new_props),
    }


def _flatten(concepts: list[dict[str, Any]]):
    for c in concepts:
        yield c
        yield from _flatten(c.get("concept", []))


def _norm(value: Any) -> Any:
    if isinstance(value, list):
        return sorted((repr(_norm(v)) for v in value))
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in sorted(value.items())}
    return value


def check_policy(change: dict[str, Any], policy) -> list[str]:
    """Kontrollera en diff mot mallens releasepolicy. Returnerar spärrande fel."""
    errors = []
    if change["removed"] and not policy.allow_code_removal:
        errors.append(f"Koder får inte tas bort enligt kodverkets regler ({policy.rationale or 'policy'}), "
                      f"men {', '.join(change['removed'][:10])} saknas i den nya versionen.")
    if not policy.allow_display_change:
        changed = [c["code"] for c in change["changed"] if "display" in c["fields"]]
        if changed:
            errors.append(f"Visningsnamn får inte ändras, men har ändrats för {', '.join(changed[:10])}.")
    return errors
