"""Konfiguration av vilken FHIR-terminologitjänst assistenten pratar med.

Tjänsten väljs med miljövariabler så att samma MCP-server kan ställas mot
t.ex. Ineras terminologitjänst, tx.nordics eller tx.fhir.org:

    TX_BASE_URL   Bas-URL till FHIR-endpointen (t.ex. https://tx.fhir.org/r4)
    TX_TOKEN      Valfri bearer-token (OAuth2 client credentials o.dyl.)
    TX_TIMEOUT    Timeout i sekunder (standard 30)
    TX_LANGUAGE   Föredraget språk för displaytexter (standard sv)
    FHIR_VERSION  FHIR-version för hela lösningen: R4 (standard) eller R5. Styr
                  bl.a. ConceptMap-formatet och paketens fhirVersions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://tx.fhir.org/r4"
FHIR_VERSIONS = {"R4": "4.0.1", "R5": "5.0.0"}


def fhir_version() -> str:
    """'R4' eller 'R5' ur FHIR_VERSION (accepterar även '4.0.1', '5.0.0', 'r5')."""
    raw = os.environ.get("FHIR_VERSION", "R4").strip().upper()
    if raw.startswith("5") or raw == "R5":
        return "R5"
    if raw.startswith("4") or raw == "R4":
        return "R4"
    raise ValueError(f"FHIR_VERSION '{raw}' stöds inte (R4 eller R5)")


@dataclass(frozen=True)
class ServerConfig:
    base_url: str = DEFAULT_BASE_URL
    token: str | None = None
    timeout: float = 30.0
    language: str = "sv"

    @classmethod
    def from_env(cls) -> "ServerConfig":
        return cls(
            base_url=os.environ.get("TX_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            token=os.environ.get("TX_TOKEN") or None,
            timeout=float(os.environ.get("TX_TIMEOUT", "30")),
            language=os.environ.get("TX_LANGUAGE", "sv"),
        )
