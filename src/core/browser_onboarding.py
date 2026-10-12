"""Local browser credential collection for the stdio installation principal."""

from __future__ import annotations

import asyncio
import html
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from src.core.credentials import OdooCredentials, store_odoo_credentials_file
from src.core.identity import Principal, PrincipalKind
from src.core.local_onboarding import transport_for_odoo_major
from src.core.onboarding import (
    OnboardingContinuation,
    OnboardingFailureCode,
    OnboardingResult,
    OnboardingStatus,
    OnboardingUnavailableError,
)
from src.mcp.odoo.connection.client import OdooClient


@dataclass
class _Flow:
    continuation: OnboardingContinuation
    result: OnboardingResult
    csrf: str
    submitting: bool = False


class LocalBrowserOnboardingProvider:
    """Lazy loopback listener; secrets enter through HTML, never through MCP."""

    def __init__(
        self,
        principal: Principal,
        *,
        client_factory: Callable | None = None,
        store_credentials: Callable | None = None,
        lifetime: timedelta = timedelta(minutes=10),
    ):
        if principal.kind is not PrincipalKind.LOCAL:
            raise ValueError("Browser onboarding requires a local installation principal.")
        self._principal = principal
        self._client_factory = client_factory or OdooClient
        self._store_credentials = store_credentials or store_odoo_credentials_file
        self._lifetime = lifetime
        self._flows: dict[str, _Flow] = {}
        self._lock = threading.Lock()
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _authorize(self, principal: Principal) -> None:
        # A remote caller or a different local identity must not obtain this
        # installation's credential collection/status endpoints.
        if principal != self._principal:
            raise OnboardingUnavailableError("Onboarding is unavailable for this principal.")

    def _flow(self, onboarding_id: str) -> _Flow | None:
        flow = self._flows.get(onboarding_id)
        if flow and datetime.now(UTC) >= flow.continuation.expires_at:
            if flow.result.status is OnboardingStatus.PENDING:
                flow.result = OnboardingResult(
                    onboarding_id, OnboardingStatus.EXPIRED, failure_code=OnboardingFailureCode.UNAVAILABLE
                )
        return flow

    async def begin(self, principal: Principal) -> OnboardingContinuation:
        self._authorize(principal)
        if self._http is None:
            self._loop = asyncio.get_running_loop()
            provider = self

            class Handler(BaseHTTPRequestHandler):
                # HTTP logs must never contain submitted values or form paths.
                def log_message(self, *_args):
                    pass

                def setup(self):
                    super().setup()
                    self.connection.settimeout(10)

                def do_GET(self):
                    provider._request(self, submit=False)

                def do_POST(self):
                    provider._request(self, submit=True)

            # Literal loopback binding and exact Host/Origin checks below block
            # LAN access, DNS rebinding, and cross-origin form submissions.
            try:
                self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            except OSError as exc:
                raise OnboardingUnavailableError("Local setup listener could not start.") from exc
            self._http.daemon_threads = True
            self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)
            self._thread.start()
        with self._lock:
            for onboarding_id in list(self._flows):
                flow = self._flow(onboarding_id)
                if flow and flow.result.status is OnboardingStatus.PENDING:
                    return flow.continuation
            # Bound retained completion state; IDs expire with this server process.
            while len(self._flows) >= 16:
                del self._flows[next(iter(self._flows))]
            onboarding_id = secrets.token_urlsafe(24)
            continuation = OnboardingContinuation(
                onboarding_id,
                f"{self._origin}/onboarding/{onboarding_id}",
                datetime.now(UTC) + self._lifetime,
            )
            self._flows[onboarding_id] = _Flow(
                continuation,
                OnboardingResult(onboarding_id, OnboardingStatus.PENDING),
                secrets.token_urlsafe(32),
            )
            return continuation

    @property
    def _origin(self) -> str:
        assert self._http is not None
        return f"http://127.0.0.1:{self._http.server_port}"

    async def get_result(self, principal: Principal, onboarding_id: str) -> OnboardingResult:
        self._authorize(principal)
        with self._lock:
            flow = self._flow(onboarding_id)
            if flow is None:
                raise OnboardingUnavailableError("Unknown onboarding request.")
            return flow.result

    def _reply(self, request: BaseHTTPRequestHandler, status: int, body: str) -> None:
        content = body.encode("utf-8")
        request.send_response(status)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Content-Length", str(len(content)))
        request.send_header("Cache-Control", "no-store")
        request.send_header("Referrer-Policy", "no-referrer")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        )
        request.end_headers()
        request.wfile.write(content)

    def _request(self, request: BaseHTTPRequestHandler, *, submit: bool) -> None:
        if request.headers.get_all("Host") != [self._origin.removeprefix("http://")]:
            self._reply(request, 403, "This setup page is only available on this computer.")
            return
        path = urlsplit(request.path)
        onboarding_id = path.path.removeprefix("/onboarding/")
        with self._lock:
            flow = self._flow(onboarding_id) if path.path.startswith("/onboarding/") else None
            if path.query or flow is None or flow.result.status is not OnboardingStatus.PENDING:
                self._reply(request, 410, "This setup request has ended. Ask Claude to create a new connection.")
                return
            if not submit:
                self._reply(request, 200, self._form(flow))
                return
            # Browsers attach Origin to form POSTs. Do not accept CORS, chunked
            # bodies, duplicate lengths, or unbounded credential payloads.
            if (
                request.headers.get_all("Origin") != [self._origin]
                or request.headers.get("Transfer-Encoding") is not None
                or len(request.headers.get_all("Content-Length", [])) != 1
                or request.headers.get_content_type() != "application/x-www-form-urlencoded"
            ):
                self._reply(request, 403, "Submit the form from this setup page.")
                return
        try:
            length = int(request.headers["Content-Length"])
            if not 0 < length <= 16384:
                raise ValueError
            fields = parse_qs(
                request.rfile.read(length).decode("utf-8"),
                strict_parsing=True,
                keep_blank_values=True,
                max_num_fields=6,
            )
            required = {"csrf", "url", "db", "username", "api_key"}
            if not required <= set(fields) or set(fields) - required - {"major"}:
                raise ValueError
            if any(len(values) != 1 for values in fields.values()):
                raise ValueError
            values = {key: value[0].strip() for key, value in fields.items()}
            if not all(values[key] for key in required):
                raise ValueError
        except (ValueError, UnicodeError, OSError):
            self._reply(request, 400, "Complete all fields with valid values.")
            return
        with self._lock:
            flow = self._flow(onboarding_id)
            if (
                flow is None
                or flow.result.status is not OnboardingStatus.PENDING
                or flow.submitting
                or not secrets.compare_digest(values.pop("csrf"), flow.csrf)
            ):
                self._reply(request, 403, "This form is no longer available.")
                return
            flow.submitting = True
        assert self._loop is not None
        # Authenticate on the serving event loop; the HTTP worker must not block
        # MCP status polling or bypass the transport's validation/safety policy.
        future = asyncio.run_coroutine_threadsafe(self._complete(onboarding_id, values), self._loop)
        try:
            ready = future.result(timeout=100)
        except Exception:
            future.cancel()
            with self._lock:
                if flow.result.status is OnboardingStatus.PENDING:
                    flow.result = OnboardingResult(
                        onboarding_id, OnboardingStatus.FAILED, failure_code=OnboardingFailureCode.VALIDATION_FAILED
                    )
            ready = False
        finally:
            values.clear()
        self._reply(
            request,
            200 if ready else 400,
            "Connection saved. Return to Claude and ask it to check the connection. You can close this tab."
            if ready
            else "Connection could not be saved. Check your details and Odoo version, "
            "then ask Claude to start setup again.",
        )

    async def _complete(self, onboarding_id: str, values: dict[str, str]) -> bool:
        client = None
        ready = False
        try:
            transport = transport_for_odoo_major(int(values.get("major") or "19"))
            url = values["url"].rstrip("/")
            parsed = urlsplit(url)
            # No credentials in URLs and no clear-text non-local HTTP targets.
            if (
                not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (
                    parsed.scheme != "https"
                    and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
                )
            ):
                raise ValueError
            credentials = OdooCredentials(url, values["db"], values["username"], values["api_key"])
            client = self._client_factory(credentials=credentials, transport=transport)
            await client.authenticate()
            with self._lock:
                flow = self._flow(onboarding_id)
                if flow is None or flow.result.status is not OnboardingStatus.PENDING:
                    return False
                # Persist only authenticated credentials and their per-database
                # transport, so a restart cannot revert an Odoo 19 profile to XML-RPC.
                self._store_credentials(
                    url, credentials.db, credentials.username, credentials.api_key, transport=transport
                )
                flow.result = OnboardingResult(
                    onboarding_id, OnboardingStatus.READY, profile_id=credentials.db, completed_at=datetime.now(UTC)
                )
                ready = True
        except Exception:
            # Neither HTTP responses nor MCP results may include upstream errors:
            # authentication libraries may echo the key or an Odoo response body.
            with self._lock:
                flow = self._flow(onboarding_id)
                if flow and flow.result.status is OnboardingStatus.PENDING:
                    flow.result = OnboardingResult(
                        onboarding_id, OnboardingStatus.FAILED, failure_code=OnboardingFailureCode.VALIDATION_FAILED
                    )
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
        return ready

    async def close(self) -> None:
        if self._http is not None:
            await asyncio.to_thread(self._http.shutdown)
            self._http.server_close()
            self._http = None
        with self._lock:
            self._flows.clear()

    @staticmethod
    def _form(flow: _Flow) -> str:
        csrf = html.escape(flow.csrf, quote=True)
        return f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connect Odoo</title>
<style>body{{font:17px system-ui;max-width:560px;margin:3rem auto;padding:0 1rem}}
label{{display:block;margin:1rem 0}}input{{display:block;width:100%;box-sizing:border-box;padding:.7rem}}
button{{padding:.8rem 1.2rem}}</style><h1>Connect your Odoo database</h1>
<p>This form runs on your computer. Enter your API key here, not in Claude chat.</p>
<form method="post"><input type="hidden" name="csrf" value="{csrf}">
<label>Odoo URL<input name="url" type="url" placeholder="https://yourcompany.odoo.com" required></label>
<label>Database name<input name="db" required></label>
<label>Login email<input name="username" type="email" autocomplete="username" required></label>
<label>Odoo major version<input name="major" type="number" min="1" value="19"></label>
<p>Transport is saved for this connection: XML-RPC for Odoo 18 and below, JSON-2 for Odoo 19+.
If you leave the version blank, Odoo 19 is assumed.</p>
<label>API key or Odoo password<input name="api_key" type="password" autocomplete="off" required></label>
<p>For Odoo 13, use your Odoo password. For Odoo 14+, use an API key.</p>
<p>Your connection will be checked before saving. Creating a connection does not enable writes.</p>
<button type="submit">Verify and connect</button></form></html>'''
