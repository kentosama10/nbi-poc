"""Documentation tests: the docs exist, and what they promise is true."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"
REQUIRED = [README, DOCS / "architecture.md", DOCS / "demo_script.md",
            DOCS / "limitations.md", DOCS / "screenshot_checklist.md",
            ROOT / "scripts" / "bootstrap.py"]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_required_docs_exist_and_are_not_empty():
    for path in REQUIRED:
        assert path.exists(), f"missing {path.relative_to(ROOT)}"
        assert len(read(path).strip()) > 200, f"{path.name} looks empty"


def test_readme_setup_and_run_takes_fewer_than_10_commands():
    block = re.search(r"<!-- setup:start -->(.*?)<!-- setup:end -->", read(README), re.S)
    assert block, "README needs the setup:start / setup:end markers"
    commands_block = re.search(r"```powershell\n(.*?)```", block.group(1), re.S)
    assert commands_block, "README needs a powershell block between the markers"
    commands = [line for line in commands_block.group(1).splitlines()
                if line.strip() and not line.strip().startswith("#")]
    assert 1 <= len(commands) < 10, f"{len(commands)} commands"


def test_architecture_diagrams_name_all_four_stores():
    for path in (README, DOCS / "architecture.md"):
        text = read(path)
        assert "```mermaid" in text, f"{path.name} has no Mermaid diagram"
        for store in ("PostgreSQL", "Redis", "MongoDB", "Elasticsearch"):
            assert store in text, f"{path.name} does not mention {store}"
    assert read(DOCS / "architecture.md").count("```mermaid") >= 4


def test_every_script_the_docs_mention_exists():
    pattern = re.compile(r"python -m scripts\.(\w+)")
    seen = set()
    for path in [README, *DOCS.glob("*.md")]:
        seen |= set(pattern.findall(read(path)))
    assert seen, "docs mention no scripts?"
    for name in sorted(seen):
        assert (ROOT / "scripts" / f"{name}.py").exists(), f"scripts/{name}.py is missing"


def test_demo_script_covers_all_five_scenarios_within_five_minutes():
    text = read(DOCS / "demo_script.md")
    for n in range(1, 6):
        assert f"Scenario {n}" in text or f"scenario {n}" in text, f"scenario {n} missing"
    assert "5:00" in text