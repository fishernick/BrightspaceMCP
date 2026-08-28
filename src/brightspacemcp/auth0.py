"""Inbound OAuth 2.0 for the ``/mcp`` endpoint: Auth0 as the authorization
server, this process as an RFC 9728 protected resource.

Three responsibilities, per the design brief:

1. ``401`` + ``WWW-Authenticate: Bearer ... resource_metadata="…"`` when there is
   no valid token.
2. Serve ``/.well-known/oauth-protected-resource`` naming the Auth0 tenant as the
   authorization server and ``https://mcp.xennick.com/mcp`` as the resource.
3. Validate the bearer JWT (sig / iss / aud / exp / scope) on every call.

(1) and (2) are done by the MCP SDK the moment ``MCPServer`` is constructed with
``auth=auth_settings()`` and ``token_verifier=Auth0TokenVerifier()`` (see
``server.py``):

* ``RequireAuthMiddleware`` wraps ``/mcp`` and returns the ``401`` with a
  ``WWW-Authenticate: Bearer error="invalid_token", …, resource_metadata="https://mcp.xennick.com/.well-known/oauth-protected-resource/mcp"``
  header whenever :meth:`Auth0TokenVerifier.verify_token` returns ``None``.

The tenant is set by ``AUTH0_ISSUER`` (``.env``); the audience the token's
``aud`` must contain is ``AUTH0_AUDIENCE`` (the Auth0 API identifier).
* ``create_protected_resource_routes`` serves the RFC 9728 document at
  ``/.well-known/oauth-protected-resource/mcp``.  :func:`register_bare_metadata_route`
  additionally mirrors it at the path-less ``/.well-known/oauth-protected-resource``
  for clients that probe there.

(3) is this module's :class:`Auth0TokenVerifier`.
"""

from __future__ import annotations

import logging
import os

import anyio
import jwt
from dotenv import load_dotenv
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

load_dotenv()

logger = logging.getLogger(__name__)

# Auth0 signs access tokens with RS256; never accept anything else (in particular
# never "none", and never an HMAC alg that would let the JWKS modulus be replayed
# as a shared secret).
_ALGORITHMS = ["RS256"]

# Small clock-skew tolerance for exp / nbf / iat.
_LEEWAY_SECONDS = 60


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value else default


def issuer() -> str:
    """The Auth0 tenant issuer, e.g. ``https://dev-l23cfku2l8umnrhn.us.auth0.com/``.

    Auth0 always emits ``iss`` *with* the trailing slash and RFC 8414 issuer
    comparison is exact-string, so we normalise to that form. Override per
    environment with ``AUTH0_ISSUER``.
    """
    raw = _env("AUTH0_ISSUER", "https://dev-l23cfku2l8umnrhn.us.auth0.com/")
    return raw if raw.endswith("/") else raw + "/"


def audience() -> str:
    """The API identifier Auth0 mints tokens for == this resource's identifier."""
    return _env("AUTH0_AUDIENCE", "https://mcp.xennick.com/mcp")


def resource_url() -> str:
    """This protected resource's identifier (RFC 9728 ``resource``)."""
    return _env("MCP_RESOURCE_URL", audience())


def jwks_uri() -> str:
    return _env("AUTH0_JWKS_URI", f"{issuer()}.well-known/jwks.json")


def required_scopes() -> list[str]:
    """Scopes the SDK's ``RequireAuthMiddleware`` will enforce (``403
    insufficient_scope`` if absent).  Space-separated in ``AUTH0_REQUIRED_SCOPES``;
    empty by default so a bare valid token is accepted and scope is merely parsed
    and surfaced.  Set e.g. ``AUTH0_REQUIRED_SCOPES="mcp:invoke"`` to require one.
    """
    return _env("AUTH0_REQUIRED_SCOPES", "").split()


