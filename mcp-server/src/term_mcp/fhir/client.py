"""Tunn asynkron klient för FHIR R4-terminologioperationer.

Klienten använder bara standardoperationer ($lookup, $validate-code, $expand,
$subsumes, $translate och sökningar) så att den fungerar mot vilken
FHIR-terminologitjänst som helst.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx2

from ..config import SNOMED, ServerConfig
from .params import parameters_to_dict

FHIR_JSON = "application/fhir+json"


class TerminologyError(RuntimeError):
    """Fel från terminologitjänsten, med OperationOutcome-text när den finns."""


class ClientCredentials:
    """OAuth2 client credentials: hämtar en åtkomsttoken och förnyar den innan den går ut (eller vid 401)."""

    def __init__(self, config: ServerConfig, transport: httpx2.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def token(self, refresh: bool = False) -> str:
        async with self._lock:
            if refresh or self._token is None or time.monotonic() > self._expires - 60:
                data = {"grant_type": "client_credentials", "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret, "scope": self.config.scope}
                async with httpx2.AsyncClient(timeout=self.config.timeout, transport=self.transport) as http:
                    response = await http.post(self.config.token_url, headers={"Accept": "application/json"},
                                               data={k: v for k, v in data.items() if v})
                if response.status_code != 200:
                    raise TerminologyError(f"Ingen åtkomsttoken från {self.config.token_url} "
                                           f"({response.status_code}: {response.text[:200]})")
                body = response.json()
                self._token = body["access_token"]
                self._expires = time.monotonic() + float(body.get("expires_in") or 300)
            return self._token


class TerminologyClient:
    def __init__(self, config: ServerConfig, transport: httpx2.AsyncBaseTransport | None = None):
        headers = {"Accept": FHIR_JSON, "Accept-Language": config.language}
        if config.token:
            headers["Authorization"] = f"Bearer {config.token}"
        self.config = config
        self._oauth = ClientCredentials(config, transport) if config.token_url and config.client_id else None
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
        response = await self._send(method, path, params=params, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            raise TerminologyError(f"{response.status_code} {path}: {_outcome_text(body) or response.text[:300]}")
        return body

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        if self._oauth is None:
            return await self._http.request(method, path, **kwargs)
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {await self._oauth.token()}"
        response = await self._http.request(method, path, headers=headers, **kwargs)
        if response.status_code == 401:  # token återkallad eller utgången: en ny och ett nytt försök
            headers["Authorization"] = f"Bearer {await self._oauth.token(refresh=True)}"
            response = await self._http.request(method, path, headers=headers, **kwargs)
        return response

    # -- SNOMED CT-edition ----------------------------------------------

    def _version(self, system: str | None, version: str | None) -> str | None:
        """Standardedition för SNOMED CT när ingen version anges (annars väljer servern sin standard,
        ofta den internationella utan svenska termer)."""
        if version or system != SNOMED:
            return version
        return self.config.snomed_edition

    def _system_version(self) -> str | None:
        """Parametern system-version till $expand och ValueSet/$validate-code: gäller bara SNOMED CT."""
        return f"{SNOMED}|{self.config.snomed_edition}" if self.config.snomed_edition else None

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
        params = {"system": system, "code": code, "version": self._version(system, version),
                  "property": properties or None, "displayLanguage": self.config.language}
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
            params = {"url": valueset, "system": system, "code": code, "display": display, "systemVersion": version,
                      "system-version": None if version else self._system_version()}
        else:
            path = "CodeSystem/$validate-code"
            params = {"url": system, "code": code, "display": display, "version": self._version(system, version)}
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
                + ([{"name": "filter", "valueString": filter}] if filter else [])
                + ([{"name": "system-version", "valueCanonical": self._system_version()}]
                   if self._system_version() else []),
            }
            vs = await self._request("POST", "ValueSet/$expand", json=body)
        else:
            params = {"url": url, "filter": filter, "count": count, "offset": offset,
                      "displayLanguage": self.config.language, "system-version": self._system_version()}
            vs = await self._request("GET", "ValueSet/$expand", params=params)
        expansion = vs.get("expansion", {})
        result = {
            "url": vs.get("url", url),
            "total": expansion.get("total"),
            "offset": expansion.get("offset", offset),
            "contains": [_slim_concept(c) for c in expansion.get("contains", [])],
        }
        # vilken version av kodsystemet expansionen gjordes mot (t.ex. svensk SNOMED CT-edition och datum)
        used = [p["valueUri"] for p in expansion.get("parameter", [])
                if p.get("name") == "used-codesystem" and p.get("valueUri")]
        if used:
            result["used_codesystems"] = used
        return result

    async def ecl(self, expression: str, filter: str | None = None, count: int = 50, offset: int = 0) -> dict[str, Any]:
        """Expandera ett ECL-uttryck (SNOMED CT Expression Constraint Language) i standardeditionen."""
        include = {"system": SNOMED, "filter": [{"property": "constraint", "op": "=", "value": expression}]}
        return await self.expand(valueset={"resourceType": "ValueSet", "compose": {"include": [include]}},
                                 filter=filter, count=count, offset=offset)

    async def subsumes(self, system: str, code_a: str, code_b: str, version: str | None = None) -> str:
        params = {"system": system, "codeA": code_a, "codeB": code_b, "version": self._version(system, version)}
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

    async def read(self, resource_type: str, rid: str) -> dict[str, Any] | None:
        """Läs en resurs på id; None om den inte finns (eller är borttagen)."""
        response = await self._send("GET", f"{resource_type}/{rid}")
        if response.status_code in (404, 410):
            return None
        if response.status_code >= 400:
            raise TerminologyError(f"{response.status_code} {resource_type}/{rid}: {response.text[:300]}")
        return response.json()

    async def delete(self, resource_type: str, rid: str) -> None:
        await self._request("DELETE", f"{resource_type}/{rid}")

    async def put_resource(self, resource: dict[str, Any]) -> dict[str, Any]:
        """Skapa/uppdatera en resurs (bara via skrivverktygen, när TX_ALLOW_WRITE=1)."""
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
