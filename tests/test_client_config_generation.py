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
    assert "${user_config." not in generated
    assert "/Users/" not in generated
    assert "\\Users\\" not in generated

    local = json.loads((tmp_path / "claude_desktop_config.odoo.json").read_text())
    assert local["mcpServers"]["odoo"]["env"] == {
        "ODOO_TRANSPORT": "xmlrpc",
        "ODOO_MCP_ENABLE_WRITES": "false",
        "ODOO_MCP_ENABLE_SELF_UPDATE": "false",
    }

    remote = json.loads(
        (tmp_path / "opencrane_remote_connector.example.json").read_text(encoding="utf-8")
    )
    assert remote["transport"]["type"] == "streamable-http"
    assert remote["authentication"]["type"] == "oauth2"
    assert remote["credential_boundary"] == "per-user-profile-custody"
    assert remote["qualification_status"] == "requires-live-opencrane-qualification"


def test_generated_transport_tracks_manifest_default(tmp_path, monkeypatch):
    # A bundle configured for Odoo 19 must produce the same usable local
    # selection, rather than copying an extension-only interpolation expression.
    manifest = generate_client_configs._load_manifest()
    manifest["user_config"]["odoo_transport"]["default"] = "json2"
    monkeypatch.setattr(generate_client_configs, "_load_manifest", lambda: manifest)
    monkeypatch.setattr(generate_client_configs, "OUT_DIR", tmp_path)
    generate_client_configs.main()
    for name in ("claude_desktop_config.odoo.json", "openclaw_mcp_servers.json"):
        config = json.loads((tmp_path / name).read_text())
        assert config["mcpServers"]["odoo"]["env"]["ODOO_TRANSPORT"] == "json2"
    assert 'ODOO_TRANSPORT: "json2"' in (tmp_path / "hermes_mcp_servers.yaml").read_text()
