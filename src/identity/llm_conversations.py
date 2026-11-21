"""Developer conversation pairs whose answers are checked by a separate Python process."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

from .conversation_ai import append_conversation_records


def _exercises(number: int) -> list[tuple[str, str]]:
    return [
        ("Depurando uma divisão inteira, qual é a saída?", f"print(divmod({number + 12}, 3))"),
        ("Qual lista esse fatiamento produz?", f"print(list(range({number + 6}))[1:6:2])"),
        ("O que dict.get retorna quando a chave não existe?", f"print({{'api': {number + 3}}}.get('teste', {number}))"),
        ("Como enumerate numera estes itens?", f"print(list(enumerate(['api', 'teste'], start={number})))"),
        ("Qual é o resultado desta compreensão de lista?",
         f"print([x * x for x in range({number}, {number + 6}) if x % 2 == 0])"),
        ("Após split e join, qual texto é impresso?",
         f"print('|'.join('api{number},teste{number},deploy{number}'.split(',')[1:]))"),
        ("Qual JSON compacto o módulo json produz?",
         f"import json\nprint(json.dumps({{'ok': True, 'count': {number}}}, sort_keys=True, separators=(',', ':')))"),
        ("Quais sequências numéricas a expressão regular encontra?",
         f"import re\nprint(re.findall(r'\\d+', 'api12 rota{number} teste7'))"),
        ("Qual é a lista ordenada após remover duplicatas?",
         f"print(sorted({{{number}, {number + 2}, {number + 1}, {number}}}))"),
        ("O que aparece quando a exceção é tratada?",
         f"try:\n    print({number} // 0)\nexcept ZeroDivisionError:\n    print('divisão por zero tratada')"),
        ("Qual é a contagem mais frequente retornada por Counter?",
         f"from collections import Counter\nprint(Counter('aaab{number}').most_common(1))"),
        ("Que valores islice seleciona do iterador?",
         f"from itertools import islice\nprint(list(islice(range({number + 10}), {number}, {number + 3})))"),
        ("Como urlencode codifica estes parâmetros de consulta?",
         f"from urllib.parse import urlencode\nprint(urlencode({{'q': 'api teste', 'pagina': {number}}}))"),
        ("Qual caminho PurePosixPath compõe?",
         f"from pathlib import PurePosixPath\nprint(PurePosixPath('/srv') / 'app' / 'modulo{number}.py')"),
        ("Qual é a soma decimal exata?",
         f"from decimal import Decimal\nprint(Decimal('{number}.1') + Decimal('0.2'))"),
        ("Como Fraction simplifica a fração?",
         f"from fractions import Fraction\nprint(Fraction({number}, {number + 2}))"),
        ("Qual é o máximo divisor comum calculado por math.gcd?",
         f"import math\nprint(math.gcd({number * 6}, {number * 9}))"),
        ("Qual data resulta desta soma com timedelta?",
         f"from datetime import date, timedelta\nprint(date(2024, 1, 1) + timedelta(days={number}))"),
        ("Após inserir duas linhas em SQLite em memória, qual soma o SELECT retorna?",
         ("import sqlite3\ncon = sqlite3.connect(':memory:')\n"
          "con.execute('create table itens (valor integer)')\n"
          f"con.executemany('insert into itens values (?)', [({number},), ({number + 2},)])\n"
          "print(con.execute('select sum(valor) from itens').fetchone()[0])\ncon.close()")),
        ("Qual valor o leitor CSV encontra na segunda linha?",
         ("import csv, io\n"
          f"rows = list(csv.reader(io.StringIO('nome,valor\\napi,{number}\\n')))\n"
          "print(rows[1][1])")),
    ]


def _checked_output(code: str, python: Path) -> str:
    command = [str(python), "-X", "utf8", "-I", "-c", code]
    try:
        first = subprocess.run(command, capture_output=True, timeout=10, check=False)
        second = subprocess.run(command, capture_output=True, timeout=10, check=False)
        first_stdout = first.stdout.decode("utf-8")
        second_stdout = second.stdout.decode("utf-8")
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Exercício determinístico não pôde ser verificado.") from exc
    if (first.returncode or second.returncode or first.stderr or second.stderr
            or first_stdout != second_stdout or not first_stdout.strip()
            or len(first.stdout) > 500):
        raise ValueError("Exercício determinístico não foi verificado; par descartado.")
    return first_stdout.strip()


def generate_reviewed_pairs(start: int, count: int, python: Path | None = None) -> list[dict[str, Any]]:
    """Use a deterministic execution as independent evidence, never Yasmin's answer."""
    if start < 0 or count < 1:
        raise ValueError("Intervalo de exemplos inválido.")
    interpreter = python or Path(sys.executable)
    records = []
    for index in range(start, start + count):
        family = index % 20
        number = index // 20 + 1
        question, code = _exercises(number)[family]
        output = _checked_output(code, interpreter)
        prompt = f"{question}\n```python\n{code}\n```"
        records.append({"capability": "conversa", "reviewed": True,
                        "messages": [{"role": "user", "content": prompt},
                                     {"role": "assistant", "content": f"O código imprime: `{output}`."}],
                        "evidence": {"kind": "deterministic_python", "python": str(interpreter),
                                     "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                                     "stdout_sha256": hashlib.sha256(output.encode()).hexdigest()},
                        "origin": "identity-llm-v1", "index": index})
    return records


def _train_partition(record: dict[str, Any]) -> bool:
    """Mirror the stable hash partition used by Yasmin's public data reader."""
    text = record["messages"][0]["content"]
    base = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(character for character in base if not unicodedata.combining(character))
    normalized = " ".join(re.findall(r"[a-z0-9]+", plain))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 5 != 0


def add_round_pairs(path: Path, round_number: int, python: Path | None = None) -> dict[str, int]:
    """Append 40+ once for a new dataset, then 10+ per round across runs."""
    if round_number < 1:
        raise ValueError("Rodada deve ser positiva.")
    previous = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (isinstance(record, dict) and record.get("origin") == "identity-llm-v1"
                    and isinstance(record.get("index"), int)):
                previous.append(record["index"])
    start = max(previous, default=-1) + 1
    initial_dataset = not previous
    needed = 40 if initial_dataset else 10
    records = generate_reviewed_pairs(start, needed, python)
    minimum_train = 20 if initial_dataset else 5
    minimum_eval = 3 if initial_dataset else 0
    while (sum(_train_partition(record) for record in records) < minimum_train
           or sum(not _train_partition(record) for record in records) < minimum_eval):
        records.extend(generate_reviewed_pairs(start + len(records), 1, python))
    added = append_conversation_records(path, records)
    return {"generated": len(records), "added": added, "first_index": start,
            "last_index": start + len(records) - 1}
