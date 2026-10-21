"""Host-owned X OAuth 2.0 PKCE and token storage.

The MCP process never accepts a token as a tool argument. It stores tokens in a
host-selected path outside the repository and exposes only account references
and expiry metadata to callers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


ACCOUNT_REF_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
DEFAULT_SCOPE = "tweet.read tweet.write users.read offline.access"
AUTHORIZATION_ENDPOINT = "https://api.x.com/2/oauth2/authorize"
TOKEN_ENDPOINT = "https://api.x.com/2/oauth2/token"


class OAuthError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def validate_account_ref(value: str) -> str:
    value = str(value).strip()
    if not ACCOUNT_REF_RE.fullmatch(value):
        raise OAuthError(
            "INVALID_ACCOUNT_REF",
            "account_ref must use 1-64 letters, digits, dot, underscore, or hyphen characters.",
        )
    return value


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthManager:
    def __init__(
        self,
        *,
        client_id: str | None,
        redirect_uri: str,
        store_path: str | Path,
        scope: str = DEFAULT_SCOPE,
        authorization_endpoint: str = AUTHORIZATION_ENDPOINT,
        token_endpoint: str = TOKEN_ENDPOINT,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.client_id = client_id.strip() if client_id else None
        self.redirect_uri = redirect_uri.strip()
        self.store_path = Path(store_path).expanduser()
        self.scope = scope
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self._now = now or time.time
        self._lock = threading.RLock()
        self._pending: dict[str, dict[str, Any]] = {}

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": "x-starter-oauth/v1", "accounts": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise OAuthError(
                "OAUTH_TOKEN_STORE_INVALID",
                "The host-owned OAuth token store cannot be read.",
                details={"reason": type(exc).__name__},
            ) from None
        if not isinstance(value, dict) or value.get("schema") != "x-starter-oauth/v1":
            raise OAuthError("OAUTH_TOKEN_STORE_INVALID", "Unsupported OAuth token store schema.")
        accounts = value.get("accounts")
        if not isinstance(accounts, dict):
            raise OAuthError("OAUTH_TOKEN_STORE_INVALID", "OAuth token store accounts must be an object.")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.store_path.with_name(
            f".{self.store_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        try:
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.store_path)
        except OSError as exc:
            raise OAuthError(
                "OAUTH_TOKEN_STORE_WRITE_FAILED",
                "The host-owned OAuth token store cannot be written.",
                details={"reason": type(exc).__name__},
            ) from None
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def start(self, account_ref: str) -> dict[str, Any]:
        if not self.client_id:
            raise OAuthError(
                "X_OAUTH_CONFIG_MISSING",
                "Set X_STARTER_X_CLIENT_ID before starting OAuth.",
            )
        account_ref = validate_account_ref(account_ref)
        if not self.redirect_uri:
            raise OAuthError("X_OAUTH_CONFIG_MISSING", "Set X_STARTER_OAUTH_REDIRECT_URI.")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        with self._lock:
            self._pending[state] = {
                "account_ref": account_ref,
                "verifier": verifier,
                "expires_at": self._now() + 600,
            }
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scope,
                "state": state,
                "code_challenge": _pkce_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        return {
            "account_ref": account_ref,
            "authorization_url": f"{self.authorization_endpoint}?{query}",
            "redirect_uri": self.redirect_uri,
            "expires_in": 600,
            "scopes": self.scope.split(),
            "disclosure": "Open the URL in the user's browser. The MCP server does not receive a password or authorization code in a tool argument.",
        }

    def callback(self, query: str) -> dict[str, Any]:
        params = parse_qs(urlparse(query).query, keep_blank_values=True)
        state = params.get("state", [""])[0]
        error = params.get("error", [""])[0]
        if error:
            raise OAuthError("X_OAUTH_DENIED", "The X authorization request was denied.", details={"error": error})
        code = params.get("code", [""])[0]
        if not state or not code:
            raise OAuthError("X_OAUTH_CALLBACK_INVALID", "The OAuth callback is missing code or state.")
        with self._lock:
            pending = self._pending.pop(state, None)
        if not pending or self._now() > float(pending["expires_at"]):
            raise OAuthError("X_OAUTH_STATE_INVALID", "The OAuth state is invalid or expired.")
        token = self._exchange_code(code, str(pending["verifier"]))
        account_ref = str(pending["account_ref"])
        self._save_token(account_ref, token)
        return {
            "account_ref": account_ref,
            "status": "connected",
            "scopes": str(token.get("scope", self.scope)).split(),
            "expires_in": token.get("expires_in"),
        }

    def _exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id or "",
                "code_verifier": verifier,
            }
        ).encode("utf-8")
        return self._token_request(body)

    def _refresh(self, refresh_token: str) -> dict[str, Any]:
        body = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id or "",
            }
        ).encode("utf-8")
        return self._token_request(body)

    def _token_request(self, body: bytes) -> dict[str, Any]:
        request = Request(
            self.token_endpoint,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                value = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise OAuthError(
                "X_OAUTH_TOKEN_EXCHANGE_FAILED",
                "X rejected the OAuth token exchange.",
                details={"status": exc.code},
            ) from None
        except (OSError, URLError, json.JSONDecodeError):
            raise OAuthError("X_OAUTH_TOKEN_EXCHANGE_FAILED", "The OAuth token exchange failed.") from None
        if not isinstance(value, dict) or not value.get("access_token"):
            raise OAuthError("X_OAUTH_TOKEN_EXCHANGE_FAILED", "X returned no access token.")
        return value

    def _save_token(self, account_ref: str, token: dict[str, Any]) -> None:
        now = self._now()
        record = {
            "access_token": str(token["access_token"]),
            "refresh_token": str(token.get("refresh_token", "")),
            "expires_at": now + int(token.get("expires_in", 7200)),
            "scope": str(token.get("scope", self.scope)),
            "token_type": str(token.get("token_type", "bearer")),
            "updated_at": now,
        }
        with self._lock:
            value = self._read()
            value["accounts"][account_ref] = record
            self._write(value)

    def access_token(self, account_ref: str) -> str:
        account_ref = validate_account_ref(account_ref)
        with self._lock:
            value = self._read()
            record = value["accounts"].get(account_ref)
        if not isinstance(record, dict) or not record.get("access_token"):
            raise OAuthError("X_AUTH_REQUIRED", "Connect this X account with OAuth before calling a write tool.")
        if float(record.get("expires_at", 0)) - self._now() > 60:
            return str(record["access_token"])
        refresh_token = str(record.get("refresh_token", ""))
        if not refresh_token:
            raise OAuthError("X_REAUTH_REQUIRED", "The X access token expired and has no refresh token.")
        token = self._refresh(refresh_token)
        if not token.get("refresh_token"):
            token["refresh_token"] = refresh_token
        self._save_token(account_ref, token)
        return str(token["access_token"])

    def status(self) -> dict[str, Any]:
        with self._lock:
            accounts = self._read().get("accounts", {})
        result = []
        for account_ref, record in accounts.items():
            if not isinstance(record, dict):
                continue
            result.append(
                {
                    "account_ref": account_ref,
                    "expires_at": record.get("expires_at"),
                    "scopes": str(record.get("scope", "")).split(),
                    "expired": float(record.get("expires_at", 0)) <= self._now(),
                }
            )
        return {
            "client_id_configured": bool(self.client_id),
            "redirect_uri": self.redirect_uri,
            "token_store_path": str(self.store_path),
            "accounts": result,
        }

    def disconnect(self, account_ref: str) -> dict[str, Any]:
        account_ref = validate_account_ref(account_ref)
        with self._lock:
            value = self._read()
            existed = account_ref in value["accounts"]
            value["accounts"].pop(account_ref, None)
            if existed:
                self._write(value)
        return {"account_ref": account_ref, "removed": existed, "token_revoked": False}
