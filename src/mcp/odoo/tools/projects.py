"""Project Delivery domain tools.

Fixed-scope project management, task tracking, and milestone monitoring.
Focus on scope impact detection, not task-level micromanagement.

Models: project.project, project.task
"""

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field

from src.mcp.odoo.tools._shared import ResponseFormat

# ── Pydantic Input Models ──


class SearchProjectsInput(BaseModel):
    """Search projects with flexible filters."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: Optional[str] = Field(default=None, description="Free text search across project name")
    client: Optional[str] = Field(default=None, description="Filter by client/partner name")
    status: Optional[str] = Field(
        default=None,
        description="Filter by project status: 'on_track', 'at_risk', 'off_track', 'on_hold', or 'done'",
    )
    project_manager: Optional[str] = Field(default=None, description="Filter by project manager name")
    company_id: Optional[int] = Field(default=None, description="Filter by Odoo company ID (multi-company)")
    stale_days: Optional[int] = Field(default=None, description="Find projects with no activity in N+ days", ge=1)
    limit: int = Field(default=20, description="Max results", ge=1, le=100)
    offset: int = Field(default=0, description="Pagination offset", ge=0)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class GetProjectInput(BaseModel):
    """Get full details for a specific project."""

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(..., description="Odoo project ID", ge=1)
    include_tasks: bool = Field(default=True, description="Include task summary grouped by stage")
    task_limit: int = Field(default=50, description="Max tasks to retrieve", ge=1, le=200)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class SearchTasksInput(BaseModel):
    """Search project tasks with flexible filters."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    project_id: Optional[int] = Field(default=None, description="Filter by project ID", ge=1)
    query: Optional[str] = Field(default=None, description="Free text search in task name")
    assignee: Optional[str] = Field(default=None, description="Filter by assignee name")
    stage: Optional[str] = Field(default=None, description="Filter by task stage name")
    overdue: Optional[bool] = Field(default=None, description="True to show only overdue tasks (deadline passed)")
    stale_days: Optional[int] = Field(default=None, description="Find tasks with no activity in N+ days", ge=1)
    company_id: Optional[int] = Field(default=None, description="Filter by Odoo company ID (multi-company)")
    limit: int = Field(default=30, description="Max results", ge=1, le=100)
    offset: int = Field(default=0, description="Pagination offset", ge=0)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# ── Stub write tool inputs ──


class ProposeTaskUpdateInput(BaseModel):
    """Propose updating fields on a task."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    task_id: int = Field(..., description="Odoo task ID", ge=1)
    updates: dict = Field(..., description="Field:value pairs to update (e.g. stage_id, date_deadline, user_ids)")
    reason: str = Field(..., description="Why this update is proposed", min_length=10)


class ProposeProjectNoteInput(BaseModel):
    """Propose adding an internal note to a project."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    project_id: int = Field(..., description="Odoo project ID", ge=1)
    note: str = Field(..., description="Internal note content", min_length=5)
    source: str = Field(default="ai_review", description="What triggered this note")


