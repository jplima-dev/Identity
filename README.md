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
identity scam --project E:\codes\meu-projeto
identity meu-projeto
identity report meu-projeto
```

`scam` analisa e salva o projeto sem executar testes. Depois, `identity NOME_DO_PROJETO` usa o caminho salvo em `Identity/NOME_DO_PROJETO/scan.json`, lê novamente os três arquivos de `Identity/config` e examina o código do projeto antes de testar. Assim, `identity tes set agente` ou `identity tes set api` muda o modo da próxima execução, mesmo para um projeto já salvo. Se o projeto não oferecer o mecanismo necessário ao modo escolhido, o Identity informa o erro.

Se o Identity estiver dentro do projeto, `identity scam` dispensa `--project`. No modo API, ajuste `base_url` em `identity.json` para a API em execução; também é possível passar `--url` ao `scam` ou ao comando do projeto salvo.

## Comandos

| Comando | Função |
| --- | --- |
| `identity` | Mostra o menu e os projetos salvos. |
| `identity scam [--project CAMINHO]` | Analisa e salva um projeto. |
| `identity NOME_DO_PROJETO` | Executa os testes do projeto salvo. |
| `identity plan [--project CAMINHO]` | Mostra o plano sem testar; `--all` lista todas as perguntas do modo agente. |
| `identity report [NOME_DO_PROJETO]` | Mostra o relatório mais recente do projeto indicado ou, sem nome, o último relatório geral. |
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

`identity.json` contém a URL base, o tempo limite, cabeçalhos e cenários HTTP. `requests_per_agent` define os ciclos de cada agente no modo API. Para autenticação em cabeçalhos, é possível usar `${NOME_DA_VARIAVEL}`; a variável precisa estar definida no ambiente.

## Testes de API

O scanner procura rotas FastAPI, Flask, Express e Fastify, além de `openapi.json` e `swagger.json`. Quando consegue inferir os corpos e relacionar as rotas de um recurso, cada agente cria um registro com marcador próprio, atualiza, consulta, lista e exclui o registro ao final. Ele tenta limpar o registro mesmo quando uma etapa intermediária falha. Leituras independentes também são testadas. Rotas que não formam um fluxo executável ficam registradas com o motivo da omissão.

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

O scanner procura exemplos de capacidades, perguntas de avaliação, ferramentas registradas e o mecanismo de resposta no código do projeto. Quando encontra um formato com adaptador executável, cria variações das perguntas e distribui os testes entre os agentes do Identity. Em cada rodada, perguntas que falharam permanecem; as que passaram são trocadas por outras. O relatório aponta capacidades e ferramentas sem exemplos.

Os testes de classificação usam o modelo local sem enviar comandos capazes de acionar ferramentas ou abrir aplicativos. Eles verificam a capacidade escolhida, sem avaliar a resposta final nem o efeito da ferramenta. Quando o adaptador oferece treino, exemplos de conversa alimentam o modelo local e o relatório registra o resultado. O treino de um modelo generativo de respostas não faz parte desse fluxo.

**A execução de testes e treino em agentes depende de um adaptador compatível.** Se o scanner reconhecer um mecanismo sem adaptador executável, o Identity informa essa limitação em vez de iniciar testes inseguros.

## Relatórios e terminal

Cada projeto salvo tem uma pasta `Identity/NOME_DO_PROJETO` com `scan.json`, resultados JSONL e um relatório distinto por modo: `relatorio-api.md` ou `relatorio-agente.md`. `identity report` também consegue ler relatórios antigos chamados `relatorio.md`. Falhas de teste, treino ou limpeza aparecem no relatório.

Em terminal interativo, o Identity mostra uma breve entrada verde, seções compactas e barras de progresso por método HTTP ou por capacidade. As barras refletem os resultados concluídos, mudam de vermelho para amarelo e verde e são atualizadas no lugar. `SCAN / TEST / REPORT` aparece somente ao digitar `identity` sem argumentos. Em saída redirecionada, a animação é omitida.

O código de saída é `0` para sucesso, `1` para falhas de testes ou treino e `2` para erro de configuração.
