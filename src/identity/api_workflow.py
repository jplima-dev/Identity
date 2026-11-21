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

from .api_checks import check_response
from .payload_ai import synthesize_body
from .progress import ProgressDisplay

PARAM = re.compile(r"\{([^{}]+)\}")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


@dataclass(frozen=True)
class ResourceFlow:
    collection: str
    create: dict[str, Any]
    update: dict[str, Any] | None
    read: dict[str, Any] | None
    list_route: dict[str, Any] | None
    delete: dict[str, Any]
    parameter: str
    create_body: dict[str, Any] | None = None
    update_body: dict[str, Any] | None = None
    parent: ResourceFlow | None = None


def _body(route: dict[str, Any], marker: str, override: dict[str, Any] | None) -> dict[str, Any] | None:
    if override is not None:
        return json.loads(json.dumps(override).replace("{identity_marker}", marker))
    schema = route.get("body_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object" or not schema.get("properties"):
        return None
    return synthesize_body(schema, marker)


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
        if not delete:
            skipped.append({"route": f"POST {path}", "reason": "Nenhum DELETE do item encontrado para limpar o registro criado."})
            continue
        sample_create = _body(create, "identityexample", create_body)
        if sample_create is None:
            skipped.append({"route": f"POST {path}", "reason": "Corpo de criação desconhecido; configure api_payloads."})
            continue
        if "identityexample" not in json.dumps(sample_create):
            skipped.append({"route": f"POST {path}", "reason": "Criação precisa de um campo único para localizar e limpar o registro de teste."})
            continue
        if update and _body(update, "example", update_body) is None:
            skipped.append({"route": f"{update['method']} {update['path']}",
                            "reason": "Corpo de atualização desconhecido; configure api_payloads."})
            update = None
        flow = ResourceFlow(create["path"], create, update, read, listed, delete, parameters[-1], create_body, update_body, parent)
        flows.append(flow)
        flows_by_collection[path] = flow
        covered.update({("POST", path), ("DELETE", delete["path"].rstrip("/") or "/")})
        if update:
            covered.add((update["method"], update["path"].rstrip("/") or "/"))
        if read:
            covered.add(("GET", read["path"].rstrip("/") or "/"))
        if listed:
            covered.add(("GET", path))
    standalone = [route for key, route in by_route.items() if key not in covered and route["method"] in {"GET", "HEAD", "OPTIONS"} and not PARAM.search(route["path"])]
    for key, route in by_route.items():
        if key not in covered and route not in standalone and not any(item["route"] == f"{route['method']} {route['path']}" for item in skipped):
            skipped.append({"route": f"{route['method']} {route['path']}", "reason": "Sem fluxo isolado e reversível para este endpoint."})
    return flows, skipped, standalone


def build_api_plan(contracts: list[dict[str, Any]], flows: list[ResourceFlow],
                   standalone: list[dict[str, Any]], direct_cases: list[dict[str, Any]],
                   skipped: list[dict[str, str]]) -> dict[str, Any]:
    """Give every discovered route a planned test or an explicit blocking reason."""
    planned: dict[str, list[dict[str, Any]]] = {}

    def include(route: dict[str, Any] | None, name: str, body: dict[str, Any] | None = None,
                cleanup: dict[str, str] | None = None) -> None:
        if route:
            test = {"name": name, "method": route["method"], "path": route.get("test_path", route["path"]),
                    "expected_status": route.get("expected_status")}
            if body is not None:
                test["body_example"] = body
            if cleanup is not None:
                test["cleanup"] = cleanup
            planned.setdefault(f"{route['method']} {route['path']}", []).append(test)

    for flow in flows:
        cleanup = {"method": "DELETE", "path": flow.delete["path"]}
        include(flow.create, f"fluxo {flow.collection}: criar", _body(flow.create, "identitypreview", flow.create_body), cleanup)
        if flow.update:
            include(flow.update, f"fluxo {flow.collection}: atualizar",
                    _body(flow.update, "identitypreview_updated", flow.update_body))
        for step, route in (("consultar", flow.read), ("listar", flow.list_route), ("excluir", flow.delete)):
            include(route, f"fluxo {flow.collection}: {step}")
    for route in standalone:
        include(route, "consulta independente")
    for case in direct_cases:
        include(case, f"caso explícito: {case['name']}", case.get("test_body"))
    reasons = {item["route"]: item["reason"] for item in skipped}
    routes = []
    for contract in contracts:
        key = f"{contract['method']} {contract['path']}"
        tests = planned.get(key, [])
        routes.append({"method": contract["method"], "path": contract["path"],
                       "source": contract.get("source"), "tests": tests,
                       "status": "planejada" if tests else "bloqueada",
                       "reason": None if tests else reasons.get(key, "Nenhum teste reversível foi gerado.")})
    return {"version": 1, "routes": routes,
            "planned": sum(bool(route["tests"]) for route in routes),
            "blocked": sum(not route["tests"] for route in routes)}


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


def _contains_marker(value: Any, marker: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_marker(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(_contains_marker(item, marker) for item in value)
    return isinstance(value, str) and marker in value


def _contains_id(value: Any, parameter: str, identifier: str) -> bool:
    if isinstance(value, dict):
        if any(_valid_id(value.get(key)) == identifier for key in (parameter, "id", "uuid")):
            return True
        return any(_contains_id(item, parameter, identifier) for item in value.values() if isinstance(item, (dict, list)))
    if isinstance(value, list):
        return any(_contains_id(item, parameter, identifier) for item in value)
    return False


def _deleted_and_absent(response: httpx.Response | None) -> bool:
    if response is None:
        return False
    return response.status_code in {404, 410}


def _absent_from_list(response: httpx.Response | None, parameter: str, identifier: str,
                      markers: tuple[str, ...]) -> bool:
    if response is None or response.status_code != 200:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    return not _contains_id(data, parameter, identifier) and not any(_contains_marker(data, marker) for marker in markers)


async def run_api_campaign(count: int, base_url: str, headers: dict[str, str], timeout: float,
                           flows: list[ResourceFlow], standalone: list[dict[str, Any]], output: Path,
                           progress: ProgressDisplay | None = None, cycles: int = 1,
                           direct_cases: list[dict[str, Any]] | None = None,
                           max_concurrency: int = 20) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    direct_cases = direct_cases or []
    read_cases = [case for case in direct_cases if case["method"] in {"GET", "HEAD", "OPTIONS"}]
    write_cases = [case for case in direct_cases if case["method"] not in {"GET", "HEAD", "OPTIONS"}]
    direct_tasks = [(agent, read_cases[(agent - 1 + iteration) % len(read_cases)])
                    for agent in range(1, count + 1) for iteration in range(cycles)] if read_cases else []
    for index, case in enumerate(read_cases[len(direct_tasks):], len(direct_tasks) + 1):
        direct_tasks.append(((index - 1) % count + 1, case))
    direct_tasks.extend(((index % count) + 1, case) for index, case in enumerate(write_cases))
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
        for method in ("POST", *([flow.update["method"]] if flow.update else []),
                       *("GET" for _ in (flow.read, flow.list_route) if _), "DELETE"):
            totals[method] = totals.get(method, 0) + 1
        update_schema = flow.update.get("body_schema") if flow.update else None
        if flow.update and flow.update["method"] == "PUT" and isinstance(update_schema, dict) and update_schema.get("required"):
            totals["PUT"] = totals.get("PUT", 0) + 1
        if flow.read:
            totals["GET"] = totals.get("GET", 0) + 1
        if flow.parent:
            totals["POST"] += 1
            totals["DELETE"] += 1
            if flow.parent.read:
                totals["GET"] += 1
    for _, route in read_tasks:
        totals[route["method"]] = totals.get(route["method"], 0) + 1
    for _, case in direct_tasks:
        totals[case["method"]] = totals.get(case["method"], 0) + 1
    if progress:
        progress.set_stage("Testando API · fluxos com limpeza", totals)
    summary: dict[str, Any] = {"agents": count, "requests": 0, "passed": 0, "failed": 0, "skipped": 0,
                               "cleanup_ok": 0, "cleanup_failed": 0, "average_ms": 0.0, "output": str(output),
                               "cases": len(flows) + len(standalone) + len(direct_cases), "complex_cases": len(flows)}
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
                   started: float, error: str | None = None, skipped: bool = False,
                   route: dict[str, Any] | None = None) -> None:
        status = response.status_code if response is not None else None
        expected = {"POST": {200, 201, 202}, "PUT": {200, 204}, "PATCH": {200, 204},
                    "GET": {200}, "DELETE": {200, 202, 204}, "HEAD": {200}, "OPTIONS": {200, 204}}
        passed = False
        statuses = sorted(expected[method])
        if response is not None and not skipped:
            passed, validation_error, statuses = check_response(response, route, expected[method])
            error = error or validation_error
            if error:
                passed = False
        await queue.put({"timestamp": datetime.now(UTC).isoformat(), "agent": agent, "phase": phase,
                         "method": method, "path": path, "route": route.get("path") if route else path,
                         "case": route.get("name") if route else None,
                         "status": status, "expected_status": statuses,
                         "passed": passed, "skipped": skipped,
                         "latency_ms": round((perf_counter() - started) * 1000, 2) if not skipped else 0.0,
                         "error": error})

    active = min(count, max_concurrency)
    limits = httpx.Limits(max_connections=active, max_keepalive_connections=active)
    semaphore = asyncio.Semaphore(active)
    stop_creating = asyncio.Event()

    def record_cleanup(confirmed: bool) -> None:
        summary["cleanup_ok" if confirmed else "cleanup_failed"] += 1
        if not confirmed:
            stop_creating.set()

    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False) as client:
        writer_task = asyncio.create_task(writer())

        async def request(agent: int, phase: str, method: str, path: str, body: Any = None,
                          route: dict[str, Any] | None = None,
                          expected_marker: str | None = None,
                          absent_id: tuple[str, str] | None = None,
                          absent_markers: tuple[str, ...] = ()) -> httpx.Response | None:
            started = perf_counter()
            try:
                response = await client.request(method, base_url + path, headers=headers, json=body)
                state_error = None
                if phase in {"cleanup_verify", "parent_cleanup_verify"} and route and route.get("expected_status") == [404, 410] and response.status_code == 200:
                    state_error = "Registro continua disponível após DELETE"
                if expected_marker and response.status_code == 200:
                    try:
                        if not _contains_marker(response.json(), expected_marker):
                            state_error = f"Registro criado/atualizado não encontrado na resposta: {expected_marker}"
                    except ValueError:
                        state_error = "Consulta não retornou JSON válido para verificar o registro"
                if absent_id and response.status_code == 200:
                    try:
                        data = response.json()
                        if (_contains_id(data, *absent_id)
                                or any(_contains_marker(data, marker) for marker in absent_markers)):
                            state_error = "Registro continua listado após DELETE"
                    except ValueError:
                        state_error = "Listagem após DELETE não retornou JSON válido"
                await emit(agent, phase, method, path, response, started, error=state_error, route=route)
                return response
            except (httpx.RequestError, ValueError, TypeError) as exc:
                await emit(agent, phase, method, path, None, started, type(exc).__name__)
                return None

        async def skipped(agent: int, phase: str, method: str, path: str, reason: str) -> None:
            await emit(agent, phase, method, path, None, perf_counter(), reason, True)

        async def exercise(agent: int, flow: ResourceFlow) -> None:
            async with semaphore:
                if stop_creating.is_set():
                    await skipped(agent, "create", "POST", flow.collection,
                                  "Novas criações suspensas após falha de limpeza")
                    return
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
                                                        _body(parent.create, parent_marker, parent.create_body), parent.create)
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
                    response = await request(agent, "create", "POST", create_path, _body(flow.create, marker, flow.create_body), flow.create)
                    if response is not None and response.status_code in {200, 201, 202}:
                        created_id = _created_id(response, flow.parameter, create_path)
                    if created_id:
                        detail = path_for(flow.delete, created_id)
                        updated_marker = marker[:12] + "u"
                        updated = None
                        if flow.update:
                            update_body = _body(flow.update, updated_marker, flow.update_body)
                            if update_body is not None:
                                updated = await request(agent, "update", flow.update["method"],
                                                        path_for(flow.update, created_id), update_body, flow.update)
                            else:
                                await skipped(agent, "update", flow.update["method"], detail, "Corpo de atualização ausente")
                        expected_marker = updated_marker if updated is not None and updated.status_code in {200, 204} else marker
                        update_schema = flow.update.get("body_schema") if flow.update else None
                        if flow.update and flow.update["method"] == "PUT" and isinstance(update_schema, dict) and update_schema.get("required"):
                            invalid_route = {**flow.update, "expected_status": [400, 422], "response_schema": None}
                            await request(agent, "invalid_update", "PUT", path_for(flow.update, created_id), {}, invalid_route)
                        if flow.read:
                            await request(agent, "read", "GET", path_for(flow.read, created_id),
                                          route=flow.read, expected_marker=expected_marker)
                        if flow.list_route:
                            await request(agent, "list", "GET", path_for(flow.list_route),
                                          route=flow.list_route, expected_marker=expected_marker)
                    else:
                        for phase, route in (("update", flow.update), ("read", flow.read), ("list", flow.list_route)):
                            if route:
                                await skipped(agent, phase, route["method"], path_for(route), "ID da criação indisponível")
                finally:
                    # A exclusão é tentada mesmo se atualização ou consulta falhar.
                    if not created_id and flow.list_route and (not parent or parent_id):
                        recovered = await request(agent, "recover", "GET", path_for(flow.list_route), route=flow.list_route)
                        if recovered is not None and recovered.status_code == 200:
                            try:
                                created_id = _find_created(recovered.json(), marker, flow.parameter)
                            except ValueError:
                                pass
                        if created_id:
                            detail = path_for(flow.delete, created_id)
                    if created_id:
                        deleted = await request(agent, "cleanup", "DELETE", detail, route=flow.delete)
                        if deleted is None or deleted.status_code not in {200, 202, 204}:
                            deleted = await request(agent, "cleanup_retry", "DELETE", detail, route=flow.delete)
                        confirmed = deleted is not None and deleted.status_code in {200, 202, 204}
                        if confirmed and flow.read:
                            verify_route = {**flow.read, "expected_status": [404, 410], "response_schema": None}
                            verified = await request(agent, "cleanup_verify", "GET", path_for(flow.read, created_id),
                                                     route=verify_route)
                            confirmed = _deleted_and_absent(verified)
                        elif confirmed and flow.list_route:
                            verified = await request(agent, "cleanup_verify", "GET", path_for(flow.list_route),
                                                     route=flow.list_route, absent_id=(flow.parameter, created_id),
                                                     absent_markers=(marker, marker[:12] + "u"))
                            confirmed = _absent_from_list(verified, flow.parameter, created_id,
                                                          (marker, marker[:12] + "u"))
                        record_cleanup(confirmed)
                    elif not parent or parent_id:
                        await skipped(agent, "cleanup", "DELETE", detail, "ID não encontrado; exclusão impossível")
                        record_cleanup(False)
                    if parent:
                        if not parent_id and parent.list_route:
                            recovered = await request(agent, "parent_recover", "GET", parent.list_route["path"], route=parent.list_route)
                            if recovered is not None and recovered.status_code == 200:
                                try:
                                    parent_id = _find_created(recovered.json(), parent_marker, parent.parameter)
                                except ValueError:
                                    pass
                        if parent_id:
                            parent_detail = parent.delete["path"].replace("{" + parent.parameter + "}", quote(parent_id, safe=""))
                            deleted = await request(agent, "parent_cleanup", "DELETE", parent_detail, route=parent.delete)
                            if deleted is None or deleted.status_code not in {200, 202, 204}:
                                deleted = await request(agent, "parent_cleanup_retry", "DELETE", parent_detail, route=parent.delete)
                            confirmed = deleted is not None and deleted.status_code in {200, 202, 204}
                            if confirmed and parent.read:
                                verify_route = {**parent.read, "expected_status": [404, 410], "response_schema": None}
                                read_detail = parent.read["path"].replace("{" + parent.parameter + "}", quote(parent_id, safe=""))
                                verified = await request(agent, "parent_cleanup_verify", "GET", read_detail,
                                                         route=verify_route)
                                confirmed = _deleted_and_absent(verified)
                            elif confirmed and parent.list_route:
                                verified = await request(agent, "parent_cleanup_verify", "GET", parent.list_route["path"],
                                                         route=parent.list_route, absent_id=(parent.parameter, parent_id),
                                                         absent_markers=(parent_marker,))
                                confirmed = _absent_from_list(verified, parent.parameter, parent_id, (parent_marker,))
                            record_cleanup(confirmed)
                        else:
                            await skipped(agent, "parent_cleanup", "DELETE", parent.delete["path"], "ID do recurso pai indisponível")
                            record_cleanup(False)

        async def read_only(agent: int, route: dict[str, Any]) -> None:
            async with semaphore:
                await request(agent, "standalone", route["method"], route["path"], route=route)

        async def direct(agent: int, case: dict[str, Any]) -> None:
            async with semaphore:
                if stop_creating.is_set() and case["method"] not in {"GET", "HEAD", "OPTIONS"}:
                    await skipped(agent, "case", case["method"], case["test_path"],
                                  "Escritas suspensas após falha de limpeza")
                    return
                await request(agent, "case", case["method"], case["test_path"], case.get("test_body"), case)

        try:
            await asyncio.gather(*(exercise(agent, flow) for agent, flow in tasks_for_agents),
                                 *(read_only(agent, route) for agent, route in read_tasks),
                                 *(direct(agent, case) for agent, case in direct_tasks))
        finally:
            await queue.put(None)
            await writer_task
    return summary
