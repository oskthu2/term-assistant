"""Vilken FHIR-terminologitjänst MCP-servern pratar med.

Tjänsten väljs med miljövariabler så att samma server kan ställas mot t.ex.
Ineras terminologitjänst, tx-nordics, tx.fhir.org eller en lokal HAPI-server:

    TX_BASE_URL     bas-URL till FHIR-endpointen (t.ex. https://tx-nordics.fhir.org/fhir)
    TX_TOKEN        valfri bearer-token (OAuth2 client credentials o.dyl.)
    TX_TIMEOUT      timeout i sekunder (standard 30)
    TX_LANGUAGE     föredraget språk för displaytexter (standard sv)
    TX_ALLOW_WRITE  "1" aktiverar förslagsverktygen (standard av)
    FHIR_VERSION    R4 (standard) eller R5 – styr bl.a. ConceptMap-formatet i förslag
    TX_PROPOSAL_BASE  bas för förslagens canonical URL:er när ingen anges (standard http://example.org/fhir)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://tx.fhir.org/r4"


@dataclass(frozen=True)
class ServerConfig:
    base_url: str = DEFAULT_BASE_URL
    token: str | None = None
    timeout: float = 30.0
    language: str = "sv"
    allow_write: bool = False
    fhir_version: str = "R4"
    proposal_base: str = "http://example.org/fhir"

    @classmethod
    def from_env(cls) -> "ServerConfig":
        return cls(
            base_url=os.environ.get("TX_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            token=os.environ.get("TX_TOKEN") or None,
            timeout=float(os.environ.get("TX_TIMEOUT", "30")),
            language=os.environ.get("TX_LANGUAGE", "sv"),
            allow_write=os.environ.get("TX_ALLOW_WRITE", "").strip().lower() in ("1", "true", "ja", "yes"),
            fhir_version="R5" if os.environ.get("FHIR_VERSION", "R4").strip().upper().lstrip("R").startswith("5")
            else "R4",
            proposal_base=os.environ.get("TX_PROPOSAL_BASE", "http://example.org/fhir").rstrip("/"),
        )
