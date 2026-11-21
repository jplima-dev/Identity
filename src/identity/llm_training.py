"""Run Yasmin's own LoRA trainer after each Identity agent round."""

from __future__ import annotations

import http.client
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .progress import ProgressDisplay
from .training_lock import training_lock as _training_lock

MODEL_BYTES = 1_600_000_000
ENV_BYTES = 3_000_000_000
RESERVE_BYTES = 1_000_000_000


def _existing_parent(path: Path) -> Path:
    current = path.resolve()
    while not current.exists():
        current = current.parent
    return current


def _volume(path: Path) -> str:
    return str(_existing_parent(path).anchor).casefold()


def training_paths(project: Path) -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[2]
    venv = Path(os.environ.get("IDENTITY_LLM_VENV", str(root / ".llm-venv" / project.name)))
    cache = Path(os.environ.get("IDENTITY_LLM_CACHE_DIR", str(root / ".llm-cache")))
    ollama = Path(os.environ.get("YASM_OLLAMA_MODELS", str(project / ".ollama-models")))
    return venv, cache, ollama


def preflight_space(project: Path, venv: Path, cache: Path, ollama: Path,
                    *, model_bytes: int = MODEL_BYTES) -> dict[str, Any]:
    """Budget weights, merged weights, Ollama import, dependencies and free reserve."""
    python = venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"
    # The publisher reloads and saves the merged model in float32, roughly twice
    # the size of the source Safetensors file, then Ollama imports another copy.
    destinations = [(project / "Agent" / "modelos" / "treino_llm", model_bytes * 2, "pesos mesclados"),
                    (ollama, model_bytes * 2, "importação no Ollama"),
                    (cache, model_bytes, "Safetensors da base"),
                    (venv, 0 if python.is_file() else ENV_BYTES, "dependências Python")]
    demands: dict[str, int] = {}
    labels: dict[str, list[str]] = {}
    for path, amount, label in destinations:
        volume = _volume(path)
        demands[volume] = demands.get(volume, 0) + amount
        labels.setdefault(volume, []).append(label)
    checks = []
    for volume, amount in demands.items():
        free = shutil.disk_usage(volume).free
        required = amount + RESERVE_BYTES
        checks.append({"volume": volume, "free_bytes": free, "required_bytes": required,
                       "items": labels[volume], "ok": free >= required})
    return {"ok": all(check["ok"] for check in checks), "volumes": checks}


