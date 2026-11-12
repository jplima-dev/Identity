"""Exercise a live conversational agent and prepare labeled training examples."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any

import httpx

from .progress import ProgressDisplay

SAFE_CAPABILITIES = (
    "CHAMADA_CURTA", "HORA", "DATA", "conversa", "listar_aplicacoes_ink",
    "consultar_estado_aplicacao_ink", "informacoes_sistema", "listar_processos",
    "listar_janelas", "janela_ativa",
)
COMPLEX_CAPABILITIES = (
    "analisar_projeto_ink", "listar_arquivos_projeto_ink", "descrever_projeto",
    "listar_cadernos_ink", "ler_arquivo", "listar_pasta", "localizar_arquivos",
    "pesquisar_conteudo", "listar_temporizadores", "listar_lembretes",
    "listar_notas", "ler_nota",
)
FALLBACK = "não consegui obter uma resposta do mecanismo de conversa"


def response_passed(label: str, answer: str, expected: str) -> bool:
    normalized = answer.casefold().strip()
    if not normalized or FALLBACK in normalized or (expected and expected.casefold() not in normalized):
        return False
    if label == "CHAMADA_CURTA":
        return "ouvindo" in normalized
    if label == "HORA":
        return bool(re.search(r"\b\d{1,2}:\d{2}\b", normalized))
    if label == "DATA":
        return bool(re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", normalized))
    return True


def generate_cases(examples: dict[str, list[str]]) -> list[dict[str, str]]:
    groups = []
    for label in (*SAFE_CAPABILITIES, *COMPLEX_CAPABILITIES):
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
        raise ValueError("O scanner não encontrou perguntas de capacidades de leitura ou conversa.")
    return cases


async def run_agent_cases(count: int, cases: list[dict[str, str]], url: str, output: Path, timeout: float, progress: ProgressDisplay | None = None, round_number: int = 1) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    start = asyncio.Event()
    summary = {"agents": count, "passed": 0, "failed": 0, "output": str(output), "outcomes": [False] * count, "round": round_number}

    async def writer() -> None:
        with output.open("w" if round_number == 1 else "a", encoding="utf-8") as stream:
            while (record := await queue.get()) is not None:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                summary["passed" if record["passed"] else "failed"] += 1
                summary["outcomes"][record["agent"] - 1] = record["passed"]
                if progress:
                    progress.advance(record["capacidade"])

    async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=min(count, 20))) as client:
        writer_task = asyncio.create_task(writer())
        slots = asyncio.Semaphore(20)

        async def agent(agent_id: int) -> None:
            case = cases[agent_id - 1]
            await start.wait()
            loop = asyncio.get_running_loop()
            started = loop.time()
            status = None
            answer = ""
            error = None
            async with slots:
                for attempt in range(3):
                    try:
                        response = await client.post(
                            url,
                            headers={"X-Yasmin-Bridge": "workspace-os"},
                            json={"texto": case["texto"]},
                        )
                        status = response.status_code
                        payload = response.json()
                        answer = str(payload.get("resposta", "")) if isinstance(payload, dict) else ""
                        error = None
                        break
                    except (httpx.RequestError, ValueError) as exc:
                        error = type(exc).__name__
                        if attempt < 2:
                            await asyncio.sleep(0.1 * (attempt + 1))
            await queue.put({
                "timestamp": datetime.now(UTC).isoformat(),
                "agent": agent_id,
                "rodada": round_number,
                "texto": case["texto"],
                "capacidade": case["rotulo"],
                "complexidade": case["complexidade"],
                "resposta": answer,
                "esperado": case["contem"],
                "status": status,
                "latency_ms": round((loop.time() - started) * 1000, 2),
                "passed": status == 200 and response_passed(case["rotulo"], answer, case["contem"]),
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


def write_training_examples(cases: list[dict[str, str]], output: Path) -> int:
    unique = {case["texto"] for case in cases if case["rotulo"] == "conversa"}
    with output.open("w", encoding="utf-8") as stream:
        for text in sorted(unique):
            stream.write(json.dumps({"texto": text, "rotulo": "nenhuma"}, ensure_ascii=False) + "\n")
    return len(unique)


def write_agent_report(output_dir: Path, result: dict[str, Any], training: dict[str, Any] | None, examples: int) -> Path:
    by_capability: Counter[str] = Counter()
    lines = [
        f"# Relatório: {output_dir.name}", "", "## Testes de conversa", "",
        f"- Agentes: {result['agents']}",
        f"- Rodadas executadas: {len(result.get('rounds', []))}",
        f"- Testes realizados: {result.get('requests', result['agents'])}",
        f"- Respostas aprovadas: {result['passed']}",
        f"- Falhas na última rodada: {result['failed']}",
        f"- Registros: {Path(result['output']).name}", "",
    ]
    for round_result in result.get("rounds", []):
        lines.append(f"- Rodada {round_result['round']}: {round_result['passed']} aprovadas, {round_result['failed']} falhas")
    lines.extend(["", "### Falhas restantes", ""])
    last_round = len(result.get("rounds", []))
    shown = 0
    with Path(result["output"]).open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            by_capability[item["capacidade"]] += 1
            if item["rodada"] == last_round and not item["passed"] and shown < 100:
                lines.append(f"- Agente {item['agent']}: {item['texto']!r}; esperado {item['esperado']!r}; resposta {item['resposta']!r}; erro {item['error']}")
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
