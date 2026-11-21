"""Project-declared LLM training with reviewed data and reversible activation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .progress import ProgressDisplay
from .training_lock import training_lock


def _inside(project: Path, value: str, field: str) -> Path:
    if not value or Path(value).is_absolute():
        raise ValueError(f"training.{field} deve ser um caminho relativo ao projeto.")
    path = (project / value).resolve()
    if not path.is_relative_to(project.resolve()) or path == project.resolve():
        raise ValueError(f"training.{field} deve ficar dentro do projeto.")
    return path


def validate_llm_training(project: Path, training: dict[str, Any], cases: list[dict[str, Any]]) -> None:
    if not any(case["split"] == "eval" and case.get("origin") != "generated" for case in cases):
        raise ValueError("Treino de LLM exige casos eval manuais e independentes.")
    for field in ("model_path", "dataset_path"):
        if not isinstance(training.get(field), str):
            raise TypeError(f"training.{field} é obrigatório.")
        _inside(project, training[field], field)
    for field, required in (
        ("command", ("{train_data}", "{candidate_model}")),
        ("evaluate_command", ("{model}", "{eval_data}")),
        ("smoke_command", ("{model}",)),
    ):
        command = training.get(field)
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise ValueError(f"training.{field} deve ser uma lista de argumentos.")
        if any(not any(token in part for part in command) for token in required):
            raise ValueError(f"training.{field} deve usar {', '.join(required)}.")
    reload_command = training.get("reload_command")
    if reload_command is not None and (not isinstance(reload_command, list) or not reload_command
                                       or not all(isinstance(part, str) for part in reload_command)):
        raise ValueError("training.reload_command deve ser uma lista de argumentos.")
    if training.get("metric", "loss") not in {"loss", "score"}:
        raise ValueError("training.metric deve ser loss ou score.")
    improvement = training.get("min_improvement", 0)
    if type(improvement) not in (int, float) or not math.isfinite(improvement) or improvement < 0:
        raise ValueError("training.min_improvement deve ser um número não negativo.")
    timeout = training.get("timeout_seconds", 3600)
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError("training.timeout_seconds deve estar entre 1 e 86400.")


def _prompt_key(messages: list[dict[str, str]]) -> str:
    return json.dumps([item["content"] for item in messages if item["role"] == "user"],
                      ensure_ascii=False, sort_keys=True)


def _reviewed_pairs(source: Path, eval_cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not source.is_file():
        raise ValueError(f"Dados revisados não encontrados: {source}")
    held_out = {_prompt_key(case["messages"]) for case in eval_cases if case["split"] == "eval"}
    pairs: list[dict[str, Any]] = []
    seen: set[str] = set()
    with source.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON inválido na linha {number} de {source.name}.") from exc
            if not isinstance(item, dict) or item.get("reviewed") is not True:
                continue
            messages = item.get("messages")
            if (not isinstance(messages, list) or len(messages) < 2
                    or not all(isinstance(message, dict) and message.get("role") in {"system", "user", "assistant"}
                               and isinstance(message.get("content"), str) and message["content"].strip()
                               for message in messages)
                    or messages[-1]["role"] != "assistant"
                    or not any(message["role"] == "user" for message in messages)):
                raise ValueError(f"Par revisado inválido na linha {number} de {source.name}.")
            key = _prompt_key(messages)
            if key in held_out:
                raise ValueError(f"Dados de treino e avaliação compartilham a pergunta da linha {number}.")
            if key not in seen:
                seen.add(key)
                pairs.append(item)
    if not pairs:
        raise ValueError("Nenhum par revisado com resposta de referência encontrado para treinar a LLM.")
    return pairs


def _run(parts: list[str], variables: dict[str, str], project: Path, timeout: int,
         log: Path) -> subprocess.CompletedProcess[str]:
    try:
        command = [part.format_map(variables) for part in parts]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Placeholder inválido no comando: {exc}") from exc
    try:
        process = subprocess.run(command, cwd=project, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        process = subprocess.CompletedProcess(command, 124, "", f"Tempo limite de {timeout}s excedido.")
    except OSError as exc:
        process = subprocess.CompletedProcess(command, 127, "", str(exc))
    log.write_text(process.stdout + process.stderr, encoding="utf-8")
    return process


def _last_json(process: subprocess.CompletedProcess[str], field: str) -> dict[str, Any]:
    try:
        value = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError(f"O comando deve imprimir JSON com {field} na última linha.") from exc
    if not isinstance(value, dict):
        raise TypeError(f"O comando deve imprimir JSON com {field}.")
    return value


def _metric(process: subprocess.CompletedProcess[str], name: str) -> float:
    value = _last_json(process, name).get(name)
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"A avaliação deve imprimir {name} finito em JSON.")
    return float(value)


def _artifact_ready(candidate: Path) -> bool:
    if candidate.is_file():
        return candidate.stat().st_size > 0
    return candidate.is_dir() and any(file.is_file() and file.stat().st_size > 0
                                      for file in candidate.rglob("*"))


def _artifact_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())


def _artifact_digest(path: Path) -> str:
    digest = hashlib.sha256()
    files = [path] if path.is_file() else sorted(file for file in path.rglob("*") if file.is_file())
    for file in files:
        if path.is_dir():
            digest.update(str(file.relative_to(path)).encode("utf-8"))
        with file.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def train_llm_adapter_round(project: Path, adapter: dict[str, Any], output_dir: Path,
                            round_number: int, progress: ProgressDisplay | None = None) -> dict[str, Any]:
    """Serialize training before evaluating or replacing the active model."""
    training = adapter["training"]
    validate_llm_training(project, training, adapter["cases"])
    model = _inside(project, training["model_path"], "model_path")
    with training_lock(model.parent / ".identity-training.lock") as acquired:
        if not acquired:
            result = {"round": round_number, "status": "bloqueado", "optimization_steps": 0,
                      "reason": "Outro treino deste modelo está em execução.",
                      "pairs_reviewed": 0, "metric_before": None, "metric_candidate": None,
                      "candidate_model": None, "model_published": None}
            round_dir = output_dir / f"llm-rodada-{round_number}"
            round_dir.mkdir(parents=True, exist_ok=True)
            (round_dir / "resultado.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                        encoding="utf-8")
            return result
        return _train_llm_adapter_unlocked(project, adapter, output_dir, round_number, progress)


def _train_llm_adapter_unlocked(project: Path, adapter: dict[str, Any], output_dir: Path,
                                round_number: int, progress: ProgressDisplay | None) -> dict[str, Any]:
    training = adapter["training"]
    round_dir = output_dir / f"llm-rodada-{round_number}"
    round_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"round": round_number, "status": "falhou", "optimization_steps": 0,
                              "pairs_reviewed": 0, "metric_before": None, "metric_candidate": None,
                              "candidate_model": None, "model_published": None}
    try:
        pairs = _reviewed_pairs(_inside(project, training["dataset_path"], "dataset_path"), adapter["cases"])
        result["pairs_reviewed"] = len(pairs)
        train_data = round_dir / "conversas_treino.jsonl"
        train_data.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in pairs),
                              encoding="utf-8")
        eval_data = round_dir / "conversas_avaliacao.jsonl"
        eval_cases = [case for case in adapter["cases"] if case["split"] == "eval"
                      and case.get("origin") != "generated"]
        eval_data.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in eval_cases),
                             encoding="utf-8")
        model = _inside(project, training["model_path"], "model_path")
        if not _artifact_ready(model):
            raise ValueError(f"Modelo ativo não encontrado ou vazio: {model}")
        candidate = round_dir / ("candidato" + model.suffix if model.is_file() else "candidato")
        project_python = (project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        values = {"python": sys.executable, "project_python": str(project_python if project_python.is_file()
                  else Path(sys.executable)), "project": str(project), "round": str(round_number),
                  "train_data": str(train_data), "eval_data": str(eval_data),
                  "candidate_model": str(candidate), "model": str(model)}
        timeout = training.get("timeout_seconds", 3600)
        metric_name = training.get("metric", "loss")
        if progress:
            progress.set_stage(f"Avaliando modelo atual · rodada {round_number}")
        before = _run(training["evaluate_command"], values, project, timeout, round_dir / "avaliacao-anterior.log")
        if before.returncode:
            raise ValueError(f"Avaliação do modelo atual falhou: {before.stderr.strip()}")
        result["metric_before"] = _metric(before, metric_name)
        if progress:
            progress.set_stage(f"Treinando LLM · rodada {round_number}")
        trained = _run(training["command"], values, project, timeout, round_dir / "treino.log")
        if trained.returncode:
            raise ValueError(f"Treino falhou: {trained.stderr.strip()}")
        steps = _last_json(trained, "optimization_steps").get("optimization_steps")
        if type(steps) is not int or steps <= 0:
            raise ValueError("Treinador não confirmou passos reais de otimização.")
        result["optimization_steps"] = steps
        if not _artifact_ready(candidate) or candidate.is_dir() != model.is_dir():
            raise ValueError("Treinador não produziu um modelo candidato válido do mesmo tipo.")
        result["candidate_model"] = str(candidate)
        if _artifact_digest(candidate) == _artifact_digest(model):
            result["status"] = "rejeitado"
            result["reason"] = "O candidato tem os mesmos pesos do modelo ativo; treino não confirmado."
            return result
        if progress:
            progress.set_stage(f"Avaliando candidato · rodada {round_number}")
        candidate_values = {**values, "model": str(candidate)}
        after = _run(training["evaluate_command"], candidate_values, project, timeout,
                     round_dir / "avaliacao-candidata.log")
        if after.returncode:
            raise ValueError(f"Avaliação candidata falhou: {after.stderr.strip()}")
        result["metric_candidate"] = _metric(after, metric_name)
        old, new = result["metric_before"], result["metric_candidate"]
        margin = training.get("min_improvement", 0)
        improved = new < old - margin if metric_name == "loss" else new > old + margin
        if not improved:
            result["status"] = "rejeitado"
            result["reason"] = f"{metric_name} não melhorou ({old:.6f} -> {new:.6f}); modelo ativo preservado."
            return result
        # Staging needs a second copy of the candidate on the model's volume.
        required = _artifact_bytes(candidate) + 64 * 1024 * 1024
        available = shutil.disk_usage(model.parent).free
        if available < required:
            result["status"] = "bloqueado"
            result["reason"] = (f"Espaço insuficiente para ativar pesos: {available} bytes livres, "
                                f"{required} necessários; modelo ativo preservado.")
            return result
        # Stage on the same volume, then atomically replace the active path.
        staged = model.with_name(model.name + ".identity-new")
        backup = model.with_name(model.name + f".identity-backup-{output_dir.name}-{round_number}")
        if staged.exists() or backup.exists():
            raise ValueError("Já existe um arquivo temporário ou backup desta rodada.")
        try:
            if model.is_dir():
                shutil.copytree(candidate, staged)
            else:
                shutil.copy2(candidate, staged)
            os.replace(model, backup)
        except OSError:
            if staged.is_dir():
                shutil.rmtree(staged)
            elif staged.exists():
                staged.unlink()
            raise
        try:
            os.replace(staged, model)
            if training.get("reload_command"):
                reloaded = _run(training["reload_command"], values, project, timeout, round_dir / "reload.log")
                if reloaded.returncode:
                    raise ValueError(f"Recarregamento falhou: {reloaded.stderr.strip()}")
            if progress:
                progress.set_stage(f"Testando modelo ativado · rodada {round_number}")
            smoke = _run(training["smoke_command"], values, project, timeout, round_dir / "smoke.log")
            if smoke.returncode or _last_json(smoke, "ok").get("ok") is not True:
                raise ValueError(f"Modelo ativado não passou no teste: {smoke.stderr.strip()}")
        except (OSError, TypeError, ValueError) as exc:
            try:
                if model.exists():
                    os.replace(model, staged)
                os.replace(backup, model)
            except OSError as restore_error:
                raise ValueError(
                    f"Falha ao restaurar modelo ativo: {restore_error}; backup em {backup}"
                ) from restore_error
            if staged.exists():
                if staged.is_dir():
                    shutil.rmtree(staged)
                else:
                    staged.unlink()
            if training.get("reload_command"):
                reloaded = _run(training["reload_command"], values, project, timeout,
                                round_dir / "reload-rollback.log")
                if reloaded.returncode:
                    raise ValueError(
                        f"Pesos restaurados, mas recarregamento falhou: {reloaded.stderr.strip()}"
                    ) from exc
            raise ValueError(f"Ativação revertida: {exc}") from exc
        result.update(status="publicado", model_published=str(model), backup=str(backup))
    except (OSError, TypeError, ValueError) as exc:
        result["reason"] = str(exc)
        if result["optimization_steps"] == 0 and (
                "Dados revisados" in str(exc) or "Nenhum par revisado" in str(exc)):
            result["status"] = "bloqueado"
    finally:
        (round_dir / "resultado.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    return result
