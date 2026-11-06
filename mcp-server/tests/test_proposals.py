"""Förslagsverktygen: kodkontroll, R4/R5-form, skydd för granskat innehåll."""

import asyncio

import pytest

from term_mcp import proposals
from term_mcp.proposals import PROPOSAL_TAG, ProposalError

NO = "http://hl7.no/fhir/CodeSystem/civil"
V3 = "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus"
CODES = {NO: {"gift": "Gift", "ugift": "Ugift", "uoppgitt": "Uoppgitt"}, V3: {"M": "Married", "S": "Never Married"}}


class FakeTx:
    def __init__(self, existing=None):
        self.store = {(r["resourceType"], r["id"]): r for r in existing or []}
        self.puts = []

    async def validate_code(self, code, system=None, **kw):
        display = CODES.get(system, {}).get(code)
        return {"result": display is not None, "display": display, "message": None if display else "okänd"}

    async def search(self, rtype, count=20, url=None, **kw):
        return [r for (t, _), r in self.store.items() if t == rtype and (url is None or r.get("url") == url)]

    async def read(self, rtype, rid):
        return self.store.get((rtype, rid))

    async def read_canonical(self, rtype, url, version=None):
        return {"url": url, "name": "Civil", "count": 3} if url == NO else None

    async def put_resource(self, r):
        self.puts.append(r)
        self.store[(r["resourceType"], r["id"])] = r
        return r

    async def expand(self, url=None, **kw):
        return {"contains": [{"code": c} for c in CODES[NO]]}

    async def delete(self, rtype, rid):
        self.store.pop((rtype, rid))


def run(coro):
    return asyncio.run(coro)


def test_conceptmap_r4_and_r5_shapes_and_coverage():
    mappings = [{"source_code": "gift", "target_code": "M"},
                {"source_code": "uoppgitt", "relation": "unmatched", "comment": "Saknas i v3"}]
    tx = FakeTx()
    r = run(proposals.conceptmap(tx, "http://example.org/fhir/ConceptMap/cm", "Cm", "Cm", NO, V3, mappings,
                                 source_valueset="http://hl7.no/vs"))
    assert r["created"] and r["coverage"]["not_mapped"] == ["ugift"]
    cm = tx.puts[0]
    assert cm["meta"]["tag"] == [PROPOSAL_TAG] and cm["status"] == "draft" and cm["sourceUri"] == "http://hl7.no/vs"
    gift, okand = cm["group"][0]["element"]
    assert gift["target"] == [{"code": "M", "display": "Married", "equivalence": "equivalent"}]
    assert okand["target"] == [{"equivalence": "unmatched", "comment": "Saknas i v3"}]
    r5 = FakeTx()
    run(proposals.conceptmap(r5, "http://example.org/fhir/ConceptMap/cm", "Cm", "Cm", NO, V3, mappings,
                             fhir_version="R5"))
    gift, okand = r5.puts[0]["group"][0]["element"]
    assert gift["target"][0]["relationship"] == "equivalent" and okand == {"code": "uoppgitt", "display": "Uoppgitt",
                                                                          "noMap": True}


def test_unknown_codes_and_missing_comments_stop_the_proposal():
    tx = FakeTx()
    r = run(proposals.conceptmap(tx, "u", "U", "U", NO, V3, [{"source_code": "gift", "target_code": "X"}]))
    assert r["created"] is False and r["unknown"][0]["code"] == "X" and not tx.puts
    with pytest.raises(ProposalError, match="motiveras"):
        run(proposals.conceptmap(tx, "u", "U", "U", NO, V3,
                                 [{"source_code": "gift", "target_code": "M", "relation": "smalare"}]))
    with pytest.raises(ProposalError, match="Okänd relation"):
        proposals.relation("ungefär")


def test_reviewed_resources_are_never_overwritten():
    reviewed = {"resourceType": "ValueSet", "id": "civil", "url": "http://ex.org/vs/civil", "status": "active"}
    tx = FakeTx([reviewed])
    with pytest.raises(ProposalError, match="granskad"):
        run(proposals.valueset(tx, "http://ex.org/vs/civil", "Annat", "x", [{"system": V3, "code": "M"}]))
    with pytest.raises(ProposalError, match="inget förslag"):
        run(proposals.valueset(tx, "http://ex.org/vs/ny", "civil", "x", [{"system": V3, "code": "M"}]))
    with pytest.raises(ProposalError, match="inget förslag"):
        run(proposals.withdraw(tx, "ValueSet", "civil"))
    # En släppt resurs som behållit förslagstaggen (HAPI) är ändå skyddad.
    released = dict(reviewed, id="sl", url="http://ex.org/vs/sl", meta={"tag": [PROPOSAL_TAG]})
    tx.store[("ValueSet", "sl")] = released
    with pytest.raises(ProposalError, match="granskad"):
        run(proposals.valueset(tx, "http://ex.org/vs/sl", "sl", "x", [{"system": V3, "code": "M"}]))
    # Ett förslag kan däremot ersättas och dras tillbaka.
    run(proposals.valueset(tx, "http://ex.org/vs/ny", "Ny", "x", [{"system": V3, "code": "M"}]))
    run(proposals.valueset(tx, "http://ex.org/vs/ny", "Ny", "x", [{"system": V3, "code": "S"}]))
    assert run(proposals.withdraw(tx, "ValueSet", "Ny"))["withdrawn"]


def test_translation_is_a_supplement_in_own_namespace():
    tx = FakeTx()
    r = run(proposals.translation(tx, NO, "sv", [{"code": "gift", "display": "gift"}]))
    cs = tx.puts[0]
    assert r["url"] == "http://example.org/fhir/CodeSystem/Civil-sv" and cs["supplements"] == NO
    assert cs["content"] == "supplement" and cs["concept"][0]["designation"] == [{"language": "sv", "value": "gift"}]
    with pytest.raises(ProposalError, match="finns inte"):
        run(proposals.translation(tx, "http://saknas", "sv", [{"code": "a", "display": "a"}]))
