"""Inloggning och behörighet: demoläge, inloggningsproxy och OpenID Connect mot en fejkad IdP."""

import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from term_inlasning.admin import auth  # noqa: E402
from term_inlasning.admin.app import create_app  # noqa: E402
from term_inlasning.admin.settings import AuthSettings, Namespace, OidcSettings, Settings  # noqa: E402
from term_inlasning.admin.store import Workspace  # noqa: E402

ISSUER = "https://idp.example.org/realms/term"
MAPPING = {"kodverk-lasare": ["läsare"], "kodverk-redaktorer": ["läsare", "redaktör"],
           "kodverk-admin": ["läsare", "administratör"]}


def workspace(tmp_path, **auth_kw):
    root = tmp_path / "ws"
    Workspace(root)
    Settings(namespaces=[Namespace(key="ex", label="Exempel", base_url="http://example.org/fhir")],
             auth=AuthSettings(**auth_kw)).save(root)
    return root


# -- roller och konfiguration ----------------------------------------------------------------------


def test_roles_from_groups_local_and_default(tmp_path):
    cfg = AuthSettings(mode="proxy", role_mapping=MAPPING, default_roles=["läsare"])
    store = auth.RoleStore(tmp_path)
    assert auth.resolve_roles(cfg, store, "anna", ["kodverk-redaktorer", "okänd-grupp"]) == {"läsare", "redaktör"}
    assert auth.resolve_roles(cfg, store, "bertil", []) == {"läsare"}
    store.assign("bertil", {"granskare"}, by="admin")
    assert auth.resolve_roles(cfg, store, "bertil", []) == {"läsare", "granskare"}
    assert store.log()[0] | {"tid": ""} == {"tid": "", "av": "admin", "användare": "bertil", "före": [],
                                            "efter": ["granskare"]}
    # demoläge: utan lokal tilldelning får man allt, med tilldelning bara den
    assert auth.resolve_roles(cfg, store, "cecilia", [], demo=True) == auth.ALL_ROLES
    assert auth.resolve_roles(cfg, store, "bertil", [], demo=True) == {"läsare", "granskare"}
    with pytest.raises(ValueError):
        store.assign("x", {"superuser"}, by="admin")


def test_check_config(monkeypatch):
    monkeypatch.delenv("TERM_SESSION_SECRET", raising=False)
    assert auth.check_config(AuthSettings(), production=False)  # demo: bara varning
    with pytest.raises(auth.ConfigError, match="spärrat"):
        auth.check_config(AuthSettings(), production=True)
    with pytest.raises(auth.ConfigError, match="Okända roller"):
        auth.check_config(AuthSettings(role_mapping={"g": ["chef"]}), production=False)
    oidc = AuthSettings(mode="oidc", oidc=OidcSettings(issuer=ISSUER, client_id="term-admin"))
    with pytest.raises(auth.ConfigError, match="TERM_SESSION_SECRET"):
        auth.check_config(oidc, production=True)
    monkeypatch.setenv("TERM_SESSION_SECRET", "x" * 32)
    auth.check_config(oidc, production=True)
    plain = AuthSettings(mode="oidc", oidc=OidcSettings(issuer="http://idp.example.org", client_id="c"))
    with pytest.raises(auth.ConfigError, match="https"):
        auth.check_config(plain, production=False)
    with pytest.raises(auth.ConfigError, match="alla adresser"):
        auth.check_config(AuthSettings(mode="proxy", trusted_proxies=["0.0.0.0/0"]), production=True)


def test_app_refuses_demo_in_production(tmp_path, monkeypatch):
    monkeypatch.setenv("TERM_PRODUCTION", "1")
    with pytest.raises(auth.ConfigError):
        create_app(workspace(tmp_path))


# -- demoläge --------------------------------------------------------------------------------------


