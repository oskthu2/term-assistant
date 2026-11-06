"""En liten MCP-server för testerna av AI-assistenten: samma verktygsnamn som term-mcp, fasta svar."""

from mcp.server.mcpserver import MCPServer

V3 = "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus"
CODES = {"M": "Married", "S": "Never Married", "D": "Divorced", "W": "Widowed"}

mcp = MCPServer("fake-term-mcp")


@mcp.tool()
async def search_concepts(system: str, text: str, count: int = 20) -> dict:
    """Fritextsök begrepp."""
    return {"contains": [{"system": V3, "code": c, "display": d} for c, d in CODES.items()
                         if text.lower() in d.lower()]}


@mcp.tool()
async def lookup_code(system: str, code: str) -> dict:
    """Slå upp ett begrepp."""
    return {"code": code, "display": CODES.get(code)}


@mcp.tool()
async def draft_valueset(url: str, name: str) -> dict:
    """Utkastverktyg – ska inte ges till assistenten i administrationsverktyget."""
    return {"created": True}


if __name__ == "__main__":
    mcp.run(transport="stdio")
