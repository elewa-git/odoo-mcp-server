"""Protocol-level MCP SDK 2 smoke tests for modern and legacy clients."""

from __future__ import annotations

import pytest

from mcp import Client
from src.mcp.odoo import server


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_sdk2_client_lists_surface_and_calls_runtime_info(monkeypatch, mode: str) -> None:
    # Keep protocol smoke independent from workstation credentials and network.
    monkeypatch.setattr(server, "list_databases", lambda: [])

    async with Client(server.mcp, mode=mode) as client:
        protocol_version = client.protocol_version
        tools = await client.list_tools()
        templates = await client.list_resource_templates()
        prompts = await client.list_prompts()
        runtime = await client.call_tool("odoo_runtime_info", {})

    assert {tool.name for tool in tools.tools} == server_test_expected_tools()
    assert protocol_version == ("2026-07-28" if mode == "auto" else "2025-11-25")
    assert {str(template.uri_template) for template in templates.resource_templates} == {
        "odoo://models/{database}",
        "odoo://model/{model_name}",
    }
    assert {prompt.name for prompt in prompts.prompts} == {
        "odoo_write_flow",
        "odoo_database_selection",
        "odoo_safety_policy",
    }
    assert runtime.is_error is False
    assert runtime.structured_content is not None
    assert runtime.structured_content["credential_custody"] == "provider"


def server_test_expected_tools() -> set[str]:
    """Reuse the frozen public tool names without reaching into SDK managers."""
    from src.mcp.odoo.tests.test_mcp_surface_contract import EXPECTED_TOOLS

    return EXPECTED_TOOLS
