"""Small local semantic model for synthetic API request bodies.

The model ranks field meanings from names and descriptions, then applies schema
constraints. It is deterministic and never sends project data to an AI service.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

FIELD_HINTS = {
    "email": {"email", "e-mail", "mail", "correio"},
    "name": {"name", "nome", "title", "titulo", "título", "username", "usuario", "usuário"},
    "description": {"description", "descricao", "descrição", "details", "detalhes", "content", "conteudo"},
    "slug": {"slug", "code", "codigo", "código", "reference", "referencia", "referência"},
    "password": {"password", "senha", "secret", "segredo"},
    "phone": {"phone", "telefone", "mobile", "celular"},
    "url": {"url", "website", "site", "link"},
    "quantity": {"quantity", "quantidade", "count", "contagem", "stock", "estoque"},
    "price": {"price", "preco", "preço", "amount", "valor", "total"},
}
MARKER_TYPES = {"name", "description", "slug", "email"}


def _meaning(key: str, schema: dict[str, Any]) -> str | None:
    words = set(re.findall(r"[\w-]+", f"{key} {schema.get('description', '')}".casefold()))
    scores = {kind: len(words & hints) for kind, hints in FIELD_HINTS.items()}
    chosen = max(scores, key=scores.get)
    return chosen if scores[chosen] else None


def _value(schema: dict[str, Any], key: str, marker: str, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    choices = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(choices, list):
        chosen = next((part for part in choices if isinstance(part, dict) and part.get("type") != "null"), None)
        return _value(chosen, key, marker, depth + 1) if chosen else None
    kind = schema.get("type", "string")
    meaning = _meaning(key, schema)
    if kind == "object":
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        wanted = required | {name for name, field in properties.items()
                             if _meaning(name, field) in MARKER_TYPES}
        return {name: _value(field, name, marker, depth + 1) for name, field in properties.items()
                if name in wanted and isinstance(field, dict)}
    if kind == "array":
        count = max(1, min(int(schema.get("minItems", 1)), 3))
        items = schema.get("items", {"type": "string"})
        return [_value(items, key, marker, depth + 1) for _ in range(count)]
    if kind in {"integer", "number"}:
        minimum = schema.get("minimum", schema.get("exclusiveMinimum", 0))
        try:
            candidate = float(minimum) + (1 if "exclusiveMinimum" in schema else 0)
            maximum = schema.get("maximum")
            if maximum is not None and candidate > float(maximum):
                candidate = float(maximum)
        except (TypeError, ValueError):
            candidate = 1
        return int(candidate) if kind == "integer" else candidate
    if kind == "boolean":
        return True
    if schema.get("format") == "uuid":
        return str(uuid.uuid5(uuid.NAMESPACE_URL, marker + key))
    if schema.get("format") == "date":
        return "2030-01-01"
    if schema.get("format") == "date-time":
        return "2030-01-01T00:00:00Z"
    if schema.get("format") == "email" or meaning == "email":
        return f"identity+{marker}@example.com"
    if schema.get("format") in {"uri", "url"} or meaning == "url":
        return f"https://example.com/identity/{marker}"
    if meaning == "password":
        return f"Identity_{marker}_Aa1!"
    if meaning == "phone":
        return "11999999999"
    if meaning == "slug":
        value = f"identity-{marker}"
    elif meaning in {"name", "description"}:
        value = f"Identity {marker}"
    else:
        value = schema.get("example", f"identity_{marker}")
    if not isinstance(value, str):
        value = f"identity_{marker}"
    minimum = max(0, int(schema.get("minLength", 0)))
    maximum = int(schema.get("maxLength", 1000))
    return value.ljust(minimum, "x")[:maximum]


def synthesize_body(schema: dict[str, Any], marker: str) -> dict[str, Any] | None:
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        return None
    body = _value(schema, "", marker)
    if not isinstance(body, dict):
        return None
    if marker not in json.dumps(body, ensure_ascii=False):
        for key, field in schema["properties"].items():
            if isinstance(field, dict) and field.get("type", "string") == "string" and not field.get("enum"):
                body[key] = _value(field, key, marker)
                if marker in json.dumps(body, ensure_ascii=False):
                    break
    return body
