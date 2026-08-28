"""Static API-key verification for inbound MCP client connections.

The MCP endpoint used to be unauthenticated: anything that could reach
``https://mcp.xennick.com/mcp`` could drive the operator's Brightspace account.
This module turns the endpoint into a token-authenticated one without pulling in
an external identity provider: the operator generates one or more API keys, puts
them in ``.env``, and hands them to the MCP clients that are allowed in. Every
``/mcp`` request must then present one as ``Authorization: Bearer <key>``.

There is no issuer, no JWKS, no token expiry, and no network call on the auth
path - :meth:`StaticApiKeyVerifier.verify_token` just constant-time-compares the
presented key against the configured set. The MCP SDK needs an ``AuthSettings``
to enable a ``token_verifier`` at all, but :func:`build_auth_settings` leaves
``resource_server_url`` unset so the SDK advertises *no* OAuth 2.0
protected-resource metadata: a 401 carries a bare ``WWW-Authenticate: Bearer``
with no discovery pointer, and clients just send the static key they were
handed instead of trying to find an authorization server that does not exist.

Generate a key with ``brightspacemcp-keygen`` (or ``python -m
brightspacemcp.apikey``); it prints both the raw key to give the client and the
``sha256:`` digest form to store in ``.env`` so the plaintext never lands on
disk. Raw values work too.

Configuration comes from the environment / ``.env`` (``brightspacemcp.auth``
has already called ``load_dotenv()`` by the time this runs):

================ ===============================================================
MCP_API_KEYS     Comma/space-separated list of accepted keys. Each entry is
                 either a raw key or ``sha256:<hex>`` (the digest of the raw
                 key). Setting this to a non-empty value is what turns
                 verification on; unset means the endpoint stays open (a startup
                 warning is logged).
MCP_RESOURCE_URL Public URL of this MCP endpoint, used as the resource
                 identifier. Default ``https://mcp.xennick.com/mcp``.
================ ===============================================================
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sys

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

logger = logging.getLogger(__name__)

DEFAULT_RESOURCE_URL = "https://mcp.xennick.com/mcp"

_SHA256_PREFIX = "sha256:"


def _split(value: str | None) -> list[str]:
    """Split a comma- or whitespace-separated env value into a clean list."""
    if not value:
        return []
    return [part for part in value.replace(",", " ").split() if part]


def _resource_url() -> str:
    return os.getenv("MCP_RESOURCE_URL", DEFAULT_RESOURCE_URL)


def _configured_keys() -> list[str]:
    """The raw ``MCP_API_KEYS`` entries (raw keys and/or ``sha256:`` digests)."""
    return _split(os.getenv("MCP_API_KEYS"))


def api_key_auth_enabled() -> bool:
    """True when at least one API key is configured, i.e. tokens are required."""
    return bool(_configured_keys())


def build_auth_settings() -> AuthSettings | None:
    """The SDK ``AuthSettings`` needed to enable a token verifier, or None.

    Passed straight to ``MCPServer(auth=...)``. ``resource_server_url`` is left
    as ``None`` on purpose: that suppresses the SDK's
    ``/.well-known/oauth-protected-resource`` document and the
    ``resource_metadata=...`` hint in the 401 ``WWW-Authenticate`` header, so a
    spec-compliant MCP client does *not* try to discover an OAuth authorization
    server (there is none) and instead just sends the static key it was given.
    ``issuer_url`` is required by the model but, with no resource metadata and no
    auth-server provider, is never advertised anywhere.
    """
    if not api_key_auth_enabled():
        return None
    return AuthSettings(
        issuer_url=_resource_url(),
        resource_server_url=None,
        required_scopes=None,
    )


def hash_key(key: str) -> str:
    """The ``sha256:<hex>`` digest form of a raw key, as stored in ``.env``."""
    return _SHA256_PREFIX + hashlib.sha256(key.encode()).hexdigest()


class StaticApiKeyVerifier(TokenVerifier):
    """Verify a presented bearer token against the configured API keys.

    Each configured entry is compared in constant time: raw entries against the
    token itself, ``sha256:`` entries against the token's digest. No network
    call, no expiry - a key is valid until removed from ``MCP_API_KEYS``.
    """

    def __init__(self) -> None:
        raw: list[str] = []
        digests: list[str] = []
        for entry in _configured_keys():
            if entry.startswith(_SHA256_PREFIX):
                digests.append(entry[len(_SHA256_PREFIX) :].strip().lower())
            else:
                raw.append(entry)
        self._raw = raw
        self._digests = digests
        self._resource = _resource_url()

    def _matches(self, token: str) -> bool:
        token_digest = hashlib.sha256(token.encode()).hexdigest()
        # OR-accumulate rather than short-circuit so the work does not depend on
        # which key (if any) matched.
        found = False
        for candidate in self._raw:
            found |= hmac.compare_digest(token, candidate)
        for candidate in self._digests:
            found |= hmac.compare_digest(token_digest, candidate)
        return found

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not self._matches(token):
            logger.info("Rejected bearer token: no matching API key")
            return None
        return AccessToken(
            token=token,
            client_id="api-key",
            scopes=[],
            expires_at=None,
            resource=self._resource,
        )


def build_token_verifier() -> TokenVerifier | None:
    """The ``TokenVerifier`` for ``MCPServer(token_verifier=...)``, or None."""
    if not api_key_auth_enabled():
        return None
    return StaticApiKeyVerifier()


def new_key() -> str:
    """A fresh URL-safe API key (~43 chars, 256 bits of entropy)."""
    return secrets.token_urlsafe(32)


def _main() -> None:
    """``brightspacemcp-keygen``: print a new key and its ``.env`` digest line."""
    key = new_key()
    sys.stdout.write(
        "New Brightspace MCP API key:\n\n"
        f"  {key}\n\n"
        "Give that to the MCP client. Add this line to .env (the raw key is\n"
        "never stored on the server):\n\n"
        f"  MCP_API_KEYS={hash_key(key)}\n\n"
        "Append more keys comma-separated to authorize additional clients.\n"
    )


if __name__ == "__main__":
    _main()
