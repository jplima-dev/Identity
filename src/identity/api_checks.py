"""Response checks derived from a discovered HTTP contract."""

from __future__ import annotations

from typing import Any

import httpx


def _check_schema(value: Any, schema: dict[str, Any], location: str = "resposta") -> str | None:
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
            cases.append({**known[key], "test_path": path, "test_body": definition.get("json"),
                          "expected_status": statuses or known[key].get("expected_status"),
                          "expected_json": definition.get("expected_json"),
                          "name": definition.get("name", f"{key} #{index}")})
    return cases
