import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_semantic_release_manages_every_version_source():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    release = project.get("tool", {}).get("semantic_release")

    assert release is not None, "python-semantic-release is not configured"
    assert release["version_toml"] == ["pyproject.toml:project.version"]
    assert release["version_variables"] == [
        "src/zenmoney_mcp/__init__.py:__version__"
    ]
    assert release["allow_zero_version"] is True
    assert release["major_on_zero"] is True
    assert release["tag_format"] == "v{version}"
    assert 'uv lock --upgrade-package "$PACKAGE_NAME"' in release["build_command"]
    assert "git add uv.lock" in release["build_command"]


def test_release_publishes_versioned_and_latest_ghcr_images():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    release_job = workflow[
        workflow.index("  release:") : workflow.index("  publish-image:")
    ]
    publish_job = workflow[workflow.index("  publish-image:") :]

    assert "packages: write" not in release_job
    assert "version: ${{ steps.release.outputs.version }}" in release_job
    assert 'if [ "$(git rev-parse HEAD)" = "$original_head" ]' in release_job
    assert 'version="$(uv version --short)"' in release_job

    assert "needs: release" in publish_job
    assert "packages: write" in publish_job
    assert "ref: v${{ needs.release.outputs.version }}" in publish_job
    assert "docker login ghcr.io" in publish_job
    assert (
        "org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}"
        in publish_job
    )
    assert 'docker push "$image:$VERSION"' in publish_job
    assert 'docker push "$image:latest"' in publish_job
