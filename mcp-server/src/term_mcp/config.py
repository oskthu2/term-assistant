"""Vilken FHIR-terminologitjänst MCP-servern pratar med.

Tjänsten väljs med miljövariabler så att samma server kan ställas mot t.ex.
Ineras terminologitjänst, tx-nordics, tx.fhir.org eller en lokal HAPI-server:

    TX_BASE_URL     bas-URL till FHIR-endpointen (t.ex. https://tx-nordics.fhir.org/fhir)
    TX_TOKEN        valfri statisk bearer-token
    TX_TOKEN_URL, TX_CLIENT_ID, TX_CLIENT_SECRET, TX_SCOPE
                    OAuth2 client credentials: token hämtas och förnyas automatiskt
    TX_SNOMED_EDITION  SNOMED CT-edition som används när ingen version anges, t.ex.
                    45991000052106 (svenska) eller http://snomed.info/sct/45991000052106/version/20251130
    TX_TIMEOUT      timeout i sekunder (standard 30)
    TX_LANGUAGE     föredraget språk för displaytexter (standard sv)
    TX_ALLOW_WRITE  "1" aktiverar förslagsverktygen (standard av)
    FHIR_VERSION    R4 (standard) eller R5 – styr bl.a. ConceptMap-formatet i förslag
    TX_PROPOSAL_BASE  bas för förslagens canonical URL:er när ingen anges (standard http://example.org/fhir)
    TX_PROPOSAL_URL   annan FHIR-server som förslagen skrivs till (t.ex. en HAPI-testserver), medan
                    uppslag och kodkontroll görs mot TX_BASE_URL (t.ex. en Ontoserver i drift)
    TX_PROPOSAL_TOKEN valfri bearer-token för förslagsservern
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

DEFAULT_BASE_URL = "https://tx.fhir.org/r4"
SNOMED = "http://snomed.info/sct"
# Kortnamn för de nordiska editionerna (modul-id), utöver fullständiga URI:er och modul-id
SNOMED_EDITIONS = {"se": "45991000052106", "sv": "45991000052106", "no": "51000202101", "nb": "51000202101",
                   "dk": "554471000005108", "da": "554471000005108", "fi": "11000229106",
                   "int": "900000000000207008"}


def snomed_edition(value: str | None) -> str | None:
    """'45991000052106', 'se' eller en fullständig URI -> http://snomed.info/sct/45991000052106[/version/…]."""
    value = (value or "").strip()
    if not value:
        return None
    value = SNOMED_EDITIONS.get(value.lower(), value)
    if re.fullmatch(r"\d{6,18}", value):
        return f"{SNOMED}/{value}"
    if re.fullmatch(rf"{re.escape(SNOMED)}/\d{{6,18}}(/version/\d{{8}})?", value):
        return value
    raise ValueError(f"TX_SNOMED_EDITION '{value}' är ingen SNOMED CT-edition (modul-id eller "
                     f"{SNOMED}/<modul>[/version/<datum>]).")


@dataclass(frozen=True)
class ServerConfig:
    base_url: str = DEFAULT_BASE_URL
    token: str | None = None
    timeout: float = 30.0
    language: str = "sv"
    allow_write: bool = False
    fhir_version: str = "R4"
    proposal_base: str = "http://example.org/fhir"
    snomed_edition: str | None = None
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None
    proposal_url: str | None = None
    proposal_token: str | None = None

    def for_proposals(self) -> "ServerConfig":
        """Konfigurationen för servern som förslagen skrivs till (samma server om TX_PROPOSAL_URL saknas)."""
        if not self.proposal_url:
            return self
        return replace(self, base_url=self.proposal_url, token=self.proposal_token, token_url=None,
                       client_id=None, client_secret=None, scope=None, proposal_url=None, proposal_token=None)

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
            snomed_edition=snomed_edition(os.environ.get("TX_SNOMED_EDITION")),
            token_url=os.environ.get("TX_TOKEN_URL") or None,
            client_id=os.environ.get("TX_CLIENT_ID") or None,
            client_secret=os.environ.get("TX_CLIENT_SECRET") or None,
            scope=os.environ.get("TX_SCOPE") or None,
            proposal_url=(os.environ.get("TX_PROPOSAL_URL") or "").rstrip("/") or None,
            proposal_token=os.environ.get("TX_PROPOSAL_TOKEN") or None,
        )
