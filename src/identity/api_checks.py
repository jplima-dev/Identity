"""Response checks derived from a discovered HTTP contract."""

from __future__ import annotations

import re
from typing import Any

import httpx


def _check_schema(value: Any, schema: dict[str, Any], location: str = "resposta") -> str | None:
    if schema.get("nullable") and value is None:
        return None
    for branch in schema.get("allOf", []):
        if isinstance(branch, dict) and (error := _check_schema(value, branch, location)):
            return error
    alternatives = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(alternatives, list) and not any(
            isinstance(branch, dict) and _check_schema(value, branch, location) is None
            for branch in alternatives):
        return f"{location} não corresponde a nenhuma alternativa do esquema"
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return f"{location} deveria ser um objeto"
        for key in schema.get("required", []):
            if key not in value:
                return f"{location}.{key} ausente"
        for key, field in schema.get("properties", {}).items():
            if key in value and isinstance(field, dict):
                error = _check_schema(value[key], field, f"{location}.{key}")
                if error:
                    return error
    elif kind == "array":
        if not isinstance(value, list):
            return f"{location} deveria ser uma lista"
        item_schema = schema.get("items", {})
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                error = _check_schema(item, item_schema, f"{location}[{index}]")
                if error:
                    return error
    elif kind == "string" and not isinstance(value, str):
        return f"{location} deveria ser texto"
    elif kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return f"{location} deveria ser inteiro"
    elif kind == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        return f"{location} deveria ser número"
    elif kind == "boolean" and not isinstance(value, bool):
        return f"{location} deveria ser booleano"
    if "enum" in schema and value not in schema["enum"]:
        return f"{location} fora dos valores permitidos"
    if "const" in schema and value != schema["const"]:
        return f"{location} difere do valor obrigatório"
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{location} é menor que minLength"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{location} excede maxLength"
        if "pattern" in schema:
            try:
                matches = re.search(schema["pattern"], value) is not None
            except re.error:
                matches = True
            if not matches:
                return f"{location} não corresponde ao pattern"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, condition in (("minimum", value < schema.get("minimum", value)),
                               ("maximum", value > schema.get("maximum", value))):
            if key in schema and condition:
                return f"{location} viola {key}"
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{location} tem menos itens que minItems"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{location} excede maxItems"
    return None


def _check_subset(actual: Any, expected: Any, location: str = "resposta") -> str | None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return f"{location} deveria ser um objeto"
        for key, value in expected.items():
            if key not in actual:
                return f"{location}.{key} ausente"
            error = _check_subset(actual[key], value, f"{location}.{key}")
            if error:
                return error
    elif actual != expected:
        return f"{location}: esperado {expected!r}, recebido {actual!r}"
    return None


def check_response(response: httpx.Response, route: dict[str, Any] | None,
                   default_statuses: set[int]) -> tuple[bool, str | None, list[int]]:
    declared = route.get("expected_status") if route else None
    statuses = set(declared) if isinstance(declared, list) and declared else default_statuses
    if response.status_code not in statuses:
        return False, f"HTTP {response.status_code}; esperado {sorted(statuses)}", sorted(statuses)
    schema = route.get("response_schema") if route and 200 <= response.status_code < 300 else None
    expected_json = route.get("expected_json") if route else None
    if route:
        for key, expected in route.get("expected_headers", {}).items():
            actual = response.headers.get(key)
            if actual != expected:
                return False, f"cabeçalho {key}: esperado {expected!r}, recebido {actual!r}", sorted(statuses)
        expected_text = route.get("expected_text_contains")
        if expected_text is not None and expected_text not in response.text:
            return False, f"resposta não contém {expected_text!r}", sorted(statuses)
    if (isinstance(schema, dict) or expected_json is not None) and response.status_code != 204:
        try:
            body = response.json()
        except ValueError:
            return False, "resposta não contém JSON válido", sorted(statuses)
        error = _check_schema(body, schema) if isinstance(schema, dict) else None
        error = error or (_check_subset(body, expected_json) if expected_json is not None else None)
        if error:
            return False, error, sorted(statuses)
    return True, None, sorted(statuses)


