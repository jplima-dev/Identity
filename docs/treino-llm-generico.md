# Treino genérico de LLM

O Identity pode testar e treinar uma LLM de outro projeto sem receber código específico para esse projeto. O projeto declara o mecanismo de conversa, os dados revisados e os comandos que sabem treinar, avaliar e carregar seus pesos. O comando `identity scam --project CAMINHO` lê e valida essa declaração; `identity NOME` executa os testes e o treino.

Uma LLM arbitrária não tem formato de pesos, função de perda ou mecanismo de carregamento universal. O Identity não altera automaticamente o código de inferência do projeto. O projeto precisa carregar o caminho `model_path` declarado e oferecer os comandos abaixo. Isso pode ser feito em uma integração única dentro do projeto, sem modificar o código do Identity.

## Contrato do projeto

Crie `identity-agent.json` na raiz do projeto. O endpoint de teste deve aceitar `POST {"messages":[...]}`, devolver `{"response":"..."}` e não executar ferramentas com efeitos externos. Exemplo para uma LLM de poemas:

```json
{
  "version": 1,
  "safe_mode": true,
  "endpoint": "http://127.0.0.1:8000/identity-test",
  "cases": [
    {
      "split": "eval",
      "name": "poema sobre mar",
      "messages": [{"role": "user", "content": "Escreva um poema curto sobre o mar"}],
      "expected": {"contains": "mar"}
    }
  ],
  "training": {
    "kind": "llm",
    "model_path": "models/ativo",
    "dataset_path": "data/poemas_revisados.jsonl",
    "metric": "loss",
    "min_improvement": 0,
    "timeout_seconds": 3600,
    "command": [".venv/Scripts/python.exe", "train.py", "--data", "{train_data}", "--base", "{model}", "--output", "{candidate_model}"],
    "evaluate_command": [".venv/Scripts/python.exe", "evaluate.py", "--model", "{model}", "--cases", "{eval_data}"],
    "smoke_command": [".venv/Scripts/python.exe", "smoke.py", "--model", "{model}"]
  }
}
```

No Linux ou macOS, ajuste o caminho do Python para `.venv/bin/python`. Também é possível usar `{project_python}`, que escolhe o Python de `.venv` quando existir, ou `{python}`, que usa o ambiente do Identity. Comandos são listas de argumentos, sem shell. `model_path` pode ser um arquivo ou diretório, mas o treinador deve produzir o mesmo tipo em `{candidate_model}`. O serviço de inferência precisa carregar o modelo ativo desse caminho. Se ele mantém pesos na memória, defina `reload_command` para recarregá-los após a troca; esse comando também é chamado numa reversão.

## Referências para o treino

`dataset_path` é um JSONL mantido pelo projeto. Cada linha aprovada contém uma resposta de referência verificada por uma pessoa ou por uma fonte independente:

```json
{"reviewed":true,"messages":[{"role":"user","content":"Escreva um poema curto sobre chuva"},{"role":"assistant","content":"A chuva risca o vidro em silêncio."}]}
```

O Identity preserva o arquivo original e copia os pares revisados e distintos para a pasta da rodada. Linhas sem `reviewed: true` não entram no treino. Perguntas dos casos `eval` não podem aparecer no treino. Conversas que o scanner gerou ou respostas da própria LLM não viram gabarito automaticamente. Sem pares revisados, a rodada fica bloqueada e nenhum peso é alterado.

## Saídas exigidas dos comandos

- `command`: produz o candidato e imprime na última linha `{"optimization_steps": 25}` com passos reais, maiores que zero.
- `evaluate_command`: imprime `{"loss": 0.42}` ou `{"score": 0.83}` conforme `metric`. O mesmo avaliador mede o modelo ativo e o candidato em casos reservados.
- `smoke_command`: carrega ou consulta o modelo ativado e imprime `{"ok": true}` na última linha. O Identity só mantém a troca se o comando terminar com código zero e confirmar `ok`.

O candidato precisa melhorar a métrica por mais que `min_improvement`. O Identity copia os pesos para um caminho temporário no volume do projeto, guarda o modelo anterior como backup, ativa o candidato e executa o teste de carregamento. Se esse teste falhar, restaura o modelo anterior. Antes da cópia, o Identity confere espaço livre no volume do modelo. Uma trava por projeto impede dois treinos simultâneos sobre os mesmos pesos. A pasta `projects/NOME/runs/EXECUCAO/llm-rodada-N/` contém os dados usados, logs, candidato e `resultado.json` com pares, passos, métricas e estado da publicação. O relatório do agente resume cada rodada.

Use:

```powershell
identity tes set agente
identity scam --project D:\codes\meu-agente
identity meu-agente
identity report meu-agente
```

O Identity não instala dependências do projeto nem inventa respostas corretas. O projeto deve fornecer um treinador compatível com sua arquitetura e dados de referência revisados. Esse contrato permite integrar outras LLMs sem adicionar uma rotina específica para cada uma no Identity.
