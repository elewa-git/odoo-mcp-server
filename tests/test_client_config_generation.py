"""Generated local and remote configuration boundary tests."""

from __future__ import annotations

import json

from scripts import generate_client_configs


def test_generated_configs_are_portable_and_contain_no_credential_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(generate_client_configs, "OUT_DIR", tmp_path)

    generate_client_configs.main()

    generated = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(tmp_path.iterdir())
    )
    assert "odoo-mcp-server" in generated
    assert "ODOO_API_KEY" not in generated
    assert "/Users/" not in generated
    assert "\\Users\\" not in generated

    local = json.loads((tmp_path / "claude_desktop_config.odoo.json").read_text())
    assert local["mcpServers"]["odoo"]["env"] == {
        "ODOO_TRANSPORT": "${user_config.odoo_transport}",
        "ODOO_MCP_ENABLE_WRITES": "${user_config.enable_writes}",
        "ODOO_MCP_ENABLE_SELF_UPDATE": "${user_config.enable_self_update}",
    }

    remote = json.loads(
        (tmp_path / "opencrane_remote_connector.example.json").read_text(encoding="utf-8")
    )
    assert remote["transport"]["type"] == "streamable-http"
    assert remote["authentication"]["type"] == "oauth2"
    assert remote["credential_boundary"] == "per-user-profile-custody"
    assert remote["qualification_status"] == "requires-live-opencrane-qualification"


def test_generated_transport_preserves_user_selection_when_default_changes(tmp_path, monkeypatch):
    # Changing an installation fallback must not bake that value into the
    # generated template and discard the user's own transport selection.
    manifest = generate_client_configs._load_manifest()
    manifest["user_config"]["odoo_transport"]["default"] = "json2"
    monkeypatch.setattr(generate_client_configs, "_load_manifest", lambda: manifest)
    monkeypatch.setattr(generate_client_configs, "OUT_DIR", tmp_path)
    generate_client_configs.main()
    for name in ("claude_desktop_config.odoo.json", "openclaw_mcp_servers.json"):
        config = json.loads((tmp_path / name).read_text())
        assert config["mcpServers"]["odoo"]["env"]["ODOO_TRANSPORT"] == "${user_config.odoo_transport}"
    assert 'ODOO_TRANSPORT: "${user_config.odoo_transport}"' in (tmp_path / "hermes_mcp_servers.yaml").read_text()
