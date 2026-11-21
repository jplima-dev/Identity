# Identity

O **Identity** é uma ferramenta de terminal para descobrir e testar rotas de APIs e para testar agentes de IA. Ele executa casos em paralelo, registra o que foi testado e gera relatórios por projeto. Quando um agente fornece um treinador compatível e dados revisados, o Identity também pode avaliar e publicar novos pesos da LLM.

O Identity não inventa credenciais, respostas corretas ou uma forma segura de desfazer alterações. Projetos com essas necessidades podem fornecer um pequeno arquivo de integração; o código do Identity não precisa ser alterado para cada projeto.

## Comece em cinco minutos

**Pré-requisitos:** Git e [uv](https://docs.astral.sh/uv/getting-started/installation/). O arquivo `.python-version` fixa Python 3.14, que o uv pode preparar durante a instalação.

No PowerShell:

```powershell
git clone https://github.com/jplima-dev/Identity.git
cd Identity
git switch master
uv sync
powershell -ExecutionPolicy Bypass -File .\install-command.ps1
```

Abra um **terminal novo** e confirme:

```powershell
identity
```

Esse comando mostra a ajuda e um exemplo de uso. O instalador adiciona o executável criado em `.venv\Scripts` ao `PATH` do usuário no Windows. Se mover a pasta Identity, execute `install-command.ps1` novamente. Sem instalar o comando global, use `uv run identity ...` dentro da pasta do repositório. No Linux ou macOS, use `uv run identity ...`.

### Testar uma API

Deixe a API de teste em execução ou declare como iniciá-la no projeto. Substitua o caminho pelo da sua API:

```powershell
identity tes set api
identity ag set 5
identity rod set 1
identity scam --project C:\projetos\minha-api --url http://127.0.0.1:8000
identity plan --project C:\projetos\minha-api --url http://127.0.0.1:8000
identity minha-api
identity report minha-api
```

O nome usado em `identity minha-api` é o nome da pasta escaneada. `scam` **apenas descobre e salva** o projeto; `identity minha-api` faz as requisições. O plano mostra quais rotas serão testadas e quais precisam de informações adicionais.

Se a API tiver um arquivo OpenAPI, o Identity pode descobrir rotas mesmo sem reconhecer a linguagem do projeto. Para autenticação, inicialização do servidor, dados especiais e limpeza, crie `identity-api.json` na raiz da API. Exemplo mínimo:

```json
{
  "version": 1,
  "base_url": "http://127.0.0.1:8000",
  "health_path": "/health",
  "openapi_path": "docs/openapi.json"
}
```

`openapi_path` é opcional quando o arquivo se chama `openapi.json` ou `swagger.json`, ou quando as rotas são encontradas no código. Veja o [guia completo de APIs](docs/api-generica.md) para autenticação, setup, uploads, parâmetros, casos explícitos e limpeza.

### Testar um agente de IA

O agente precisa oferecer um endpoint de teste que receba `{"messages":[...]}` e devolva uma resposta em JSON. Esse endpoint não deve executar ferramentas que abram aplicativos ou alterem o computador. Crie `identity-agent.json` na raiz do projeto, por exemplo:

```json
{
  "version": 1,
  "safe_mode": true,
  "endpoint": "http://127.0.0.1:8000/identity-test",
  "cases": [
    {
      "split": "eval",
      "messages": [{"role": "user", "content": "Olá"}],
      "expected": {"contains": "Olá"}
    }
  ]
}
```

Depois execute:

```powershell
identity tes set agente
identity ag set 5
identity rod set 2
identity scam --project C:\projetos\meu-agente
identity meu-agente
identity report meu-agente
```

O scan também gera variações de perguntas a partir das capacidades encontradas. **Testar classificação ou registrar conversas não altera os pesos da LLM.** Para treinar pesos, o projeto deve declarar no mesmo manifesto seu modelo ativo, um JSONL de pares revisados e comandos de treino, avaliação e teste de carregamento. Consulte o [guia de treino genérico de LLM](docs/treino-llm-generico.md). O Identity só ativa um candidato que melhorou na avaliação e passou no teste de carregamento; se a ativação falhar, restaura o modelo anterior.

## Comandos

| Comando | O que faz |
| --- | --- |
| `identity` | Mostra ajuda, exemplo e projetos salvos. |
| `identity scam [--project CAMINHO]` | Escaneia e salva um projeto sem executar testes. |
| `identity plan [--project CAMINHO]` | Mostra os testes planejados e as rotas bloqueadas. |
| `identity NOME` | Executa o projeto salvo. |
| `identity report [NOME]` | Mostra o diagnóstico do projeto ou o último diagnóstico. |
| `identity tes`, `identity ag`, `identity rod` | Mostram o modo, número de agentes e rodadas. |
| `identity tes set api` / `identity tes set agente` | Muda o modo. |
| `identity ag set 5` / `identity rod set 2` | Define agentes e rodadas. |

Use `identity --help` para ver as opções. Não existe `identity run`: depois do scan, execute `identity NOME`.

## Configuração e segurança

A pasta `config/` contém três marcadores: `api.tes` ou `agente.tes` seleciona o modo; `N.ag` define quantos agentes participam; `N.rod` define as rodadas do modo agente. O Identity lê os três arquivos a cada execução. No modo API, `.rod` precisa existir, mas a campanha executa uma rodada. `max_concurrency` limita quantos agentes fazem requisições ao mesmo tempo; o padrão é 20.

`identity.json` contém opções gerais, como URL padrão e tempo limite. Um `identity-api.json` dentro da API prevalece sobre essas opções; `--url` prevalece sobre ambos. Para chamadas que alteram dados, use uma API e um banco de teste. Por padrão, mutações automáticas só são permitidas em localhost. O Identity tenta criar, consultar, atualizar e excluir registros quando encontra um fluxo reversível; rotas sem teste seguro aparecem como bloqueadas. Uma rota descoberta sem cobertura faz a execução terminar com erro.

O **CLI não precisa de `.env`**. O `.env.example` se refere ao módulo de banco opcional em `app/`. Se usar cabeçalhos com `${VARIAVEL}`, defina a variável no terminal antes de executar o Identity, ou forneça um `auth_command` no manifesto da API. Nunca coloque credenciais reais nos manifestos versionados.

## Resultados e diagnóstico

Cada projeto escaneado recebe uma pasta local:

```text
projects/NOME/
  scan.json
  testes-api.json               # modo API
  conversas/                    # modo agente
  runs/ID_DA_EXECUCAO/
    resultados.jsonl
    summary.json
    relatorio-api.md ou relatorio-agente.md
    logs e dados de treino, quando houver
```

`identity report NOME` mostra só o que ajuda no debug: falhas, cobertura, limpeza pendente e estado do treino. O relatório completo fica na pasta da execução. Essas pastas são ignoradas pelo Git.

Códigos de saída: **0** quando a execução passou, **1** para falha de teste, treino, limpeza ou cobertura incompleta, e **2** para erro de configuração. Descoberta estática pode não encontrar rotas ou capacidades registradas dinamicamente; use OpenAPI e os manifestos do projeto para completar o contrato.

## Desenvolvimento

```powershell
uv sync
uv run python -m unittest discover -s tests
uv run ruff check src tests
```

No Windows, também é possível usar `.venv\Scripts\python.exe` e `.venv\Scripts\ruff.exe` diretamente. Os testes usam projetos e servidores locais de exemplo; não treinam uma LLM real nem exigem credenciais externas.
