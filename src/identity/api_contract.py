"""Per-project API contract and managed test runtime."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


def _project_file(project: Path, value: str, label: str) -> Path:
    path = (project / value).resolve()
    if Path(value).is_absolute() or not path.is_relative_to(project.resolve()) or path == project.resolve():
        raise ValueError(f"{label} deve apontar para um arquivo dentro do projeto.")
    return path


def load_api_contract(project: Path) -> dict[str, Any]:
    path = project / "identity-api.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Não foi possível ler identity-api.json: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("identity-api.json requer version: 1.")
    for key in ("base_url", "health_path"):
        if key in data and not isinstance(data[key], str):
            raise TypeError(f"identity-api.json: {key} deve ser texto.")
    if "base_url" in data:
        url = urlsplit(data["base_url"])
        if url.scheme not in {"http", "https"} or not url.netloc or url.query or url.fragment:
            raise ValueError("identity-api.json: base_url deve ser HTTP(S) sem query.")
    if "health_path" in data and not data["health_path"].startswith("/"):
        raise ValueError("identity-api.json: health_path deve começar com /.")
    for key in ("start_command", "setup_command", "auth_command", "teardown_command", "stop_command"):
        if key in data and (not isinstance(data[key], list) or not data[key] or
                            any(not isinstance(arg, str) for arg in data[key])):
            raise TypeError(f"identity-api.json: {key} deve ser uma lista de argumentos.")
    for key in ("headers", "api_cases", "api_payloads"):
        if key in data and not isinstance(data[key], dict):
            raise TypeError(f"identity-api.json: {key} deve ser um objeto.")
    for key in ("allow_remote_mutations", "isolated_environment"):
        if key in data and type(data[key]) is not bool:
            raise TypeError(f"identity-api.json: {key} deve ser true ou false.")
    if "openapi_path" in data:
        if not isinstance(data["openapi_path"], str):
            raise TypeError("identity-api.json: openapi_path deve ser texto.")
        document = _project_file(project, data["openapi_path"], "openapi_path")
        if not document.is_file():
            raise ValueError(f"Documento OpenAPI não encontrado: {document}")
        try:
            specification = json.loads(document.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"openapi_path não contém JSON válido: {exc}") from exc
        if not isinstance(specification, dict) or not isinstance(specification.get("paths"), dict):
            raise ValueError("openapi_path requer um objeto OpenAPI com paths.")
    ready = data.get("ready_timeout_seconds", 30)
    if type(ready) not in (int, float) or not 0 < ready <= 300:
        raise ValueError("identity-api.json: ready_timeout_seconds deve ficar entre 1 e 300.")
    timeout = data.get("timeout_seconds", 10)
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise ValueError("identity-api.json: timeout_seconds deve ficar entre 1 e 300.")
    for key in ("requests_per_agent", "max_concurrency"):
        if key in data and (type(data[key]) is not int or data[key] < 1 or data[key] > 1000):
            raise ValueError(f"identity-api.json: {key} deve ser inteiro de 1 a 1000.")
    if data.get("isolated_environment") and not (data.get("setup_command") and data.get("teardown_command")):
        raise ValueError("isolated_environment exige setup_command e teardown_command.")
    for route, cases in data.get("api_cases", {}).items():
        if not isinstance(cases, list):
            raise TypeError(f"api_cases[{route}] deve ser uma lista.")
        for case in cases:
            if not isinstance(case, dict):
                raise TypeError(f"api_cases[{route}] deve conter objetos.")
            if route.split(" ", 1)[0].upper() not in {"GET", "HEAD", "OPTIONS"} and not (
                    case.get("cleanup") or data.get("isolated_environment")):
                raise ValueError(f"{route} altera dados; declare cleanup ou isolated_environment com teardown.")
    return data


def _command(project: Path, parts: list[str], label: str, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(parts, cwd=project, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{label} não iniciou ou excedeu o tempo limite: {exc}") from exc
    if process.returncode:
        raise ValueError(f"{label} falhou (código {process.returncode}): {process.stderr[-500:]}")
    return process


def _ready(url: str, path: str, timeout: float, process: subprocess.Popen[bytes] | None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            response = httpx.get(url + path, timeout=2, follow_redirects=False)
            if 200 <= response.status_code < 500:
                return True
        except httpx.RequestError:
            pass
        time.sleep(0.2)
    return False


@dataclass
class ApiRuntimeState:
    headers: dict[str, str] = field(default_factory=dict)
    cleanup_errors: list[str] = field(default_factory=list)


@contextmanager
def api_runtime(project: Path, contract: dict[str, Any], base_url: str,
                output: Path) -> Iterator[ApiRuntimeState]:
    """Start only an owned server, run fixtures, and always attempt cleanup."""
    process: subprocess.Popen[bytes] | None = None
    log = None
    state = ApiRuntimeState()
    setup_attempted = False
    runtime_ready = False
    timeout = float(contract.get("ready_timeout_seconds", 30))
    try:
        health = contract.get("health_path", "/")
        if contract.get("start_command") and not _ready(base_url, health, 1, None):
            output.mkdir(parents=True, exist_ok=True)
            log = (output / "servidor.log").open("wb")
            try:
                process = subprocess.Popen(
                    contract["start_command"], cwd=project, stdout=log,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except OSError as exc:
                raise ValueError(f"Não foi possível iniciar a API: {exc}") from exc
        if not _ready(base_url, health, timeout, process):
            raise ValueError(f"API indisponível em {base_url}{health}; consulte servidor.log se houver.")
        runtime_ready = True
        if contract.get("setup_command"):
            setup_attempted = True
            _command(project, contract["setup_command"], "setup_command", timeout)
        if contract.get("auth_command"):
            response = _command(project, contract["auth_command"], "auth_command", timeout)
            try:
                value = json.loads(response.stdout.strip().splitlines()[-1])
            except (IndexError, json.JSONDecodeError) as exc:
                raise ValueError("auth_command deve imprimir JSON com headers na última linha.") from exc
            if not isinstance(value, dict) or not isinstance(value.get("headers"), dict) or any(
                    not isinstance(key, str) or not isinstance(item, str)
                    for key, item in value["headers"].items()):
                raise TypeError("auth_command deve retornar headers como objeto de texto.")
            state.headers = value["headers"]
        yield state
    finally:
        try:
            if (runtime_ready or setup_attempted) and contract.get("teardown_command"):
                try:
                    _command(project, contract["teardown_command"], "teardown_command", timeout)
                except ValueError as exc:
                    state.cleanup_errors.append(str(exc))
            if process is not None and contract.get("stop_command"):
                try:
                    _command(project, contract["stop_command"], "stop_command", timeout)
                except ValueError as exc:
                    state.cleanup_errors.append(str(exc))
        finally:
            try:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            except OSError as exc:
                state.cleanup_errors.append(f"Falha ao encerrar API iniciada pelo Identity: {exc}")
            finally:
                if log is not None:
                    log.close()
