"""Startup diagnostics must survive task groups without leaking input data."""

from src.mcp.odoo.utils.startup_diagnostics import CheckFailure, StartupCheckError, startup_failure


def test_nested_import_failure_preserves_cause_and_discards_secrets():
    error = ExceptionGroup("key=secret", [ExceptionGroup("body=private", [
        ModuleNotFoundError("secret private body", name="mcp.server.fastmcp"),
    ])])
    result = startup_failure("dependency/import validation", error)
    assert "mcp.server.fastmcp" in result
    assert "dependency/import validation" in result
    assert "secret" not in result
    assert "private" not in result


def test_subprocess_stderr_only_exposes_missing_module():
    result = startup_failure("MCP initialization", RuntimeError("Authorization: bearer secret"),
                             "sensitive HTTP body\nModuleNotFoundError: No module named 'mcp.server.fastmcp'\n")
    assert "mcp.server.fastmcp" in result
    assert "secret" not in result
    assert "sensitive" not in result


def test_chained_network_failure_is_distinct_from_credentials():
    cause = OSError(8, "private response")
    error = RuntimeError("api_key=secret")
    error.__cause__ = cause
    result = startup_failure("Odoo authentication", error)
    assert "OS error 8" in result
    assert "secret" not in result
    assert "private" not in result


def test_controlled_verifier_failure_has_an_actionable_reason():
    result = startup_failure("MCP read-only tool execution", StartupCheckError(CheckFailure.PING))
    assert "ping did not confirm the intended database" in result
