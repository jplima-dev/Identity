"""Command line entry point for concurrent API testing."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .agent_adapter import (
    load_adapter,
    run_adapter_cases,
    train_adapter,
    write_adapter_report,
)
from .agent_mode import (
    generate_cases,
    run_agent_cases,
    write_agent_report,
    write_training_examples,
)
from .api_checks import plan_route_cases
from .api_discovery import discover_contracts
from .api_workflow import build_api_plan, plan_resources, run_api_campaign
from .campaign import next_cases
from .conversation_ai import (
    discover_features,
    generate_conversations,
    save_conversations,
)
from .llm_conversations import add_round_pairs
from .llm_training import train_llm_round
from .offline_model import OfflineCapabilityModel
from .payload_ai import synthesize_body
from .progress import ProgressDisplay
from .project import ProjectConfig, parse_project, run_project, write_report
from .report_view import debug_summary
from .saved_projects import (
    identity_root,
    load_project,
    project_folder,
    save_project,
    saved_names,
)
from .scanner import discover_agent, discover_api, discover_base_url

ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}")
METHODS = {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}


class ConfigurationError(ValueError):
    """A configuration file cannot be used safely or unambiguously."""


@dataclass(frozen=True)
class Scenario:
    method: str
    path: str
    expected_status: tuple[int, ...]
    headers: dict[str, str]
    json_body: Any = None


@dataclass(frozen=True)
class RunConfig:
    base_url: str
    scenarios: tuple[Scenario, ...]
    headers: dict[str, str]
    requests_per_agent: int
    timeout_seconds: float
    project: ProjectConfig | None
    max_concurrency: int = 20


def agent_count(root: Path) -> int:
    candidates = [p for p in (root / "config").glob("*.ag") if p.is_file() and p.stem.isdecimal()]
    if len(candidates) != 1:
        raise ConfigurationError(
            "config deve conter exatamente um arquivo numérico .ag (por exemplo, '100.ag')."
        )
    count = int(candidates[0].stem)
    if count < 1:
        raise ConfigurationError("O número de agentes deve ser maior que zero.")
    return count


def run_mode(root: Path) -> str:
    modes = [name for name in ("agente", "api") if (root / "config" / f"{name}.tes").is_file()]
    if len(modes) != 1:
        raise ConfigurationError("Crie exatamente um arquivo em config: 'agente.tes' ou 'api.tes'.")
    return modes[0]


def round_count(root: Path) -> int:
    candidates = [p for p in (root / "config").glob("*.rod") if p.is_file() and p.stem.isdecimal()]
    if len(candidates) != 1:
        raise ConfigurationError("config deve conter exatamente um arquivo numérico .rod (por exemplo, '10.rod').")
    rounds = int(candidates[0].stem)
    if rounds < 1:
        raise ConfigurationError("O número de rodadas deve ser maior que zero.")
    return rounds


def set_setting(root: Path, kind: str, value: str) -> Path:
    value = value.strip()
    if kind == "tes":
        value = value.casefold()
        if value not in {"api", "agente"}:
            raise ConfigurationError("tes set aceita apenas api ou agente.")
        name = f"{value}.tes"
    else:
        if not value.isdecimal() or int(value) < 1:
            raise ConfigurationError(f"{kind} set requer um número inteiro maior que zero.")
        name = f"{int(value)}.{kind}"
    folder = root / "config"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_text("", encoding="utf-8")
    for file in folder.glob(f"*.{kind}"):
        if file.is_file() and file != target:
            file.unlink()
    return target


def _expand(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigurationError(f"Variável de ambiente obrigatória não definida: {name}")
        return os.environ[name]

    return ENV_VAR.sub(replace, value)


def _headers(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise ConfigurationError(f"{label} deve ser um objeto de nomes e valores de texto.")
    return {key: _expand(val) for key, val in value.items()}


def load_config(path: Path, url_override: str | None = None, requests_override: int | None = None, include_project: bool = True,
                allow_empty_scenarios: bool = False) -> RunConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuração não encontrada: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Não foi possível ler {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("identity.json deve conter um objeto JSON.")

    base_url = url_override or raw.get("base_url")
    if not isinstance(base_url, str):
        raise ConfigurationError("Defina base_url em identity.json ou passe --url.")
    base_url = _expand(base_url).rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
        raise ConfigurationError("base_url deve ser uma URL HTTP(S) sem query ou fragmento.")

    requests_per_agent = requests_override if requests_override is not None else raw.get("requests_per_agent", 1)
    if type(requests_per_agent) is not int or requests_per_agent < 1:
        raise ConfigurationError("requests_per_agent deve ser um inteiro maior que zero.")
    timeout = raw.get("timeout_seconds", 10)
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise ConfigurationError("timeout_seconds deve estar entre 0 e 300 segundos.")
    headers = _headers(raw.get("headers", {}), "headers")
    max_concurrency = raw.get("max_concurrency", 20)
    if type(max_concurrency) is not int or not 1 <= max_concurrency <= 1000:
        raise ConfigurationError("max_concurrency deve estar entre 1 e 1000.")

    scenarios_raw = raw.get("scenarios")
    if allow_empty_scenarios and not scenarios_raw:
        scenarios_raw = [{"method": "GET", "path": "/"}]
    if not isinstance(scenarios_raw, list) or not scenarios_raw:
        raise ConfigurationError("scenarios deve conter pelo menos um cenário.")
    scenarios: list[Scenario] = []
    for index, item in enumerate(scenarios_raw, 1):
        if not isinstance(item, dict):
            raise ConfigurationError(f"Cenário {index} deve ser um objeto.")
        method = item.get("method", "GET")
        path_value = item.get("path")
        if not isinstance(method, str) or method.upper() not in METHODS:
            raise ConfigurationError(f"Método inválido no cenário {index}.")
        if not isinstance(path_value, str) or not path_value.startswith("/") or path_value.startswith("//"):
            raise ConfigurationError(f"Cenário {index}: path deve começar com uma única barra.")
        statuses = item.get("expected_status", [200])
        if type(statuses) is int:
            statuses = [statuses]
        if not isinstance(statuses, list) or not statuses or any(type(s) is not int or s < 100 or s > 599 for s in statuses):
            raise ConfigurationError(f"Cenário {index}: expected_status deve conter códigos HTTP válidos.")
        scenarios.append(Scenario(
            method=method.upper(),
            path=path_value,
            expected_status=tuple(statuses),
            headers=_headers(item.get("headers", {}), f"headers do cenário {index}"),
            json_body=item.get("json"),
        ))
    try:
        project = parse_project(raw.get("project")) if include_project else None
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(str(exc)) from exc
    return RunConfig(base_url, tuple(scenarios), headers, requests_per_agent, float(timeout), project,
                     max_concurrency)


async def run_agents(count: int, config: RunConfig, output: Path, progress: ProgressDisplay | None = None) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=1000)
    summary: dict[str, Any] = {"agents": count, "requests": 0, "passed": 0, "failed": 0, "average_ms": 0.0, "output": str(output)}

    async def writer() -> None:
        total_ms = 0.0
        with output.open("w", encoding="utf-8") as stream:
            while (record := await queue.get()) is not None:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                summary["requests"] += 1
                summary["passed" if record["passed"] else "failed"] += 1
                if progress:
                    progress.advance(record["method"])
                total_ms += record["latency_ms"]
            if summary["requests"]:
                summary["average_ms"] = round(total_ms / summary["requests"], 2)

    start = asyncio.Event()
    limits = httpx.Limits(max_connections=count, max_keepalive_connections=min(count, 100))
    async with httpx.AsyncClient(timeout=config.timeout_seconds, limits=limits, follow_redirects=False) as client:
        writer_task = asyncio.create_task(writer())

        async def agent(agent_id: int) -> None:
            await start.wait()
            for iteration in range(config.requests_per_agent):
                scenario = config.scenarios[(agent_id - 1 + iteration) % len(config.scenarios)]
                url = config.base_url + scenario.path
                now = datetime.now(UTC).isoformat()
                loop = asyncio.get_running_loop()
                started = loop.time()
                status = None
                error = None
                try:
                    response = await client.request(
                        scenario.method,
                        url,
                        headers={**config.headers, **scenario.headers},
                        json=scenario.json_body,
                    )
                    status = response.status_code
                except httpx.RequestError as exc:
                    error = type(exc).__name__
                await queue.put({
                    "timestamp": now,
                    "agent": agent_id,
                    "iteration": iteration + 1,
                    "method": scenario.method,
                    "path": scenario.path,
                    "status": status,
                    "expected_status": list(scenario.expected_status),
                    "passed": status in scenario.expected_status,
                    "latency_ms": round((loop.time() - started) * 1000, 2),
                    "error": error,
                })

        tasks = [asyncio.create_task(agent(i)) for i in range(1, count + 1)]
        start.set()
        try:
            await asyncio.gather(*tasks)
        finally:
            await queue.put(None)
            await writer_task
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(prog="identity", description="Agentes simultâneos para testar APIs.")
    subcommands = parser.add_subparsers(dest="command")
    cwd = Path.cwd()
    root = identity_root(cwd, Path(__file__))
    scam = subcommands.add_parser("scam", help="Escaneia e salva um projeto para execução posterior.")
    scam.add_argument("--project", type=Path, help="Pasta do projeto alvo; padrão: pasta que contém o Identity.")
    scam.add_argument("--url", help="URL base da API.")
    for name, description in (("plan", "Mostra o plano de testes sem executá-lo."),):
        command = subcommands.add_parser(name, help=description)
        command.add_argument("--project", type=Path, help="Pasta do projeto alvo.")
        command.add_argument("--url", help="URL base da API.")
        if name == "plan":
            command.add_argument("--all", action="store_true", help="Mostra todas as perguntas geradas.")
    report_command = subcommands.add_parser("report", help="Mostra o último relatório salvo.")
    report_command.add_argument("project", nargs="?", help="Nome do projeto salvo; sem nome, usa o último relatório.")
    for name in ("tes", "rod", "ag"):
        setting = subcommands.add_parser(name, help=f"Mostra ou define o arquivo .{name} ativo.")
        setting.add_argument("action", nargs="?", choices=["set"], help="Define um novo valor.")
        setting.add_argument("value", nargs="?", help="Novo valor: api/agente para tes, número para ag/rod.")
    reserved = {"scam", "plan", "report", "tes", "rod", "ag", "config"}
    names = []
    for name in saved_names(root):
        if name in reserved:
            continue
        names.append(name)
        saved_command = subcommands.add_parser(name, help=f"Executa o projeto salvo {name}.")
        saved_command.add_argument("--url", help="Sobrescreve a URL base da API.")
        saved_command.add_argument("--requests-per-agent", type=int, help="Ciclos por agente.")
        saved_command.add_argument("--output", type=Path, help="Arquivo JSONL de resultados.")
        saved_command.set_defaults(saved=name)
    ProgressDisplay().show_intro(show_tagline=len(sys.argv) == 1)
    args = parser.parse_args()
    if args.command is None:
        welcome = ProgressDisplay()
        welcome.show_panel("IDENTITY  /  COMANDOS", [
            ("scam", "Analisa e salva um projeto"),
            ("NOME_DO_PROJETO", "Executa os testes do projeto salvo"),
            ("plan", "Mostra os testes planejados"),
            ("report", "Abre o último relatório"),
            ("tes / ag / rod", "Consulta ou altera a configuração"),
        ])
        welcome.show_panel("EXEMPLO  /  API", [
            ("1. Modo", "identity tes set api"),
            ("2. Agentes", "identity ag set 20"),
            ("3. Rodadas", "identity rod set 1"),
            ("4. Escanear", "identity scam --project D:\\codes\\minha-api"),
            ("5. Testar", "identity minha-api"),
            ("6. Relatório", "identity report minha-api"),
        ])
        if names:
            welcome.show_panel("PROJETOS SALVOS", [(name, f"identity {name}") for name in names[:8]])
            if len(names) > 8:
                print(f"  ... e mais {len(names) - 8} projeto(s)")
        print("  Use identity --help para ver todas as opções.\n")
        return
    if args.command in {"tes", "rod", "ag"}:
        try:
            if args.action == "set":
                if args.value is None and not sys.stdin.isatty():
                    raise ConfigurationError(f"Informe o valor: identity {args.command} set VALOR.")
                value = args.value or input(f"Novo valor de {args.command}: ").strip()
                changed = set_setting(root, args.command, value)
                ProgressDisplay().show_panel("CONFIGURAÇÃO ATUALIZADA", [("Arquivo", str(changed))])
                return
            if args.command == "tes":
                mode = run_mode(root)
                value = f"{mode}.tes  ->  modo {mode}"
            elif args.command == "rod":
                value = f"{round_count(root)} rodada(s)  ->  {round_count(root)}.rod"
            else:
                value = f"{agent_count(root)} agente(s)  ->  {agent_count(root)}.ag"
        except ConfigurationError as exc:
            parser.exit(2, f"Erro: {exc}\n")
        ProgressDisplay().show_panel(f"CONFIGURAÇÃO  /  .{args.command}", [("Arquivo", value), ("Pasta", str(root / 'config'))])
        return
    if args.command == "report":
        report_names = ("relatorio-agente.md", "relatorio-api.md", "relatorio.md")
        try:
            target_names = [args.project] if args.project else sorted(set(saved_names(root)) | {
                path.name for path in root.iterdir() if path.is_dir() and
                any((path / filename).is_file() for filename in report_names)
            })
            folders = [project_folder(root, name) for name in target_names]
        except ValueError as exc:
            parser.exit(2, f"Erro: {exc}\n")
        candidates = [report for folder in folders for run in (folder / "runs").glob("*")
                      for name in report_names if (report := run / name).is_file()]
        candidates.extend(path for name in target_names for filename in report_names
                          if (path := root / name / filename).is_file())
        if not candidates:
            parser.exit(2, "Erro: nenhum relatório encontrado.\n")
        latest = max(candidates, key=lambda path: path.stat().st_mtime)
        kind = "API" if latest.name == "relatorio-api.md" else "Agente" if latest.name == "relatorio-agente.md" else "Legado"
        project_name = latest.parent.parent.parent.name if latest.parent.parent.name == "runs" else latest.parent.name
        rows, problems, hidden = debug_summary(latest)
        display = ProgressDisplay()
        display.show_panel("IDENTITY  /  DEBUG", [
            ("Projeto", project_name), ("Tipo", kind),
            ("Execução", latest.parent.name if latest.parent.parent.name == "runs" else "legada"),
            *rows, ("Relatório completo", str(latest)),
        ])
        if problems:
            display.show_panel("DIAGNÓSTICO", [(str(index), problem) for index, problem in enumerate(problems, 1)] +
                               ([("Mais", f"{hidden} ocorrência(s) no relatório completo")] if hidden else []))
        return
    try:
        saved_scan = load_project(root, args.saved) if getattr(args, "saved", None) else None
    except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Erro: {exc}\n")
    progress = ProgressDisplay().start()
    progress_status = "Erro"
    try:
        count = agent_count(root)
        mode = run_mode(root)
        rounds_configured = round_count(root)
        config = load_config(root / "identity.json", args.url, getattr(args, "requests_per_agent", None), False, True)
        project_path = None
        discovery = None
        adapter = None
        inventory = None
        generated = []
        llm_results = []
        scan_summary = None
        api_plan = None
        if args.command in {"scam", "plan"} or saved_scan:
            project_path = (Path(saved_scan["project"]) if saved_scan else
                            getattr(args, "project", None) or (root.parent if root.name.casefold() == "identity" else cwd)).resolve()
            if project_path == root or not project_path.is_dir():
                raise ConfigurationError("Coloque Identity dentro do projeto ou informe --project.")
            progress.show_panel("IDENTITY  /  SCANNER DE PROJETOS", [
                ("Projeto", str(project_path)),
                ("Modo", "Agente" if mode == "agente" else "API"),
                ("Agentes", str(count)),
                ("Concorrência", str(min(count, config.max_concurrency))),
                ("Execução", f"{rounds_configured} rodadas de classificação" if mode == "agente"
                 else f"{config.requests_per_agent} ciclo(s) por agente"),
            ])
            if mode == "api":
                routes = discover_api(project_path)
                if not routes:
                    raise ConfigurationError("Nenhuma rota foi encontrada no código do projeto.")
                contracts = discover_contracts(project_path)
                seen_contracts = {(route["method"], route["path"]) for route in contracts}
                contracts.extend({**route, "body_schema": None} for route in routes if (route["method"], route["path"]) not in seen_contracts)
                raw_api = json.loads((root / "identity.json").read_text(encoding="utf-8"))
                payloads = raw_api.get("api_payloads", {})
                if not isinstance(payloads, dict):
                    raise ConfigurationError("api_payloads deve ser um objeto por rota de criação.")
                configured_cases = raw_api.get("api_cases", {})
                if not isinstance(configured_cases, dict):
                    raise ConfigurationError("api_cases deve ser um objeto por rota descoberta.")
                direct_cases = plan_route_cases(contracts, configured_cases)
                allow_remote_mutations = raw_api.get("allow_remote_mutations", False)
                if not isinstance(allow_remote_mutations, bool):
                    raise ConfigurationError("allow_remote_mutations deve ser true ou false.")
                discovered_url = discover_base_url(project_path)
                config = replace(config, base_url=args.url or (saved_scan.get("base_url") if saved_scan else None)
                                 or discovered_url or config.base_url)
                flows, skipped_routes, standalone = plan_resources(contracts, payloads)
                hostname = urlsplit(config.base_url).hostname
                if hostname not in {"127.0.0.1", "localhost", "::1"} and not allow_remote_mutations:
                    skipped_routes.extend({"route": f"POST {flow.collection}", "reason": "Mutação remota requer allow_remote_mutations=true."} for flow in flows)
                    flows = []
                    blocked_cases = [case for case in direct_cases if case["method"] not in {"GET", "HEAD", "OPTIONS"}]
                    skipped_routes.extend({"route": f"{case['method']} {case['path']}",
                                           "reason": "Mutação remota requer allow_remote_mutations=true."} for case in blocked_cases)
                    direct_cases = [case for case in direct_cases if case not in blocked_cases]
                explicit_routes = {f"{case['method']} {case['path']}" for case in direct_cases}
                skipped_routes = [item for item in skipped_routes if item["route"] not in explicit_routes]
                api_plan = build_api_plan(contracts, flows, standalone, direct_cases, skipped_routes)
                scan_summary = {"project": str(project_path), "mode": "api", "base_url": config.base_url,
                                "routes": contracts, "resource_flows": [flow.collection for flow in flows],
                                "standalone_routes": standalone, "direct_cases": direct_cases,
                                "untested_routes": skipped_routes, "test_plan": "testes-api.json"}
                progress.show_panel("SCANNER  /  API", [
                    ("Rotas", str(len(contracts))),
                    ("Fluxos com limpeza", str(len(flows))),
                    ("Corpos inferidos", str(sum(flow.create_body is None for flow in flows))),
                    ("Leituras", str(len(standalone))),
                    ("Casos explícitos", str(len(direct_cases))),
                    ("Rotas planejadas", str(api_plan["planned"])),
                    ("Rotas bloqueadas", str(api_plan["blocked"])),
                    ("Servidor", config.base_url),
                ])
            else:
                adapter = load_adapter(project_path)
                if adapter:
                    scan_summary = {"project": str(project_path), "mode": "agente",
                                    "adapter": "identity-agent.json", "cases": len(adapter["cases"]),
                                    "training_configured": bool(adapter.get("training"))}
                    progress.show_panel("SCANNER  /  AGENTE", [
                        ("Casos", str(len(adapter["cases"]))),
                        ("Treino", "configurado" if adapter.get("training") else "não configurado"),
                        ("Método", "Endpoint seguro declarado pelo projeto"),
                    ])
                else:
                    discovery = discover_agent(project_path)
                    if discovery and discovery["kind"] == "yasmin-command":
                        python = project_path / ".venv" / "Scripts" / "python.exe"
                        trainer = Path(__file__).with_name("train_yasmin.py")
                        model = project_path / "Agent" / "modelos" / "intencoes_local.json"
                        if saved_scan and (not python.is_file() or not trainer.is_file()):
                            raise ConfigurationError("Treinador ou Python do agente não encontrado.")
                        # Project test suites may execute desktop tools; the offline classifier is the test boundary.
                        config = replace(config, project=ProjectConfig(project_path, python, trainer,
                            ("--project", "{project_dir}", "--dados", "{output_dir}/treino_exemplos.jsonl", "--saida", "{project_dir}/Agent/modelos/intencoes_local.json"),
                            (), model))
                        scan_summary = {"project": str(project_path), "mode": "agente", "mechanism": discovery,
                                        "training_script": str(trainer)}
                    else:
                        scan_summary = {"project": str(project_path), "mode": "agente",
                                        "mechanism": discovery or "detectado pelo inventário estático",
                                        "executable": False}
                inventory = discover_features(project_path, adapter, discovery)
                if not adapter and (not discovery or discovery["kind"] != "yasmin-command"):
                    if not inventory["capabilities"] and not inventory["mechanisms"]:
                        raise ConfigurationError("Nenhuma capacidade ou conversa de agente encontrada no projeto.")
                    if args.command not in {"scam", "plan"}:
                        raise ConfigurationError("Para testar este agente, configure identity-agent.json com um endpoint seguro.")
                generated = generate_conversations(inventory)
                if adapter and adapter.get("capability_field"):
                    generated_cases = [{"split": "eval", "name": f"gerado:{item['capability']}:{index}",
                                        "messages": item["messages"], "expected": {"equals": item["capability"]},
                                        "response_field": adapter["capability_field"], "origin": "generated"}
                                       for index, item in enumerate(generated, 1)]
                    adapter = {**adapter, "cases": [*adapter["cases"], *generated_cases]}
                if adapter and not any(case["split"] == "eval" for case in adapter["cases"]):
                    raise ConfigurationError("Nenhuma conversa avaliável; informe cases ou capacidades com capability_field.")
                if adapter:
                    scan_summary["cases"] = len(adapter["cases"])
                scan_summary["capabilities_found"] = len(inventory["capabilities"])
                scan_summary["generated_conversations"] = len(generated)
                scan_summary["conversation_data"] = "conversas/dados_gerados.jsonl"
                progress.show_panel("CONVERSAS  /  GERADAS", [
                    ("Capacidades", str(len(inventory["capabilities"]))),
                    ("Variações", str(len(generated))),
                    ("Destino", str(project_folder(root, project_path.name) / "conversas")),
                ])
        if args.command == "plan":
            if mode == "api":
                for flow in flows:
                    steps = ["criar", *(["atualizar"] if flow.update else []),
                             *(["consultar"] if flow.read else []), *(["listar"] if flow.list_route else []), "excluir"]
                    print(f"FLUXO {flow.collection}  ->  {', '.join(steps)}")
                    create_preview = flow.create_body if flow.create_body is not None else synthesize_body(
                        flow.create.get("body_schema") or {}, "identitypreview")
                    print(f"      POST: {json.dumps(create_preview, ensure_ascii=False)}")
                    if flow.update:
                        update_preview = flow.update_body if flow.update_body is not None else synthesize_body(
                            flow.update.get("body_schema") or {}, "identitypreview_updated")
                        print(f"      {flow.update['method']}: {json.dumps(update_preview, ensure_ascii=False)}")
                for route in standalone:
                    print(f"LER   {route['method']} {route['path']}")
                for case in direct_cases:
                    print(f"CASO  {case['name']} -> {case['method']} {case['test_path']}")
                for item in skipped_routes:
                    print(f"PULAR {item['route']}: {item['reason']}")
            else:
                if adapter:
                    for case in (adapter["cases"] if args.all else adapter["cases"][:20]):
                        print(f"TESTE {case.get('name', 'conversa')}: {case['messages'][-1]['content']}")
                    for item in (generated if args.all else generated[:10]):
                        print(f"GERADO {item['capability']}: {item['messages'][-1]['content']}")
                    progress_status = "Finalizado"
                    return
                if not discovery or discovery["kind"] != "yasmin-command":
                    for item in (generated if args.all else generated[:20]):
                        print(f"GERADO {item['capability']}: {item['messages'][-1]['content']}")
                    progress_status = "Finalizado"
                    return
                pool = generate_cases(discovery["examples"], discovery.get("evaluation"))
                progress.show_panel("SCANNER  /  AGENTE", [
                    ("Capacidades", str(len(discovery["capabilities"]))),
                    ("Ferramentas", str(len(discovery.get("registered_tools", [])))),
                    ("Perguntas", str(len(pool))),
                    ("Sem exemplos", str(len(discovery.get("uncovered_tools", [])))),
                    ("Método", "Classificação local, sem executar ferramentas"),
                ])
                for case in (pool if args.all else pool[:20]):
                    print(f"TESTE {case['rotulo']}: {case['texto']}")
                if len(pool) > 20 and not args.all:
                    print(f"... mais {len(pool) - 20} perguntas; use --all para listar todas.")
            progress_status = "Finalizado"
            return
        if args.command == "scam":
            if project_path.name in reserved:
                raise ConfigurationError(f"Nome de projeto reservado: {project_path.name}")
            saved = save_project(root, project_path.name, scan_summary)
            if api_plan is not None:
                (saved.parent / "testes-api.json").write_text(
                    json.dumps(api_plan, ensure_ascii=False, indent=2), encoding="utf-8")
            if mode == "agente" and inventory is not None:
                save_conversations(project_folder(root, project_path.name), inventory, generated)
            progress.show_panel("PROJETO SALVO", [
                ("Nome", project_path.name),
                ("Scan", str(saved)),
                ("Executar", f"identity {project_path.name}"),
            ])
            progress_status = "Finalizado"
            return
        project_dir = project_folder(root, project_path.name)
        if scan_summary:
            save_project(root, project_path.name, scan_summary)
        if api_plan is not None:
            (project_dir / "testes-api.json").write_text(
                json.dumps(api_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        if mode == "agente" and inventory is not None:
            _, conversation_file = save_conversations(project_dir, inventory, generated)
        output_dir = project_dir / "runs" / datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        output_dir.mkdir(parents=True, exist_ok=True)
        output = args.output or output_dir / "resultados.jsonl"
        if mode == "api":
            result = asyncio.run(run_api_campaign(count, config.base_url, config.headers, config.timeout_seconds,
                                                   flows, standalone, output, progress, config.requests_per_agent,
                                                   direct_cases, config.max_concurrency))
            result["base_url"] = config.base_url
            result["untested_routes"] = skipped_routes
            result["discovered_routes"] = len(contracts)
            discovered_keys = {f"{route['method']} {route['path']}" for route in contracts}
            with output.open(encoding="utf-8") as records:
                tested_keys = {f"{item['method']} {item.get('route', item['path'])}" for line in records
                               if not (item := json.loads(line)).get("skipped")}
            result["tested_routes"] = len(discovered_keys & tested_keys)
            result["untested_routes"] = [*skipped_routes, *({"route": key, "reason": "Nenhum caso executado."}
                for key in sorted(discovered_keys - tested_keys) if key not in {item["route"] for item in skipped_routes})]
            progress.set_stage("Gerando relatório")
            report = write_report(output_dir, None, result, None)
            failed = result["failed"] or result["cleanup_failed"] or not result["requests"]
        else:
            if adapter:
                pool = [case for case in adapter["cases"] if case["split"] == "eval"]
                assignments = [pool[index % len(pool)] for index in range(count)]
                cursor = count
                round_results = []
                for round_number in range(1, rounds_configured + 1):
                    current = asyncio.run(run_adapter_cases(count, adapter, output, progress, assignments, round_number,
                                                            config.max_concurrency))
                    round_results.append(current)
                    progress.show_round(round_number, rounds_configured, current["passed"], current["failed"])
                    assignments, cursor, _ = next_cases(pool, assignments, current["outcomes"], cursor)
                result = {**round_results[-1], "requests": count * len(round_results),
                          "rounds": [{"round": item["round"], "passed": item["passed"],
                                      "failed": item["failed"]} for item in round_results]}
                project_result = train_adapter(project_path, adapter, output_dir, progress, generated)
                progress.set_stage("Gerando relatório")
                report = write_adapter_report(output_dir, result, project_result)
                failed = bool(result["failed"] or project_result["status"] in {"falhou", "rejeitado"})
            else:
                if config.project is None:
                    raise ConfigurationError("O modo agente requer project em identity.json.")
                if discovery is None:
                    raise ConfigurationError("O scanner não encontrou um modelo de capacidades do agente.")
                capability_model = OfflineCapabilityModel(
                    project_path / "Agent" / "modelos" / "capacidades_local.json.gz",
                    list(discovery["examples"]),
                    discovery["examples"].get("CHAMADA_CURTA", ()),
                )
                pool = generate_cases(discovery["examples"], discovery.get("evaluation"))
                progress.show_panel("SCANNER  /  AGENTE", [
                    ("Capacidades", str(len(discovery["capabilities"]))),
                    ("Ferramentas", str(len(discovery.get("registered_tools", [])))),
                    ("Perguntas", f"{len(pool)} variações"),
                    ("Sem exemplos", str(len(discovery.get("uncovered_tools", [])))),
                    ("Método", "Classificação local, sem abrir aplicativos"),
                ])
                (output_dir / "perguntas_geradas.json").write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
                assignments = [pool[i % len(pool)] for i in range(count)]
                cursor = count
                rounds = []
                for round_number in range(1, rounds_configured + 1):
                    totals = Counter(case["rotulo"] for case in assignments)
                    progress.set_stage(f"Classificação local · rodada {round_number}", dict(totals))
                    current = asyncio.run(run_agent_cases(count, assignments, capability_model, output, progress, round_number))
                    rounds.append(current)
                    assignments, cursor, _ = next_cases(pool, assignments, current["outcomes"], cursor)
                    if (project_path.name.casefold() == "yasmin"
                            and (project_path / "Agent" / "treino_llm" / "__main__.py").is_file()):
                        pair_result = add_round_pairs(conversation_file, round_number)
                        llm_result = train_llm_round(project_path, conversation_file, output_dir, round_number, progress)
                        llm_result["pairs_added"] = pair_result["added"]
                        llm_results.append(llm_result)
                    progress.show_round(
                        round_number, rounds_configured, current["passed"], current["failed"],
                        f"LLM {llm_results[-1]['status']}" if llm_results else None,
                    )
                    if llm_results and llm_results[-1]["status"] in {"bloqueado", "falhou"}:
                        break
                result = {**rounds[-1], "rounds": [{"round": item["round"], "passed": item["passed"], "failed": item["failed"]} for item in rounds],
                          "rounds_configured": rounds_configured,
                          "requests": count * len(rounds), "pool_size": len(pool),
                          "uncovered_tools": discovery.get("uncovered_tools", [])}
                examples = write_training_examples(pool, output_dir / "treino_exemplos.jsonl")
                project_result = run_project(config.project, output_dir, progress)
                progress.set_stage("Gerando relatório")
                report = write_agent_report(output_dir, result, project_result, examples, llm_results)
                llm_blocked = any(item["status"] in {"bloqueado", "falhou"} for item in llm_results)
                no_llm_improvement = bool(llm_results and not any(
                    item["status"] == "publicado" for item in llm_results))
                failed = bool(result["failed"] or project_result["training_exit_code"]
                              or llm_blocked or no_llm_improvement
                              or len(rounds) < rounds_configured)
        machine_summary = {"project": project_path.name, "mode": mode, "failed": bool(failed),
                           "report": str(report), "result": result}
        if mode == "agente":
            machine_summary["training"] = project_result
            if llm_results:
                machine_summary["llm_training"] = llm_results
        (output_dir / "summary.json").write_text(json.dumps(machine_summary, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
        progress_status = "Finalizado"
    except (ConfigurationError, OSError, TypeError, ValueError) as exc:
        parser.exit(2, f"Erro: {exc}\n")
    except KeyboardInterrupt:
        progress_status = "Interrompido"
        parser.exit(130, "Execução interrompida.\n")
    finally:
        progress.finish(progress_status)
    if mode == "api":
        progress.show_panel("RESULTADO  /  API", [
            ("Requisições", str(result["requests"])),
            ("Rotas testadas", f"{result['tested_routes']}/{result['discovered_routes']}"),
            ("Sucessos", str(result["passed"])),
            ("Falhas", str(result["failed"])),
            ("Limpeza", f"{result['cleanup_ok']} confirmadas, {result['cleanup_failed']} pendentes"),
            ("Latência média", f"{result['average_ms']} ms"),
            ("Relatório", str(report)),
        ])
    else:
        if adapter:
            progress.show_panel("RESULTADO  /  AGENTE", [
                ("Conversas", f"{result['passed']}/{count} na última rodada"),
                ("Treino", project_result["status"]),
                ("Relatório", str(report)),
            ])
        else:
            progress.show_panel("RESULTADO  /  AGENTE", [
                ("Rodadas", str(len(result["rounds"]))),
                ("Classificações", f"{result['passed']}/{count} na última rodada"),
                ("Classificador", "OK" if project_result["training_exit_code"] == 0 else "Falhou"),
                ("LLM de conversa", llm_results[-1]["status"] if llm_results else "não configurada"),
                *([("Motivo LLM", llm_results[-1]["reason"])]
                  if llm_results and llm_results[-1].get("reason") else []),
                ("Relatório", str(report)),
            ])
    if failed:
        sys.exit(1)
