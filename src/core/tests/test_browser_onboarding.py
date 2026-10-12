"""Browser flow qualification uses real loopback HTTP and fake Odoo auth only."""

import asyncio
import json
import os
import re
from datetime import timedelta

import httpx
import pytest
from playwright.async_api import async_playwright

from mcp import Client
from src.core import browser_onboarding, credentials
from src.core.browser_onboarding import LocalBrowserOnboardingProvider
from src.core.identity import LocalInstallationPrincipalProvider
from src.core.onboarding import OnboardingFailureCode, OnboardingStatus, OnboardingUnavailableError
from src.mcp.odoo import server


class FakeOdooClient:
    instances = []
    reject = False

    def __init__(self, *, credentials=None, transport=None):
        self.credentials = credentials or globals()["credentials"].get_odoo_credentials()
        self.transport_name = transport or self.credentials.transport or "xmlrpc"
        self.db = self.credentials.db
        self.closed = False
        self.instances.append(self)

    async def authenticate(self):
        if self.reject:
            raise RuntimeError("private body: synthetic-secret-key")
        return 7

    async def get_current_user(self):
        return {"id": 7, "name": "Test user"}

    async def close(self):
        self.closed = True


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "CONFIG_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr(server, "APPROVAL_PATH", tmp_path / "approvals.sqlite3")
    monkeypatch.setattr(browser_onboarding, "OdooClient", FakeOdooClient)
    monkeypatch.setattr(server, "OdooClient", FakeOdooClient)
    monkeypatch.setattr(FakeOdooClient, "instances", [])
    monkeypatch.setattr(FakeOdooClient, "reject", False)
    monkeypatch.setenv("ODOO_TRANSPORT", "xmlrpc")
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "0")


async def submit(http, form_url, *, db="demo", major="19", **overrides):
    form = await http.get(form_url)
    assert form.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', form.text).group(1)
    fields = dict(
        csrf=csrf,
        url="https://example.odoo.com",
        db=db,
        username="user@example.com",
        major=major,
        api_key="synthetic-secret-key",
    )
    fields.update(overrides)
    if major is None:
        fields.pop("major")
    return await http.post(form_url, data=fields, headers={"Origin": str(httpx.URL(form_url).copy_with(path=""))})


@pytest.mark.asyncio
async def test_real_mcp_start_form_poll_ping_and_switch_multiple_databases(isolated):
    async with Client(server.mcp, mode="legacy") as mcp:
        runtime = (await mcp.call_tool("odoo_runtime_info", {})).structured_content
        assert runtime["credential_onboarding_available"] is True
        assert runtime["writes_currently_allowed"] is False
        async with httpx.AsyncClient(trust_env=False) as http:
            for db, major, transport in [("old", "13", "xmlrpc"), ("new", "", "json2")]:
                begin = (await mcp.call_tool("odoo_setup_credentials", {"input": {}})).structured_content
                assert "synthetic-secret-key" not in json.dumps(begin)
                response = await submit(http, begin["url"], db=db, major=major)
                assert response.status_code == 200
                assert "synthetic-secret-key" not in response.text
                status = (
                    await mcp.call_tool(
                        "odoo_setup_credentials",
                        {
                            "input": {"onboarding_id": begin["onboarding_id"]},
                        },
                    )
                ).structured_content
                assert status["status"] == "ready"
                assert (await mcp.call_tool("odoo_ping", {})).structured_content["result"]["db"] == db
                assert (await mcp.call_tool("odoo_runtime_info", {})).structured_content["odoo_transport"] == transport
            databases = (await mcp.call_tool("odoo_list_databases", {})).structured_content
            assert set(databases["databases"]) == {"old", "new"}
            await mcp.call_tool("odoo_switch_database", {"input": {"db": "old"}})
            assert (await mcp.call_tool("odoo_ping", {})).structured_content["result"]["db"] == "old"
            assert (await mcp.call_tool("odoo_runtime_info", {})).structured_content["odoo_transport"] == "xmlrpc"
    assert all(client.closed for client in FakeOdooClient.instances)
    assert credentials.CONFIG_PATH.stat().st_mode & 0o777 == 0o600
    assert credentials.get_odoo_credentials("new").transport == "json2"
    assert credentials.get_odoo_credentials("old").transport == "xmlrpc"


