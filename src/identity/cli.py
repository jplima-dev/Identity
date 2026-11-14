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

from .agent_mode import (
    generate_cases,
    run_agent_cases,
    write_agent_report,
    write_training_examples,
)
from .api_discovery import discover_contracts
from .api_workflow import plan_resources, run_api_campaign
from .campaign import next_cases
from .offline_model import OfflineCapabilityModel
from .progress import ProgressDisplay
from .project import ProjectConfig, parse_project, run_project, write_report
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


def agent_count(root: Path) -> int:
    candidates = [p for p in root.glob("*.ag") if p.is_file() and p.stem.isdecimal()]
    if len(candidates) != 1:
        raise ConfigurationError(
            "A raiz deve conter exatamente um arquivo numérico .ag (por exemplo, '100.ag')."
        )
    count = int(candidates[0].stem)
    if count < 1:
        raise ConfigurationError("O número de agentes deve ser maior que zero.")
    return count


def run_mode(root: Path) -> str:
    modes = [name for name in ("agente", "api") if (root / f"{name}.tes").is_file()]
    if len(modes) != 1:
        raise ConfigurationError("Crie exatamente um arquivo de modo na raiz: 'agente.tes' ou 'api.tes'.")
    return modes[0]


def round_count(root: Path) -> int:
    candidates = [p for p in root.glob("*.rod") if p.is_file() and p.stem.isdecimal()]
    if len(candidates) != 1:
        raise ConfigurationError("O modo agente requer exatamente um arquivo numérico .rod (por exemplo, '10.rod').")
    rounds = int(candidates[0].stem)
    if rounds < 1:
        raise ConfigurationError("O número de rodadas deve ser maior que zero.")
    return rounds


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
    return RunConfig(base_url, tuple(scenarios), headers, requests_per_agent, float(timeout), project)


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
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="Executa os agentes definidos pelo arquivo .ag na raiz.")
    run.add_argument("--url", help="Sobrescreve base_url em identity.json.")
    run.add_argument("--requests-per-agent", type=int, help="Número de requisições por agente.")
    run.add_argument("--output", type=Path, help="Caminho do arquivo JSONL de resultados.")
    scam = subcommands.add_parser("scam", help="Escaneia o projeto atual e executa os testes do modo escolhido.")
    scam.add_argument("--project", type=Path, help="Pasta do projeto alvo; padrão: pasta que contém o Identity.")
    scam.add_argument("--url", help="URL base da API.")
    scam.add_argument("--output", type=Path, help="Caminho do arquivo JSONL de resultados.")
    args = parser.parse_args()
    cwd = Path.cwd()
    root = cwd / "Identity" if (cwd / "Identity" / "identity.json").is_file() else cwd
    progress = ProgressDisplay().start()
    try:
        count = agent_count(root)
        mode = run_mode(root)
        config = load_config(root / "identity.json", args.url, getattr(args, "requests_per_agent", None), False, True)
        project_path = None
        discovery = None
        scan_summary = None
        if args.command in {"scam", "run"}:
            project_path = (getattr(args, "project", None) or (root.parent if root.name.casefold() == "identity" else cwd)).resolve()
            if project_path == root or not project_path.is_dir():
                raise ConfigurationError("Coloque Identity dentro do projeto ou informe --project.")
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
                allow_remote_mutations = raw_api.get("allow_remote_mutations", False)
                if not isinstance(allow_remote_mutations, bool):
                    raise ConfigurationError("allow_remote_mutations deve ser true ou false.")
                discovered_url = discover_base_url(project_path)
                config = replace(config, base_url=args.url or discovered_url or config.base_url)
                flows, skipped_routes, standalone = plan_resources(contracts, payloads)
                hostname = urlsplit(config.base_url).hostname
                if hostname not in {"127.0.0.1", "localhost", "::1"} and not allow_remote_mutations:
                    skipped_routes.extend({"route": f"POST {flow.collection}", "reason": "Mutação remota requer allow_remote_mutations=true."} for flow in flows)
                    flows = []
                scan_summary = {"project": str(project_path), "mode": "api", "base_url": config.base_url,
                                "routes": contracts, "resource_flows": [flow.collection for flow in flows],
                                "standalone_routes": standalone, "untested_routes": skipped_routes}
            else:
                discovery = discover_agent(project_path)
                if not discovery or discovery["kind"] != "yasmin-command":
                    raise ConfigurationError("Mecanismo de resposta encontrado sem adaptador executável; configure um adaptador para esse agente.")
                python = project_path / ".venv" / "Scripts" / "python.exe"
                trainer = Path(__file__).with_name("train_yasmin.py")
                model = project_path / "Agent" / "modelos" / "intencoes_local.json"
                if not python.is_file() or not trainer.is_file():
                    raise ConfigurationError("Treinador ou Python do agente não encontrado.")
                # Project test suites may execute desktop tools; the offline classifier is the test boundary.
                config = replace(config, project=ProjectConfig(project_path, python, trainer,
                    ("--project", "{project_dir}", "--dados", "{output_dir}/treino_exemplos.jsonl", "--saida", "{project_dir}/Agent/modelos/intencoes_local.json"),
                    (), model))
                scan_summary = {"project": str(project_path), "mode": "agente", "mechanism": discovery, "training_script": str(trainer)}
        output_dir = root / (project_path.name if project_path else config.project.path.name if config.project else "results")
        output_dir.mkdir(parents=True, exist_ok=True)
        if scan_summary:
            (output_dir / "scan.json").write_text(json.dumps(scan_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        output = args.output or output_dir / f"run-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}.jsonl"
        if mode == "api":
            result = asyncio.run(run_api_campaign(count, config.base_url, config.headers, config.timeout_seconds,
                                                   flows, standalone, output, progress, config.requests_per_agent))
            result["base_url"] = config.base_url
            result["untested_routes"] = skipped_routes
            result["discovered_routes"] = len(contracts)
            progress.set_stage("Gerando relatório")
            report = write_report(output_dir, None, result, None)
            failed = result["failed"] or result["cleanup_failed"] or not result["requests"]
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
            (output_dir / "perguntas_geradas.json").write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
            assignments = [pool[i % len(pool)] for i in range(count)]
            cursor = count
            rounds = []
            for round_number in range(1, round_count(root) + 1):
                totals = Counter(case["rotulo"] for case in assignments)
                progress.set_stage(f"Classificação local · rodada {round_number}", dict(totals))
                current = asyncio.run(run_agent_cases(count, assignments, capability_model, output, progress, round_number))
                rounds.append(current)
                assignments, cursor, _ = next_cases(pool, assignments, current["outcomes"], cursor)
            result = {**rounds[-1], "rounds": [{"round": item["round"], "passed": item["passed"], "failed": item["failed"]} for item in rounds],
                      "requests": count * len(rounds), "pool_size": len(pool),
                      "uncovered_tools": discovery.get("uncovered_tools", [])}
            examples = write_training_examples(pool, output_dir / "treino_exemplos.jsonl")
            project_result = run_project(config.project, output_dir, progress)
            progress.set_stage("Gerando relatório")
            report = write_agent_report(output_dir, result, project_result, examples)
            failed = result["failed"] or project_result["training_exit_code"]
    except (ConfigurationError, OSError, ValueError) as exc:
        parser.exit(2, f"Erro: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Execução interrompida.\n")
    finally:
        progress.finish()
    if mode == "api":
        print(f"Total: {result['requests']} | sucesso: {result['passed']} | falha: {result['failed']} | "
              f"limpeza: {result['cleanup_ok']} confirmadas, {result['cleanup_failed']} pendentes | média: {result['average_ms']} ms")
    else:
        print(f"Rodadas: {len(result['rounds'])} | classificações finais: {result['passed']}/{count} | treino: {project_result['training_exit_code']}")
    print(f"Relatório: {report}")
    if failed:
        sys.exit(1)
