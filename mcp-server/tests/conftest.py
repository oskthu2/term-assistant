import os
from pathlib import Path

# Demo-klienten startar MCP-servern som underprocess; den ska hitta paketet även när det inte är installerat.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (_SRC, os.environ.get("PYTHONPATH")) if p)
