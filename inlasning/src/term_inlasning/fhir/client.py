"""Tunn asynkron klient för FHIR R4-terminologioperationer.

Klienten använder bara standardoperationer ($lookup, $validate-code, $expand,
$subsumes, $translate och sökningar) så att den fungerar mot vilken
FHIR-terminologitjänst som helst.
"""

from __future__ import annotations

from typing import Any

import httpx2

from ..config import ServerConfig
from .params import parameters_to_dict

FHIR_JSON = "application/fhir+json"


class TerminologyError(RuntimeError):
    """Fel från terminologitjänsten, med OperationOutcome-text när den finns."""


class TerminologyClient:
    def __init__(self, config: ServerConfig, transport: httpx2.AsyncBaseTransport | None = None):
        headers = {"Accept": FHIR_JSON, "Accept-Language": config.language}
        if config.token:
            headers["Authorization"] = f"Bearer {config.token}"
        self.config = config
        self._http = httpx2.AsyncClient(
            base_url=config.base_url + "/",
            headers=headers,
            timeout=config.timeout,
            follow_redirects=True,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "TerminologyClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- lågnivå ---------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        params = kwargs.pop("params", None)
        if params is not None:
            # Ta bort tomma värden men behåll upprepade parametrar (listor).
            params = [(k, v) for k, v in _flatten(params) if v is not None and v != ""]
        if "json" in kwargs:
            kwargs.setdefault("headers", {})["Content-Type"] = FHIR_JSON
        response = await self._http.request(method, path, params=params, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            raise TerminologyError(f"{response.status_code} {path}: {_outcome_text(body) or response.text[:300]}")
        return body

    # -- metadata och sökning -------------------------------------------

    async def capabilities(self) -> dict[str, Any]:
        cs = await self._request("GET", "metadata", params={"_summary": "true"})
        return {
            "software": cs.get("software", {}),
            "fhirVersion": cs.get("fhirVersion"),
            "implementation": cs.get("implementation", {}),
        }

    async def search(self, resource_type: str, count: int = 20, **criteria: Any) -> list[dict[str, Any]]:
        params = {"_count": count, "_summary": "true", **criteria}
        bundle = await self._request("GET", resource_type, params=params)
        return [entry["resource"] for entry in bundle.get("entry", []) if "resource" in entry]

    async def read_canonical(self, resource_type: str, url: str, version: str | None = None) -> dict[str, Any] | None:
        hits = await self.search(resource_type, count=1, url=url, version=version, _summary=None)
        return hits[0] if hits else None

    # -- operationer ------------------------------------------------------

    async def lookup(
        self, system: str, code: str, version: str | None = None, properties: list[str] | None = None
    ) -> dict[str, Any]:
        params = {"system": system, "code": code, "version": version, "property": properties or None,
                  "displayLanguage": self.config.language}
        return parameters_to_dict(await self._request("GET", "CodeSystem/$lookup", params=params))

    async def validate_code(
        self,
        code: str,
        system: str | None = None,
        valueset: str | None = None,
        display: str | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        if valueset:
            path = "ValueSet/$validate-code"
            params = {"url": valueset, "system": system, "code": code, "display": display, "systemVersion": version}
        else:
            path = "CodeSystem/$validate-code"
            params = {"url": system, "code": code, "display": display, "version": version}
        params["displayLanguage"] = self.config.language
        return parameters_to_dict(await self._request("GET", path, params=params))

    async def expand(
        self,
        url: str | None = None,
        valueset: dict[str, Any] | None = None,
        filter: str | None = None,
        count: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Expandera ett ValueSet, antingen via canonical URL eller inline-definition."""
        if valueset is not None:
            body = {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "valueSet", "resource": valueset},
                    {"name": "count", "valueInteger": count},
                    {"name": "offset", "valueInteger": offset},
                    {"name": "displayLanguage", "valueCode": self.config.language},
                ]
                + ([{"name": "filter", "valueString": filter}] if filter else []),
            }
            vs = await self._request("POST", "ValueSet/$expand", json=body)
        else:
            params = {"url": url, "filter": filter, "count": count, "offset": offset,
                      "displayLanguage": self.config.language}
            vs = await self._request("GET", "ValueSet/$expand", params=params)
        expansion = vs.get("expansion", {})
        return {
            "url": vs.get("url", url),
            "total": expansion.get("total"),
            "offset": expansion.get("offset", offset),
            "contains": [_slim_concept(c) for c in expansion.get("contains", [])],
        }

    async def subsumes(self, system: str, code_a: str, code_b: str, version: str | None = None) -> str:
        params = {"system": system, "codeA": code_a, "codeB": code_b, "version": version}
        result = parameters_to_dict(await self._request("GET", "CodeSystem/$subsumes", params=params))
        return result.get("outcome", "unknown")

    async def hierarchy(self, system: str, code: str, version: str | None = None) -> dict[str, Any]:
        """Föräldrar och barn för ett begrepp.

        Använder ``$lookup`` med egenskaperna ``parent``/``child``. Om servern
        inte returnerar barn görs en fallback via ``$expand`` med filtret
        ``is-a`` (som då ger alla ättlingar, inte bara direkta barn).
        """
        info = await self.lookup(system, code, version, properties=["parent", "child"])
        parents, children = [], []
        for prop in info.get("property", []):
            if not isinstance(prop, dict):
                continue
            target = prop.get("value")
            if isinstance(target, dict):
                target = target.get("code")
            if prop.get("code") == "parent":
                parents.append(target)
            elif prop.get("code") == "child":
                children.append(target)
        result: dict[str, Any] = {"code": code, "display": info.get("display"), "parents": parents, "children": children}
        if not children:
            vs = {
                "resourceType": "ValueSet",
                "compose": {"include": [{"system": system, "version": version,
                                         "filter": [{"property": "concept", "op": "is-a", "value": code}]}]},
            }
            try:
                expansion = await self.expand(valueset=_drop_none(vs), count=100)
                result["descendants"] = [c for c in expansion["contains"] if c.get("code") != code]
            except TerminologyError as exc:
                result["descendants_error"] = str(exc)
        return result

    async def translate(
        self,
        code: str,
        system: str,
        conceptmap: str | None = None,
        target_system: str | None = None,
    ) -> dict[str, Any]:
        params = {"url": conceptmap, "system": system, "code": code, "targetsystem": target_system}
        return parameters_to_dict(await self._request("GET", "ConceptMap/$translate", params=params))

    async def put_resource(self, resource: dict[str, Any]) -> dict[str, Any]:
        """Skapa/uppdatera en resurs (används vid publicering av releaser)."""
        rtype, rid = resource["resourceType"], resource.get("id")
        if rid:
            return await self._request("PUT", f"{rtype}/{rid}", json=resource)
        return await self._request("POST", rtype, json=resource)


def _flatten(params: dict[str, Any]):
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                yield key, item
        else:
            yield key, value


def _slim_concept(concept: dict[str, Any]) -> dict[str, Any]:
    keep = ("system", "version", "code", "display", "abstract", "inactive")
    slim = {k: concept[k] for k in keep if k in concept}
    if "contains" in concept:
        slim["contains"] = [_slim_concept(c) for c in concept["contains"]]
    return slim


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


def _outcome_text(body: dict[str, Any]) -> str:
    if body.get("resourceType") != "OperationOutcome":
        return ""
    return "; ".join(
        issue.get("diagnostics") or issue.get("details", {}).get("text", "") for issue in body.get("issue", [])
    )
