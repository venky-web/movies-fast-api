"""The README keeps up with the code."""

import re
from pathlib import Path

import pytest

from app.core.config import Settings
from app.main import create_app

ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def tour_requests() -> list[tuple[str, str]]:
    """(method, path) of every curl command in the tour, with the PowerShell variables replaced by `x`."""
    found = []
    for line in README.splitlines():
        if "curl.exe" not in line or not (url := re.search(r'"\$api([^"]*)"', line)):
            continue
        method = re.search(r"-X (\w+)", line)
        path = re.sub(r"\$\([^)]*\)|\$\w+", "x", url[1]).split("?")[0]
        found.append((method[1] if method else "GET", path))
    return found


def test_the_tour_calls_every_endpoint(tmp_path):
    spec = create_app(Settings(_env_file=None, uploads_dir=tmp_path)).openapi()
    requests = tour_requests()
    templates = sorted(spec["paths"], key=lambda path: path.count("{"))  # literal paths before placeholders

    called = set()
    for method, path in requests:
        for template in templates:
            pattern = re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(template))
            if re.fullmatch(pattern, path) and method.lower() in spec["paths"][template]:
                called.add((method, template))
                break
    every = {(method.upper(), path) for path, item in spec["paths"].items() for method in item}

    assert every - called == set()
    assert ("GET", "/health") in requests
    assert "scripts/watch_seats.py" in README


@pytest.mark.parametrize("field", list(Settings.model_fields))
def test_every_setting_is_documented(field):
    assert f"| `{field.upper()}` |" in README
    assert f"{field.upper()}=" in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_relative_links_work_in_a_clone():
    for target in re.findall(r"\]\(([^)]+)\)", README):
        if target.startswith("#"):
            heading = target[1:].replace("-", " ")
            assert re.search(rf"^#+ {heading}$", README, re.M | re.I), target
        elif not target.startswith("http"):
            assert (ROOT / target).exists(), target
            assert not target.startswith(".claude"), "that folder is not in the repository"
