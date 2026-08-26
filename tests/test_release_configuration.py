import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_documentation_only_pushes_do_not_start_the_workflow():
    trigger = WORKFLOW.read_text(encoding="utf-8").split("permissions:", 1)[0]

    assert 'push:\n    paths-ignore:\n      - "**/*.md"\n      - "docs/**"' in trigger
    assert "\n  pull_request:\n" in trigger


def test_pypi_distribution_keeps_the_existing_cli_name():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert project["project"]["name"] == "zenmoney-mcp-server"
    assert project["project"]["scripts"]["zenmoney-mcp"] == (
        "zenmoney_mcp.entrypoint:main"
    )
    assert project["project"]["urls"] == {
        "Repository": "https://github.com/ekho/zenmoney-mcp",
        "Issues": "https://github.com/ekho/zenmoney-mcp/issues",
    }


def test_source_distribution_limits_selected_files_to_package_sources():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"] == [
        "/src"
    ]


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
    assert 'previous_tag="$(git describe --tags --abbrev=0)"' in release_job
    assert 'released_tag="$(git describe --tags --abbrev=0)"' in release_job
    assert 'if [ "$released_tag" = "$previous_tag" ]' in release_job
    assert 'version="${released_tag#v}"' in release_job
    assert "original_head" not in release_job

    assert "needs: release" in publish_job
    assert "if: needs.release.outputs.version != ''" in publish_job
    assert "group: publish-image" in publish_job
    assert "queue: max" in publish_job
    assert "packages: write" in publish_job
    assert "ref: v${{ needs.release.outputs.version }}" in publish_job
    assert "docker login ghcr.io" in publish_job
    assert (
        "docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8"
        in publish_job
    )
    assert (
        "docker/setup-buildx-action@37fe631027851001ddb9b187196cc803df7f5f0e"
        in publish_job
    )
    assert (
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a"
        in publish_job
    )
    assert "platforms: linux/amd64,linux/arm64" in publish_job
    assert "push: true" in publish_job
    assert (
        "org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}"
        in publish_job
    )
    assert (
        "${{ steps.image.outputs.image }}:${{ needs.release.outputs.version }}"
        in publish_job
    )
    assert "${{ steps.image.outputs.image }}:latest" in publish_job


def test_release_workflow_publishes_fresh_distributions_with_oidc():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "version: ${{ steps.release.outputs.version }}" in workflow
    assert "uv build --no-sources" in workflow
    assert "uses: actions/upload-artifact@" in workflow
    assert "publish-pypi:" in workflow
    assert "if: needs.release.outputs.version != ''" in workflow
    assert "name: pypi" in workflow
    assert "id-token: write" in workflow
    assert "uses: actions/download-artifact@" in workflow
    assert "run: uv publish" in workflow


def test_release_image_uses_the_built_wheel_with_locked_dependencies():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    docker_job = workflow[
        workflow.index("  docker-compose:") : workflow.index("  remote-mcp-smoke:")
    ]
    publish_job = workflow[
        workflow.index("  publish-image:") : workflow.index("  publish-pypi:")
    ]

    assert docker_job.index("uv build --wheel --no-sources") < docker_job.index(
        "docker build -t zenmoney-mcp:remote-test ."
    )
    assert "name: dist" in publish_job
    assert "path: dist/" in publish_job
    assert "COPY dist/*.whl" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "uv pip install --no-deps" in dockerfile
    assert "COPY src" not in dockerfile


def test_readme_uses_the_pypi_distribution_for_uvx():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "uvx --from zenmoney-mcp-server zenmoney-mcp" in readme
