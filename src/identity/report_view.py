"""Compact CLI diagnosis from a full project report and its machine summary."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def _failures(output: Any, mode: str, last_round: int, limit: int = 5) -> tuple[list[str], int]:
    if not isinstance(output, str) or not Path(output).is_file():
        return [], 0
    grouped: Counter[str] = Counter()
    priority: dict[str, int] = {}
    with Path(output).open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            if item.get("passed") or item.get("skipped"):
                continue
            if mode == "agente" and item.get("rodada") != last_round:
                continue
            if mode == "api":
                label = f"{item.get('method')} {item.get('path')}: {item.get('error') or 'HTTP ' + str(item.get('status'))}"
            elif "capacidade" in item:
                label = (f"{item.get('capacidade')}: esperado {item.get('capacidade')}, "
                         f"previsto {item.get('predicao')} — {item.get('texto', '')}")
            else:
                label = f"{item.get('case', 'conversa')}: {item.get('error', 'resposta incorreta')}"
            grouped[label] += 1
            if mode == "api" and "cleanup" in str(item.get("phase", "")):
                priority[label] = 2
            elif mode == "api" and item.get("phase") == "invalid_update":
                priority[label] = 1
    ranked = sorted(grouped, key=lambda label: (priority.get(label, 0), grouped[label]), reverse=True)
    return [f"{label} ({grouped[label]}x)" if grouped[label] > 1 else label for label in ranked[:limit]], sum(grouped.values())


def _legacy(report: Path) -> tuple[list[tuple[str, str]], list[str], int]:
    rows: list[tuple[str, str]] = [("Resumo", "Relatório antigo; diagnóstico parcial")]
    problems = []
    section = ""
    for line in report.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            section = line.lstrip("# ").casefold()
        elif line.startswith("- "):
            entry = line[2:]
            if entry.casefold().startswith(("falhas:", "falhas na última", "limpezas não confirmadas:",
                                            "rotas executadas:", "código de saída do treino:", "estado:")):
                key, _, value = entry.partition(":")
                rows.append((key, value.strip()))
            elif any(term in section for term in ("falhas", "erros do treino", "erros dos testes")) and not entry.startswith("Nenhuma"):
                problems.append(entry)
    if not problems:
        problems.append("Veja o arquivo completo para detalhes desta execução antiga.")
    return rows, problems[:5], max(0, len(problems) - 5)


def debug_summary(report: Path) -> tuple[list[tuple[str, str]], list[str], int]:
    """Return terminal rows, the most useful failures, and hidden issue count."""
    summary_file = report.parent / "summary.json"
    if not summary_file.is_file():
        return _legacy(report)
    try:
        data = json.loads(summary_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _legacy(report)
    result = data.get("result", {})
    mode = data.get("mode")
    if mode not in {"api", "agente"} or not isinstance(result, dict):
        return _legacy(report)
    configured = result.get("rounds_configured") if mode == "agente" else None
    completed = len(result.get("rounds", [])) if mode == "agente" else 0
    incomplete = type(configured) is int and configured > completed
    blocked_routes = bool(result.get("untested_routes")) if mode == "api" else False
    state = ("Campanha incompleta" if incomplete else
             "Falhas encontradas" if result.get("failed") or result.get("cleanup_failed") else
             "Cobertura incompleta" if blocked_routes else
             "Falhas encontradas" if data.get("failed") else
             "Sem falhas detectadas")
    rows = [("Estado", state)]
    if mode == "api":
        rows.extend([
            ("Rotas", f"{result.get('tested_routes', 0)}/{result.get('discovered_routes', 0)} testadas"),
            ("Requisições", str(result.get("requests", 0))),
            ("Falhas HTTP/dados", str(result.get("failed", 0))),
            ("Limpeza pendente", str(result.get("cleanup_failed", 0))),
            ("Rotas sem teste", str(len(result.get("untested_routes", [])))),
        ])
    else:
        rows.extend([
            ("Rodadas", f"{completed}/{configured}" if type(configured) is int else str(completed)),
            ("Testes", str(result.get("requests", 0))),
            ("Falhas finais", str(result.get("failed", 0))),
        ])
        training = data.get("training", {})
        if isinstance(training, dict):
            status = training.get("status")
            if status is None and "training_exit_code" in training:
                status = "OK" if training["training_exit_code"] == 0 else "Falhou"
            label = "Classificador" if data.get("llm_training") else "Treino"
            rows.append((label, str(status or "não informado")))
            if training.get("before") and training.get("after"):
                before, after = training["before"], training["after"]
                rows.append(("Avaliação", f"{before['passed']}/{before['total']} → {after['passed']}/{after['total']}"))
            generic_rounds = training.get("rounds", [])
            if generic_rounds:
                rows.append(("Passos LLM", str(sum(item.get("optimization_steps", 0) for item in generic_rounds))))
                rows.append(("Candidatos rejeitados", str(sum(item.get("status") == "rejeitado"
                                                            for item in generic_rounds))))
                latest = generic_rounds[-1]
                if latest.get("metric_before") is not None and latest.get("metric_candidate") is not None:
                    rows.append(("Última avaliação", f"{latest['metric_before']} → {latest['metric_candidate']}"))
        llm = data.get("llm_training", [])
        if isinstance(llm, list) and llm:
            rows.append(("LLM de conversa", str(llm[-1].get("status", "não informado"))))
            rows.append(("Passos LoRA", str(sum(item.get("optimization_steps", 0) for item in llm))))
            rejected = sum(item.get("status") == "rejeitado" for item in llm)
            if rejected:
                rows.append(("Rejeitados", str(rejected)))
    last_round = len(result.get("rounds", [])) or 1
    untested = result.get("untested_routes", []) if mode == "api" else []
    problems, total = _failures(result.get("output"), mode, last_round, 3 if untested else 5)
    if mode == "api":
        problems.extend(f"Sem teste: {item['route']} — {item['reason']}" for item in untested[:2])
        total += len(untested)
        if result.get("cleanup_failed") and not any("DELETE" in problem for problem in problems):
            problems.append("Uma ou mais exclusões não puderam ser confirmadas.")
            total += 1
    else:
        training = data.get("training", {})
        if isinstance(training, dict) and training.get("reason"):
            problems.append(f"Treino: {training['reason']}")
            total += 1
        if isinstance(training, dict) and training.get("training_exit_code"):
            log = training.get("training_log", "")
            errors = [line for line in log.splitlines() if any(word in line for word in ("Erro", "Error", "FAIL", "Traceback"))]
            problems.extend(f"Treino: {line}" for line in errors[-2:])
            total += len(errors[-2:])
        llm = data.get("llm_training", [])
        if isinstance(llm, list):
            for item in llm:
                if isinstance(item, dict) and item.get("status") in {"bloqueado", "falhou"}:
                    problems.append(f"LLM, rodada {item.get('round')}: {item.get('reason') or item.get('status')}")
                    total += 1
            if llm and all(isinstance(item, dict) and item.get("status") == "rejeitado" for item in llm):
                problems.append("LLM: nenhum candidato melhorou a perda reservada; modelo ativo preservado.")
                total += 1
    return rows, problems[:5], max(0, total - 5)
