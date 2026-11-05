"""`make new-agent NAME=x`: copy agents/_template into agents/x and a test into tests/.

The template is a small working agent (add / list / delete), so the new folder
loads, passes its test and answers on Telegram before a single line is
changed. Then you replace the TODOs. Nothing in the core needs to change:
the registry finds the folder on the next start (ADR-0009, ADR-0020).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from erranderpy.core.registry import AGENTS_DIR, RESERVED_NAMES

TEMPLATE = AGENTS_DIR / "_template"
TESTS_DIR = AGENTS_DIR.parent / "tests"
NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
TEST_TEMPLATE = "test_agent.py.tmpl"


def create(name: str, agents_dir: Path = AGENTS_DIR, tests_dir: Path = TESTS_DIR) -> list[Path]:
    """Create the agent folder and its test. Returns the files written."""
    if not NAME.fullmatch(name):
        raise ValueError(f"'{name}': use 2-31 lowercase letters, digits or _, starting with a letter (e.g. notes)")
    if name in RESERVED_NAMES or name.startswith("_"):
        raise ValueError(f"'{name}' is reserved")
    target = agents_dir / name
    if target.exists():
        raise ValueError(f"{target} already exists")
    test_file = tests_dir / f"test_agent_{name}.py"

    title = name.replace("_", " ").capitalize()
    written = []
    target.mkdir(parents=True)
    for source in sorted(TEMPLATE.iterdir()):
        if source.name.startswith((".", "__")) or not source.is_file():
            continue
        text = source.read_text(encoding="utf-8").replace("{{name}}", name).replace("{{Name}}", title)
        destination = test_file if source.name == TEST_TEMPLATE else target / source.name
        destination.write_text(text, encoding="utf-8")
        written.append(destination)
    return written


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].strip():
        print("Usage: make new-agent NAME=notes")
        return 2
    name = args[0].strip()
    try:
        written = create(name)
    except ValueError as exc:
        print(f"✗ {exc}")
        return 1
    root = AGENTS_DIR.parent
    print(f"✓ Created agent '{name}':")
    for path in written:
        print(f"  {path.relative_to(root)}")
    print(
        "\nNext:\n"
        f"  1. agents/{name}/agent.yaml  fill description and examples (the router reads them)\n"
        f"  2. agents/{name}/tools.py    shape the tools around what the agent keeps\n"
        f"  3. agents/{name}/evals.yaml  add 5-10 real messages and the expected tool call\n"
        "  4. make test && make eval\n"
        "  5. restart the bot: the agent is picked up automatically"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
