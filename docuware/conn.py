from __future__ import annotations
import logging
import math
import tempfile
import time
from pathlib import Path
import requests
import urllib.parse as urlparse
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

from docuware import cijson, errors, parser, utils

from urllib3.exceptions import InsecureRequestWarning
from urllib3 import disable_warnings

from docuware.organization import Organization
disable_warnings(InsecureRequestWarning)


log = logging.getLogger(__name__)


DEFAULT_HEADERS = {
    "User-Agent": "Python docuware-client",
}

JSON_HEADERS = {
    "Accept": "application/json",
}

TEXT_HEADERS = {
    "Accept": "text/plain",
}

class Authenticator(ABC):
    def is_token_expired(self) -> bool:
        """Cookie authenticators have no access-token expiry metadata."""
        return False

    def relogin(self, conn: Connection) -> dict:
        """Reauthenticate using the credentials retained by the authenticator."""
        return self.login(conn)

    @abstractmethod
    def authenticate(self, conn: Connection) -> requests.Session:
        ...

    @abstractmethod
    def login(self, conn: Connection) -> dict:
        ...

    @abstractmethod
    def logoff(self, conn: Connection) -> None:
        ...

    def _get(self, conn: Connection, path: str) -> dict:
        url = conn.make_url(path)
        resp = conn.session.get(url, headers={**DEFAULT_HEADERS, **JSON_HEADERS})
        if resp.status_code == 200:
            return resp.json(object_hook=conn._json_object_hook)
        raise errors.ResourceError("Failed to get resource", url=url, status_code=resp.status_code)

    def _post(
        self,
        conn: Connection,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        data:Optional[Any] = None
    ) -> dict:
        url = conn.make_url(path)
        headers = {**DEFAULT_HEADERS, **(headers or {}), **JSON_HEADERS}
        resp = conn.session.post(url, headers=headers, data=data)
        if resp.status_code == 200:
            return resp.json(object_hook=conn._json_object_hook)
        raise errors.ResourceError("Failed to post to resource", url=url, status_code=resp.status_code)

class CookieAuthenticator(Authenticator):
    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        organization: Optional[str] = None,
        saved_state: Optional[dict] = None,
    ):
        self.password = password
        self.username = username
        self.organization = organization
        self.cookies = saved_state
        self.result: Optional[dict] = None
        self._warn = True

    def authenticate(self, conn: Connection) -> requests.Session:
        if self.cookies:
            log.debug("Authenticating with cookies")
            conn.session.cookies.update(self.cookies)
        else:
            if self._warn:
                log.warning("Cookie authentication not available")
                self._warn = False
        return conn.session

    def login(self, conn: Connection) -> dict:
        endpoint = "/DocuWare/Platform/Account/Logon"

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {
            "LoginType": "DocuWare",
            "RedirectToMyselfInCaseOfError": "false",
            "RememberMe": "false",
            "Password": self.password,
            "UserName": self.username,
        }
        if self.organization:
            data["Organization"] = self.organization

        try:
            self.result = self._post(conn, endpoint, headers=headers, data=data)
            self.cookies = requests.utils.dict_from_cookiejar(conn.session.cookies)
            return self.cookies
        except errors.ResourceError as exc:
            raise errors.AccountError(f"Log in failed with code {exc.status_code}")

    def logoff(self, conn: Connection) -> None:
        self._get(conn, "/DocuWare/Platform/Account/Logoff")


