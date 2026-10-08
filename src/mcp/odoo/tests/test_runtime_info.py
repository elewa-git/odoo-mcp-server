from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.core.identity import LocalInstallationPrincipalProvider
from src.core.onboarding import (
    OnboardingContinuation,
    OnboardingResult,
    OnboardingStatus,
)
from src.mcp.odoo import server


class FakeRequestContext:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class FakeContext:
    def __init__(self, lifespan_context):
        self.request_context = FakeRequestContext(lifespan_context)


class FakeOnboardingProvider:
    def __init__(self) -> None:
        self.principal_ids: list[str] = []

    async def begin(self, principal):
        self.principal_ids.append(principal.id)
        return OnboardingContinuation(
            onboarding_id="onboarding-1",
            url="http://127.0.0.1:8765/onboarding/onboarding-1",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    async def get_result(self, principal, onboarding_id):
        self.principal_ids.append(principal.id)
        return OnboardingResult(
            onboarding_id=onboarding_id,
            status=OnboardingStatus.READY,
            profile_id="profile-1",
            completed_at=datetime.now(UTC),
        )


def _context(*, onboarding_provider=None) -> FakeContext:
    kwargs = {
        "odoo": None,
        "principal_provider": LocalInstallationPrincipalProvider("local-user"),
    }
    if onboarding_provider is not None:
        kwargs["onboarding_provider"] = onboarding_provider
    return FakeContext(server.AppContext(**kwargs))


class TestRuntimeInfo:
    @pytest.mark.asyncio
    async def test_runtime_info_reports_transport_without_credential_presence(self, monkeypatch):
        monkeypatch.setenv("ODOO_TRANSPORT", "json2")
        monkeypatch.setenv("ODOO_API_KEY", "must-not-be-observed")

        info = await server.odoo_runtime_info(_context())
        serialized = json.dumps(info)

        assert info["odoo_transport"] == "json2"
        assert info["credential_custody"] == "provider"
        assert info["credential_onboarding_available"] is False
        assert "api_key" not in serialized.casefold()
        assert "must-not-be-observed" not in serialized
        assert "session_writes" not in serialized

    @pytest.mark.asyncio
    async def test_runtime_info_reports_provider_onboarding_availability(self):
        info = await server.odoo_runtime_info(
            _context(onboarding_provider=FakeOnboardingProvider())
        )

        assert info["credential_onboarding_available"] is True

    @pytest.mark.asyncio
    async def test_setup_credentials_returns_only_out_of_band_continuation(self):
        provider = FakeOnboardingProvider()
        result = await server.odoo_setup_credentials(
            server.OdooSetupCredentialsInput(),
            _context(onboarding_provider=provider),
        )

        assert result["onboarding_id"] == "onboarding-1"
        assert result["url"].startswith("http://127.0.0.1:")
        assert provider.principal_ids == ["local:local-installation:local-user"]
        assert "api_key" not in json.dumps(result).casefold()

    @pytest.mark.asyncio
    async def test_setup_credentials_polls_principal_bound_non_secret_status(self):
        provider = FakeOnboardingProvider()
        result = await server.odoo_setup_credentials(
            server.OdooSetupCredentialsInput(onboarding_id="onboarding-1"),
            _context(onboarding_provider=provider),
        )

        assert result == {
            "onboarding_id": "onboarding-1",
            "status": "ready",
            "profile_id": "profile-1",
            "failure_code": None,
            "completed_at": result["completed_at"],
        }
        assert "api_key" not in json.dumps(result).casefold()

    @pytest.mark.asyncio
    async def test_setup_credentials_fails_closed_without_onboarding_adapter(self):
        result = await server.odoo_setup_credentials(
            server.OdooSetupCredentialsInput(),
            _context(),
        )

        assert result == {
            "status": "unavailable",
            "failure_code": "onboarding_unavailable",
        }

    @pytest.mark.asyncio
    async def test_legacy_session_write_tools_never_grant_authority(self, monkeypatch):
        monkeypatch.delenv("ODOO_MCP_ENABLE_WRITES", raising=False)

        enable_result = await server.odoo_enable_session_writes()
        disable_result = await server.odoo_disable_session_writes()
        info = await server.odoo_runtime_info(_context())

        assert "cannot grant write authority" in enable_result
        assert "no state change" in disable_result
        assert info["writes_currently_allowed"] is False
