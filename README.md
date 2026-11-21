# Identity

**Teste sua API antes do deploy e encontre falhas no roteamento de agentes de IA.**

O Identity analisa o código de um projeto, descobre o que pode testar e executa casos em paralelo. No modo `api`, verifica rotas HTTP e fluxos de criação, consulta, atualização e exclusão. No modo `agente`, identifica capacidades e mecanismos de resposta; quando há um adaptador compatível, gera variações de perguntas, testa a classificação e treina o modelo local. Os resultados ficam em relatórios separados por projeto e por modo.

## Use a branch master

Depois de clonar o repositório, entre na branch `master` antes de instalar e executar o Identity:

```powershell
git switch master
```

Os comandos abaixo devem ser executados a partir dessa branch.

## Instalação no Windows

Na pasta principal do Identity, execute:

```powershell
uv sync
powershell -ExecutionPolicy Bypass -File .\install-command.ps1
```

Abra um terminal novo e confira com `identity --help`. O instalador adiciona `Identity\.venv\Scripts` ao `PATH` do usuário. Se mover a pasta, execute o instalador novamente. Antes da instalação no `PATH`, use `uv run identity ...` dentro da pasta Identity.

## Comece por aqui

Coloque a pasta `Identity` dentro do projeto que quer testar ou indique o caminho dele com `--project`. Para uma API:

```powershell
identity tes set api
identity ag set 100
identity rod set 10
identity scam --project D:\codes\meu-projeto
identity meu-projeto
identity report meu-projeto
```

`scam` analisa e salva o projeto sem executar testes. No modo API, ele também gera `Identity/projects/NOME_DO_PROJETO/testes-api.json`, com um teste planejado ou um motivo de bloqueio para cada rota descoberta. Depois, `identity NOME_DO_PROJETO` usa o caminho salvo em `Identity/projects/NOME_DO_PROJETO/scan.json`, lê novamente os três arquivos de `Identity/config` e examina o código do projeto antes de testar. Assim, `identity tes set agente` ou `identity tes set api` muda o modo da próxima execução, mesmo para um projeto já salvo. Se o projeto não oferecer o mecanismo necessário ao modo escolhido, o Identity informa o erro.

Se o Identity estiver dentro do projeto, `identity scam` dispensa `--project`. No modo API, ajuste `base_url` em `identity.json` para a API em execução; também é possível passar `--url` ao `scam` ou ao comando do projeto salvo.

## Comandos

| Comando | Função |
| --- | --- |
| `identity` | Mostra os comandos, um exemplo completo de uso e os projetos salvos. |
| `identity scam [--project CAMINHO]` | Analisa e salva um projeto. |
| `identity NOME_DO_PROJETO` | Executa os testes do projeto salvo. |
| `identity plan [--project CAMINHO]` | Mostra o plano sem testar; `--all` lista todas as perguntas do modo agente. |
| `identity report [NOME_DO_PROJETO]` | Mostra um resumo de debug do projeto indicado ou da última execução geral. |
| `identity tes`, `identity ag`, `identity rod` | Mostram os valores atuais. |
| `identity tes set api` / `identity tes set agente` | Define o modo. |
| `identity ag set 100` / `identity rod set 10` | Define o número de agentes e de rodadas. |

Use `identity --help` e `identity COMANDO --help` para ver as opções. O comando `identity run` não é usado: execute o projeto pelo nome salvo.

## Configuração

O diretório `Identity/config` contém três arquivos marcadores:

| Arquivo | Significado |
| --- | --- |
| `api.tes` **ou** `agente.tes` | Modo da execução. |
| `100.ag`, por exemplo | Quantidade de agentes simultâneos. |
| `10.rod`, por exemplo | Rodadas de classificação no modo agente. |

O Identity lê e valida os três arquivos em cada execução. No modo API, `.rod` também precisa ser válido, mas a campanha executa uma rodada. Os comandos `set` substituem o marcador anterior da mesma extensão. Os valores numéricos devem ser inteiros maiores que zero.

`identity.json` contém a URL base, o tempo limite, cabeçalhos e cenários HTTP. `requests_per_agent` define os ciclos de cada agente no modo API. `max_concurrency` limita quantos agentes fazem requisições ao mesmo tempo (padrão: 20); o arquivo `.ag` continua definindo quantos agentes participam da execução. Para autenticação em cabeçalhos, é possível usar `${NOME_DA_VARIAVEL}`; a variável precisa estar definida no ambiente.