class OAuth2Authenticator(Authenticator):
    def __init__(
        self,
        username: Optional[str],
        password: Optional[str],
        organization: Optional[str] = None,
        saved_state: Optional[dict] = None,
    ):
        self.password = password
        self.username = username
        self.organization = organization
        self.token = (saved_state or {}).get("access_token")
        self.acquired_at = (saved_state or {}).get("acquired_at")
        self.expires_in = (saved_state or {}).get("expires_in")
        self.result: dict = {}

    def is_token_expired(self) -> bool:
        """Unknown, invalid, future-dated, and missing token state is expired."""
        if not isinstance(self.token, str) or not self.token:
            return True
        values = (self.acquired_at, self.expires_in)
        try:
            invalid = any(isinstance(value, bool) or not isinstance(value, (int, float))
                          or not math.isfinite(value) or value < 0 for value in values)
        except OverflowError:
            return True
        if invalid:
            return True
        now = time.time()
        return now < self.acquired_at or now >= self.acquired_at + self.expires_in

    def _saved_state(self) -> dict:
        return {"access_token": self.token, "acquired_at": self.acquired_at,
                "expires_in": self.expires_in}

    def _apply_access_token(self, conn: Connection, token: Optional[str]) -> None:
        if token:
            conn.session.headers.update({
               "Authorization": f"Bearer {token}"
            })
        else:
            if "Authorization" in conn.session.headers:
                del conn.session.headers["Authorization"]

    def _get_access_token(self, conn: Connection) -> str:
        log.debug("Requesting access token")
        try:
            acquired_at = time.time()
            # According to https://support.docuware.com/en-us/knowledgebase/article/KBA-37505:
            # Step 1: Get responsible Identity Service
            res = self._get(conn, "/DocuWare/Platform/Home/IdentityServiceInfo")

            # Step 2: Get Identity Service Configuration
            path = f"{res.get('IdentityServiceUrl', '').rstrip('/')}/.well-known/openid-configuration"
            res = self._get(conn, path)

            # Step 3: Obtain an Access Token
            path = res.get("token_endpoint") or "/DocuWare/Identity/connect/token"
            data = {
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "client_id": "docuware.platform.net.client",
                "scope": "docuware.platform"
            }
            self.result = self._post(conn, path, data=data)
            token = self.result.get("access_token")
            if not isinstance(token, str) or not token:
                raise errors.AccountError("Token response is missing an access token")
            self.acquired_at = acquired_at
            self.expires_in = self.result.get("expires_in")
            return token
        except errors.ResourceError as exc:
            raise errors.AccountError("Access-token authentication failed",
                                      status_code=exc.status_code) from None
        except (ValueError, TypeError, AttributeError, requests.exceptions.InvalidJSONError):
            # Requests JSONDecodeError is also a RequestException, but is a bad
            # token response rather than a retryable network failure.
            raise errors.AccountError("Access-token authentication failed") from None
        except requests.RequestException as exc:
            # Preserve timeout/connection classification for callers' retry policy,
            # but discard request objects and provider/transport diagnostic text.
            raise type(exc)("Access-token transport failed") from None

    def authenticate(self, conn: Connection) -> requests.Session:
        # A failed login must not leave a stale bearer header available for reuse.
        self.token = None
        self.acquired_at = None
        self.expires_in = None
        self.result = {}
        self._apply_access_token(conn, None)
        self.token = self._get_access_token(conn)
        self._apply_access_token(conn, self.token)
        return conn.session

    def login(self, conn: Connection) -> dict:
        if self.is_token_expired():
            conn.session = self.authenticate(conn)
        else:
            self._apply_access_token(conn, self.token)
        return self._saved_state()

    def relogin(self, conn: Connection) -> dict:
        """Force a single login, even if the current token has not expired."""
        conn.session = self.authenticate(conn)
        return self._saved_state()

    def logoff(self, conn: Connection) -> None:
        if self.token:
            # FIXME: How to revoke an access token?
            #self._get(conn, "/DocuWare/Identity/connect/revocation")
            self.token = None
            self.acquired_at = None
            self.expires_in = None
            self._apply_access_token(conn, None)


