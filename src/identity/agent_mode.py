"""Test agent capability classification without executing desktop commands."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from time import perf_counter
from typing import Any

from .offline_model import OfflineCapabilityModel
from .progress import ProgressDisplay

COMPLEX_CAPABILITIES = (
    "analisar_projeto_ink", "listar_arquivos_projeto_ink", "descrever_projeto",
    "listar_cadernos_ink", "ler_arquivo", "listar_pasta", "localizar_arquivos",
    "pesquisar_conteudo", "listar_temporizadores", "listar_lembretes",
    "listar_notas", "ler_nota",
)
def generate_cases(examples: dict[str, list[str]]) -> list[dict[str, str]]:
    groups = []
    for label in examples:
        group = []
        for phrase in examples.get(label, []):
            variants = (phrase,) if label == "CHAMADA_CURTA" else (
                phrase, f"yasmin, {phrase}", f"por favor, {phrase}", f"{phrase} por favor",
            )
            for text in variants:
                group.append({"texto": text, "contem": "", "rotulo": label,
                              "complexidade": "complexa" if label in COMPLEX_CAPABILITIES else "basica"})
        if group:
            groups.append(group)
    cases = [case for row in zip_longest(*groups) for case in row if case is not None]
    if not cases:
        raise ValueError("O scanner não encontrou perguntas de capacidades.")
    return cases


async def run_agent_cases(count: int, cases: list[dict[str, str]], model: OfflineCapabilityModel, output: Path, progress: ProgressDisplay | None = None, round_number: int = 1) -> dict[str, Any]:
    if len(cases) != count:
        raise ValueError("Cada agente precisa de um caso de teste.")
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = {"agents": count, "passed": 0, "failed": 0, "output": str(output), "outcomes": [False] * count, "round": round_number}
    with output.open("w" if round_number == 1 else "a", encoding="utf-8") as stream:
        for agent_id, case in enumerate(cases, 1):
            started = perf_counter()
            predicted, confidence = model.predict(case["texto"])
            passed = predicted == case["rotulo"]
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "agent": agent_id,
                "rodada": round_number,
                "texto": case["texto"],
                "capacidade": case["rotulo"],
                "complexidade": case["complexidade"],
                "tipo_teste": "classificacao_local",
                "predicao": predicted,
                "confianca": round(confidence, 6),
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "passed": passed,
                "error": None,
            }
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            summary["passed" if passed else "failed"] += 1
            summary["outcomes"][agent_id - 1] = passed
            if progress:
                progress.advance(case["rotulo"])
    return summary


def write_training_examples(cases: list[dict[str, str]], output: Path) -> int:
    unique = {case["texto"] for case in cases if case["rotulo"] == "conversa"}
    with output.open("w", encoding="utf-8") as stream:
        for text in sorted(unique):
            stream.write(json.dumps({"texto": text, "rotulo": "nenhuma"}, ensure_ascii=False) + "\n")
    return len(unique)


def write_agent_report(output_dir: Path, result: dict[str, Any], training: dict[str, Any] | None, examples: int) -> Path:
    by_capability: Counter[str] = Counter()
    lines = [
        f"# Relatório: {output_dir.name}", "", "## Classificação local de capacidades", "",
        "- Execução: modelo local em memória; nenhum comando enviado à Yasmin ou ao desktop.",
        "- Limite: estes testes avaliam o roteamento da intenção, não a execução nem a resposta das ferramentas.",
        f"- Agentes: {result['agents']}",
        f"- Rodadas executadas: {len(result.get('rounds', []))}",
        f"- Testes realizados: {result.get('requests', result['agents'])}",
        f"- Classificações corretas: {result['passed']}",
        f"- Falhas na última rodada: {result['failed']}",
        f"- Registros: {Path(result['output']).name}", "",
    ]
    for round_result in result.get("rounds", []):
        lines.append(f"- Rodada {round_result['round']}: {round_result['passed']} corretas, {round_result['failed']} falhas")
    lines.extend(["", "### Falhas restantes", ""])
    last_round = len(result.get("rounds", []))
    shown = 0
    with Path(result["output"]).open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            by_capability[item["capacidade"]] += 1
            if item["rodada"] == last_round and not item["passed"] and shown < 100:
                lines.append(f"- Agente {item['agent']}: {item['texto']!r}; esperado {item['capacidade']!r}; previsto {item['predicao']!r} ({item['confianca']:.1%})")
                shown += 1
    if result["failed"] > shown:
        lines.append(f"- Mais {result['failed'] - shown} falhas no JSONL.")
    elif not shown:
        lines.append("Nenhuma falha restante.")
    lines.extend(["", "## Capacidades testadas", "", *(f"- {label}: {total}" for label, total in by_capability.items()), "", "## Treino", "", f"- Exemplos de conversa adicionados: {examples}"])
    if training is not None:
        lines.extend([
            f"- Código de saída do treino: {training['training_exit_code']}",
            f"- Código de saída dos testes: {training['tests_exit_code']}",
            "- Logs: treino.log, testes.log", "", "```text", training["training_log"].strip(), "```", "",
        ])
    report = output_dir / "relatorio.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report
