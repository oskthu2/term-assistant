import copy
import io
import json
import tarfile

import httpx2
import pytest

from term_paket.build import build
from term_paket.config import ConfigError, parse_config
from term_paket.load import load
from term_paket.package import Package, assign_ids, latest_packages, read_package_file, write_package
from term_paket.sources import FhirServer, collect

BASE = "https://tx.example.org/fhir"
CS = {"resourceType": "CodeSystem", "id": "123", "url": "http://ex.org/cs/civil", "version": "1.0.0",
      "name": "Civil", "status": "active", "content": "complete", "count": 2,
      "meta": {"versionId": "7", "lastUpdated": "2025-01-01T00:00:00Z"},
      "concept": [{"code": "G", "display": "Gift"}, {"code": "O", "display": "Ogift"}]}
VS = {"resourceType": "ValueSet", "id": "vs-civil", "url": "http://ex.org/vs/civil", "version": "1.0.0",
      "name": "CivilVS", "status": "active",
      "compose": {"include": [{"system": "http://ex.org/cs/civil"}, {"system": "http://snomed.info/sct",
                                                                     "concept": [{"code": "1"}]}]}}
BIG = {"resourceType": "CodeSystem", "url": "http://ex.org/cs/stor", "version": "1", "name": "Stor",
       "content": "complete", "count": 99999}


def server(store: dict[str, list[dict]], seen: list[str] | None = None) -> FhirServer:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(str(request.url))
        rtype = request.url.path.rsplit("/", 1)[-1]
        url = request.url.params.get("url")
        hits = [r for r in store.get(rtype, []) if r["url"] == url]
        if request.url.params.get("_summary") != "false":
            hits = [{k: v for k, v in r.items() if k not in ("concept", "compose")} |
                    {"meta": {"tag": [{"code": "SUBSETTED"}]}} for r in hits]
        return httpx2.Response(200, json={"resourceType": "Bundle", "entry": [{"resource": r} for r in hits]})
    return FhirServer(BASE, transport=httpx2.MockTransport(handler), retries=0)


def config(**extra):
    raw = {"name": "se.test.terminologi", "title": "Test", "classification": {
        "system": "http://terminology.hl7.org/CodeSystem/v3-Confidentiality", "code": "U"},
        "sources": [{"type": "fhir", "base": BASE, "include": [
            {"resourceType": "ValueSet", "url": VS["url"], "withCodeSystems": True},
            {"resourceType": "CodeSystem", "url": BIG["url"]}]}]}
    raw.update(extra)
    return parse_config(raw)


def test_config_is_validated():
    with pytest.raises(ConfigError) as exc:
        parse_config({"name": "Ogiltigt Namn", "sources": [{"type": "ftp"}]})
    assert "name" in str(exc.value) and "okänd typ" in str(exc.value)


def test_collect_follows_dependencies_and_skips_large():
    seen = []
    collected = collect(config(), {BASE: server({"ValueSet": [VS], "CodeSystem": [CS, BIG]}, seen)})
    types = sorted((r["resourceType"], r["url"]) for r in collected.resources)
    assert types == [("CodeSystem", CS["url"]), ("ValueSet", VS["url"])]
    assert any("snomed" in n and "externt beroende" in n for n in collected.notes)
    assert any("För stort" in n for n in collected.notes)
    assert all("_summary=false" in u for u in seen)               # hela resurser, inte sammanfattningar
    cs = next(r for r in collected.resources if r["resourceType"] == "CodeSystem")
    assert "meta" not in cs                                         # serverns lagringsmetadata tas bort