def _run(command: list[str], cwd: Path, log: Path, timeout: int,
         extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_ACTIVE", "__PYVENV_LAUNCHER__"):
        environment.pop(name, None)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.update(extra_env or {})
    try:
        result = subprocess.run(command, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                                capture_output=True, timeout=timeout, check=False, env=environment)
    except subprocess.TimeoutExpired as exc:
        def decoded(value: bytes | str | None) -> str:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value or ""

        result = subprocess.CompletedProcess(
            command, 124, decoded(exc.stdout),
            decoded(exc.stderr) + f"\nTempo limite de {timeout}s excedido: {exc}",
        )
    except OSError as exc:
        result = subprocess.CompletedProcess(command, 124, "", str(exc))
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    return result


def _ollama_active_error(project: Path, model: str | None) -> str | None:
    """Catch a stale Ollama server before spending CPU on a new adapter."""
    if not model or os.environ.get("YASM_OLLAMA_URL", "http://127.0.0.1:11435").rstrip("/") != "http://127.0.0.1:11435":
        return None
    connection = http.client.HTTPConnection("127.0.0.1", 11435, timeout=5)
    try:
        connection.request("GET", "/api/tags")
        response = connection.getresponse()
        if response.status != 200:
            return f"Ollama local respondeu HTTP {response.status}; verifique o servidor da Yasmin."
        payload = json.loads(response.read(1_000_000))
        names = {entry.get("name") for entry in payload.get("models", []) if isinstance(entry, dict)}
    except (OSError, ValueError, TypeError) as exc:
        return f"Ollama local indisponível para validar o modelo ativo: {exc}"
    finally:
        connection.close()
    if model not in names:
        return (f"Ollama da porta 11435 não oferece o modelo ativo {model}. "
                f"Confirme que o servidor usa OLLAMA_MODELS={project / '.ollama-models'}.")
    return None


def _base_python(project: Path, output: Path) -> tuple[Path | None, tuple[int, int] | None, str | None]:
    app_python = (project / ".venv" / "Scripts" / "python.exe" if os.name == "nt"
                  else project / ".venv" / "bin" / "python")
    if not app_python.is_file():
        return None, None, f"Python da Yasmin não encontrado: {app_python}"
    probe = _run([str(app_python), "-I", "-c",
                  "import json,sys; print(json.dumps({'base':sys._base_executable,'version':sys.version_info[:2]}))"],
                 project, output / "ambiente-python-base.log", 30)
    if probe.returncode:
        return None, None, f"Python da Yasmin não iniciou: {probe.stderr[-500:]}"
    try:
        info = json.loads(probe.stdout.strip())
        executable = Path(info["base"])
        version = tuple(info["version"])
    except (ValueError, KeyError, TypeError):
        return None, None, "Não foi possível identificar o Python base da Yasmin."
    if not executable.is_file() or len(version) != 2 or not all(type(part) is int for part in version):
        return None, None, "Python base da Yasmin inválido."
    return executable, version, None


def _check_environment(python: Path, version: tuple[int, int], project: Path,
                       output: Path) -> str | None:
    code = ("import json,sys,venv; print(json.dumps({'version':sys.version_info[:2],"
            "'stdlib':venv.__file__,'prefix':sys.prefix,'base_prefix':sys.base_prefix}))")
    checked = _run([str(python), "-I", "-c", code], project, output / "ambiente-versao.log", 30)
    if checked.returncode:
        return f"Ambiente criado não inicia: {checked.stderr[-500:]}"
    try:
        info = json.loads(checked.stdout.strip())
    except (ValueError, TypeError):
        return "Ambiente criado não informou sua versão."
    expected = python.parent.parent.resolve()
    if (tuple(info.get("version", ())) != version or Path(info.get("prefix", "")).resolve() != expected
            or not Path(info.get("stdlib", "")).is_file()
            or f"{version[0]}.{version[1]}" not in str(info.get("stdlib", ""))):
        return "Ambiente Python usa versão, prefixo ou biblioteca padrão incompatível."
    return None


def _active(path: Path) -> tuple[bytes | None, dict[str, Any] | None]:
    content = path.read_bytes() if path.is_file() else None
    try:
        parsed = json.loads(content) if content else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return content, parsed if isinstance(parsed, dict) else None


def _reviewed_count(path: Path) -> int:
    if not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(item, dict) and item.get("capability") == "conversa"
                and item.get("reviewed") is True and isinstance(item.get("messages"), list)
                and len(item["messages"]) == 2):
            count += 1
    return count


