"""Train Yasmin's local intent model with conversation examples while preserving corpus size."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--dados", type=Path, required=True)
    parser.add_argument("--saida", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.project / "Agent"))
    from agente.modelo_treinavel import ModeloIntencoesLocal
    from treinar_agente import avaliar, exemplos_base, exemplos_usuario

    base = exemplos_base()
    extras = exemplos_usuario(args.dados)
    seen = {text for text, _ in extras}
    if len(seen) != len(extras):
        parser.error("Exemplos adicionais duplicados.")
    remaining = [(text, label) for text, label in base if text not in seen]
    replaceable = [i for i, (_, label) in enumerate(remaining) if label == "nenhuma"]
    if len(extras) > len(replaceable):
        parser.error("Há mais exemplos de conversa que exemplos substituíveis no corpus.")
    for index in reversed(replaceable[: len(extras)]):
        remaining.pop(index)
    model = ModeloIntencoesLocal()
    model.treinar(remaining + extras)
    correct, errors = avaliar(model)
    print(json.dumps({"exemplos_base": len(remaining), "exemplos_conversa": len(extras), "avaliacao_acertos": correct, "avaliacao_total": correct + len(errors)}, ensure_ascii=False))
    if correct / (correct + len(errors)) < 0.85 or any(expected == "nenhuma" and predicted != "nenhuma" for _, expected, predicted in errors):
        print(f"Modelo não publicado: {errors}")
        return 1
    model.salvar(args.saida)
    print(f"Modelo publicado: {args.saida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
