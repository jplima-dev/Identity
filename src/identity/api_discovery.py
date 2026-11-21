"""Discover HTTP contracts from OpenAPI documents and common source patterns."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
EXCLUDED = {".git", ".venv", "node_modules", "backups", "tests", "Identity", "__pycache__", "dist", "build", ".next", "vendor"}
JS_ROUTE = re.compile(r"\b(?:app|router|server|fastify)\s*\.\s*(get|post|put|patch|delete|head|options)\s*\(\s*['\"`]([^'\"`]+)['\"`]", re.IGNORECASE)
JS_EXPRESS = re.compile(r"\b(?:app|router)\s*\.\s*route\s*\(\s*['\"`]([^'\"`]+)['\"`]\s*\)\s*\.\s*(get|post|put|patch|delete)\s*\(", re.IGNORECASE)


def _constant(node: ast.AST | None) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError):
        return None


def _name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _name(node.value)
    return ""


def _field_schema(node: ast.AST) -> dict[str, Any]:
    name = _name(node)
    if name in {"int", "Integer"}:
        return {"type": "integer"}
    if name in {"float", "Decimal"}:
        return {"type": "number"}
    if name == "bool":
        return {"type": "boolean"}
    if name in {"list", "List", "Sequence", "tuple"}:
        item = node.slice if isinstance(node, ast.Subscript) else ast.Name(id="str", ctx=ast.Load())
        return {"type": "array", "items": _field_schema(item)}
    if name in {"Optional", "Union"} and isinstance(node, ast.Subscript):
        item = node.slice.elts[0] if isinstance(node.slice, ast.Tuple) else node.slice
        return _field_schema(item)
    if name == "Literal" and isinstance(node, ast.Subscript):
        values = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        return {"enum": [_constant(value) for value in values]}
    if name in {"EmailStr", "email"}:
        return {"type": "string", "format": "email"}
    if name in {"UUID", "uuid"}:
        return {"type": "string", "format": "uuid"}
    if name in {"date", "datetime"}:
        return {"type": "string", "format": "date-time" if name == "datetime" else "date"}
    if name in {"str", "SecretStr", "Any"}:
        return {"type": "string"}
    return {"$model": name} if name else {"type": "string"}


def _models(tree: ast.Module, known: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = dict(known or {})
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        parents = [_name(base) for base in node.bases]
        if not any(parent in {"BaseModel", "SQLModel"} or parent in models for parent in parents):
            continue
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parent in parents:
            if parent in models:
                properties.update(models[parent].get("properties", {}))
                required.extend(models[parent].get("required", []))
        for field in node.body:
            if not isinstance(field, ast.AnnAssign) or not isinstance(field.target, ast.Name):
                continue
            key = field.target.id
            properties[key] = _field_schema(field.annotation)
            is_field = isinstance(field.value, ast.Call) and _name(field.value.func) == "Field"
            has_default = is_field and any(keyword.arg == "default" for keyword in field.value.keywords)
            required_field = field.value is None or (is_field and not has_default and
                              (not field.value.args or isinstance(field.value.args[0], ast.Constant) and field.value.args[0].value is Ellipsis))
            if required_field:
                required.append(key)
            elif key in required:
                required.remove(key)
            if is_field:
                for keyword in field.value.keywords:
                    if keyword.arg in {"example", "default", "min_length", "max_length", "ge", "le"}:
                        value = _constant(keyword.value)
                        if value is not None:
                            properties[key][{"min_length": "minLength", "max_length": "maxLength", "ge": "minimum", "le": "maximum"}.get(keyword.arg, keyword.arg)] = value
        models[node.name] = {"type": "object", "properties": properties, "required": list(dict.fromkeys(required))}
    return models


def _resolve_model(schema: dict[str, Any], models: dict[str, dict[str, Any]], depth: int = 0) -> dict[str, Any]:
    if depth > 4:
        return {"type": "string"}
    if "$model" in schema:
        return _resolve_model(models.get(schema["$model"], {"type": "string"}), models, depth + 1)
    result = dict(schema)
    if "properties" in result:
        result["properties"] = {key: _resolve_model(value, models, depth + 1) for key, value in result["properties"].items()}
    if "items" in result:
        result["items"] = _resolve_model(result["items"], models, depth + 1)
    return result


def _python_routes(file: Path, project: Path, models: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    routers: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        if isinstance(target, ast.Name) and _name(node.value.func) in {"APIRouter", "FastAPI", "Blueprint", "Flask"}:
            routers[target.id] = next((_constant(kw.value) for kw in node.value.keywords if kw.arg in {"prefix", "url_prefix"}), "") or ""
    routes = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body_schema = None
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            if argument.annotation is not None and _name(argument.annotation) in models:
                body_schema = _resolve_model(models[_name(argument.annotation)], models)
                break
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr.lower()
            if method not in METHODS | {"route", "api_route"} or not decorator.args:
                continue
            path = _constant(decorator.args[0])
            if not isinstance(path, str) or not path.startswith("/"):
                continue
            owner = _name(decorator.func.value)
            prefix = routers.get(owner, "")
            full_path = (str(prefix).rstrip("/") + path).replace("//", "/")
            methods = [method] if method in METHODS else next((_constant(keyword.value) for keyword in decorator.keywords
                                                                 if keyword.arg == "methods"), ["GET"])
            if not isinstance(methods, (list, tuple)):
                continue
            for selected in methods:
                if not isinstance(selected, str) or selected.lower() not in METHODS:
                    continue
                routes.append({"method": selected.upper(), "path": full_path, "source": str(file.relative_to(project)),
                               "body_schema": body_schema if selected.lower() in {"post", "put", "patch"} else None,
                               "router_owner": owner})
    return routes


def _openapi_routes(project: Path, explicit: Path | None = None) -> list[dict[str, Any]]:
    routes = []
    files = [file for file in project.rglob("*.json")
             if file.name.lower() in {"openapi.json", "swagger.json"}
             and not any(part in EXCLUDED for part in file.relative_to(project).parts)]
    if explicit is not None and explicit not in files:
        files.append(explicit)
    for file in files:
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict) or "paths" not in document:
            continue
        components = document.get("components") or {}
        schemas = components.get("schemas") or document.get("definitions") or {}

        def resolve(schema: dict[str, Any], depth: int = 0, schemas: dict[str, Any] = schemas) -> dict[str, Any]:
            if depth > 5:
                return {"type": "string"}
            ref = schema.get("$ref")
            if isinstance(ref, str) and ref.startswith(("#/components/schemas/", "#/definitions/")):
                return resolve(schemas.get(ref.rsplit("/", 1)[-1], {}), depth + 1)
            if isinstance(schema.get("allOf"), list):
                parts = [resolve(part, depth + 1) for part in schema["allOf"]]
                return {"type": "object", "properties": {key: value for part in parts for key, value in part.get("properties", {}).items()},
                        "required": list(dict.fromkeys(key for part in parts for key in part.get("required", [])))}
            result = dict(schema)
            if "properties" in result:
                result["properties"] = {key: resolve(value, depth + 1) for key, value in result["properties"].items()}
            if "items" in result:
                result["items"] = resolve(result["items"], depth + 1)
            for key in ("oneOf", "anyOf"):
                if isinstance(result.get(key), list):
                    result[key] = [resolve(part, depth + 1) if isinstance(part, dict) else part
                                   for part in result[key]]
            return result

        for path, operations in document["paths"].items():
            if not isinstance(operations, dict):
                continue
            for method, operation in operations.items():
                if method.lower() not in METHODS or not isinstance(operation, dict):
                    continue
                request_body = operation.get("requestBody") or {}
                ref = request_body.get("$ref") if isinstance(request_body, dict) else None
                if isinstance(ref, str) and ref.startswith("#/components/requestBodies/"):
                    request_body = components.get("requestBodies", {}).get(ref.rsplit("/", 1)[-1], {})
                content = request_body.get("content", {}) if isinstance(request_body, dict) else {}
                schema = content.get("application/json", {}).get("schema")
                if schema is None:
                    schema = next((item.get("schema") for item in operation.get("parameters", [])
                                   if isinstance(item, dict) and item.get("in") == "body"), None)
                responses = operation.get("responses", {})
                statuses = sorted(int(code) for code in responses if str(code).isdigit() and 200 <= int(code) < 300)
                response = responses.get(str(statuses[0]), {}) if statuses else {}
                response_content = response.get("content", {}) if isinstance(response, dict) else {}
                response_schema = response_content.get("application/json", {}).get("schema")
                routes.append({"method": method.upper(), "path": path, "source": str(file.relative_to(project)),
                               "body_schema": resolve(schema) if isinstance(schema, dict) else None,
                               "expected_status": statuses or None,
                               "response_schema": resolve(response_schema) if isinstance(response_schema, dict) else None})
    return routes


def discover_contracts(project: Path, openapi_path: Path | None = None) -> list[dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    files = [file for file in project.rglob("*") if file.is_file() and file.suffix in {".py", ".js", ".ts"}
             and not any(part in EXCLUDED for part in file.relative_to(project).parts)]
    models: dict[str, dict[str, Any]] = {}
    trees = []
    for file in files:
        if file.suffix == ".py":
            try:
                trees.append(ast.parse(file.read_text(encoding="utf-8", errors="replace")))
            except SyntaxError:
                continue
    for _ in range(3):
        for tree in trees:
            models.update(_models(tree, models))
    included: dict[str, str] = {}
    for file in files:
        if file.suffix != ".py":
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        imports: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imports[alias.asname or alias.name] = node.module.rsplit(".", 1)[-1] if alias.name == "router" else alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".")[0]] = alias.name.rsplit(".", 1)[-1]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _name(node.func) != "include_router" or not node.args:
                continue
            prefix = next((_constant(keyword.value) for keyword in node.keywords if keyword.arg == "prefix"), "")
            if not isinstance(prefix, str) or not prefix:
                continue
            router_arg = node.args[0]
            alias = router_arg.value.id if isinstance(router_arg, ast.Attribute) and isinstance(router_arg.value, ast.Name) else _name(router_arg)
            module = imports.get(alias, alias)
            included[module] = prefix
    for file in files:
        if file.suffix == ".py":
            routes = _python_routes(file, project, models)
            if file.stem in included:
                for route in routes:
                    route["path"] = included[file.stem].rstrip("/") + route["path"]
        else:
            content = file.read_text(encoding="utf-8", errors="replace")
            routes = [{"method": method.upper(), "path": re.sub(r"/:([A-Za-z_]\w*)", r"/{\1}", path),
                       "source": str(file.relative_to(project)), "body_schema": None}
                      for method, path in JS_ROUTE.findall(content)]
            routes += [{"method": method.upper(), "path": re.sub(r"/:([A-Za-z_]\w*)", r"/{\1}", path),
                        "source": str(file.relative_to(project)), "body_schema": None}
                       for path, method in JS_EXPRESS.findall(content)]
        for route in routes:
            if not route["path"].startswith("/") or route["path"].startswith("//"):
                continue
            route.pop("router_owner", None)
            found[(route["method"], route["path"])] = route
    for route in _openapi_routes(project, openapi_path):
        found[(route["method"], route["path"])] = route
    return sorted(found.values(), key=lambda route: (route["path"], route["method"]))
