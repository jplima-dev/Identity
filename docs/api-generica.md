# Testes genéricos de API

Crie `identity-api.json` na raiz do projeto. `identity scam --project CAMINHO` lê o manifesto sem iniciar serviços; `identity NOME` prepara o ambiente, testa as rotas e executa a limpeza. O arquivo é opcional: projetos antigos continuam usando `Identity/identity.json`. As opções do projeto têm prioridade sobre as globais; `--url` tem prioridade sobre a URL do manifesto.

## Exemplo

```json
{
  "version": 1,
  "base_url": "http://127.0.0.1:8000",
  "openapi_path": "docs/service-schema.json",
  "health_path": "/health",
  "ready_timeout_seconds": 30,
  "start_command": [".venv/Scripts/python.exe", "-m", "uvicorn", "app:app", "--port", "8000"],
  "setup_command": [".venv/Scripts/python.exe", "scripts/test_setup.py"],
  "teardown_command": [".venv/Scripts/python.exe", "scripts/test_teardown.py"],
  "auth_command": [".venv/Scripts/python.exe", "scripts/test_auth.py"],
  "headers": {"X-Identity-Test": "1"},
  "max_concurrency": 10,
  "requests_per_agent": 1,
  "api_cases": {
    "GET /search": [{
      "path": "/search",
      "query": {"term": "poema"},
      "expected_status": [200],
      "expected_json": {"term": "poema"},
      "expected_headers": {"X-Api-Version": "1"},
      "expected_text_contains": "poema",
      "max_latency_ms": 2000
    }],
    "POST /items": [{
      "path": "/items",
      "isolated": true,
      "json": {"name": "identity-test"},
      "expected_status": [201],
      "cleanup": {
        "method": "DELETE",
        "path": "/items/{response.id}",
        "verify_path": "/items/{response.id}",
        "expected_status": [200, 204]
      }
    }]
  }
}
```

Ajuste os comandos para o ambiente do projeto. `openapi_path` pode ser um JSON com qualquer nome dentro do projeto. O scanner também reconhece `openapi.json`, `swagger.json` e padrões de rotas FastAPI, Flask, Express e Fastify. O plano em `projects/NOME/testes-api.json` lista cada rota descoberta com seu teste ou motivo de bloqueio.

`start_command` é opcional quando o servidor já está ativo. Se o Identity o iniciar, encerra o processo após os testes. O comando deve manter o servidor em primeiro plano; `health_path` precisa responder antes da autenticação. `setup_command` prepara banco e fixtures; `teardown_command` desfaz a preparação mesmo se os testes falharem. `isolated_environment: true` exige ambos. Falha no teardown entra no relatório como limpeza pendente.

`auth_command` imprime na última linha `{"headers":{"Authorization":"Bearer TOKEN_DE_TESTE"}}`. O token é usado só durante a execução e não é salvo no scan ou nos registros. Cabeçalhos estáticos aceitam ${VARIAVEL_DE_AMBIENTE}. Para mutações fora de localhost, declare `allow_remote_mutations: true`.

## Casos de teste

Uma chave de `api_cases` deve corresponder a uma rota descoberta, como `GET /items/{id}`; `path` informa o endereço concreto. Casos aceitam `query`, `headers`, `json`, `form` e `files`. Um upload usa um arquivo dentro do projeto:

```json
{
  "path": "/upload",
  "isolated": true,
  "form": {"categoria": "poema"},
  "files": {"arquivo": {"path": "fixtures/poema.txt", "content_type": "text/plain"}},
  "expected_status": [201]
}
```

Escritas exigem `isolated: true`. No manifesto do projeto, exigem também `cleanup` ou `isolated_environment: true` com teardown. `cleanup.path` pode usar `{response.id}`; `verify_path` confirma com GET que o recurso foi removido. Se a limpeza falhar, novas criações automáticas são suspensas. Rotas sem limpeza conhecida ficam bloqueadas no plano.

O Identity verifica status, estrutura JSON, campos, cabeçalhos, texto e latência quando declarados. Fluxos CRUD são gerados quando corpos, IDs e DELETE permitem uma execução reversível. Para regras de negócio e dados que não podem ser inferidos com segurança, declare `api_payloads` e `api_cases`. `identity plan --project CAMINHO` mostra o plano antes de testar.

```powershell
identity tes set api
identity scam --project D:\codes\minha-api
identity minha-api
identity report minha-api
```

A execução sai com código 1 se testes ou limpeza falharem, ou se alguma rota descoberta continuar sem teste. Credenciais, dados válidos e regras para desfazer alterações não podem ser deduzidos com segurança de toda API apenas lendo o código; o manifesto fornece essas informações sem mudar o Identity.
