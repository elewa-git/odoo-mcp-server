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
    assert "ODOO_TRANSPORT" not in generated
    assert "/Users/" not in generated
    assert "\\Users\\" not in generated

    remote = json.loads(
        (tmp_path / "opencrane_remote_connector.example.json").read_text(encoding="utf-8")
    )
    assert remote["transport"]["type"] == "streamable-http"
    assert remote["authentication"]["type"] == "oauth2"
    assert remote["credential_boundary"] == "per-user-profile-custody"
    assert remote["qualification_status"] == "requires-live-opencrane-qualification"