class Connection:
    def __init__(
        self,
        base_url: str,
        case_insensitive: bool = True,
        verify_certificate: bool = True,
        authenticator: Optional[Authenticator] = None,
    ):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.verify = verify_certificate
        self.authenticator = authenticator
        self._json_object_hook = cijson.case_insensitive_hook if case_insensitive else None

    def is_token_expired(self) -> bool:
        """Return OAuth expiry, or True when no authenticator is configured."""
        return self.authenticator is None or self.authenticator.is_token_expired()

    def relogin(self) -> dict:
        """Explicit reauthentication; callers own spacing and lockout policy."""
        if self.authenticator is None:
            raise errors.AccountError("No authenticator configured")
        return self.authenticator.relogin(self)

    def make_path(self, path: str, query: dict) -> str:
        u = urlparse.urlsplit(path)
        q = "&".join(
            ([u.query] if u.query else []) +
            [f"{urlparse.quote_plus(k)}={urlparse.quote_plus(v)}" for k, v in query.items()])
        return urlparse.urlunsplit(u._replace(query=q))

    def make_url(self, path: str, query: Optional[dict] = None) -> str:
        if query:
            path = self.make_path(path, query)
        return urlparse.urljoin(self.base_url, path)

    def _post(self, url: str, headers: Optional[Dict[str, str]] = None, json: Optional[dict] = None, data: Optional[Any] = None):
        headers = {**DEFAULT_HEADERS, **headers} if headers else DEFAULT_HEADERS
        resp = self.session.post(url, headers=headers, json=json, data=data)
        if resp.status_code in (401, 403) and self.authenticator:
            self.session = self.authenticator.authenticate(self)
            resp = self.session.post(url, headers=headers, json=json, data=data)
        return resp

    def post(self, path: str, headers: Optional[Dict[str, str]] = None, json: Optional[dict] = None, data: Optional[Any] = None):
        url = self.make_url(path)
        resp = self._post(url, headers=headers, json=json, data=data)
        if resp.status_code == 200:
            return resp
        else:
            raise errors.ResourceError(
                f"POST request failed with code {resp.status_code}",
                url=url,
                status_code=resp.status_code
            )

    def post_json(self, path: str, headers: Optional[Dict[str, str]] = None, json: Optional[dict] = None, data: Optional[Any] = None):
        headers = {**headers, **JSON_HEADERS} if headers else JSON_HEADERS
        return self.post(path, headers=headers, json=json, data=data).json(object_hook=self._json_object_hook)

    def post_text(self, path: str, headers: Optional[Dict[str, str]] = None, json: Optional[dict] = None, data: Optional[Any] = None) -> str:
        headers = {**headers, **TEXT_HEADERS} if headers else TEXT_HEADERS
        return self.post(path, headers=headers, json=json, data=data).text

    def _put(self, url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Any] = None, json: Optional[dict] = None, data: Optional[Any] = None):
        headers = {**DEFAULT_HEADERS, **headers} if headers else DEFAULT_HEADERS
        resp = self.session.put(url, headers=headers, params=params, json=json, data=data)
        if resp.status_code in (401, 403) and self.authenticator:
            self.session = self.authenticator.authenticate(self)
            resp = self.session.put(url, headers=headers, params=params, json=json, data=data)
        return resp

    def put(self, path: str, headers: Optional[Dict[str, str]] = None, params: Optional[Any] = None, json: Optional[dict] = None, data: Optional[Any] = None):
        url = self.make_url(path)
        resp = self._put(url, headers=headers, params=params, json=json, data=data)
        if resp.status_code == 200:
            return resp
        else:
            raise errors.ResourceError(
                f"PUT request failed with code {resp.status_code} and message \'{resp.content}\'",
                url=url,
                status_code=resp.status_code
            )

    def put_json(self, path: str, headers: Optional[Dict[str, str]] = None, params: Optional[Any] = None, json: Optional[dict] = None,
                 data: Optional[Any] = None):
        headers = {**headers, **JSON_HEADERS} if headers else JSON_HEADERS
        return self.put(path, headers=headers, params=params, json=json, data=data).json(
            object_hook=self._json_object_hook)

    def put_text(self, path: str, headers: Optional[Dict[str, str]] = None, params: Optional[Any] = None, json: Optional[dict] = None,
                 data: Optional[Any] = None):
        headers = {**headers, **TEXT_HEADERS} if headers else TEXT_HEADERS
        return self.put(path, headers=headers, params=params, json=json, data=data).text

    def _get(self, url: str, headers: Optional[Dict[str, str]] = None,
             data: Optional[Any] = None, *, stream: bool = False):
        headers = {**DEFAULT_HEADERS, **headers} if headers else DEFAULT_HEADERS
        options = {"stream": True, "timeout": (10, 60)} if stream else {}
        resp = self.session.get(url, headers=headers, data=data, **options)
        if resp.status_code in (401, 403) and self.authenticator:
            resp.close()
            self.session = self.authenticator.authenticate(self)
            resp = self.session.get(url, headers=headers, data=data, **options)
        return resp

    def get(self, path: str, headers: Optional[Dict[str, str]] = None, data: Optional[Any] = None):
        url = self.make_url(path)
        resp = self._get(url, headers=headers, data=data)
        if resp.status_code == 200:
            return resp
        else:
            raise errors.ResourceError(
                f"GET request failed with code {resp.status_code}",
                url=url,
                status_code=resp.status_code
            )

    def get_json(self, path: str, headers: Optional[Dict[str, str]] = None):
        headers = {**headers, **JSON_HEADERS} if headers else JSON_HEADERS
        return self.get(path, headers=headers).json(object_hook=self._json_object_hook)

    def get_text(self, path: str, headers: Optional[Dict[str, str]] = None):
        headers = {**headers, **TEXT_HEADERS} if headers else TEXT_HEADERS
        return self.get(path, headers=headers).text

    def _delete(self, url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Any] = None, json: Optional[dict] = None,
                data: Optional[Any] = None):
        headers = {**DEFAULT_HEADERS, **headers} if headers else DEFAULT_HEADERS
        resp = self.session.delete(url, headers=headers, params=params, json=json, data=data)
        if resp.status_code in (401, 403) and self.authenticator:
            self.session = self.authenticator.authenticate(self)
            resp = self.session.delete(url, headers=headers, params=params, json=json, data=data)
        return resp

    def delete(self, path: str, headers: Optional[Dict[str, str]] = None):
        url = self.make_url(path)
        resp = self._delete(url, headers=headers)
        if resp.status_code == 200:
            return resp
        else:
            raise errors.ResourceError(
                f"DELETE request failed with code {resp.status_code}",
                url=url,
                status_code=resp.status_code
            )

    def get_bytes(self, path: str, mime_type: Optional[str] = None, data: Optional[Any] = None) -> Tuple[bytes, str, str]:
        url = self.make_url(path)
        resp = self._get(url, headers={"Accept": mime_type if mime_type else "*/*"}, data=data)
        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            content_length = resp.headers.get("Content-Length")
            content_disposition = parser.parse_content_disposition(resp.headers.get("Content-Disposition"))
            if content_length and len(resp.content) != int(content_length):
                raise errors.ResourceError(
                    f"Unexpected content length: expected {content_length}, got {len(resp.content)}",
                    url=url, status_code=resp.status_code)
            return resp.content, content_type, content_disposition.get("filename", "unknown.bin")
        raise errors.ResourceNotFoundError(
            f"Download failed, code {resp.status_code}",
            url=url, status_code=resp.status_code)

    def stream_to_file(self, url: str, dest: Path, *,
                       expected_size: Optional[int] = None) -> Path:
        """Stream identity-encoded bytes; atomically replace dest only on success.

        The parent directory must exist. Symlink destinations are rejected; any
        existing regular file remains intact on HTTP, size, or write failure.
        """
        if expected_size is not None and (
                isinstance(expected_size, bool) or not isinstance(expected_size, int)
                or expected_size < 0):
            raise ValueError("expected_size must be a non-negative integer or None")
        dest = Path(dest)
        if dest.is_symlink():
            raise ValueError("Symlink destinations are not supported")
        url = self.make_url(url)
        response = self._get(url, headers={"Accept": "*/*", "Accept-Encoding": "identity"},
                             stream=True)
        temporary = None
        try:
            if response.status_code != 200:
                raise errors.ResourceError("Stream download failed", url=url,
                                           status_code=response.status_code)
            # iter_content transparently decompresses, while Content-Length counts
            # encoded bytes. Require identity instead of silently comparing unlike sizes.
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise errors.ResourceError("Stream download requires identity encoding", url=url)
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                if not content_length.isascii() or not content_length.isdecimal():
                    raise errors.ResourceError("Invalid Content-Length", url=url)
                content_length = int(content_length)
            sizes = [size for size in (content_length, expected_size) if size is not None]
            if len(set(sizes)) > 1:
                raise errors.ResourceError("Content-Length disagrees with expected_size", url=url)
            transferred = 0
            with tempfile.NamedTemporaryFile(mode="wb", dir=dest.parent,
                                             prefix=f".{dest.name}.", delete=False) as output:
                temporary = Path(output.name)
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    transferred += len(chunk)
                    if sizes and transferred > sizes[0]:
                        raise errors.ResourceError("Downloaded size exceeds expected length", url=url)
                    output.write(chunk)
            if sizes and transferred != sizes[0]:
                raise errors.ResourceError(
                    f"Unexpected content length: expected {sizes[0]}, got {transferred}", url=url)
            if dest.is_symlink():
                raise ValueError("Symlink destinations are not supported")
            temporary.replace(dest)
            temporary = None
            return dest
        finally:
            try:
                response.close()
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

# vim: set et sw=4 ts=4:
