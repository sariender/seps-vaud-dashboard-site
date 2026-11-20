"""Copy reviewed dashboard source into an empty, separate public checkout.

This intentionally copies files rather than cloning the private repository:
the public repository receives neither private files nor private Git history.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_pages import PUBLIC_WEB_FILES


CORE_FILES = (
    "README.md", "LICENSE", ".python-version", "requirements.txt", "run.py",
    "server.py", "water_service.py", "heat_service.py",
    "scripts/build_pages.py", "scripts/prepare_pages_repository.py",
    "deploy/github-pages/pages.yml", "deploy/github-pages/README.md",
    "tests/test_server.py", "tests/test_weather.py", "tests/test_live_water.py",
    "tests/test_live_heat.py", "tests/test_pages_build.py",
    "tests/test_pages_repository.py", "tests/test_temperature_field.cjs",
    "tests/test_weather_condition.cjs", "tests/test_dashboard_startup.cjs",
    "tests/test_dashboard_data.cjs", "tests/test_water_startup.cjs",
    # This saved public weather fixture supports explicit local design mode
    # and existing regression tests. build_pages excludes it from the website.
    "web/design-snapshot.json",
)
PUBLIC_SOURCE_FILES = CORE_FILES + tuple(f"web/{name}" for name in PUBLIC_WEB_FILES)
PUBLIC_GITIGNORE = "_site/\n.cache/\n__pycache__/\n*.py[cod]\n.venv/\nnode_modules/\n.DS_Store\n"


def _read_source(root: Path, relative: str) -> bytes:
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Refusing a public source symlink: {relative}")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Missing or unsafe public source: {relative}")
    return path.read_bytes()


def prepare(output: Path, *, source_root: Path = ROOT) -> list[str]:
    output, source_root = Path(output), Path(source_root)
    if source_root.is_symlink() or output.is_symlink():
        raise ValueError("Source and destination must not be symlinks")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("The public checkout destination must be empty")
    if output.resolve() == source_root.resolve():
        raise ValueError("Use a separate directory for the public checkout")
    # Validate every input before writing anything.
    contents = {name: _read_source(source_root, name) for name in PUBLIC_SOURCE_FILES}
    contents[".github/workflows/pages.yml"] = contents["deploy/github-pages/pages.yml"]
    contents[".gitignore"] = PUBLIC_GITIGNORE.encode("utf-8")
    output.mkdir(parents=True, exist_ok=True)
    for name, body in contents.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return sorted(contents)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    names = prepare(args.output)
    print(json.dumps({"files": len(names), "output": str(args.output),
                      "private_history_copied": False}))


if __name__ == "__main__":
    main()
