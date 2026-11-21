"""Persist project scans for the short `identity NAME` command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def identity_root(cwd: Path, package_file: Path) -> Path:
    embedded = cwd / "Identity"
    if (embedded / "identity.json").is_file():
        return embedded
    if (cwd / "identity.json").is_file():
        return cwd
    return package_file.resolve().parents[2]


def project_folder(root: Path, name: str) -> Path:
    if (not name or name in {".", ".."} or name.casefold() in {"config", ".venv", "src", "tests", "app", ".git"}
            or Path(name).name != name or "/" in name or "\\" in name):
        raise ValueError("Nome de projeto inválido.")
    folder = (root / "projects" / name).resolve()
    if folder.parent != (root / "projects").resolve() or not folder.is_relative_to(root.resolve()):
        raise ValueError("O projeto salvo deve ficar dentro do Identity.")
    return folder


def _scan_path(root: Path, name: str) -> Path:
    return project_folder(root, name) / "scan.json"


def _legacy_scan_path(root: Path, name: str) -> Path:
    project_folder(root, name)
    return root / name / "scan.json"


def saved_names(root: Path) -> list[str]:
    names = {path.parent.name for path in (root / "projects").glob("*/scan.json")}
    names.update(path.parent.name for path in root.glob("*/scan.json") if path.parent.name != "projects")
    return sorted(names)


def save_project(root: Path, name: str, scan: dict[str, Any]) -> Path:
    path = _scan_path(root, name)
    for existing in (path, _legacy_scan_path(root, name)):
        if existing.is_file():
            previous = json.loads(existing.read_text(encoding="utf-8"))
            if (not isinstance(previous, dict) or not isinstance(previous.get("project"), str)
                    or Path(previous["project"]).resolve() != Path(scan["project"]).resolve()):
                raise ValueError(f"O nome {name!r} já está ligado a outro projeto.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(scan, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def load_project(root: Path, name: str) -> dict[str, Any]:
    path = _scan_path(root, name)
    if not path.is_file():
        path = _legacy_scan_path(root, name)
        if not path.is_file():
            raise ValueError(f"Projeto {name!r} não salvo. Use identity scam --project CAMINHO.")
    scan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(scan, dict) or scan.get("mode") not in {"api", "agente"}:
        raise ValueError(f"Scan inválido para {name!r}. Rode identity scam novamente.")
    if not isinstance(scan.get("project"), str):
        raise TypeError(f"Caminho salvo inválido para {name!r}. Rode identity scam novamente.")
    project = Path(scan["project"]).resolve()
    if not project.is_dir() or project.name.casefold() != name.casefold():
        raise ValueError(f"Projeto salvo indisponível: {project}. Rode identity scam novamente.")
    return scan
