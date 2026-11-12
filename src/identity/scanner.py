"""Source inspection for a project containing an Identity installation."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

EXCLUDED = {".git", ".venv", "node_modules", "backups", "tests", "Identity", "__pycache__"}
DECORATOR = re.compile(r"@(?:\w+\.)?(get|post|put|patch|delete|head|options)\(\s*['\"]([^'\"]+)['\"]")
GET_ROUTE = re.compile(r"(?:parsed\.)?path\s*==\s*['\"](/api/[^'\"]+)['\"]")
LOCAL_URL = re.compile(r"https?://(?:127\.0\.0\.1|localhost):(\d+)")


def source_files(project: Path) -> list[Path]:
    files = []
    for path in project.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".js", ".ts"} and not any(part in EXCLUDED for part in path.relative_to(project).parts):
            files.append(path)
    return files


def discover_api(project: Path) -> list[dict[str, str]]:
    found: set[tuple[str, str, str]] = set()
    for file in source_files(project):
        content = file.read_text(encoding="utf-8", errors="replace")
        source = str(file.relative_to(project))
        prefix = ""
        if file.suffix == ".py":
            try:
                tree = ast.parse(content)
            except SyntaxError:
                tree = None
            if tree:
                for node in tree.body:
                    if isinstance(node, (ast.Assign, ast.AnnAssign)):
                        value = node.value
                        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "APIRouter":
                            prefix = next((arg.value.value for arg in value.keywords if arg.arg == "prefix" and isinstance(arg.value, ast.Constant) and isinstance(arg.value.value, str)), "")
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for decorator in node.decorator_list:
                            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr.lower() in {"get", "post", "put", "patch", "delete", "head", "options"} and decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                                route = decorator.args[0].value
                                if route.startswith("/"):
                                    found.add((decorator.func.attr.upper(), prefix.rstrip("/") + route, source))
        for method, route in DECORATOR.findall(content):
            if file.suffix != ".py" and route.startswith("/"):
                found.add((method.upper(), route, source))
        if file.name == "server.py" and "def do_GET" in content:
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
    bridge = project / "Agent" / "servicos" / "comandos_workspace.py"
    if bridge.is_file():
        content = bridge.read_text(encoding="utf-8", errors="replace")
        port = re.search(r"^PORTA\s*=\s*(\d+)", content, re.MULTILINE)
        if port and 'self.path != "/command"' in content:
            return {"url": f"http://127.0.0.1:{port.group(1)}/command", "kind": "yasmin-command", "source": str(bridge.relative_to(project)), "capabilities": capabilities, "examples": examples}
    for file in source_files(project):
        content = file.read_text(encoding="utf-8", errors="replace")
        if "def responder(" in content or "def processar_comando(" in content:
            return {"kind": "python-response", "source": str(file.relative_to(project))}
    return None