@pytest.mark.asyncio
async def test_browser_rejects_cross_origin_host_csrf_and_replay(isolated):
    principal = await LocalInstallationPrincipalProvider("test").resolve()
    provider = LocalBrowserOnboardingProvider(principal)
    try:
        flow = await provider.begin(principal)
        assert await provider.begin(principal) == flow
        async with httpx.AsyncClient(trust_env=False) as http:
            assert (await http.get(flow.url, headers={"Host": "attacker.example"})).status_code == 403
            assert (
                await http.post(flow.url, data={"api_key": "secret"}, headers={"Origin": "https://attacker.example"})
            ).status_code == 403
            assert (await submit(http, flow.url, csrf="wrong")).status_code == 403
            assert not credentials.CONFIG_PATH.exists()
            assert (await submit(http, flow.url)).status_code == 200
            assert (await http.get(flow.url)).status_code == 410
        other = await LocalInstallationPrincipalProvider("other").resolve()
        with pytest.raises(OnboardingUnavailableError):
            await provider.get_result(other, flow.onboarding_id)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_auth_failure_preserves_profiles_and_never_echoes_secret(isolated, monkeypatch, capsys):
    credentials.store_odoo_credentials_file("https://old.example", "old", "user", "old-key", transport="xmlrpc")
    previous = credentials.CONFIG_PATH.read_bytes()
    monkeypatch.setattr(FakeOdooClient, "reject", True)
    principal = await LocalInstallationPrincipalProvider("test").resolve()
    provider = LocalBrowserOnboardingProvider(principal)
    try:
        flow = await provider.begin(principal)
        async with httpx.AsyncClient(trust_env=False) as http:
            response = await submit(http, flow.url)
        assert response.status_code == 400
        assert "synthetic-secret-key" not in response.text
        result = await provider.get_result(principal, flow.onboarding_id)
        assert result.status is OnboardingStatus.FAILED
        assert result.failure_code is OnboardingFailureCode.AUTHENTICATION_FAILED
        assert "authentication_failed" in response.text
        assert credentials.CONFIG_PATH.read_bytes() == previous
        assert "synthetic-secret-key" not in json.dumps(result.to_public_dict())
        assert "synthetic-secret-key" not in str(capsys.readouterr())
        assert all(client.closed for client in FakeOdooClient.instances)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_expired_forms_cannot_save_and_listeners_are_isolated(isolated):
    principal = await LocalInstallationPrincipalProvider("test").resolve()
    providers = [
        LocalBrowserOnboardingProvider(principal, lifetime=timedelta(seconds=-1)),
        LocalBrowserOnboardingProvider(principal),
    ]
    try:
        expired, other = [await p.begin(principal) for p in providers]
        assert httpx.URL(expired.url).port != httpx.URL(other.url).port
        async with httpx.AsyncClient(trust_env=False) as http:
            assert (await http.get(expired.url)).status_code == 410
        assert (await providers[0].get_result(principal, expired.onboarding_id)).status is OnboardingStatus.EXPIRED
        assert not credentials.CONFIG_PATH.exists()
    finally:
        await asyncio.gather(*(p.close() for p in providers))


def test_unreadable_credential_store_is_not_replaced(isolated):
    credentials.CONFIG_PATH.write_text("broken-json")
    with pytest.raises(RuntimeError):
        credentials.store_odoo_credentials_file("https://example.com", "new", "user", "key", transport="json2")
    assert credentials.CONFIG_PATH.read_text() == "broken-json"