## Testes de API

O scanner procura rotas FastAPI, Flask, Express e Fastify, além de `openapi.json` e `swagger.json`. Cada rota descoberta entra no plano: o Identity executa leituras independentes sem parâmetros, fluxos que consegue criar e limpar, e casos adicionais definidos pelo projeto. O fluxo pode incluir atualização, consulta do item e listagem quando essas rotas existem; `POST` e `DELETE` correspondentes já permitem um teste com limpeza. As demais rotas aparecem no plano e no relatório com o motivo da omissão. Quando há OpenAPI, status de sucesso e estrutura JSON declarados são verificados; um `200` com corpo inválido falha.

Um pequeno inferidor semântico local escolhe dados de teste a partir dos nomes, descrições e restrições dos campos do esquema. Ele reconhece, por exemplo, email, nome, descrição, código, preço, quantidade e datas, e cria valores únicos por agente. `identity plan --project CAMINHO` mostra um exemplo dos corpos de criação e atualização antes de qualquer requisição. Não há chamada a um serviço externo de IA. Quando o esquema não permite deduzir um corpo confiável, a rota fica pendente para `api_payloads`.

Em um fluxo com limpeza, cada agente cria um registro com marcador próprio, executa as etapas disponíveis e exclui o registro ao final. As consultas precisam mostrar o marcador criado ou atualizado; quando há rota de consulta do item, ela precisa retornar `404` ou `410` após o `DELETE`. Quando há apenas listagem, o Identity confirma que o identificador e o marcador desapareceram da coleção. Se o esquema de um `PUT` exigir campos, o Identity envia ainda um corpo vazio ao registro de teste e espera erro de validação (`400` ou `422`). Ele tenta limpar o registro mesmo quando uma etapa intermediária falha. Se uma limpeza não puder ser confirmada, novas criações automáticas são suspensas; requisições já iniciadas ainda terminam. O relatório mostra quantas rotas foram realmente executadas. Para testar uma rota com parâmetros ou um caso específico, adicione `api_cases` a `identity.json`:

```json
{
  "api_cases": {
    "GET /users/{id}": [
      {
        "name": "consulta de usuário conhecido",
        "path": "/users/123",
        "expected_status": [200],
        "expected_json": {"id": 123}
      }
    ]
  }
}
```

`expected_json` compara os campos informados e aceita campos adicionais na resposta. Casos explícitos também podem usar `json` e outros métodos HTTP. Para um caso de escrita, defina `"isolated": true` no caso e use um ambiente descartável: fora dos fluxos CRUD, o Identity não consegue garantir a limpeza. Mutação remota exige também `allow_remote_mutations: true`.

Se o corpo de criação ou atualização não puder ser inferido, configure `api_payloads` em `identity.json`:

```json
{
  "api_payloads": {
    "/users": {
      "create": {
        "name": "{identity_marker}",
        "email": "identity+{identity_marker}@example.com"
      },
      "update": {"name": "{identity_marker}_alterado"}
    }
  }
}
```

O corpo de criação precisa conter `{identity_marker}` para ajudar a localizar e excluir o registro. Por padrão, fluxos que alteram dados são executados somente em `localhost`. Para permitir esses fluxos em um servidor remoto, defina `"allow_remote_mutations": true` em `identity.json`. Confirme o alvo antes de usar essa opção.

## Testes de agente

Para testar um agente de qualquer projeto, crie `identity-agent.json` na raiz dele. O endpoint declarado deve aceitar `POST` com `{"messages": [...]}` e responder JSON com o campo `response`. Use um endpoint de teste que **não execute ferramentas nem altere o computador**. `safe_mode: true` é a declaração do projeto de que esse isolamento existe; o Identity não consegue comprová-lo automaticamente.

Ao executar `identity scam`, o Identity lê o código Python e o manifesto sem executar o agente. Ele identifica exemplos, ferramentas e funções de conversa e gera perguntas com variações de contexto, cortesia e múltiplos turnos. Os dados são salvos em `Identity/projects/NOME_DO_PROJETO/conversas/`, mesmo antes da primeira execução de testes. O arquivo `funcionalidades.json` contém o inventário; `dados_gerados.jsonl` contém uma conversa por linha com o rótulo da capacidade e `reviewed: false`. O escaneamento funciona sem `identity-agent.json`; para executar os testes de um agente genérico, configure esse adaptador com um endpoint seguro. O scanner estático pode deixar de encontrar capacidades registradas dinamicamente; declare essas capacidades em `identity-agent.json` quando necessário.

