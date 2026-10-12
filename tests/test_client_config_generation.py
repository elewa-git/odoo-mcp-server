"""Generated local and remote configuration boundary tests."""

from __future__ import annotations

import json

from scripts import generate_client_configs


def test_generated_configs_are_portable_and_contain_no_credential_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(generate_client_configs, "OUT_DIR", tmp_path)

    generate_client_configs.main()

    generated = "\n".join(path.read_text(encoding="utf-8") for path in sorted(tmp_path.iterdir()))
    assert "odoo-mcp-server" in generated
    assert "ODOO_API_KEY" not in generated
    assert "/Users/" not in generated
    assert "\\Users\\" not in generated

    local = json.loads((tmp_path / "claude_desktop_config.odoo.json").read_text())
    assert local["mcpServers"]["odoo"]["env"] == {
        "ODOO_MCP_ENABLE_WRITES": "${user_config.enable_writes}",
        "ODOO_MCP_ENABLE_SELF_UPDATE": "${user_config.enable_self_update}",
    }

    remote = json.loads((tmp_path / "opencrane_remote_connector.example.json").read_text(encoding="utf-8"))
    assert remote["transport"]["type"] == "streamable-http"
    assert remote["authentication"]["type"] == "oauth2"
    assert remote["credential_boundary"] == "per-user-profile-custody"
    assert remote["qualification_status"] == "requires-live-opencrane-qualification"


def test_generated_configs_keep_policies_configurable_but_no_global_transport(tmp_path, monkeypatch):
    # Transport now belongs to each database. Global policy settings remain
    # user-configurable, so moving transport must not hardcode those controls.
    manifest = generate_client_configs._load_manifest()
    assert "odoo_transport" not in manifest["user_config"]
    monkeypatch.setattr(generate_client_configs, "_load_manifest", lambda: manifest)
    monkeypatch.setattr(generate_client_configs, "OUT_DIR", tmp_path)
    generate_client_configs.main()
    for name in ("claude_desktop_config.odoo.json", "openclaw_mcp_servers.json"):
        config = json.loads((tmp_path / name).read_text())
        env = config["mcpServers"]["odoo"]["env"]
        assert "ODOO_TRANSPORT" not in env
        assert env["ODOO_MCP_ENABLE_WRITES"] == "${user_config.enable_writes}"
    assert "ODOO_TRANSPORT" not in (tmp_path / "hermes_mcp_servers.yaml").read_text()
