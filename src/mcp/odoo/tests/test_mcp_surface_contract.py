"""Compatibility contract for the public MCP surface during the v2 rewrite."""

import json

from src.mcp.odoo.server import mcp

EXPECTED_TOOLS = {
    "odoo_ping",
    "odoo_search_read",
    "odoo_read_records",
    "odoo_log_internal_note",
    "odoo_schedule_activity",
    "odoo_fields_get",
    "odoo_search_count",
    "odoo_diagnose_call",
    "odoo_preview_write",
    "odoo_validate_write",
    "odoo_execute_approved_write",
    "odoo_setup_credentials",
    "odoo_list_databases",
    "odoo_switch_database",
    "odoo_enable_session_writes",
    "odoo_disable_session_writes",
    "odoo_runtime_info",
    "odoo_check_for_update",
    "odoo_apply_self_update",
    "odoo_search_opportunities",
    "odoo_get_opportunity",
    "odoo_propose_stage_change",
    "odoo_propose_log_note",
    "odoo_propose_activity",
    "odoo_propose_field_update",
    "odoo_search_contacts",
    "odoo_get_pipeline_summary",
    "odoo_search_projects",
    "odoo_get_project",
    "odoo_search_tasks",
    "odoo_propose_task_update",
    "odoo_propose_project_note",
    "odoo_propose_project_activity",
}

EXPECTED_RESOURCE_TEMPLATES = {
    "odoo://models/{database}",
    "odoo://model/{model_name}",
}

EXPECTED_PROMPTS = {
    "odoo_write_flow",
    "odoo_database_selection",
    "odoo_safety_policy",
}


def test_public_mcp_names_remain_explicit_during_rewrite() -> None:
    """Require intentional test updates whenever a public MCP name changes."""
    tools = {tool.name for tool in mcp._tool_manager.list_tools()}
    resources = {str(template.uri_template) for template in mcp._resource_manager.list_templates()}
    prompts = {prompt.name for prompt in mcp._prompt_manager.list_prompts()}

    assert tools == EXPECTED_TOOLS
    assert resources == EXPECTED_RESOURCE_TEMPLATES
    assert prompts == EXPECTED_PROMPTS


def test_write_execution_remains_destructive_and_validation_read_only() -> None:
    """Freeze the client-visible annotations that reinforce write governance."""
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    execute = tools["odoo_execute_approved_write"].annotations
    validate = tools["odoo_validate_write"].annotations

    assert execute is not None
    assert execute.read_only_hint is False
    assert execute.destructive_hint is True
    assert validate is not None
    assert validate.read_only_hint is True
    assert validate.destructive_hint is False


def test_public_tool_contract_never_accepts_an_odoo_api_key() -> None:
    """Keep downstream credentials outside every MCP tool input boundary."""
    public_contract = [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "meta": tool.meta,
        }
        for tool in mcp._tool_manager.list_tools()
    ]

    assert "api_key" not in json.dumps(public_contract).casefold()
