"""Run a project's existing trainer and tests, then write a local report."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .progress import ProgressDisplay


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    python: Path
    training_script: Path
    training_args: tuple[str, ...]
    test_modules: tuple[str, ...]
    model_path: Path | None


def parse_project(raw: Any) -> ProjectConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
        raise TypeError("project.path deve indicar a pasta do projeto.")
    path = Path(raw["path"]).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Projeto não encontrado: {path}")
    python_value = raw.get("python")
    if not isinstance(python_value, str):
        raise TypeError("project.python deve indicar o interpretador do projeto.")
    python = Path(python_value)
    if not python.is_absolute():
        python = path / python
    if not python.is_file():
        raise ValueError(f"Python do projeto não encontrado: {python}")
    training_script = raw.get("training_script")
    if not isinstance(training_script, str):
        raise TypeError("project.training_script deve indicar o script de treino.")
    script = (path / training_script).resolve()
    if not script.is_file() or not script.is_relative_to(path):
        raise ValueError("Script de treino não encontrado dentro do projeto.")
    args = raw.get("training_args", [])
    tests = raw.get("test_modules", [])
    if not isinstance(args, list) or any(not isinstance(v, str) for v in args):
        raise ValueError("project.training_args deve ser uma lista de textos.")
    if not isinstance(tests, list) or any(not isinstance(v, str) for v in tests):
        raise ValueError("project.test_modules deve ser uma lista de nomes de testes.")
    model_value = raw.get("model_path")
    model_path = (path / model_value).resolve() if isinstance(model_value, str) else None
    if model_path is not None and not model_path.is_relative_to(path):
        raise ValueError("project.model_path deve ficar dentro do projeto.")
    return ProjectConfig(path, python, script, tuple(args), tuple(tests), model_path)


def _execute(command: list[str], cwd: Path, output: Path) -> tuple[int, str]:
    env = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_ACTIVE"):
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(command, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=600, check=False)
        log = result.stdout + result.stderr
        code = result.returncode
    except subprocess.TimeoutExpired as exc:
        log = str(exc)
        code = 124
    output.write_text(log, encoding="utf-8")
    return code, log


def run_project(config: ProjectConfig, output_dir: Path, progress: ProgressDisplay | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    backup = output_dir / "modelo_anterior.json"
    if config.model_path and config.model_path.is_file():
        shutil.copy2(config.model_path, backup)
    args = [arg.replace("{output_dir}", str(output_dir)).replace("{project_dir}", str(config.path)) for arg in config.training_args]
    if progress:
        progress.set_stage("Treinando modelo")
    train_code, train_log = _execute(
        [str(config.python), str(config.training_script), *args],
        config.path,
        output_dir / "treino.log",
    )
    test_code = None
    test_log = ""
    if config.test_modules:
        if progress:
            progress.set_stage("Testando funcionalidades")
        test_code, test_log = _execute(
            [str(config.python), "-m", "unittest", *config.test_modules],
            config.path,
            output_dir / "testes.log",
        )
    if config.model_path and backup.exists() and (train_code or test_code):
        shutil.copy2(backup, config.model_path)
    return {"training_exit_code": train_code, "training_log": train_log, "tests_exit_code": test_code, "tests_log": test_log}


def write_report(output_dir: Path, project: ProjectConfig | None, api: dict[str, Any], project_result: dict[str, Any] | None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    api_errors: Counter[str] = Counter()
    blocked: Counter[str] = Counter()
    with Path(api["output"]).open(encoding="utf-8") as records:
        for line in records:
            item = json.loads(line)
            if item.get("skipped"):
                blocked[f"{item['method']} {item['path']}: {item.get('error') or 'etapa ignorada'}"] += 1
            elif not item["passed"]:
                api_errors[f"{item['method']} {item['path']}: {item['error'] or 'HTTP ' + str(item['status'])}"] += 1
    lines = [
        f"# Relatório: {output_dir.parent.parent.name if output_dir.parent.name == 'runs' else output_dir.name}",
        "",
        "## API",
        "",
        f"- Agentes: {api['agents']}",
        f"- URL base: {api.get('base_url', 'não informada')}",
        f"- Endpoints descobertos: {api.get('discovered_routes', 'não informado')}",
        f"- Rotas executadas: {api.get('tested_routes', 'não informado')}/{api.get('discovered_routes', 'não informado')}",
        f"- Cenários: {api.get('cases', 'não informado')} (fluxos CRUD: {api.get('complex_cases', 'não informado')})",
        f"- Requisições: {api['requests']}",
        f"- Sucessos: {api['passed']}",
        f"- Falhas: {api['failed']}",
        f"- Etapas não executadas: {api.get('skipped', 0)}",
        f"- Recursos excluídos ao final: {api.get('cleanup_ok', 0)}",
        f"- Limpezas não confirmadas: {api.get('cleanup_failed', 0)}",
        f"- Latência média: {api['average_ms']} ms",
        f"- Dados: {Path(api['output']).name}",
        "",
        "### Falhas da API",
        "",
        *(f"- {reason}: {count}" for reason, count in api_errors.items()),
        *(("Nenhuma falha.",) if not api_errors else ()),
        "",
        "### Etapas bloqueadas",
        "",
        *(f"- {reason}: {count}" for reason, count in blocked.items()),
        *(("Nenhuma.",) if not blocked else ()),
        "",
        "### Rotas descobertas sem execução automática",
        "",
        *(f"- {item['route']}: {item['reason']}" for item in api.get("untested_routes", [])),
        *(("Nenhuma.",) if not api.get("untested_routes") else ()),
        "",
    ]
    if project is not None and project_result is not None:
        lines.extend([
            "## Treinamento do projeto",
            "",
            f"- Projeto: {project.path}",
            f"- Script: {project.training_script.relative_to(project.path)}",
            f"- Código de saída: {project_result['training_exit_code']}",
            "- Log: treino.log",
            "",
            "### Resultado do treino",
            "",
            "```text",
            project_result["training_log"].strip(),
            "```",
            "",
            "## Testes de funcionalidades",
            "",
            f"- Módulos: {', '.join(project.test_modules) or 'nenhum'}",
            f"- Código de saída: {project_result['tests_exit_code']}",
            "- Log: testes.log" if project.test_modules else "- Nenhum teste configurado",
            "",
        ])
        for title, log in (("Erros do treino", project_result["training_log"]), ("Erros dos testes", project_result["tests_log"])):
            failures = [line for line in log.splitlines() if "Error" in line or "Erro" in line or "FAIL" in line or "Traceback" in line]
            lines.extend([f"### {title}", "", *(f"- {line}" for line in failures[:30])] if failures else [f"### {title}", "", "Nenhum erro identificado no log."])
            lines.append("")
    report = output_dir / "relatorio-api.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report