class ProposeProjectActivityInput(BaseModel):
    """Propose scheduling a follow-up activity on a project."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    project_id: int = Field(..., description="Odoo project ID", ge=1)
    summary: str = Field(..., description="Short description of the activity", min_length=3, max_length=200)
    date_deadline: str = Field(..., description="Due date in YYYY-MM-DD format")
    note: str = Field(default="", description="Optional longer description")


# ── Registration ──


def register(mcp: MCPServer, get_odoo: Any) -> None:
    """Register all project delivery tools on the shared MCP server.

    Args:
        mcp: The FastMCP server instance.
        get_odoo: Callable(ctx) -> OdooClient to extract the shared client from context.
    """

    @mcp.tool(
        name="odoo_search_projects",
        annotations={
            "title": "Search Projects",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def search_projects(params: SearchProjectsInput, ctx: Context) -> str:
        """Search Odoo projects with flexible filters.

        Searches by name, client, status, project manager, and staleness.
        Multi-company aware. Returns project overview with status, client, and task count.
        """
        odoo = await get_odoo(ctx)
        if isinstance(odoo, str):
            return odoo
        domain: list = [("active", "=", True)]

        if params.query:
            domain.append(("name", "ilike", params.query))

        if params.client:
            domain.append(("partner_id.name", "ilike", params.client))

        if params.status:
            domain.append(("last_update_status", "=", params.status))

        if params.project_manager:
            domain.append(("user_id.name", "ilike", params.project_manager))

        if params.company_id:
            domain.append(("company_id", "=", params.company_id))

        if params.stale_days:
            cutoff = (datetime.now() - timedelta(days=params.stale_days)).strftime("%Y-%m-%d %H:%M:%S")
            domain.append(("write_date", "<=", cutoff))

        fields = [
            "name", "partner_id", "user_id", "date_start", "date",
            "task_count", "last_update_status", "tag_ids",
            "company_id", "write_date", "description",
        ]

        results = await odoo.search_read(
            "project.project", domain, fields,
            limit=params.limit, offset=params.offset, order="write_date desc",
        )
        total = await odoo.search_count("project.project", domain)

        if not results:
            return "No projects found matching your criteria."

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"total": total, "count": len(results), "projects": results}, indent=2, default=str)

        status_emoji = {
            "on_track": "🟢", "at_risk": "🟡", "off_track": "🔴",
            "on_hold": "⏸️", "done": "✅", False: "⚪",
        }

        lines = [f"# Projects ({len(results)} of {total})", ""]
        for proj in results:
            client = proj["partner_id"][1] if proj.get("partner_id") else "No client"
            pm = proj["user_id"][1] if proj.get("user_id") else "Unassigned"
            status = proj.get("last_update_status") or False
            emoji = status_emoji.get(status, "⚪")
            tasks = proj.get("task_count", 0)

            lines.append(f"## {emoji} {proj['name']} (ID: {proj['id']})")
            lines.append(f"- **Client:** {client}")
            lines.append(f"- **PM:** {pm}")
            lines.append(f"- **Status:** {status or 'not set'}")
            lines.append(f"- **Tasks:** {tasks}")
            if proj.get("date_start") or proj.get("date"):
                lines.append(f"- **Timeline:** {proj.get('date_start', '?')} → {proj.get('date', '?')}")
            lines.append(f"- **Last updated:** {proj.get('write_date', 'unknown')}")
            lines.append("")

        return "\n".join(lines)

    @mcp.tool(
        name="odoo_get_project",
        annotations={
            "title": "Get Project Details",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_project(params: GetProjectInput, ctx: Context) -> str:
        """Get full details for a specific project including tasks grouped by stage.

        Returns project metadata, status, timeline, and a task breakdown by stage.
        Use this to understand project health before proposing updates.
        """
        odoo = await get_odoo(ctx)
        if isinstance(odoo, str):
            return odoo
        fields = [
            "name", "partner_id", "user_id", "date_start", "date",
            "task_count", "last_update_status", "last_update_color",
            "tag_ids", "company_id", "description", "write_date", "create_date",
        ]

        records = await odoo.read("project.project", [params.project_id], fields)
        if not records:
            return f"Error: Project ID {params.project_id} not found."

        proj = records[0]

        tasks_by_stage: dict[str, list] = {}
        if params.include_tasks:
            task_fields = [
                "name", "stage_id", "user_ids", "date_deadline",
                "priority", "write_date", "state",
            ]
            tasks = await odoo.search_read(
                "project.task",
                [("project_id", "=", params.project_id)],
                task_fields,
                limit=params.task_limit,
                order="stage_id asc, priority desc, date_deadline asc",
            )
            for task in tasks:
                stage_name = task["stage_id"][1] if task.get("stage_id") else "No Stage"
                tasks_by_stage.setdefault(stage_name, []).append(task)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"project": proj, "tasks_by_stage": tasks_by_stage}, indent=2, default=str)

        client = proj["partner_id"][1] if proj.get("partner_id") else "No client"
        pm = proj["user_id"][1] if proj.get("user_id") else "Unassigned"

        lines = [
            f"# {proj['name']} (ID: {proj['id']})",
            "",
            f"**Client:** {client}",
            f"**PM:** {pm}",
            f"**Status:** {proj.get('last_update_status') or 'not set'}",
            f"**Timeline:** {proj.get('date_start', 'not set')} → {proj.get('date', 'not set')}",
            f"**Tasks:** {proj.get('task_count', 0)}",
            f"**Last updated:** {proj.get('write_date')}",
        ]

        if proj.get("description"):
            lines.extend(["", "## Description", proj["description"]])

        if tasks_by_stage:
            lines.extend(["", "## Tasks by Stage"])
            today = datetime.now().strftime("%Y-%m-%d")
            for stage_name, stage_tasks in tasks_by_stage.items():
                lines.append(f"\n### {stage_name} ({len(stage_tasks)})")
                for t in stage_tasks:
                    assignees = ", ".join(
                        u[1] if isinstance(u, list) else str(u)
                        for u in (t.get("user_ids") or [])
                    ) or "Unassigned"
                    deadline = t.get("date_deadline", "no deadline")
                    overdue = ""
                    if deadline and deadline != "no deadline" and str(deadline) < today:
                        overdue = " ⚠️ OVERDUE"
                    priority_map = {"0": "", "1": " ★"}
                    star = priority_map.get(str(t.get("priority", "0")), "")
                    lines.append(f"- {t['name']}{star} — {assignees} — due: {deadline}{overdue}")

        return "\n".join(lines)

    @mcp.tool(
        name="odoo_search_tasks",
        annotations={
            "title": "Search Tasks",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def search_tasks(params: SearchTasksInput, ctx: Context) -> str:
        """Search project tasks with flexible filters.

        Find tasks by project, assignee, stage, overdue status, and staleness.
        Multi-company aware. Use to identify blockers, overdue work, and stale tasks.
        """
        odoo = await get_odoo(ctx)
        if isinstance(odoo, str):
            return odoo
        domain: list = []

        if params.project_id:
            domain.append(("project_id", "=", params.project_id))

        if params.query:
            domain.append(("name", "ilike", params.query))

        if params.assignee:
            domain.append(("user_ids.name", "ilike", params.assignee))

        if params.stage:
            domain.append(("stage_id.name", "ilike", params.stage))

        if params.overdue:
            today = datetime.now().strftime("%Y-%m-%d")
            domain.append(("date_deadline", "<", today))
            domain.append(("state", "not in", ["1_done", "1_canceled"]))

        if params.stale_days:
            cutoff = (datetime.now() - timedelta(days=params.stale_days)).strftime("%Y-%m-%d %H:%M:%S")
            domain.append(("write_date", "<=", cutoff))

        if params.company_id:
            domain.append(("company_id", "=", params.company_id))

        fields = [
            "name", "project_id", "stage_id", "user_ids", "date_deadline",
            "priority", "write_date", "state",
        ]

        results = await odoo.search_read(
            "project.task", domain, fields,
            limit=params.limit, offset=params.offset, order="date_deadline asc, priority desc",
        )
        total = await odoo.search_count("project.task", domain)

        if not results:
            return "No tasks found matching your criteria."

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"total": total, "count": len(results), "tasks": results}, indent=2, default=str)

        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f"# Tasks ({len(results)} of {total})", ""]
        for t in results:
            project = t["project_id"][1] if t.get("project_id") else "No project"
            stage = t["stage_id"][1] if t.get("stage_id") else "No stage"
            assignees = ", ".join(
                u[1] if isinstance(u, list) else str(u)
                for u in (t.get("user_ids") or [])
            ) or "Unassigned"
            deadline = t.get("date_deadline", "no deadline")
            overdue = ""
            if deadline and deadline != "no deadline" and str(deadline) < today:
                overdue = " ⚠️ OVERDUE"

            lines.append(f"### {t['name']} (ID: {t['id']}){overdue}")
            lines.append(f"- **Project:** {project}")
            lines.append(f"- **Stage:** {stage}")
            lines.append(f"- **Assigned:** {assignees}")
            lines.append(f"- **Deadline:** {deadline}")
            lines.append(f"- **Last updated:** {t.get('write_date', 'unknown')}")
            lines.append("")

        return "\n".join(lines)

    # ── Write tool stubs (not yet implemented) ──

    @mcp.tool(
        name="odoo_propose_task_update",
        annotations={
            "title": "Update Task Fields",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def propose_task_update(params: ProposeTaskUpdateInput, ctx: Context) -> str:
        """Propose updating fields on a project task. NOT YET IMPLEMENTED."""
        return "Error: Task updates are not yet implemented. This tool will be enabled in a future release."

    @mcp.tool(
        name="odoo_propose_project_note",
        annotations={
            "title": "Add Project Note",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def propose_project_note(params: ProposeProjectNoteInput, ctx: Context) -> str:
        """Add an internal log note to a project. NOT YET IMPLEMENTED."""
        return "Error: Project notes are not yet implemented. This tool will be enabled in a future release."

    @mcp.tool(
        name="odoo_propose_project_activity",
        annotations={
            "title": "Schedule Project Activity",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def propose_project_activity(params: ProposeProjectActivityInput, ctx: Context) -> str:
        """Schedule a follow-up activity on a project. NOT YET IMPLEMENTED."""
        return "Error: Project activities are not yet implemented. This tool will be enabled in a future release."
