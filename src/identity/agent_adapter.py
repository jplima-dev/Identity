"""Project-owned contract for testing conversations and publishing trained models."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

import httpx

from .progress import ProgressDisplay


def load_adapter(project: Path) -> dict[str, Any] | None:
    file = project / "identity-agent.json"
    if not file.is_file():
        return None
    data = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("identity-agent.json requer version: 1.")
    endpoint = data.get("endpoint")
    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if not parsed or parsed.scheme not in {"http", "https"} or not parsed.netloc or data.get("safe_mode") is not True:
        raise ValueError("identity-agent.json requer endpoint HTTP(S) e safe_mode: true (sem executar ferramentas).")
    timeout = data.get("timeout_seconds", 10)
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise ValueError("identity-agent.json timeout_seconds deve estar entre 0 e 300.")
    if not isinstance(data.get("response_field", "response"), str):
        raise TypeError("identity-agent.json response_field deve ser texto.")
    capability_field = data.get("capability_field")
    if capability_field is not None and (not isinstance(capability_field, str) or not capability_field):
        raise ValueError("identity-agent.json capability_field deve ser texto não vazio.")
    cases = data.get("cases", [])
    if not isinstance(cases, list) or (not cases and not capability_field):
        raise ValueError("identity-agent.json requer cases ou capability_field.")
    data["cases"] = cases
    for index, case in enumerate(cases, 1):
        if (not isinstance(case, dict) or not isinstance(case.get("messages"), list) or
                not case["messages"] or case.get("split") not in {"train", "eval"} or
                not all(isinstance(message, dict) and message.get("role") in {"user", "assistant", "system"}
                            and isinstance(message.get("content"), str) for message in case["messages"])):
            raise ValueError(f"Caso {index} inválido em identity-agent.json.")
        expected = case.get("expected")
        if not isinstance(expected, dict) or not any(isinstance(expected.get(key), str) for key in ("contains", "equals")):
            raise ValueError(f"Caso {index} requer expected.contains ou expected.equals.")
    if not any(case["split"] == "eval" for case in cases) and not capability_field:
        raise ValueError("identity-agent.json requer casos de avaliação separados ou capability_field.")
    train_messages = {json.dumps(case["messages"], ensure_ascii=False, sort_keys=True)
                      for case in cases if case["split"] == "train"}
    eval_messages = {json.dumps(case["messages"], ensure_ascii=False, sort_keys=True)
                     for case in cases if case["split"] == "eval"}
    if train_messages & eval_messages:
        raise ValueError("A mesma conversa não pode aparecer em train e eval.")
    training = data.get("training")
    if isinstance(training, dict) and training.get("kind") == "llm":
        from .llm_adapter import validate_llm_training

        validate_llm_training(project, training, cases)
    elif training is not None:
        if (not isinstance(training, dict) or not isinstance(training.get("command"), list) or
                not isinstance(training.get("evaluate_command"), list) or
                not all(isinstance(arg, str) for arg in [*training["command"], *training["evaluate_command"]]) or
                not training["command"] or not training["evaluate_command"] or
                not isinstance(training.get("model_path"), str) or
                not any(case["split"] == "eval" for case in cases) or
                not (any(case["split"] == "train" for case in cases) or training.get("use_generated") is True)):
            raise ValueError("training requer command, evaluate_command, model_path, eval manual e dados train.")
        model = (project / training["model_path"]).resolve()
        if not model.is_relative_to(project.resolve()) or model == project.resolve():
            raise ValueError("training.model_path deve ficar dentro do projeto.")
        if (not any("{train_data}" in arg for arg in training["command"]) or
                not any("{candidate_model}" in arg for arg in training["command"]) or
                not any("{model}" in arg for arg in training["evaluate_command"]) or
                not any("{eval_data}" in arg for arg in training["evaluate_command"])):
            raise ValueError("Comandos de treino/avaliação devem usar train_data, candidate_model, model e eval_data.")
    return data


async def run_adapter_cases(count: int, adapter: dict[str, Any], output: Path,
                            progress: ProgressDisplay | None = None,
                            assignments: list[dict[str, Any]] | None = None,
                            round_number: int = 1,
                            max_concurrency: int = 20) -> dict[str, Any]:
    cases = [case for case in adapter["cases"] if case["split"] == "eval"]
    assignments = assignments or [cases[(agent - 1) % len(cases)] for agent in range(1, count + 1)]
    if len(assignments) != count:
        raise ValueError("Cada agente precisa de um caso de avaliação.")
    output.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress.set_stage(f"Testando conversas · rodada {round_number}", {"conversa": count})
    semaphore = asyncio.Semaphore(min(count, max_concurrency))
    async with httpx.AsyncClient(timeout=adapter.get("timeout_seconds", 10), follow_redirects=False) as client:
        async def test(agent: int) -> dict[str, Any]:
            case = assignments[agent - 1]
            started = perf_counter()
            response_text = None
            error = None
            status = None
            async with semaphore:
                try:
                    response = await client.post(adapter["endpoint"], json={"messages": case["messages"]})
                    status = response.status_code
                    payload = response.json()
                    field = case.get("response_field", adapter.get("response_field", "response"))
                    response_text = payload.get(field) if isinstance(payload, dict) else None
                    if status != 200 or not isinstance(response_text, str):
                        error = f"HTTP {status} ou campo de resposta ausente"
                    elif "equals" in case["expected"] and response_text != case["expected"]["equals"]:
                        error = "resposta diferente da esperada"
                    elif "contains" in case["expected"] and case["expected"]["contains"] not in response_text:
                        error = "resposta não contém o texto esperado"
                except (httpx.RequestError, ValueError, TypeError) as exc:
                    error = type(exc).__name__
            if progress:
                progress.advance("conversa")
            return {"timestamp": datetime.now(UTC).isoformat(), "agent": agent, "rodada": round_number,
                    "case": case.get("name", "conversa"), "origin": case.get("origin", "manual"),
                    "messages": case["messages"], "expected": case["expected"], "response": response_text,
                    "status": status, "passed": error is None, "error": error,
                    "latency_ms": round((perf_counter() - started) * 1000, 2)}

        records = await asyncio.gather(*(test(agent) for agent in range(1, count + 1)))
    with output.open("w" if round_number == 1 else "a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"agents": count, "requests": count, "passed": sum(item["passed"] for item in records),
            "failed": sum(not item["passed"] for item in records), "output": str(output),
            "unique_cases": len(cases), "outcomes": [item["passed"] for item in records],
            "round": round_number}


def train_adapter(project: Path, adapter: dict[str, Any], output_dir: Path,
                  progress: ProgressDisplay | None = None,
                  generated: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    training = adapter.get("training")
    if training is None:
        return {"status": "não configurado"}
    if progress:
        progress.set_stage("Treinando modelo candidato")
    train_data = output_dir / "conversas_treino.jsonl"
    eval_data = output_dir / "conversas_avaliacao.jsonl"
    held_out = {json.dumps(case["messages"], ensure_ascii=False, sort_keys=True)
                for case in adapter["cases"] if case["split"] == "eval" and case.get("origin") != "generated"}
    manual_train = any(case["split"] == "train" for case in adapter["cases"])
    eligible_generated = [case for case in generated or []
                          if json.dumps(case["messages"], ensure_ascii=False, sort_keys=True) not in held_out]
    if not manual_train and not eligible_generated:
        raise ValueError("Nenhum dado de treino separado dos casos de avaliação manual.")
    for split, path in (("train", train_data), ("eval", eval_data)):
        with path.open("w", encoding="utf-8") as stream:
            for case in adapter["cases"]:
                if case["split"] == split and (split != "eval" or case.get("origin") != "generated"):
                    stream.write(json.dumps(case, ensure_ascii=False) + "\n")
            if split == "train" and training.get("use_generated") is True:
                for case in eligible_generated:
                    stream.write(json.dumps({**case, "split": "train"}, ensure_ascii=False) + "\n")
    model = (project / training["model_path"]).resolve()
    candidate = output_dir / f"modelo_candidato{model.suffix}"
    values = {"train_data": str(train_data), "eval_data": str(eval_data),
              "candidate_model": str(candidate), "model_path": str(model)}

    def invoke(parts: list[str], selected_model: Path) -> subprocess.CompletedProcess[str]:
        variables = {**values, "model": str(selected_model)}
        try:
            command = [part.format_map(variables) for part in parts]
        except KeyError as exc:
            raise ValueError(f"Placeholder desconhecido no comando de treino: {exc}") from exc
        try:
            return subprocess.run(command, cwd=project, text=True, capture_output=True, timeout=300, check=False)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(command, 124, "", "Tempo limite de 300 segundos excedido.")

    def score_from(process: subprocess.CompletedProcess[str]) -> dict[str, int]:
        try:
            score = json.loads(process.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise ValueError("Avaliador deve imprimir um JSON com passed e total na última linha.") from exc
        if (not isinstance(score, dict) or type(score.get("passed")) is not int or
                type(score.get("total")) is not int or score["total"] < 1 or
                not 0 <= score["passed"] <= score["total"]):
            raise ValueError("Avaliador deve imprimir JSON com passed e total válidos.")
        return score

    baseline = None
    if model.is_file():
        before = invoke(training["evaluate_command"], model)
        if before.returncode:
            raise ValueError(f"Avaliação do modelo atual falhou: {before.stderr.strip()}")
        baseline = score_from(before)
    trained = invoke(training["command"], candidate)
    (output_dir / "treino.log").write_text(trained.stdout + trained.stderr, encoding="utf-8")
    if trained.returncode or not candidate.is_file():
        return {"status": "falhou", "reason": "Treinador não produziu um modelo candidato.", "exit_code": trained.returncode}
    if progress:
        progress.set_stage("Avaliando modelo candidato")
    after = invoke(training["evaluate_command"], candidate)
    if after.returncode:
        return {"status": "falhou", "reason": after.stderr.strip(), "exit_code": after.returncode}
    score = score_from(after)
    ratio = score["passed"] / score["total"]
    old_ratio = baseline["passed"] / baseline["total"] if baseline else None
    threshold = training.get("min_score", 0.8)
    if not isinstance(threshold, (float, int)) or not 0 <= threshold <= 1:
        raise ValueError("training.min_score deve estar entre 0 e 1.")
    if ratio < threshold or (old_ratio is not None and ratio <= old_ratio):
        return {"status": "rejeitado", "before": baseline, "after": score,
                "reason": "O candidato não superou o modelo atual ou o mínimo exigido."}
    backup = output_dir / f"modelo_anterior{model.suffix}"
    if model.is_file():
        shutil.copy2(model, backup)
    model.parent.mkdir(parents=True, exist_ok=True)
    temporary = model.with_name(model.name + ".identity-new")
    shutil.copy2(candidate, temporary)
    temporary.replace(model)
    return {"status": "publicado", "before": baseline, "after": score,
            "model": str(model), "backup": str(backup) if backup.is_file() else None}


def write_adapter_report(output_dir: Path, result: dict[str, Any], training: dict[str, Any]) -> Path:
    project_name = output_dir.parent.parent.name if output_dir.parent.name == "runs" else output_dir.name
    lines = [f"# Relatório: {project_name}", "", "## Conversas", "",
             f"- Casos distintos de avaliação: {result['unique_cases']}",
             f"- Rodadas: {len(result.get('rounds', [])) or 1}",
             f"- Execuções: {result['requests']}", f"- Sucessos: {result['passed']}",
             f"- Falhas na última rodada: {result['failed']}", f"- Registros: {Path(result['output']).name}",
             "", "## Treino", "", f"- Estado: {training['status']}"]
    lines.extend(f"- Rodada {item['round']}: {item['passed']} sucessos, {item['failed']} falhas"
                 for item in result.get("rounds", []))
    if training.get("before"):
        lines.append(f"- Avaliação anterior: {training['before']['passed']}/{training['before']['total']}")
    if training.get("after"):
        lines.append(f"- Avaliação candidata: {training['after']['passed']}/{training['after']['total']}")
    if training.get("reason"):
        lines.append(f"- Motivo: {training['reason']}")
    for item in training.get("rounds", []):
        lines.extend(["", f"### LLM · rodada {item['round']}", "",
                      f"- Estado: {item['status']}",
                      f"- Pares revisados: {item['pairs_reviewed']}",
                      f"- Passos de otimização: {item['optimization_steps']}",
                      f"- Métrica anterior: {item['metric_before']}",
                      f"- Métrica candidata: {item['metric_candidate']}",
                      f"- Modelo candidato: {item['candidate_model'] or 'não produzido'}",
                      f"- Modelo ativado: {item['model_published'] or 'não publicado'}"])
        if item.get("reason"):
            lines.append(f"- Motivo: {item['reason']}")
    lines.extend(["", "## Falhas", ""])
    with Path(result["output"]).open(encoding="utf-8") as stream:
        failed = []
        for line in stream:
            item = json.loads(line)
            if not item["passed"] and item.get("rodada") == (len(result.get("rounds", [])) or 1):
                failed.append(item)
    lines.extend(f"- {item['case']}: {item['error']}" for item in failed[:100])
    if not failed:
        lines.append("Nenhuma.")
    report = output_dir / "relatorio-agente.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