def _extract_scopes(claims: dict) -> list[str]:
    """OAuth scope from an Auth0 access token.

    Auth0 puts delegated scopes in the space-delimited ``scope`` string; some
    tenants / setups use ``scp`` (string or list); RBAC permissions land in
    ``permissions`` (list).  Merge them all, order-preserving and de-duped.
    """
    out: list[str] = []

    def _add(value: object) -> None:
        items = value.split() if isinstance(value, str) else value if isinstance(value, list) else []
        for item in items:
            if isinstance(item, str) and item and item not in out:
                out.append(item)

    _add(claims.get("scope"))
    _add(claims.get("scp"))
    _add(claims.get("permissions"))
    return out


class Auth0TokenVerifier(TokenVerifier):
    """Validates a bearer JWT against the Auth0 tenant on every ``/mcp`` call.

    Checks, in order: RS256 signature against the tenant JWKS (keys fetched once
    and cached, keyed by ``kid``, refreshed on rotation), ``iss`` exact-match,
    ``aud`` contains this resource, ``exp`` / ``nbf`` / ``iat`` (60s leeway), and
    that ``exp`` / ``iss`` / ``aud`` are actually present.  Any failure -> ``None``,
    which the SDK turns into the ``401`` + ``WWW-Authenticate`` challenge.
    """

    def __init__(
        self,
        *,
        issuer_url: str | None = None,
        audience_value: str | None = None,
        jwks_url: str | None = None,
        leeway: int = _LEEWAY_SECONDS,
    ) -> None:
        self.issuer = issuer_url or issuer()
        self.audience = audience_value or audience()
        self.leeway = leeway
        # PyJWKClient does blocking urllib I/O on cache miss; it caches the JWKS
        # for `lifespan` seconds and individual signing keys by kid.
        self._jwks = jwt.PyJWKClient(
            jwks_url or jwks_uri(),
            cache_keys=True,
            lifespan=600,
        )

    def _verify_sync(self, token: str) -> AccessToken | None:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=_ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception as exc:  # noqa: BLE001 - any failure is an auth failure
            logger.info("Bearer token rejected: %s: %s", type(exc).__name__, exc)
            return None

        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or claims.get("sub") or ""),
            scopes=_extract_scopes(claims),
            expires_at=int(claims["exp"]),
            resource=self.audience,
            subject=claims.get("sub"),
            claims=claims,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        # Keep the blocking JWKS fetch off the event loop.
        return await anyio.to_thread.run_sync(self._verify_sync, token)


def auth_settings() -> AuthSettings:
    """``AuthSettings`` that puts the SDK into resource-server-only mode: it will
    require a bearer token on ``/mcp`` and publish protected-resource metadata,
    but host no authorization endpoints of its own (Auth0 does that).
    """
    return AuthSettings(
        issuer_url=AnyHttpUrl(issuer()),
        resource_server_url=AnyHttpUrl(resource_url()),
        required_scopes=required_scopes() or None,
    )


def _metadata() -> ProtectedResourceMetadata:
    return ProtectedResourceMetadata(
        resource=AnyHttpUrl(resource_url()),
        authorization_servers=[AnyHttpUrl(issuer())],
        scopes_supported=required_scopes() or None,
    )


def register_bare_metadata_route(mcp) -> None:
    """Also answer the path-less ``/.well-known/oauth-protected-resource``.

    RFC 9728 §3.1 puts the document at ``/.well-known/oauth-protected-resource/mcp``
    for a resource whose identifier has a ``/mcp`` path, and that is the URL the
    ``WWW-Authenticate: resource_metadata=...`` hint points spec-compliant clients
    at.  This extra route is a compatibility shim for clients that only try the
    root path.  ``custom_route`` handlers are not behind ``RequireAuthMiddleware``.
    """

    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
    async def protected_resource_metadata(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
        return JSONResponse(
            _metadata().model_dump(mode="json", exclude_none=True),
            headers={
                "Cache-Control": "public, max-age=3600",
                "Access-Control-Allow-Origin": "*",
            },
        )


__all__ = [
    "Auth0TokenVerifier",
    "auth_settings",
    "register_bare_metadata_route",
    "issuer",
    "audience",
    "resource_url",
    "required_scopes",
]
