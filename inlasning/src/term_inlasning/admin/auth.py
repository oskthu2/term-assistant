"""Inloggning och behörighet för administrationsverktyget.

Tre lägen (``settings.json`` → ``auth.mode``):

``demo``   användarval i sidhuvudet (``TERM_DEV_USERS``). Ingen riktig inloggning – bara för demo
           och test. Spärras när ``TERM_PRODUCTION=1``.
``proxy``  en betrodd inloggningsproxy (t.ex. oauth2-proxy, Entra Application Proxy) autentiserar och
           sätter ``X-Forwarded-User``/``X-Forwarded-Groups``. Huvudena godtas bara från adresser i
           ``auth.trusted_proxies`` och, om ``TERM_PROXY_SECRET`` är satt, med rätt ``X-Proxy-Secret``.
``oidc``   verktyget loggar själv in mot organisationens IdP med OpenID Connect (authorization code +
           PKCE, state och nonce). ID-tokenets signatur, utfärdare, mottagare, giltighetstid och nonce
           kontrolleras. Sessionen ligger i en signerad, HttpOnly-cookie.

Roller (additiva – en person kan ha flera):

=============  ============================================================================
läsare         se projekt, releaser, förslag och logg
redaktör       läsa in, skapa och ändra projekt (ändringar granskas om kedjan kräver det),
               skapa releasekandidater, ta in förslag
granskare      godkänna eller avslå ändringsförslag och releasekandidater (fyra ögon gäller)
publicerare    publicera godkända releaser till paketkatalog och terminologitjänst
administratör  inställningar och rolltilldelning
=============  ============================================================================

Rollerna kommer från två håll och slås ihop:

1. **IdP:n** – grupper eller approller i ``auth.roles_claim`` kopplas till roller i
   ``auth.role_mapping`` (konfiguration som kod; försvinner personen ur gruppen i IdP:n
   försvinner behörigheten vid nästa inloggning).
2. **Lokalt** – administratörer kan tilldela roller till enskilda personer i verktyget
   (``<arbetsyta>/roller.json``, varje ändring loggas). Gäller direkt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROLES = {
    "läsare": "Se projekt, releaser, förslag och logg",
    "redaktör": "Läsa in, skapa och ändra projekt, skapa releasekandidater, ta in förslag",
    "granskare": "Godkänna eller avslå ändringsförslag och releasekandidater (fyra ögon gäller)",
    "publicerare": "Publicera godkända releaser",
    "administratör": "Inställningar och rolltilldelning",
}
ALL_ROLES = frozenset(ROLES)


class AuthError(Exception):
    """Inloggningen misslyckades (visas för användaren utan tekniska detaljer)."""


class ConfigError(RuntimeError):
    """Inloggningen är felkonfigurerad – verktyget startar inte."""


@dataclass
class Identity:
    username: str
    name: str | None = None
    email: str | None = None
    groups: list[str] = field(default_factory=list)
    roles: frozenset[str] = frozenset()
    source: str = "demo"

    @property
    def authenticated(self) -> bool:
        return bool(self.username)

    def has(self, role: str) -> bool:
        return role in self.roles


ANONYMOUS = Identity(username="", source="anonym")


# -- roller ------------------------------------------------------------------------------------------


class RoleStore:
    """Lokala rolltilldelningar och kända användare i arbetsytan, med logg över varje ändring."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "roller.json"
        self.log_path = self.root / "behorighet.log.jsonl"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"users": {}, "seen": {}}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def assigned(self, username: str) -> set[str]:
        return set(self._read()["users"].get(username, []))

    def all(self) -> dict:
        return self._read()

    def assign(self, username: str, roles: set[str], by: str) -> None:
        unknown = set(roles) - ALL_ROLES
        if unknown:
            raise ValueError(f"Okända roller: {', '.join(sorted(unknown))}")
        data = self._read()
        before = sorted(data["users"].get(username, []))
        if roles:
            data["users"][username] = sorted(roles)
        else:
            data["users"].pop(username, None)
        self._write(data)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"tid": time.strftime("%Y-%m-%dT%H:%M:%S"), "av": by, "användare": username,
                                 "före": before, "efter": sorted(roles)}, ensure_ascii=False) + "\n")

    def seen(self, identity: Identity) -> None:
        """Kom ihåg vem som loggat in (namn, grupper), så att administratören kan tilldela roller."""
        data = self._read()
        entry = {"name": identity.name, "email": identity.email, "groups": identity.groups,
                 "last_seen": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": identity.source}
        if data["seen"].get(identity.username, {}) | {"last_seen": entry["last_seen"]} != entry:
            data["seen"][identity.username] = entry
            self._write(data)

    def log(self, limit: int = 50) -> list[dict]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines[-limit:]][::-1]