def plan_route_cases(contracts: list[dict[str, Any]], configured: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach explicit pytest-like examples to discovered routes."""
    known = {f"{route['method']} {route['path']}": route for route in contracts}
    cases = []
    for key, definitions in configured.items():
        if key not in known:
            raise ValueError(f"api_cases menciona uma rota não descoberta: {key}")
        if not isinstance(definitions, list) or not definitions:
            raise ValueError(f"api_cases[{key}] deve ser uma lista não vazia.")
        for index, definition in enumerate(definitions, 1):
            if not isinstance(definition, dict):
                raise TypeError(f"api_cases[{key}][{index}] deve ser um objeto JSON.")
            path = definition.get("path", known[key]["path"])
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "{" in path or "}" in path:
                raise ValueError(f"api_cases[{key}][{index}] requer um caminho concreto iniciado por /.")
            statuses = definition.get("expected_status")
            if statuses is not None and (not isinstance(statuses, list) or not statuses or
                                         any(type(value) is not int or value < 100 or value > 599 for value in statuses)):
                raise ValueError(f"api_cases[{key}][{index}] tem expected_status inválido.")
            if known[key]["method"] not in {"GET", "HEAD", "OPTIONS"} and definition.get("isolated") is not True:
                raise ValueError(f"api_cases[{key}][{index}] altera dados; use isolated: true em ambiente descartável.")
            for field in ("query", "headers", "expected_headers", "form"):
                value = definition.get(field, {})
                if not isinstance(value, dict) or any(
                        not isinstance(name, str) or not isinstance(item, str if field in {"headers", "expected_headers"}
                                          else (str, int, float, bool))
                        for name, item in value.items()):
                    raise TypeError(f"api_cases[{key}][{index}].{field} deve ser um objeto de valores simples.")
            files = definition.get("files", {})
            if not isinstance(files, dict) or any(
                    not isinstance(name, str) or not isinstance(item, dict)
                    or not isinstance(item.get("path"), str)
                    or (item.get("content_type") is not None and not isinstance(item["content_type"], str))
                    for name, item in files.items()):
                raise TypeError(f"api_cases[{key}][{index}].files deve mapear campos para caminhos de arquivo.")
            if ("json" in definition) and (definition.get("form") or files):
                raise ValueError(f"api_cases[{key}][{index}] não pode misturar json com form/files.")
            expected_text = definition.get("expected_text_contains")
            if expected_text is not None and not isinstance(expected_text, str):
                raise TypeError(f"api_cases[{key}][{index}].expected_text_contains deve ser texto.")
            latency = definition.get("max_latency_ms")
            if latency is not None and (type(latency) not in (int, float) or latency <= 0):
                raise ValueError(f"api_cases[{key}][{index}].max_latency_ms deve ser positivo.")
            cleanup = definition.get("cleanup")
            if cleanup is not None:
                if (not isinstance(cleanup, dict) or cleanup.get("method", "DELETE") not in {"DELETE", "POST"}
                        or not isinstance(cleanup.get("path"), str) or not cleanup["path"].startswith("/")):
                    raise ValueError(f"api_cases[{key}][{index}].cleanup requer method e path HTTP.")
                if cleanup.get("verify_path") is not None and (
                        not isinstance(cleanup["verify_path"], str) or
                        not cleanup["verify_path"].startswith("/")):
                    raise ValueError(f"api_cases[{key}][{index}].cleanup.verify_path deve começar com /.")
                statuses_cleanup = cleanup.get("expected_status", [200, 202, 204])
                if (not isinstance(statuses_cleanup, list) or not statuses_cleanup or
                        any(type(item) is not int or not 100 <= item <= 599 for item in statuses_cleanup)):
                    raise ValueError(f"api_cases[{key}][{index}].cleanup.expected_status inválido.")
                cleanup = dict(cleanup)
                shape = re.sub(r"\{response\.[^{}]+\}", "{}", cleanup["path"])
                matched = next((route_key for route_key in known
                                if route_key.startswith(cleanup.get("method", "DELETE") + " ")
                                and re.sub(r"\{[^{}]+\}", "{}", route_key.split(" ", 1)[1]) == shape), None)
                cleanup["route"] = matched.split(" ", 1)[1] if matched else cleanup["path"]
                if cleanup.get("verify_path"):
                    verify_shape = re.sub(r"\{response\.[^{}]+\}", "{}", cleanup["verify_path"])
                    matched_get = next((route_key for route_key in known
                                        if route_key.startswith("GET ")
                                        and re.sub(r"\{[^{}]+\}", "{}", route_key.split(" ", 1)[1])
                                        == verify_shape), None)
                    cleanup["verify_route"] = (matched_get.split(" ", 1)[1] if matched_get
                                               else cleanup["verify_path"])
            cases.append({**known[key], "test_path": path, "test_body": definition.get("json"),
                          "expected_status": statuses or known[key].get("expected_status"),
                          "expected_json": definition.get("expected_json"),
                          "expected_headers": definition.get("expected_headers", {}),
                          "expected_text_contains": expected_text,
                          "max_latency_ms": latency,
                          "query": definition.get("query", {}),
                          "headers": definition.get("headers", {}),
                          "form": definition.get("form", {}),
                          "files": files,
                          "cleanup": cleanup,
                          "name": definition.get("name", f"{key} #{index}")})
    return cases