def test_versions_follow_semver_and_unchanged_builds_nothing(tmp_path):
    store = {"ValueSet": [VS], "CodeSystem": [CS]}
    cfg = config()
    first = build(cfg, collect(cfg, {BASE: server(store)}), tmp_path, today="20260101000000")
    assert first.changed and first.version == "1.0.0" and first.file.exists()
    pkg = read_package_file(first.file)
    assert {r["id"] for r in pkg.resources} == {"r-123", "vs-civil"}  # HAPI kräver en bokstav i id
    assert all(r["meta"]["security"][0]["code"] == "U" for r in pkg.resources)

    again = build(cfg, collect(cfg, {BASE: server(store)}), tmp_path, previous=pkg)
    assert not again.changed and again.version == "1.0.0"

    changed = copy.deepcopy(store)
    changed["CodeSystem"][0]["concept"][0]["display"] = "Gift (ändrad)"
    patch = build(cfg, collect(cfg, {BASE: server(changed)}), tmp_path, previous=pkg)
    assert (patch.version, patch.changes.changed) == ("1.0.1", [f"CodeSystem {CS['url']}"])

    changed["CodeSystem"][0]["version"] = "2.0.0"
    minor = build(cfg, collect(cfg, {BASE: server(changed)}), tmp_path, previous=read_package_file(patch.file))
    assert minor.version == "1.1.0" and "1.0.0 -> 2.0.0" in minor.changes.new_version[0]

    removed = build(cfg, collect(cfg, {BASE: server({"ValueSet": [VS]})}), tmp_path,
                    previous=read_package_file(minor.file))
    assert removed.version == "2.0.0" and removed.changes.removed == [f"CodeSystem {CS['url']}"]
    feed = json.loads((tmp_path / "se.test.terminologi.feed.json").read_text())
    assert [v["version"] for v in feed["versions"]] == ["2.0.0", "1.1.0", "1.0.1", "1.0.0"]
    assert latest_packages(tmp_path)["se.test.terminologi"].name.endswith("-2.0.0.tgz")
    assert "Borttagna" in (tmp_path / "se.test.terminologi-2.0.0.md").read_text()


def test_same_content_gives_identical_file():
    pkg = Package({"name": "a.b", "version": "1.0.0", "date": "20260101000000", "fhirVersions": ["4.0.1"]},
                  [dict(CS, id="cs")])
    assert write_package(pkg) == write_package(copy.deepcopy(pkg))


def test_summary_resources_are_refused():
    srv = server({"CodeSystem": [CS]})
    srv.search = lambda rtype, **kw: [dict(CS, meta={"tag": [{"code": "SUBSETTED"}]})]
    with pytest.raises(ValueError, match="sammanfattat"):
        srv.canonical("CodeSystem", CS["url"])


def test_packages_source_reads_import_service_releases(tmp_path):
    files = {"package/package.json": {"name": "se.exempel.kodverk.kv-ex", "version": "1.2.0"},
             "package/CodeSystem-KV.json": dict(CS, id="KV", url="http://example.org/fhir/CodeSystem/KV"),
             "package/other/provenance.json": {"resourceType": "Bundle"}}
    for version in ("1.1.0", "1.2.0"):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in files.items():
                content = dict(content, version=version) if name.endswith("package.json") else content
                data = json.dumps(content).encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        (tmp_path / f"kv-ex-{version}.tgz").write_bytes(buf.getvalue())
    cfg = parse_config({"name": "se.test.alla", "sources": [{"type": "packages", "path": str(tmp_path)}]})
    collected = collect(cfg, {})
    assert [r["url"] for r in collected.resources] == ["http://example.org/fhir/CodeSystem/KV"]
    assert collected.sources == ["paket se.exempel.kodverk.kv-ex#1.2.0"]


def test_load_uses_transactions_and_falls_back_to_single_puts():
    resources = [dict(CS, id="cs"), dict(VS, id="vs"), dict(VS, id="trasig", url="http://ex.org/vs/trasig")]
    pkg = Package({"name": "a.b", "version": "1.0.0"}, resources)
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx2.Response(400, json={"resourceType": "OperationOutcome", "issue": [{"diagnostics": "x"}]})
        if request.url.path.endswith("/trasig"):
            return httpx2.Response(422, json={"resourceType": "OperationOutcome",
                                              "issue": [{"diagnostics": "ogiltig"}]})
        return httpx2.Response(200, json={"resourceType": "Bundle"})

    report = load(pkg, "http://localhost:8080/fhir", transport=httpx2.MockTransport(handler), log=lambda m: None)
    assert calls[0] == ("POST", "/fhir/")                      # en transaktion först
    assert ("PUT", "/fhir/CodeSystem/cs") in calls             # sedan en och en
    assert len(report.loaded) == 2 and "ogiltig" in report.failed[0]


def test_assign_ids_are_unique_and_valid():
    resources = [{"resourceType": "ValueSet", "id": "a b"}, {"resourceType": "ValueSet", "id": "a-b"},
                 {"resourceType": "CodeSystem", "url": "http://x/CodeSystem/42"}]
    assign_ids(resources)
    assert [r["id"] for r in resources] == ["a-b", "a-b-2", "r-42"]