def resolve_roles(auth, store: RoleStore, username: str, groups: list[str], demo: bool = False) -> frozenset[str]:
    """IdP-grupper via auth.role_mapping + lokala tilldelningar + auth.default_roles.
    I demoläge får en användare utan lokal tilldelning alla roller (så att demon fungerar direkt)."""
    local = store.assigned(username)
    if demo and not local:
        return ALL_ROLES
    roles = set(auth.default_roles) | local
    for g in groups:
        roles |= set(auth.role_mapping.get(g, []))
    return frozenset(roles & ALL_ROLES)


# -- proxy ------------------------------------------------------------------------------------------


def trusted_proxy(client_host: str | None, trusted: list[str]) -> bool:
    if not client_host:
        return False
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    for entry in trusted:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def proxy_identity(headers, client_host: str | None, auth) -> Identity | None:
    """Användaren enligt inloggningsproxyn – bara från en betrodd adress och med rätt hemlighet."""
    if not trusted_proxy(client_host, auth.trusted_proxies):
        return None
    headers = {k.lower(): v for k, v in headers.items()}
    secret = os.environ.get("TERM_PROXY_SECRET")
    if secret and not hmac.compare_digest(headers.get("x-proxy-secret", ""), secret):
        return None
    user = (headers.get(auth.proxy_user_header.lower()) or "").strip()
    if not user:
        return None
    groups = [g.strip() for g in (headers.get(auth.proxy_groups_header.lower()) or "").split(",") if g.strip()]
    return Identity(username=user, groups=groups, source="proxy")


# -- OIDC -------------------------------------------------------------------------------------------