```json
{
  "version": 1,
  "safe_mode": true,
  "endpoint": "http://127.0.0.1:8000/identity-test",
  "capability_field": "capability",
  "capabilities": [
    {"name": "agenda", "description": "Consultar compromissos", "examples": ["Quais são meus compromissos?"]}
  ],
  "cases": [
    {"split": "train", "messages": [{"role": "user", "content": "Olá"}], "expected": {"contains": "Olá"}},
    {"split": "eval", "name": "saudação", "messages": [{"role": "user", "content": "Bom dia"}], "expected": {"contains": "Bom dia"}}
  ],
  "training": {
    "model_path": "models/model.bin",
    "command": ["python", "train.py", "--data", "{train_data}", "--output", "{candidate_model}"],
    "evaluate_command": ["python", "evaluate.py", "--model", "{model}", "--cases", "{eval_data}"],
    "min_score": 0.8
  }
}
```

`capability_field` é opcional. Se o endpoint também devolver `{"capability": "agenda"}`, o Identity transforma as conversas geradas em testes de classificação e compara esse campo com a capacidade esperada. Isso testa o roteamento da intenção; a resposta final continua sendo verificada pelos casos manuais com `expected.contains` ou `expected.equals`. Você pode omitir `cases` quando houver `capability_field` e capacidades descobertas. Para treinar, mantenha casos `eval` manuais independentes das conversas geradas.

O avaliador deve imprimir na última linha um JSON como `{"passed": 8, "total": 10}`. O Identity treina um arquivo candidato, avalia o modelo atual e o candidato nos casos `eval`, e só publica o candidato se atingir `min_score` **e superar** o resultado anterior. Uma cópia do modelo antigo fica na pasta de resultados. O formato do modelo e os comandos de treino/avaliação pertencem ao projeto; sem `training`, o relatório indica que não houve treino. Os casos `train` não entram nos testes de avaliação.

Para incluir as conversas geradas no arquivo `{train_data}`, defina `"use_generated": true` dentro de `training`. Os registros gerados têm rótulos de capacidade, mas não respostas finais inventadas; revise a qualidade desses dados e adapte o treinador do projeto ao formato JSONL antes de usá-los. A avaliação que decide a publicação do modelo usa somente os casos `eval` manuais.

Cada agente testa uma conversa por rodada. Na rodada seguinte, casos com falha permanecem e casos aprovados são trocados. O relatório registra respostas, erros e o resultado do treino. A integração de classificação local existente continua disponível para projetos reconhecidos pelo scanner; seus testes verificam a capacidade escolhida, não a resposta final. Nesse fluxo local, o Identity executa todas as rodadas configuradas antes de iniciar o treino do modelo.

## Relatórios e terminal

Os dados novos ficam organizados por projeto e por execução:

```text
Identity/projects/NOME_DO_PROJETO/
  scan.json
  testes-api.json (modo API)
  conversas/
    funcionalidades.json
    dados_gerados.jsonl
  runs/AAAAmmdd-HHMMSS-microssegundos/
    resultados.jsonl
    summary.json
    relatorio-api.md ou relatorio-agente.md
    logs e dados de treino, quando existirem
```

`identity report NOME_DO_PROJETO` seleciona a execução mais recente desse projeto; `identity report` seleciona a última entre todos. O terminal mostra apenas o diagnóstico: falhas mais frequentes, cobertura, limpeza pendente, rotas sem teste e resultado do treino. O caminho do relatório completo aparece no final do resumo. Pastas antigas diretamente na raiz do Identity continuam legíveis, sem apagar nem mover resultados anteriores.

Em terminal interativo, o Identity mostra uma breve entrada verde, seções compactas e barras de progresso por método HTTP ou por capacidade. As barras refletem os resultados concluídos, mudam de vermelho para amarelo e verde e são atualizadas no lugar. `SCAN / TEST / REPORT` aparece somente ao digitar `identity` sem argumentos. Em saída redirecionada, a animação é omitida.

O código de saída é `0` para sucesso, `1` para falhas de testes ou treino e `2` para erro de configuração.