def test_demo_ignores_user_headers_and_local_roles_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("TERM_DEV_USERS", "anna,bertil,cecilia")
    root = workspace(tmp_path)
    client = TestClient(create_app(root), follow_redirects=True)
    page = client.get("/", headers={"X-Remote-User": "mallory", "X-Forwarded-User": "mallory"}).text
    assert "mallory" not in page and "okänd" in page
    # cecilia begränsas till läsare: kan se men inte läsa in, administrera eller ändra roller
    auth.RoleStore(root).assign("cecilia", {"läsare"}, by="test")
    client.cookies.set("dev_user", "cecilia")
    assert client.get("/").status_code == 200
    r = client.get("/import")
    assert r.status_code == 403 and "redaktör" in r.text
    assert client.get("/settings").status_code == 403
    assert client.post("/anvandare", data={"username": "cecilia", "r_administratör": "on"}).status_code == 403
    # anna (alla roller i demoläge) ger cecilia redaktörsrollen
    client.cookies.set("dev_user", "anna")
    page = client.get("/anvandare").text
    assert "cecilia" in page and "Användare och roller" in page
    r = client.post("/anvandare", data={"username": "cecilia", "r_läsare": "on", "r_redaktör": "on"})
    assert "sparade" in r.text and auth.RoleStore(root).assigned("cecilia") == {"läsare", "redaktör"}
    client.cookies.set("dev_user", "cecilia")
    assert client.get("/import").status_code == 200


def test_cross_site_post_and_security_headers(tmp_path):
    client = TestClient(create_app(workspace(tmp_path)), follow_redirects=False)
    r = client.post("/dev/user", data={"user": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.post("/dev/user", data={"user": "x"}, headers={"Origin": "http://testserver"}).status_code == 303
    headers = client.get("/").headers
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"


# -- inloggningsproxy ------------------------------------------------------------------------------


def test_proxy_trusts_only_configured_proxy_and_secret(tmp_path, monkeypatch):
    root = workspace(tmp_path, mode="proxy", role_mapping=MAPPING, trusted_proxies=["testclient"])
    # TestClient ansluter från "testclient" som inte är en IP-adress – alltså aldrig betrodd
    client = TestClient(create_app(root))
    r = client.get("/", headers={"X-Forwarded-User": "anna", "X-Forwarded-Groups": "kodverk-admin"})
    assert r.status_code == 401
    ident = auth.proxy_identity({"x-forwarded-user": "anna", "x-forwarded-groups": "kodverk-lasare, x"},
                                "10.0.0.5", AuthSettings(mode="proxy", trusted_proxies=["10.0.0.0/24"]))
    assert ident.username == "anna" and ident.groups == ["kodverk-lasare", "x"]
    assert auth.proxy_identity({"x-forwarded-user": "anna"}, "10.0.1.5",
                               AuthSettings(mode="proxy", trusted_proxies=["10.0.0.0/24"])) is None
    monkeypatch.setenv("TERM_PROXY_SECRET", "hemligt")
    cfg = AuthSettings(mode="proxy", trusted_proxies=["10.0.0.0/24"])
    assert auth.proxy_identity({"x-forwarded-user": "anna"}, "10.0.0.5", cfg) is None
    assert auth.proxy_identity({"x-forwarded-user": "anna", "x-proxy-secret": "hemligt"}, "10.0.0.5", cfg)


def test_proxy_roles_in_app(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "trusted_proxy", lambda host, trusted: True)
    root = workspace(tmp_path, mode="proxy", role_mapping=MAPPING)
    client = TestClient(create_app(root))
    reader = {"X-Forwarded-User": "lars", "X-Forwarded-Groups": "kodverk-lasare"}
    assert client.get("/", headers=reader).status_code == 200
    assert client.get("/import", headers=reader).status_code == 403
    assert client.get("/dev/user").status_code in (404, 405)  # inget användarval utanför demoläget
    admin = {"X-Forwarded-User": "ada", "X-Forwarded-Groups": "kodverk-admin"}
    page = client.get("/anvandare", headers=admin).text
    assert "lars" in page and "kodverk-lasare" in page     # kända användare med grupper
    client.post("/anvandare", headers=admin, data={"username": "lars", "r_läsare": "on", "r_redaktör": "on"})
    assert client.get("/import", headers=reader).status_code == 200
    r = client.post("/anvandare", headers=admin, data={"username": "ada"})
    assert "sparade" in r.text  # ada har adminrollen via gruppen, inte lokalt – får tömma sina lokala roller


# -- OpenID Connect --------------------------------------------------------------------------------


class FakeIdp:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.claims: dict = {}
        self.alg = "RS256"
        self.kid = "k1"
        self.token_requests: list[dict] = []

    def id_token(self, **override):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "term-admin", "sub": "u-1", "iat": now, "exp": now + 300,
                  "preferred_username": "anna", "name": "Anna Andersson", "email": "anna@example.org",
                  "groups": ["kodverk-redaktorer", "annan-grupp"]} | self.claims | override
        if self.alg == "none":
            return jwt.encode(claims, None, algorithm="none")
        if self.alg == "HS256":
            return jwt.encode(claims, "term-admin-secret-som-ar-lang-nog-32b", algorithm="HS256", headers={"kid": self.kid})
        return jwt.encode(claims, self.key, algorithm=self.alg, headers={"kid": self.kid})

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx2.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth", "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/certs", "end_session_endpoint": f"{ISSUER}/logout",
                "id_token_signing_alg_values_supported": ["RS256", "HS256", "none"]})
        if path.endswith("/certs"):
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx2.Response(200, json={"keys": [jwk | {"kid": "k1", "use": "sig", "alg": "RS256"}]})
        if path.endswith("/token"):
            form = parse_qs(request.content.decode())
            self.token_requests.append(form)
            return httpx2.Response(200, json={"access_token": "a", "token_type": "Bearer",
                                              "id_token": self.id_token(nonce=self.nonce)})
        return httpx2.Response(404)