_SAFE_ALGS = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class OidcClient:
    """OpenID Connect med authorization code + PKCE. ``transport`` gör det möjligt att testa utan nätverk."""

    def __init__(self, settings, client_secret: str | None, transport=None):
        self.settings = settings
        self.client_secret = client_secret
        self.transport = transport
        self._metadata: dict | None = None
        self._jwks: dict | None = None

    def _http(self):
        import httpx2
        return httpx2.AsyncClient(timeout=15, transport=self.transport, follow_redirects=False)

    async def metadata(self) -> dict:
        if self._metadata is None:
            url = self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
            async with self._http() as http:
                response = await http.get(url)
            response.raise_for_status()
            meta = response.json()
            if meta.get("issuer") != self.settings.issuer:
                raise ConfigError(f"IdP:ns issuer {meta.get('issuer')} matchar inte konfigurationen "
                                  f"{self.settings.issuer}")
            self._metadata = meta
        return self._metadata

    async def authorization_url(self, redirect_uri: str) -> tuple[str, dict]:
        """(adress till IdP:n, värden som sparas i sessionen till callback)."""
        meta = await self.metadata()
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
        params = {"response_type": "code", "client_id": self.settings.client_id, "redirect_uri": redirect_uri,
                  "scope": " ".join(self.settings.scopes), "state": state, "nonce": nonce,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{meta['authorization_endpoint']}?{urlencode(params)}", {
            "state": state, "nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri,
            "started": int(time.time())}

    async def exchange(self, code: str, pending: dict) -> dict:
        meta = await self.metadata()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
                "client_id": self.settings.client_id, "code_verifier": pending["verifier"]}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        async with self._http() as http:
            response = await http.post(meta["token_endpoint"], data=data, headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise AuthError(f"IdP:n avvisade inloggningen ({response.status_code}).")
        tokens = response.json()
        if not tokens.get("id_token"):
            raise AuthError("IdP:n skickade inget ID-token.")
        return tokens

    async def _jwks_set(self, refresh: bool = False):
        import jwt
        if self._jwks is None or refresh:
            meta = await self.metadata()
            async with self._http() as http:
                response = await http.get(meta["jwks_uri"])
            response.raise_for_status()
            self._jwks = response.json()
        return jwt.PyJWKSet.from_dict(self._jwks)

    async def validate(self, id_token: str, nonce: str) -> dict[str, Any]:
        import jwt
        meta = await self.metadata()
        header = jwt.get_unverified_header(id_token)
        alg = header.get("alg")
        allowed = _SAFE_ALGS & set(meta.get("id_token_signing_alg_values_supported", ["RS256"]))
        if alg not in allowed:
            raise AuthError(f"ID-tokenet har en otillåten signaturalgoritm ({alg}).")
        keys = await self._jwks_set()
        key = next((k for k in keys.keys if k.key_id == header.get("kid")), None)
        if key is None:  # nyckelbyte hos IdP:n
            keys = await self._jwks_set(refresh=True)
            key = next((k for k in keys.keys if k.key_id == header.get("kid")), None)
        if key is None:
            raise AuthError("ID-tokenet är signerat med en okänd nyckel.")
        try:
            claims = jwt.decode(id_token, key.key, algorithms=[alg], audience=self.settings.client_id,
                                issuer=self.settings.issuer, leeway=60,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            raise AuthError(f"ID-tokenet godkändes inte ({type(exc).__name__}).") from exc
        if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise AuthError("ID-tokenet hör inte till den här inloggningen (nonce).")
        return claims

    async def logout_url(self, id_token: str | None, return_to: str) -> str | None:
        meta = await self.metadata()
        endpoint = meta.get("end_session_endpoint")
        if not endpoint:
            return None
        params = {"post_logout_redirect_uri": return_to, "client_id": self.settings.client_id}
        if id_token:
            params["id_token_hint"] = id_token
        return f"{endpoint}?{urlencode(params)}"


def claim(claims: dict, path: str) -> Any:
    """Punktnotation: 'realm_access.roles' -> claims['realm_access']['roles']."""
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def identity_from_claims(claims: dict, oidc, auth) -> dict:
    """Det som sparas i sessionen: bara grupper som betyder något i role_mapping (cookien hålls liten)."""
    raw = claim(claims, oidc.roles_claim) or []
    groups = [raw] if isinstance(raw, str) else [str(g) for g in raw]
    relevant = [g for g in groups if g in auth.role_mapping]
    username = claim(claims, oidc.username_claim) or claims.get("email") or claims["sub"]
    return {"username": str(username), "name": claims.get("name"), "email": claims.get("email"),
            "groups": relevant, "sub": claims["sub"]}


# -- kontroll vid start ------------------------------------------------------------------------------


def check_config(auth, production: bool | None = None) -> list[str]:
    """Stoppa felkonfigurationer innan verktyget startar. Returnerar varningar; kastar ConfigError för fel."""
    production = os.environ.get("TERM_PRODUCTION") == "1" if production is None else production
    errors, warnings = [], []
    unknown = {r for roles in auth.role_mapping.values() for r in roles} | set(auth.default_roles)
    unknown -= ALL_ROLES
    if unknown:
        errors.append(f"Okända roller i auth: {', '.join(sorted(unknown))} (giltiga: {', '.join(ROLES)})")
    if auth.mode == "demo":
        if production:
            errors.append("Demoläget (användarval utan inloggning) är spärrat när TERM_PRODUCTION=1. "
                          "Använd auth.mode = \"oidc\" eller \"proxy\".")
        warnings.append("Demoläge: ingen riktig inloggning – använd bara för demo och test.")
    if auth.mode == "oidc":
        oidc = auth.oidc
        if oidc is None:
            errors.append("auth.mode = oidc kräver auth.oidc (issuer, client_id).")
        else:
            local = oidc.issuer.startswith(("http://localhost", "http://127.0.0.1"))
            if not oidc.issuer.startswith("https://") and not (local and not production):
                errors.append("IdP:ns issuer ska använda https.")
            if oidc.public_url and not oidc.public_url.startswith("https://") and production:
                errors.append("public_url ska använda https i produktion.")
        secret = os.environ.get("TERM_SESSION_SECRET", "")
        if len(secret) < 32:
            errors.append("TERM_SESSION_SECRET saknas eller är kortare än 32 tecken (sessionernas signeringsnyckel).")
        if not os.environ.get("TERM_OIDC_CLIENT_SECRET"):
            warnings.append("TERM_OIDC_CLIENT_SECRET saknas – fungerar bara om klienten är publik (PKCE utan hemlighet).")
    if auth.mode == "proxy":
        if any(t in ("0.0.0.0/0", "::/0") for t in auth.trusted_proxies):
            errors.append("auth.trusted_proxies får inte godta alla adresser – då kan vem som helst sätta "
                          "användarhuvudet.")
        if not os.environ.get("TERM_PROXY_SECRET"):
            warnings.append("TERM_PROXY_SECRET saknas – användarhuvudet godtas från betrodda adresser utan "
                            "delad hemlighet.")
    if errors:
        raise ConfigError("; ".join(errors))
    return warnings
