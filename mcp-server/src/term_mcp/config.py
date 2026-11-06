"""Vilken FHIR-terminologitjänst MCP-servern pratar med.

Tjänsten väljs med miljövariabler så att samma server kan ställas mot t.ex.
Ineras terminologitjänst, tx-nordics, tx.fhir.org eller en lokal HAPI-server:

    TX_BASE_URL     bas-URL till FHIR-endpointen (t.ex. https://tx-nordics.fhir.org/fhir)
    TX_TOKEN        valfri bearer-token (OAuth2 client credentials o.dyl.)
    TX_TIMEOUT      timeout i sekunder (standard 30)
    TX_LANGUAGE     föredraget språk för displaytexter (standard sv)
    TX_ALLOW_WRITE  "1" aktiverar skrivverktygen (standard av)
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

    @classmethod
    def from_env(cls) -> "ServerConfig":
        return cls(
            base_url=os.environ.get("TX_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            token=os.environ.get("TX_TOKEN") or None,
            timeout=float(os.environ.get("TX_TIMEOUT", "30")),
            language=os.environ.get("TX_LANGUAGE", "sv"),
            allow_write=os.environ.get("TX_ALLOW_WRITE", "").strip().lower() in ("1", "true", "ja", "yes"),
        )
