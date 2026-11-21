"""Source inspection for a project containing an Identity installation."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .api_discovery import discover_contracts

EXCLUDED = {".git", ".venv", "node_modules", "backups", "tests", "Identity", "__pycache__", "dist", "build", ".next", "vendor"}
GET_ROUTE = re.compile(r"(?:parsed\.)?path\s*==\s*['\"](/api/[^'\"]+)['\"]")
LOCAL_URL = re.compile(r"https?://(?:127\.0\.0\.1|localhost):(\d+)")


def source_files(project: Path) -> list[Path]:
    files = []
    for path in project.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".js", ".ts"} and not any(part in EXCLUDED for part in path.relative_to(project).parts):
            files.append(path)
    return files


def discover_api(project: Path) -> list[dict[str, str]]:
    found = {(route["method"], route["path"], route["source"]) for route in discover_contracts(project)}
    for file in source_files(project):
        if file.name != "server.py":
            continue
        content = file.read_text(encoding="utf-8", errors="replace")
        source = str(file.relative_to(project))
        if "def do_GET" in content:
            get_block = content.split("def do_GET", 1)[1].split("def do_POST", 1)[0]
            for route in GET_ROUTE.findall(get_block):
                found.add(("GET", route, source))
    return [{"method": method, "path": route, "source": source} for method, route, source in sorted(found)]


def discover_base_url(project: Path) -> str | None:
    readmes = sorted(project.rglob("README.md"), key=lambda path: len(path.parts))
    for path in readmes:
        if any(part in EXCLUDED for part in path.relative_to(project).parts):
            continue
        match = LOCAL_URL.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            return f"http://127.0.0.1:{match.group(1)}"
    return None


def discover_agent(project: Path) -> dict[str, Any] | None:
    capabilities = []
    examples: dict[str, list[str]] = {}
    evaluation: dict[str, list[str]] = {}
    registered_tools: dict[str, dict[str, str]] = {}
    corpus = project / "Agent" / "agente" / "treino_capacidades.py"
    if corpus.is_file():
        tree = ast.parse(corpus.read_text(encoding="utf-8", errors="replace"))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "PEDIDOS" and isinstance(node.value, ast.Dict):
                for key, value in zip(node.value.keys, node.value.values):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        phrases = ast.literal_eval(value)
                        if isinstance(phrases, tuple) and all(isinstance(phrase, str) for phrase in phrases):
                            examples[key.value] = list(phrases)
                capabilities = list(examples)
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "AVALIACAO" for target in node.targets):
                try:
                    pairs = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    pairs = ()
                if isinstance(pairs, (list, tuple)):
                    for pair in pairs:
                        if (isinstance(pair, (list, tuple)) and len(pair) == 2
                                and isinstance(pair[0], str) and isinstance(pair[1], str) and pair[1] in examples):
                            evaluation.setdefault(pair[1], []).append(pair[0])
    for file in source_files(project):
        if file.suffix != ".py":
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Ferramenta"
                    and len(node.args) >= 2 and all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args[:2])):
                name = node.args[0].value
                registered_tools[name] = {"name": name, "description": node.args[1].value,
                                          "source": str(file.relative_to(project))}
    bridge = project / "Agent" / "servicos" / "comandos_workspace.py"
    if bridge.is_file():
        content = bridge.read_text(encoding="utf-8", errors="replace")
        port = re.search(r"^PORTA\s*=\s*(\d+)", content, re.MULTILINE)
        if port and re.search(r"['\"]/command['\"]", content):
            return {"url": f"http://127.0.0.1:{port.group(1)}/command", "kind": "yasmin-command", "source": str(bridge.relative_to(project)),
                    "capabilities": capabilities, "examples": examples, "evaluation": evaluation,
                    "registered_tools": sorted(registered_tools.values(), key=lambda item: item["name"]),
                    "uncovered_tools": sorted(set(registered_tools) - set(examples))}
    for file in source_files(project):
        content = file.read_text(encoding="utf-8", errors="replace")
        if "def responder(" in content or "def processar_comando(" in content:
            return {"kind": "python-response", "source": str(file.relative_to(project))}
    return None
