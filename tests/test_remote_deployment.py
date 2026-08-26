"""Static contract tests for the private remote MCP deployment."""

from __future__ import annotations

import json
import os
import re
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
GHCR_IMAGE = "ghcr.io/ekho/zenmoney-mcp:0.4.0"


def _compose_config() -> dict:
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "CONTROL_PLANE_TUNNEL_ID": "tunnel_0123456789abcdef0123456789abcdef",
        },
        text=True,
    )
    return json.loads(result.stdout)


def test_compose_keeps_mcp_private_and_separates_credentials():
    config = _compose_config()
    services = config["services"]
    networks = config["networks"]
    volumes = config["volumes"]

    assert set(services) == {"zenmoney-mcp", "zenmoney-sync", "tunnel-client"}
    assert "ports" not in services["zenmoney-mcp"]
    mcp_mounts = {
        mount["target"]: mount for mount in services["zenmoney-mcp"]["volumes"]
    }
    sync_mounts = {
        mount["target"]: mount for mount in services["zenmoney-sync"]["volumes"]
    }
    tunnel_mounts = {
        mount["target"] for mount in services["tunnel-client"].get("volumes", [])
    }
    assert mcp_mounts["/data"]["read_only"] is True
    assert mcp_mounts["/sync-control"].get("read_only", False) is False
    assert sync_mounts["/sync-control"].get("read_only", False) is False
    assert mcp_mounts["/sync-control"]["source"] == "zenmoney-sync-control"
    assert sync_mounts["/sync-control"]["source"] == "zenmoney-sync-control"
    assert "/sync-control" not in tunnel_mounts
    assert set(volumes) == {"zenmoney-data", "zenmoney-sync-control"}
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

    assert "secrets" not in services["zenmoney-mcp"]
    assert services["zenmoney-sync"]["secrets"] == [
        {
            "source": "zenmoney-token",
            "target": "/run/secrets/zenmoney-token",
        }
    ]
    assert Path(config["secrets"]["zenmoney-token"]["file"]) == (
        ROOT / "deploy" / "remote-mcp" / "secrets" / "zenmoney-token"
    )
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


def test_deployment_uses_the_versioned_ghcr_image():
    services = _compose_config()["services"]
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert services["zenmoney-mcp"]["image"] == GHCR_IMAGE
    assert services["zenmoney-sync"]["image"] == GHCR_IMAGE
    assert f"ZENMONEY_IMAGE={GHCR_IMAGE}" in env_example
    assert "docker compose --env-file deploy/remote-mcp/.env" in runbook
    assert "-f deploy/remote-mcp/compose.yaml pull" in runbook
    assert "ZENMONEY_IMAGE: zenmoney-mcp:remote-test" in workflow


def test_operations_and_ci_cover_sensitive_backups_pin_updates_and_runtime_smoke():
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "ZENMONEY_TOKEN" not in env_example
    assert "sensitive financial data" in runbook
    assert "previews and results" in runbook
    assert "never automatically replays" in runbook
    assert "docker buildx imagetools inspect" in runbook
    assert "github.com/openai/tunnel-client/releases" in runbook
    assert "validate_snapshot" in runbook
    assert "HardenedDatabase(stage, journal_mode='DELETE')" in runbook
    assert re.search(r"file-source remapping is\s+not implemented", runbook)
    assert "sudo install -o 10001 -g 10001 -m 0400" in runbook
    assert "ZENMONEY_TOKEN:" not in workflow
    prepare_secret = workflow.index("Prepare synthetic ZenMoney secret")
    validate_compose = workflow.index("Validate Compose")
    assert prepare_secret < validate_compose
    assert "sudo install -o 10001 -g 10001 -m 0400" in workflow
    assert "deploy/remote-mcp/secrets/zenmoney-token" in workflow
    assert "os.getuid() == 10001" in workflow
    assert "read_secret('ZENMONEY_TOKEN')" in workflow
    assert "docker compose" in workflow and "up -d --no-deps zenmoney-mcp" in workflow
    assert 'mounts["/data"]["RW"] is False' in workflow
    assert 'mounts["/sync-control"]["RW"] is True' in workflow
