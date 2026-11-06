import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# Demo-klienten startar MCP-servern som underprocess; den ska hitta paketet även när det inte är installerat.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (_SRC, os.environ.get("PYTHONPATH")) if p)


VALUESETS = {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": {
    "resourceType": "ValueSet", "url": "http://hl7.se/fhir/ig/base/ValueSet/SEBaseMaritalStatusVS",
    "version": "1.1.0", "name": "SEBaseMaritalStatusVS", "title": "SE ValueSet for marital status",
    "status": "active"}}]}


class FakeFhir(BaseHTTPRequestHandler):
    requests: list[str] = []

    def do_GET(self):  # noqa: N802
        FakeFhir.requests.append(self.path)
        body = json.dumps(VALUESETS if self.path.startswith("/fhir/ValueSet") else
                          {"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1",
                           "software": {"name": "Fake"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/fhir+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def fhir():
    server = HTTPServer(("127.0.0.1", 0), FakeFhir)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/fhir"
    server.shutdown()
