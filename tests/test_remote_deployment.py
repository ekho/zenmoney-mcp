"""Static contract tests for the private remote MCP deployment."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
COMPOSE_FILE = ROOT / "deploy" / "remote-mcp" / "compose.yaml"
ENV_EXAMPLE = ROOT / "deploy" / "remote-mcp" / ".env.example"
RUNBOOK = ROOT / "deploy" / "remote-mcp" / "README.md"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TUNNEL_IMAGE = (
    "ghcr.io/openai/tunnel-client:v0.0.12@"
    "sha256:b1e9eb675e6a64775685c323c2af8c2810ea14e1a27c8ce4c68f2994cd7c5e8e"
)


def _compose_config() -> dict:
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "CONTROL_PLANE_TUNNEL_ID": "tunnel_0123456789abcdef0123456789abcdef",
            "ZENMONEY_TOKEN": "synthetic-static-test-token",
        },
        text=True,
    )
    return json.loads(result.stdout)


def test_compose_keeps_mcp_private_and_separates_credentials():
    config = _compose_config()
    services = config["services"]
    networks = config["networks"]

    assert set(services) == {"zenmoney-mcp", "zenmoney-sync", "tunnel-client"}
    assert "ports" not in services["zenmoney-mcp"]
    assert services["zenmoney-mcp"]["volumes"][0]["read_only"] is True
    assert services["zenmoney-mcp"]["networks"] == {"mcp_internal": None}
    assert services["zenmoney-sync"]["networks"] == {"egress": None}
    assert set(services["tunnel-client"]["networks"]) == {"mcp_internal", "egress"}
    assert networks["mcp_internal"]["internal"] is True

    mcp_environment = services["zenmoney-mcp"].get("environment", {})
    tunnel_environment = services["tunnel-client"].get("environment", {})
    sync_environment = services["zenmoney-sync"].get("environment", {})
    assert "ZENMONEY_TOKEN" not in mcp_environment
    assert "ZENMONEY_TOKEN_FILE" not in mcp_environment
    assert "CONTROL_PLANE_API_KEY" not in mcp_environment
    assert "ZENMONEY_TOKEN" not in tunnel_environment
    assert "ZENMONEY_TOKEN_FILE" not in tunnel_environment
    assert "CONTROL_PLANE_API_KEY" not in sync_environment
    assert sync_environment["ZENMONEY_TOKEN_FILE"] == "/run/secrets/zenmoney-token"

    sync_secret = services["zenmoney-sync"]["secrets"]
    assert sync_secret == [
        {
            "source": "zenmoney-token",
            "target": "zenmoney-token",
            "uid": "10001",
            "gid": "10001",
            "mode": "0400",
        }
    ]
    assert config["secrets"]["zenmoney-token"]["environment"] == "ZENMONEY_TOKEN"
    assert {
        secret["source"] for secret in services["tunnel-client"]["secrets"]
    } == {"control-plane-api-key"}


def test_compose_uses_pinned_tunnel_contract_and_hardened_roles():
    services = _compose_config()["services"]
    tunnel = services["tunnel-client"]

    assert tunnel["image"] == TUNNEL_IMAGE
    assert tunnel["command"] == [
        "--control-plane.tunnel-id=tunnel_0123456789abcdef0123456789abcdef",
        "--control-plane.api-key=file:/run/secrets/control-plane-api-key",
        "--mcp.server-url=http://zenmoney-mcp:8000/mcp",
        "--health.listen-addr=:8080",
        "--log.level=info",
        "--log.format=json",
    ]

    for name in ("zenmoney-mcp", "zenmoney-sync", "tunnel-client"):
        service = services[name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["restart"] == "unless-stopped"
        assert "/tmp" in service["tmpfs"]

    assert services["zenmoney-mcp"]["user"] == "10001:10001"
    assert services["zenmoney-sync"]["user"] == "10001:10001"
    assert services["zenmoney-sync"]["stop_grace_period"] in {"1m10s", "70s"}


def test_operations_and_ci_cover_sensitive_backups_pin_updates_and_runtime_smoke():
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "ZENMONEY_TOKEN=replace-with-your-token" in env_example
    assert "sensitive financial data" in runbook
    assert "docker buildx imagetools inspect" in runbook
    assert "github.com/openai/tunnel-client/releases" in runbook
    assert "validate_snapshot" in runbook
    assert "HardenedDatabase(stage, journal_mode='DELETE')" in runbook
    assert "os.getuid() == 10001" in workflow
    assert "read_secret('ZENMONEY_TOKEN')" in workflow
    assert "docker compose" in workflow and "up -d --no-deps zenmoney-mcp" in workflow
    assert 'mount["RW"] is False' in workflow