@pytest.mark.asyncio
@pytest.mark.parametrize("major", ["", None])
async def test_browser_defaults_blank_or_absent_version_to_odoo_19(isolated, major):
    principal = await LocalInstallationPrincipalProvider("test").resolve()
    provider = LocalBrowserOnboardingProvider(principal)
    try:
        flow = await provider.begin(principal)
        async with httpx.AsyncClient(trust_env=False) as http:
            assert (await submit(http, flow.url, major=major)).status_code == 200
        assert credentials.get_odoo_credentials("demo").transport == "json2"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_native_browser_submission_stores_locally_without_synthetic_origin_header(isolated):
    principal = await LocalInstallationPrincipalProvider("test").resolve()
    provider = LocalBrowserOnboardingProvider(principal)
    try:
        flow = await provider.begin(principal)
        async with async_playwright() as playwright:
            # CI uses bundled Chromium; local QA may supply installed Chrome.
            # Always use a new headless context, never a personal browser profile.
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=os.environ.get("ODOO_MCP_TEST_BROWSER_EXECUTABLE"),
            )
            try:
                page = await browser.new_page()
                await page.goto(flow.url)
                for name, value in dict(
                    url="https://example.odoo.com",
                    db="browser-db",
                    username="test@example.com",
                    api_key="synthetic-key",
                ).items():
                    await page.locator(f'[name="{name}"]').fill(value)
                # The browser generates Origin under the page's referrer policy.
                # Supplying this header manually would hide the original bug.
                async with page.expect_navigation():
                    await page.get_by_role("button", name="Verify and connect").click()
                assert "Connection saved" in await page.locator("body").inner_text()
            finally:
                await browser.close()
        assert (await provider.get_result(principal, flow.onboarding_id)).status is OnboardingStatus.READY
        assert credentials.get_odoo_credentials("browser-db").transport == "json2"
        assert credentials.CONFIG_PATH.stat().st_mode & 0o777 == 0o600
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_null_origin_stays_rejected_and_mcp_can_restart_pending_flow(isolated):
    async with Client(server.mcp, mode="legacy") as mcp:
        begin = (await mcp.call_tool("odoo_setup_credentials", {"input": {}})).structured_content
        async with httpx.AsyncClient(trust_env=False) as http:
            response = await http.post(begin["url"], data={"api_key": "synthetic-key"}, headers={"Origin": "null"})
            assert response.status_code == 403
            assert "request_rejected" in response.text
            fresh = (await mcp.call_tool("odoo_setup_credentials", {"input": {"restart": True}})).structured_content
            assert fresh["onboarding_id"] != begin["onboarding_id"]
            assert (await http.get(begin["url"])).status_code == 410
            assert (await submit(http, fresh["url"])).status_code == 200
        previous = (
            await mcp.call_tool(
                "odoo_setup_credentials",
                {
                    "input": {"onboarding_id": begin["onboarding_id"]},
                },
            )
        ).structured_content
        assert previous["failure_code"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["url", "storage"])
async def test_invalid_dashboard_url_and_storage_failures_have_bounded_codes(isolated, failure):
    principal = await LocalInstallationPrincipalProvider("test").resolve()

    def reject_storage(*args, **kwargs):
        raise PermissionError("synthetic-key private response")

    provider = LocalBrowserOnboardingProvider(
        principal, store_credentials=reject_storage if failure == "storage" else None
    )
    try:
        flow = await provider.begin(principal)
        async with httpx.AsyncClient(trust_env=False) as http:
            response = await submit(
                http,
                flow.url,
                url="https://www.odoo.sh/project/demo/branches/main/backups"
                if failure == "url"
                else "https://example.odoo.com",
            )
        result = await provider.get_result(principal, flow.onboarding_id)
        expected = "invalid_url" if failure == "url" else "storage_failed"
        assert result.failure_code.value == expected
        assert expected in response.text
        assert "synthetic-key" not in response.text
        assert not credentials.CONFIG_PATH.exists()
    finally:
        await provider.close()
