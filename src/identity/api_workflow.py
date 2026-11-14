"""Exercise one API resource per agent and clean up only records it created."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .progress import ProgressDisplay

PARAM = re.compile(r"\{([^{}]+)\}")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


@dataclass(frozen=True)
class ResourceFlow:
    collection: str
    create: dict[str, Any]
    update: dict[str, Any]
    read: dict[str, Any] | None
    list_route: dict[str, Any] | None
    delete: dict[str, Any]
    parameter: str
    create_body: dict[str, Any] | None = None
    update_body: dict[str, Any] | None = None
    parent: ResourceFlow | None = None


def _sample(schema: dict[str, Any], key: str, marker: str, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if key.casefold() in {"name", "nome", "username", "email", "title", "titulo"} and not schema.get("enum"):
        if schema.get("format") == "email" or key.casefold() == "email":
            return f"identity+{marker}@example.com"
        return f"identity_{marker}"
    if "example" in schema:
        return schema["example"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    choices = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(choices, list):
        choice = next((item for item in choices if item.get("type") != "null"), None)
        return _sample(choice, key, marker, depth + 1) if choice else None
    kind = schema.get("type", "string")
    if kind == "object":
        properties = schema.get("properties", {})
        wanted = set(schema.get("required", []))
        wanted.update(name for name in properties if name.casefold() in {"name", "nome", "username", "email", "title", "titulo"})
        return {name: _sample(properties[name], name, marker, depth + 1) for name in properties if name in wanted}
    if kind == "array":
        return [_sample(schema.get("items", {"type": "string"}), key, marker, depth + 1)]
    if kind == "integer":
        return max(int(schema.get("minimum", 1)), 1)
    if kind == "number":
        return max(float(schema.get("minimum", 1)), 1.0)
    if kind == "boolean":
        return True
    if schema.get("format") == "email" or "email" in key.casefold():
        return f"identity+{marker}@example.com"
    if schema.get("format") == "uuid":
        return str(uuid.uuid4())
    if schema.get("format") == "date":
        return "2030-01-01"
    if schema.get("format") == "date-time":
        return "2030-01-01T00:00:00Z"
    if "password" in key.casefold() or "senha" in key.casefold():
        return f"Identity_{marker}_Aa1!"
    text = f"identity_{marker}"
    return text.ljust(max(int(schema.get("minLength", 1)), len(text)), "x")[:int(schema.get("maxLength", 1000))]


def _body(route: dict[str, Any], marker: str, override: dict[str, Any] | None) -> dict[str, Any] | None:
    if override is not None:
        return json.loads(json.dumps(override).replace("{identity_marker}", marker))
    schema = route.get("body_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object" or not schema.get("properties"):
        return None
    return _sample(schema, "", marker)


def _valid_id(value: Any) -> str | None:
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return text if SAFE_ID.fullmatch(text) else None


def plan_resources(contracts: list[dict[str, Any]], payloads: dict[str, Any] | None = None) -> tuple[list[ResourceFlow], list[dict[str, str]], list[dict[str, Any]]]:
    payloads = payloads or {}
    by_route = {(route["method"], route["path"].rstrip("/") or "/"): route for route in contracts}
    flows: list[ResourceFlow] = []
    flows_by_collection: dict[str, ResourceFlow] = {}
    skipped: list[dict[str, str]] = []
    covered: set[tuple[str, str]] = set()
    def route_for(method: str, path: str) -> dict[str, Any] | None:
        shape = PARAM.sub("{}", path)
        return next((route for (candidate_method, candidate_path), route in by_route.items()
                     if candidate_method == method and PARAM.sub("{}", candidate_path) == shape), None)

    for (method, path), create in sorted(by_route.items(), key=lambda item: item[0][1].count("{")):
        if method != "POST":
            continue
        parent_parameters = PARAM.findall(path)
        parent = None
        if parent_parameters:
            if len(parent_parameters) != 1:
                skipped.append({"route": f"POST {path}", "reason": "Mais de um recurso pai; configure um fluxo específico."})
                continue
            parent_collection = path.split("/{", 1)[0]
            parent = flows_by_collection.get(parent_collection)
            if parent is None:
                skipped.append({"route": f"POST {path}", "reason": "Fluxo de criação e limpeza do recurso pai não encontrado."})
                continue
        details = [(candidate_path, PARAM.findall(candidate_path)) for _, candidate_path in by_route if candidate_path.startswith(path + "/")]
        details = [(candidate_path, parameters) for candidate_path, parameters in details if len(parameters) == len(parent_parameters) + 1
                   and candidate_path == path + "/{" + parameters[-1] + "}"]
        if not details:
            skipped.append({"route": f"POST {path}", "reason": "Rota de item com {id} não encontrada."})
            continue
        detail, parameters = details[0]
        update = route_for("PUT", detail) or route_for("PATCH", detail)
        delete = route_for("DELETE", detail)
        read = route_for("GET", detail)
        listed = by_route.get(("GET", path))
        overrides = payloads.get(path, {}) if isinstance(payloads.get(path, {}), dict) else {}
        create_body = overrides.get("create")
        update_body = overrides.get("update")
        if not update or not delete or not (read or listed):
            skipped.append({"route": f"POST {path}", "reason": "Fluxo requer atualização, leitura e DELETE do mesmo recurso."})
            continue
        sample_create = _body(create, "identityexample", create_body)
        if sample_create is None or _body(update, "example", update_body) is None:
            skipped.append({"route": f"POST {path}", "reason": "Corpo de criação/atualização desconhecido; configure api_payloads."})
            continue
        if "identityexample" not in json.dumps(sample_create):
            skipped.append({"route": f"POST {path}", "reason": "Criação precisa de um campo único para localizar e limpar o registro de teste."})
            continue
        flow = ResourceFlow(create["path"], create, update, read, listed, delete, parameters[-1], create_body, update_body, parent)
        flows.append(flow)
        flows_by_collection[path] = flow
        covered.update({("POST", path), (update["method"], update["path"].rstrip("/") or "/"),
                        ("DELETE", delete["path"].rstrip("/") or "/")})
        if read:
            covered.add(("GET", read["path"].rstrip("/") or "/"))
        if listed:
            covered.add(("GET", path))
    standalone = [route for key, route in by_route.items() if key not in covered and route["method"] in {"GET", "HEAD", "OPTIONS"} and not PARAM.search(route["path"])]
    for key, route in by_route.items():
        if key not in covered and route not in standalone and not any(item["route"] == f"{route['method']} {route['path']}" for item in skipped):
            skipped.append({"route": f"{route['method']} {route['path']}", "reason": "Sem fluxo isolado e reversível para este endpoint."})
    return flows, skipped, standalone


def _created_id(response: httpx.Response, parameter: str, collection: str) -> str | None:
    location = response.headers.get("location", "")
    if location:
        location_path = urlsplit(location).path.rstrip("/")
        tail = location_path.rsplit("/", 1)[-1]
        if location_path.startswith(collection.rstrip("/") + "/"):
            identifier = _valid_id(tail)
            if identifier:
                return identifier
    try:
        data = response.json()
    except ValueError:
        return None
    for candidate in (data, data.get("data") if isinstance(data, dict) else None, data.get("item") if isinstance(data, dict) else None):
        if isinstance(candidate, dict):
            for key in (parameter, "id", "uuid", "user_id", "usuario_id"):
                value = candidate.get(key)
                identifier = _valid_id(value)
                if identifier:
                    return identifier
    return None


def _find_created(data: Any, marker: str, parameter: str) -> str | None:
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                found = _find_created(value, marker, parameter)
                if found:
                    return found
        if any(marker in str(value) for value in data.values() if isinstance(value, str)):
            for key in (parameter, "id", "uuid"):
                identifier = _valid_id(data.get(key))
                if identifier:
                    return identifier
    elif isinstance(data, list):
        for item in data:
            found = _find_created(item, marker, parameter)
            if found:
                return found
    return None


async def run_api_campaign(count: int, base_url: str, headers: dict[str, str], timeout: float,
                           flows: list[ResourceFlow], standalone: list[dict[str, Any]], output: Path,
                           progress: ProgressDisplay | None = None, cycles: int = 1) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    tasks_for_agents = [(agent_id, flows[(agent_id - 1 + iteration) % len(flows)])
                        for agent_id in range(1, count + 1) for iteration in range(cycles)] if flows else []
    for index, flow in enumerate(flows[len(tasks_for_agents):], len(tasks_for_agents) + 1):
        tasks_for_agents.append(((index - 1) % count + 1, flow))
    read_tasks = [(agent_id, standalone[(agent_id - 1 + iteration) % len(standalone)])
                  for agent_id in range(1, count + 1) for iteration in range(cycles)] if standalone else []
    for index, route in enumerate(standalone[len(read_tasks):], len(read_tasks) + 1):
        read_tasks.append(((index - 1) % count + 1, route))
    totals: dict[str, int] = {}
    for _, flow in tasks_for_agents:
        for method in ("POST", flow.update["method"], *("GET" for _ in (flow.read, flow.list_route) if _), "DELETE"):
            totals[method] = totals.get(method, 0) + 1
        if flow.parent:
            totals["POST"] += 1
            totals["DELETE"] += 1
    for _, route in read_tasks:
        totals[route["method"]] = totals.get(route["method"], 0) + 1
    if progress:
        progress.set_stage("Testando API · fluxos com limpeza", totals)
    summary: dict[str, Any] = {"agents": count, "requests": 0, "passed": 0, "failed": 0, "skipped": 0,
                               "cleanup_ok": 0, "cleanup_failed": 0, "average_ms": 0.0, "output": str(output),
                               "cases": len(flows) + len(standalone), "complex_cases": len(flows)}
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=1000)

    async def writer() -> None:
        total_ms = 0.0
        with output.open("w", encoding="utf-8") as stream:
            while (record := await queue.get()) is not None:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                if record["skipped"]:
                    summary["skipped"] += 1
                else:
                    summary["requests"] += 1
                    summary["passed" if record["passed"] else "failed"] += 1
                    total_ms += record["latency_ms"]
                if progress and record["phase"] not in {"recover", "parent_recover", "cleanup_retry", "parent_cleanup_retry"}:
                    progress.advance(record["method"])
            if summary["requests"]:
                summary["average_ms"] = round(total_ms / summary["requests"], 2)

    async def emit(agent: int, phase: str, method: str, path: str, response: httpx.Response | None,
                   started: float, error: str | None = None, skipped: bool = False) -> None:
        status = response.status_code if response is not None else None
        expected = {"POST": {200, 201, 202}, "PUT": {200, 204}, "PATCH": {200, 204},
                    "GET": {200}, "DELETE": {200, 202, 204}, "HEAD": {200}, "OPTIONS": {200, 204}}
        await queue.put({"timestamp": datetime.now(UTC).isoformat(), "agent": agent, "phase": phase,
                         "method": method, "path": path, "status": status, "expected_status": sorted(expected[method]),
                         "passed": not skipped and status in expected[method], "skipped": skipped,
                         "latency_ms": round((perf_counter() - started) * 1000, 2) if not skipped else 0.0,
                         "error": error})

    limits = httpx.Limits(max_connections=min(count, 20), max_keepalive_connections=min(count, 20))
    semaphore = asyncio.Semaphore(20)
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False) as client:
        writer_task = asyncio.create_task(writer())

        async def request(agent: int, phase: str, method: str, path: str, body: Any = None) -> httpx.Response | None:
            started = perf_counter()
            try:
                response = await client.request(method, base_url + path, headers=headers, json=body)
                await emit(agent, phase, method, path, response, started)
                return response
            except (httpx.RequestError, ValueError, TypeError) as exc:
                await emit(agent, phase, method, path, None, started, type(exc).__name__)
                return None

        async def skipped(agent: int, phase: str, method: str, path: str, reason: str) -> None:
            await emit(agent, phase, method, path, None, perf_counter(), reason, True)

        async def exercise(agent: int, flow: ResourceFlow) -> None:
            async with semaphore:
                marker = uuid.uuid4().hex[:16]
                created_id: str | None = None
                parent_id: str | None = None
                parent_marker = uuid.uuid4().hex[:16]
                parent = flow.parent
                parent_token = PARAM.findall(flow.collection)[0] if parent else None

                def path_for(route: dict[str, Any], item_id: str | None = None) -> str:
                    path = route["path"]
                    if parent_token and parent_id:
                        path = path.replace("{" + parent_token + "}", quote(parent_id, safe=""))
                    if item_id:
                        path = PARAM.sub(quote(item_id, safe=""), path)
                    return path

                detail = path_for(flow.delete)
                try:
                    if parent:
                        parent_response = await request(agent, "parent_create", "POST", parent.collection,
                                                        _body(parent.create, parent_marker, parent.create_body))
                        if parent_response is not None and parent_response.status_code in {200, 201, 202}:
                            parent_id = _created_id(parent_response, parent.parameter, parent.collection)
                    if parent and not parent_id:
                        await skipped(agent, "create", "POST", flow.collection, "ID do recurso pai indisponível")
                        for phase, route in (("update", flow.update), ("read", flow.read), ("list", flow.list_route),
                                             ("cleanup", flow.delete)):
                            if route:
                                await skipped(agent, phase, route["method"], route["path"], "Recurso pai indisponível")
                        return
                    create_path = path_for(flow.create)
                    response = await request(agent, "create", "POST", create_path, _body(flow.create, marker, flow.create_body))
                    if response is not None and response.status_code in {200, 201, 202}:
                        created_id = _created_id(response, flow.parameter, create_path)
                    if created_id:
                        detail = path_for(flow.delete, created_id)
                        update_body = _body(flow.update, marker + "_updated", flow.update_body)
                        if update_body is not None:
                            await request(agent, "update", flow.update["method"], path_for(flow.update, created_id), update_body)
                        else:
                            await skipped(agent, "update", flow.update["method"], detail, "Corpo de atualização ausente")
                        if flow.read:
                            await request(agent, "read", "GET", path_for(flow.read, created_id))
                        if flow.list_route:
                            await request(agent, "list", "GET", path_for(flow.list_route))
                    else:
                        for phase, route in (("update", flow.update), ("read", flow.read), ("list", flow.list_route)):
                            if route:
                                await skipped(agent, phase, route["method"], path_for(route), "ID da criação indisponível")
                finally:
                    # A exclusão é tentada mesmo se atualização ou consulta falhar.
                    if not created_id and flow.list_route and (not parent or parent_id):
                        recovered = await request(agent, "recover", "GET", path_for(flow.list_route))
                        if recovered is not None and recovered.status_code == 200:
                            try:
                                created_id = _find_created(recovered.json(), marker, flow.parameter)
                            except ValueError:
                                pass
                        if created_id:
                            detail = path_for(flow.delete, created_id)
                    if created_id:
                        deleted = await request(agent, "cleanup", "DELETE", detail)
                        if deleted is None or deleted.status_code not in {200, 202, 204}:
                            deleted = await request(agent, "cleanup_retry", "DELETE", detail)
                        summary["cleanup_ok" if deleted is not None and deleted.status_code in {200, 202, 204} else "cleanup_failed"] += 1
                    elif not parent or parent_id:
                        await skipped(agent, "cleanup", "DELETE", detail, "ID não encontrado; exclusão impossível")
                        summary["cleanup_failed"] += 1
                    if parent:
                        if not parent_id and parent.list_route:
                            recovered = await request(agent, "parent_recover", "GET", parent.list_route["path"])
                            if recovered is not None and recovered.status_code == 200:
                                try:
                                    parent_id = _find_created(recovered.json(), parent_marker, parent.parameter)
                                except ValueError:
                                    pass
                        if parent_id:
                            parent_detail = parent.delete["path"].replace("{" + parent.parameter + "}", quote(parent_id, safe=""))
                            deleted = await request(agent, "parent_cleanup", "DELETE", parent_detail)
                            if deleted is None or deleted.status_code not in {200, 202, 204}:
                                deleted = await request(agent, "parent_cleanup_retry", "DELETE", parent_detail)
                            summary["cleanup_ok" if deleted is not None and deleted.status_code in {200, 202, 204} else "cleanup_failed"] += 1
                        else:
                            await skipped(agent, "parent_cleanup", "DELETE", parent.delete["path"], "ID do recurso pai indisponível")
                            summary["cleanup_failed"] += 1

        async def read_only(agent: int, route: dict[str, Any]) -> None:
            async with semaphore:
                await request(agent, "standalone", route["method"], route["path"])

        try:
            await asyncio.gather(*(exercise(agent, flow) for agent, flow in tasks_for_agents),
                                 *(read_only(agent, route) for agent, route in read_tasks))
        finally:
            await queue.put(None)
            await writer_task
    return summary
