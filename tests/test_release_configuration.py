import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]


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
