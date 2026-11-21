"""Static feature inventory and local synthetic conversation generation.

Source code is parsed as text/AST; project functions and tools are never invoked.
Generated records contain capability labels, not invented final answers.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from .scanner import source_files

CORPUS_NAMES = {"PEDIDOS", "CAPABILITIES", "INTENTS", "EXAMPLES", "EXEMPLOS"}
TOOL_NAMES = {"Ferramenta", "Tool", "register_tool", "function_tool"}
RESPONSE_NAMES = {"responder", "processar_comando", "chat", "invoke", "respond", "reply"}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _name(node.func)
    return ""


def discover_features(project: Path, manifest: dict[str, Any] | None = None,
                      legacy: dict[str, Any] | None = None) -> dict[str, Any]:
    features: dict[str, dict[str, Any]] = {}
    mechanisms: list[dict[str, str]] = []

    def add(name: str, description: str, examples: list[str], source: str) -> None:
        if not name.strip():
            return
        description = description if isinstance(description, str) else ""
        item = features.setdefault(name, {"name": name, "description": description,
                                          "examples": [], "sources": []})
        if description and not item["description"]:
            item["description"] = description
        item["examples"] = list(dict.fromkeys([*item["examples"], *examples]))
        if source not in item["sources"]:
            item["sources"].append(source)

    for file in source_files(project):
        if file.suffix != ".py":
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        source = str(file.relative_to(project))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if (any(isinstance(target, ast.Name) and target.id in CORPUS_NAMES for target in targets)
                        and isinstance(value, ast.Dict)):
                    try:
                        corpus = ast.literal_eval(value)
                    except (ValueError, SyntaxError, TypeError):
                        corpus = {}
                    if isinstance(corpus, dict):
                        for label, phrases in corpus.items():
                            if isinstance(label, str) and isinstance(phrases, (list, tuple)):
                                add(label, "", [phrase for phrase in phrases if isinstance(phrase, str)], source)
            if isinstance(node, ast.Call) and _name(node.func) in TOOL_NAMES and node.args:
                try:
                    label = ast.literal_eval(node.args[0])
                    description = ast.literal_eval(node.args[1]) if len(node.args) > 1 else ""
                except (ValueError, SyntaxError, TypeError):
                    continue
                if isinstance(label, str):
                    add(label, description if isinstance(description, str) else "", [], source)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in RESPONSE_NAMES:
                    mechanisms.append({"kind": "python-response", "source": source, "symbol": node.name})
                if any(_name(decorator) in TOOL_NAMES | {"tool"} for decorator in node.decorator_list):
                    doc = (ast.get_docstring(node) or "").splitlines()
                    add(node.name, doc[0] if doc else "", [], source)
    if legacy:
        for label, phrases in legacy.get("examples", {}).items():
            add(label, "", phrases, legacy.get("source", "scanner"))
        for tool in legacy.get("registered_tools", []):
            add(tool["name"], tool.get("description", ""), [], tool.get("source", "scanner"))
        mechanisms.append({"kind": legacy.get("kind", "desconhecido"), "source": legacy.get("source", "scanner")})
    if manifest:
        mechanisms.insert(0, {"kind": "http-safe", "source": "identity-agent.json",
                              "endpoint": manifest["endpoint"]})
        declared = manifest.get("capabilities", [])
        if not isinstance(declared, list):
            raise ValueError("identity-agent.json capabilities deve ser uma lista.")
        for entry in declared:
            if isinstance(entry, str):
                add(entry, "", [], "identity-agent.json")
            elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
                examples = entry.get("examples", [])
                if not isinstance(examples, list) or not all(isinstance(value, str) for value in examples):
                    raise ValueError("capabilities.examples deve conter textos.")
                add(entry["name"], entry.get("description", ""), examples, "identity-agent.json")
            else:
                raise TypeError("Cada capability deve ter um nome.")
    return {"mechanisms": list({json.dumps(item, sort_keys=True): item for item in mechanisms}.values()),
            "capabilities": sorted(features.values(), key=lambda item: item["name"])}


def generate_conversations(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    seen: set[tuple[str, str]] = set()
    for feature in inventory["capabilities"]:
        name = feature["name"]
        seeds = feature["examples"][:8]
        if not seeds:
            description = feature["description"].strip().rstrip(".!?")
            seeds = [description or name.replace("_", " ")]
        for seed in seeds:
            base = seed.strip().rstrip(".!?")
            if not base:
                continue
            for variant, text in (("base", base), ("cortesia", f"Por favor, {base}"),
                                  ("contexto", f"Preciso de ajuda: {base}")):
                key = (name, text.casefold())
                if key in seen:
                    continue
                seen.add(key)
                records.append({"capability": name, "variant": variant,
                                "messages": [{"role": "user", "content": text}],
                                "source": feature["sources"], "reviewed": False})
        if seeds:
            records.append({"capability": name, "variant": "multiturno",
                            "messages": [{"role": "user", "content": "Pode me ajudar com uma tarefa?"},
                                         {"role": "assistant", "content": "Claro. O que você precisa?"},
                                         {"role": "user", "content": seeds[0]}],
                            "source": feature["sources"], "reviewed": False})
    return records


def save_conversations(folder: Path, inventory: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path]:
    destination = folder / "conversas"
    destination.mkdir(parents=True, exist_ok=True)
    inventory_file = destination / "funcionalidades.json"
    conversations_file = destination / "dados_gerados.jsonl"
    inventory_file.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    append_conversation_records(conversations_file, records)
    return inventory_file, conversations_file


def append_conversation_records(path: Path, records: list[dict[str, Any]]) -> int:
    """Add distinct records atomically while retaining the original JSONL bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_bytes() if path.is_file() else b""

    def key(record: dict[str, Any]) -> str:
        return json.dumps([record.get("capability"), record.get("messages")],
                          ensure_ascii=False, sort_keys=True)

    known = set()
    for line in original.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(item, dict):
            known.add(key(item))
    additions = []
    for record in records:
        signature = key(record)
        if signature not in known:
            additions.append(record)
            known.add(signature)
    if not additions:
        return 0
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(original)
        if original and not original.endswith(b"\n"):
            stream.write(b"\n")
        for record in additions:
            stream.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    temporary.replace(path)
    return len(additions)