@pytest.fixture
def oidc(tmp_path, monkeypatch):
    monkeypatch.setenv("TERM_SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("TERM_OIDC_CLIENT_SECRET", "klienthemlighet")
    root = workspace(tmp_path, mode="oidc", oidc=OidcSettings(issuer=ISSUER, client_id="term-admin"),
                     role_mapping=MAPPING)
    idp = FakeIdp()
    client = TestClient(create_app(root, oidc_transport=httpx2.MockTransport(idp.handler)), follow_redirects=False)
    return client, idp, root


def login(client, idp, **override) -> httpx2.Response:
    r = client.get("/import")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login?next=%2Fimport")
    r = client.get(r.headers["location"])
    location = urlsplit(r.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/auth"
    q = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == "http://testserver/auth/callback"
    idp.nonce = override.pop("nonce", q["nonce"])
    return client.get("/auth/callback", params={"code": "c1", "state": override.pop("state", q["state"])})


def test_oidc_login_roles_and_logout(oidc):
    client, idp, root = oidc
    r = login(client, idp)
    assert r.status_code == 303 and r.headers["location"] == "/import"
    form = idp.token_requests[0]
    assert form["client_secret"] == ["klienthemlighet"] and form["code_verifier"][0]
    page = client.get("/").text
    assert "Anna Andersson" in page and "Logga ut" in page and "demoläge" not in page
    assert client.get("/import").status_code == 200                       # redaktör via grupp
    assert client.get("/anvandare").status_code == 403                    # inte administratör
    seen = auth.RoleStore(root).all()["seen"]["anna"]
    assert seen["groups"] == ["kodverk-redaktorer"] and seen["email"] == "anna@example.org"
    r = client.get("/auth/logout")
    assert r.status_code == 303 and r.headers["location"].startswith(f"{ISSUER}/logout?") and "auth%2Futloggad" in r.headers["location"]
    assert client.get("/import").status_code == 303                       # utloggad


@pytest.mark.parametrize("case", ["state", "nonce", "aud", "iss", "expired", "alg-none", "alg-hs256", "kid"])
def test_oidc_rejects_bad_responses(oidc, case):
    client, idp, _ = oidc
    override = {}
    if case in ("state", "nonce"):
        override[case] = "fel"
    elif case == "aud":
        idp.claims = {"aud": "annan-klient"}
    elif case == "iss":
        idp.claims = {"iss": "https://evil.example"}
    elif case == "expired":
        idp.claims = {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}
    elif case == "alg-none":
        idp.alg = "none"
    elif case == "alg-hs256":
        idp.alg = "HS256"
    elif case == "kid":
        idp.kid = "okänd"
    r = login(client, idp, **override)
    assert r.status_code in (400, 401), r.text[:500]
    assert client.get("/import").status_code == 303                       # fortfarande inte inloggad


def test_oidc_rejects_open_redirect(oidc):
    client, idp, _ = oidc
    client.get("/auth/login", params={"next": "//evil.example/x"})
    location = urlsplit(client.get("/auth/login", params={"next": "https://evil.example"}).headers["location"])
    q = {k: v[0] for k, v in parse_qs(location.query).items()}
    idp.nonce = q["nonce"]
    r = client.get("/auth/callback", params={"code": "c", "state": q["state"]})
    assert r.headers["location"] == "/"
