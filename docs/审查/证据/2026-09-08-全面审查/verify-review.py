"""Verify read-only review delivery without opening credential or library contents."""
import ast
import hashlib
import importlib.metadata
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

evidence = Path(__file__).resolve().parent
repo = evidence.parents[3]
inventory = json.loads((evidence / "source-inventory.json").read_text(encoding="utf-8-sig"))
baseline = json.loads((repo / "archive/2026-09-07-旧项目组/workspace/handoff-baseline.json").read_text(encoding="utf-8-sig"))

def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest().upper()

source_differences = [row["Path"] for row in inventory if digest(repo / row["Path"]) != row["SHA256"]]
protected_differences = [row["Path"] for row in baseline["ProtectedFiles"] if digest(repo / row["Path"]) != row["SHA256"]]
syntax_files = [repo / row["Path"] for row in inventory if Path(row["Path"]).suffix in {".py", ".spec"}]
for path in syntax_files:
    ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

documents = [repo / "AGENTS.md", repo / "README.md"] + list((repo / "docs").rglob("*.md"))
missing_links, invalid_utf8, bad_fences = [], [], []
for path in documents:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        invalid_utf8.append(str(path.relative_to(repo)))
        continue
    if len(re.findall(r"^```", content, re.M)) % 2:
        bad_fences.append(str(path.relative_to(repo)))
    for target in re.findall(r"\]\(([^)]+)\)", content):
        target = target.strip().strip("<>")
        if target.startswith(("http:", "https:", "#", "mailto:", "app:")):
            continue
        target = unquote(target.split("#", 1)[0])
        target = re.sub(r":\d+$", "", target)
        if target and not (path.parent / target).exists():
            missing_links.append({"document":str(path.relative_to(repo)), "target":target})

counts = Counter(Path(row["Path"]).suffix for row in inventory)
versions = {name:importlib.metadata.version(name) for name in (
    "gallery-dl", "requests", "sqlalchemy", "fastapi", "pydantic", "starlette",
    "playwright", "pillow", "pytest", "pyinstaller")}
result = {
    "verified_at":datetime.now(timezone.utc).isoformat(),
    "git_head":subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
    "source_inventory_files":len(inventory), "source_text_lines":sum(row.get("Lines") or 0 for row in inventory),
    "source_types":dict(counts), "source_files_changed":source_differences,
    "protected_baseline_files":len(baseline["ProtectedFiles"]), "protected_files_changed":protected_differences,
    "syntax_files_parsed":len(syntax_files), "versions":versions,
    "documents_checked":len(documents), "missing_local_links":missing_links,
    "invalid_utf8":invalid_utf8, "unbalanced_code_fences":bad_fences,
    "credential_contents_in_report":False,
    "note":"Only application and protected-file digests checked. Review documentation changes are intentional."
}
output = evidence / "review-verification.json"
output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(1 if any((source_differences, protected_differences, result["missing_local_links"], invalid_utf8, bad_fences)) else 0)