def _restore_active(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        temporary = path.with_name(path.name + ".identity-restore")
        temporary.write_bytes(previous)
        temporary.replace(path)


def prepare_environment(project: Path, venv: Path, cache: Path, output: Path) -> tuple[Path | None, str | None]:
    """Install once in a separate venv; leave Yasmin's application venv untouched."""
    python = venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"
    requirements = project / "Agent" / "requirements-treino-llm.txt"
    if not requirements.is_file():
        return None, f"Dependências de treino ausentes: {requirements}"
    base, version, error = _base_python(project, output)
    if error or base is None or version is None:
        return None, error
    if not python.is_file():
        venv.parent.mkdir(parents=True, exist_ok=True)
        created = _run([str(base), "-I", "-m", "venv", str(venv)],
                       project, output / "ambiente-venv.log", 300)
        if created.returncode or not python.is_file():
            return None, f"Falha ao criar ambiente Python separado: {created.stderr[-500:]}"
    incompatible = _check_environment(python, version, project, output)
    if incompatible:
        return None, incompatible
    check = _run([str(python), "-c", "import torch, transformers, peft, safetensors"],
                 project, output / "ambiente-verificacao.log", 120)
    if check.returncode:
        cache.mkdir(parents=True, exist_ok=True)
        installed = _run([str(python), "-m", "pip", "install", "--no-cache-dir", "-r", str(requirements)],
                         project, output / "ambiente-instalacao.log", 1800)
        if installed.returncode:
            return None, f"Instalação das dependências falhou; consulte ambiente-instalacao.log: {installed.stderr[-500:]}"
        check = _run([str(python), "-c", "import torch, transformers, peft, safetensors"],
                     project, output / "ambiente-verificacao.log", 120)
        if check.returncode:
            return None, f"Dependências instaladas, mas importação falhou: {check.stderr[-500:]}"
    converter = Path(__file__).resolve().parents[2] / ".llm-tools" / "llama.cpp" / "convert_hf_to_gguf.py"
    if not converter.is_file():
        converter.parent.parent.mkdir(parents=True, exist_ok=True)
        cloned = _run(["git", "clone", "--depth", "1", "--filter=blob:none",
                       "https://github.com/ggml-org/llama.cpp.git", str(converter.parent)],
                      project, output / "ambiente-conversor-clone.log", 600)
        if cloned.returncode or not converter.is_file():
            return None, f"Conversor GGUF indisponível; consulte ambiente-conversor-clone.log: {cloned.stderr[-500:]}"
    conversion_check = _run([str(python), "-c", "import gguf, sentencepiece, google.protobuf"],
                            project, output / "ambiente-conversor-verificacao.log", 120)
    if conversion_check.returncode:
        conversion_install = _run([str(python), "-m", "pip", "install", "--no-cache-dir",
                                   str(converter.parent / "gguf-py"), "sentencepiece", "protobuf"],
                                  project, output / "ambiente-conversor-instalacao.log", 1800)
        if conversion_install.returncode:
            return None, ("Dependências do conversor GGUF falharam; consulte "
                          f"ambiente-conversor-instalacao.log: {conversion_install.stderr[-500:]}")
        conversion_check = _run([str(python), "-c", "import gguf, sentencepiece, google.protobuf"],
                                project, output / "ambiente-conversor-verificacao.log", 120)
        if conversion_check.returncode:
            return None, f"Conversor GGUF instalado, mas importação falhou: {conversion_check.stderr[-500:]}"
    return python, None


def train_llm_round(project: Path, conversations: Path, output: Path, round_number: int,
                    progress: ProgressDisplay | None = None) -> dict[str, Any]:
    """Serialize training for a project before touching its active model."""
    output.mkdir(parents=True, exist_ok=True)
    with _training_lock(conversations.parent / ".llm-training.lock") as acquired:
        if not acquired:
            _, before = _active(project / "Agent" / "modelos" / "llm_ativa.json")
            result = {"status": "bloqueado", "round": round_number,
                      "reason": "Outro treino da LLM deste projeto está em execução; aguarde antes de iniciar outra campanha.",
                      "pairs_reviewed": _reviewed_count(conversations),
                      "model_before": before.get("modelo") if before else None,
                      "optimization_steps": 0}
            (output / f"llm-rodada-{round_number}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
        return _train_llm_round_unlocked(project, conversations, output, round_number, progress)


def _train_llm_round_unlocked(project: Path, conversations: Path, output: Path, round_number: int,
                              progress: ProgressDisplay | None = None) -> dict[str, Any]:
    """Dry-run, optimize, evaluate and confirm Yasmin published a new adapter."""
    output.mkdir(parents=True, exist_ok=True)
    venv, cache, ollama = training_paths(project)
    active_path = project / "Agent" / "modelos" / "llm_ativa.json"
    before_bytes, before = _active(active_path)
    pairs_reviewed = _reviewed_count(conversations)
    disk = preflight_space(project, venv, cache, ollama)
    if not disk["ok"]:
        shortages = [check for check in disk["volumes"] if not check["ok"]]
        details = "; ".join(f"{check['volume']} {check['free_bytes'] / 1e9:.2f} GB livres, "
                            f"{check['required_bytes'] / 1e9:.2f} GB necessários" for check in shortages)
        result = {"status": "bloqueado", "reason": f"Espaço insuficiente: {details}.",
                  "disk": disk, "round": round_number, "pairs_reviewed": pairs_reviewed,
                  "model_before": before.get("modelo") if before else None, "optimization_steps": 0}
        (output / f"llm-rodada-{round_number}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    ollama_error = _ollama_active_error(project, before.get("modelo") if before else None)
    if ollama_error:
        result = {"status": "bloqueado", "reason": ollama_error, "disk": disk,
                  "round": round_number, "pairs_reviewed": pairs_reviewed,
                  "model_before": before.get("modelo") if before else None, "optimization_steps": 0}
        (output / f"llm-rodada-{round_number}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    if progress:
        progress.set_stage(f"Preparando ambiente da LLM · rodada {round_number}")
    python, error = prepare_environment(project, venv, cache, output)
    if error or python is None:
        result = {"status": "bloqueado", "reason": error, "disk": disk,
                  "round": round_number, "pairs_reviewed": pairs_reviewed,
                  "model_before": before.get("modelo") if before else None, "optimization_steps": 0}
        (output / f"llm-rodada-{round_number}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    command = [str(python), "-m", "treino_llm", "--identity-conversas", str(conversations),
               "--cache-dir", str(cache)]
    agent = project / "Agent"
    if progress:
        progress.set_stage(f"Validando pares da LLM · rodada {round_number}")
    dry = _run([*command, "--dry-run"], agent, output / f"llm-rodada-{round_number}-dry.log", 300)
    if dry.returncode:
        result = {"status": "bloqueado", "reason": f"Dry-run falhou: {dry.stderr[-500:]}",
                  "round": round_number, "pairs_reviewed": pairs_reviewed,
                  "model_before": before.get("modelo") if before else None, "optimization_steps": 0}
    else:
        try:
            counts = next(json.loads(line) for line in dry.stdout.splitlines() if line.startswith("{"))
        except (StopIteration, json.JSONDecodeError):
            counts = {}
        if (counts.get("treino_revisado", 0) < 20 or counts.get("avaliacao_reservada", 0) < 3
                or counts.get("novos_desde_publicacao", 0) < 5):
            result = {"status": "bloqueado", "reason": "Dry-run não confirmou 20 pares de treino, 3 reservados e 5 novos.",
                      "round": round_number, "pairs_reviewed": pairs_reviewed,
                      "model_before": before.get("modelo") if before else None,
                      "pairs_train": counts.get("treino_revisado"),
                      "pairs_eval": counts.get("avaliacao_reservada"),
                      "new_pairs": counts.get("novos_desde_publicacao"), "optimization_steps": 0}
            (output / f"llm-rodada-{round_number}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
        weights_root = project / "Agent" / "modelos" / "treino_llm"
        existing_adapters = set(weights_root.glob("*/adaptador/adapter_model.safetensors"))
        converter = Path(__file__).resolve().parents[2] / ".llm-tools" / "llama.cpp" / "convert_hf_to_gguf.py"
        # The trainer starts with the new reviewed pairs and then replays old
        # pairs. Cover every new training pair once, with a small replay budget.
        step_budget = max(16, counts["novos_desde_publicacao"] + 8)
        if progress:
            progress.set_stage(f"Treinando pesos LoRA · rodada {round_number} · até {step_budget} passos")
        training_timeout = max(1800, step_budget * 30)
        trained = _run([*command, "--max-passos", str(step_budget)], agent,
                       output / f"llm-rodada-{round_number}-treino.log", training_timeout,
                       {"IDENTITY_GGUF_CONVERTER": str(converter)})
        new_adapters = set(weights_root.glob("*/adaptador/adapter_model.safetensors")) - existing_adapters
        candidate_adapter = max(new_adapters, key=lambda path: path.stat().st_mtime) if new_adapters else None
        metrics = {}
        for line in trained.stdout.splitlines():
            if line.startswith("{"):
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and "passos" in candidate:
                    metrics = candidate
        if (not metrics and trained.returncode == 124 and "Tempo limite" in trained.stderr
                and len(new_adapters) == 1 and candidate_adapter):
            metrics_path = candidate_adapter.parent.parent / "metricas.json"
            try:
                recovered = json.loads(metrics_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                recovered = None
            if (isinstance(recovered, dict) and recovered.get("treino") == counts.get("treino_revisado")
                    and recovered.get("avaliacao") == counts.get("avaliacao_reservada")
                    and type(recovered.get("passos")) is int
                    and 0 < recovered["passos"] <= step_budget):
                metrics = recovered
        after_bytes, after = _active(active_path)
        steps = metrics.get("passos")
        loss_before = metrics.get("perda_anterior")
        loss_candidate = metrics.get("perda_candidata")
        improved = (type(loss_before) in (float, int) and type(loss_candidate) in (float, int)
                    and math.isfinite(loss_before) and math.isfinite(loss_candidate)
                    and loss_before > 0
                    and loss_candidate < loss_before - max(1e-5, loss_before * 0.01))
        adapter = Path(after["adaptador"]) if after and isinstance(after.get("adaptador"), str) else None
        published = (trained.returncode == 0 and type(steps) is int and steps > 0
                     and metrics.get("melhorou") is True and improved and after_bytes != before_bytes
                     and adapter is not None and (adapter / "adapter_model.safetensors").is_file()
                     and isinstance(after.get("modelo"), str) and after["modelo"].startswith("yasmin-treino:")
                     and "Pesos LoRA publicados e ativados:" in trained.stdout)
        if not published and after_bytes != before_bytes:
            _restore_active(active_path, before_bytes)
        status = "publicado" if published else "rejeitado" if metrics.get("melhorou") is False else "falhou"
        last_line = trained.stdout.splitlines()[-1] if trained.stdout.splitlines() else "Treino não publicou o candidato."
        if published:
            reason = None
        elif metrics.get("melhorou") is False and type(loss_before) in (float, int) and type(loss_candidate) in (float, int):
            reason = (f"Perda reservada não melhorou ({loss_before:.6f} -> {loss_candidate:.6f}); "
                      "modelo ativo preservado.")
        elif trained.returncode == 124 and metrics:
            reason = (f"{steps} passos e avaliação concluídos, mas a publicação excedeu o limite de tempo; "
                      "modelo ativo preservado.")
        else:
            errors = [line.strip() for line in trained.stderr.splitlines() if line.strip()]
            reason = errors[-1] if errors else last_line
        result = {"status": status, "round": round_number,
                  "pairs_reviewed": pairs_reviewed,
                  "pairs_train": counts.get("treino_revisado"), "pairs_eval": counts.get("avaliacao_reservada"),
                  "new_pairs": counts.get("novos_desde_publicacao"), "optimization_steps": steps or 0,
                  "optimization_steps_target": step_budget,
                  "training_timeout_seconds": training_timeout,
                  "loss_before": loss_before, "loss_candidate": loss_candidate,
                  "adapter": str(adapter / "adapter_model.safetensors") if published and adapter else None,
                  "candidate_adapter": str(candidate_adapter) if candidate_adapter else None,
                  "model_before": before.get("modelo") if before else None,
                  "model_published": after.get("modelo") if published and after else None,
                  "reason": None if published else reason}
    (output / f"llm-rodada-{round_number}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
