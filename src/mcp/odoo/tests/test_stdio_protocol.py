"""Exercise the installed server process, rather than an in-memory dispatcher."""

import json
import sys
import tempfile

import pytest
from mcp.client.stdio import stdio_client

from mcp import ClientSession, StdioServerParameters
from src.mcp.odoo.utils.startup_diagnostics import startup_failure


@pytest.mark.asyncio
async def test_stdio_initialize_discover_and_credentials_free_read(tmp_path):
    # Override the loader before lifespan starts, so workstation profiles and
    # approval state cannot affect this subprocess or trigger network access.
    code = (
        "from pathlib import Path; from src.core import credentials; "
        f"credentials.CONFIG_PATH = Path({str(tmp_path / 'missing.json')!r}); "
        "from src.mcp.odoo import server; "
        f"server.APPROVAL_PATH = Path({str(tmp_path / 'approvals.sqlite3')!r}); "
        "server.main()"
    )
    params = StdioServerParameters(command=sys.executable, args=["-c", code], env={
        "ODOO_MCP_ENABLE_WRITES": "0", "ODOO_MCP_ENABLE_SELF_UPDATE": "0",
    })
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=20) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "odoo_runtime_info" in {tool.name for tool in tools.tools}
            result = await session.call_tool("odoo_runtime_info", {})
            assert not result.is_error
            runtime = json.loads(result.content[0].text)
            assert runtime["write_execution_enabled"] is False
            assert runtime["self_update_enabled"] is False


@pytest.mark.asyncio
async def test_failed_server_import_exposes_subprocess_cause():
    # Reproduce the v1 import that originally failed under SDK 2. The real MCP
    # client wraps the terminated child in task groups, hiding its import error.
    params = StdioServerParameters(command=sys.executable, args=[
        "-c", "from mcp.server.fastmcp import FastMCP",
    ])
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        with pytest.raises(Exception) as captured:
            async with stdio_client(params, errlog=stderr) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=10) as session:
                    await session.initialize()
        stderr.seek(0)
        message = startup_failure("MCP initialization", captured.value, stderr.read())
    assert "missing module mcp.server.fastmcp" in message
