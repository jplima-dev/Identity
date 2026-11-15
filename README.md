# Identity

Coloque a pasta `Identity` dentro do projeto que deseja verificar ou passe `--project E:\caminho\projeto`. Execute `identity scam --project E:\caminho\projeto` para analisar e salvar o projeto. Depois, execute `identity NOME_DO_PROJETO` para iniciar os testes usando o caminho salvo em `Identity/NOME_DO_PROJETO/scan.json` e o modo atual em `Identity/config/*.tes`.

Para chamar `identity` diretamente no Windows, execute `powershell -ExecutionPolicy Bypass -File .\install-command.ps1` uma vez dentro de Identity e abra um terminal novo. O instalador usa o executável já criado em `.venv\Scripts` e adiciona essa pasta ao PATH do usuário sem alterar outras entradas. Depois disso, use `identity --help` ou `identity scam --project E:\caminho\projeto` sem `uv run`. Se mover a pasta Identity, execute o instalador novamente no novo local.

Ao iniciar `identity` em um terminal interativo, uma breve animação verde apresenta o comando. A frase `SCAN / TEST / REPORT` aparece apenas ao digitar `identity` sem argumentos. A animação não aparece quando a saída é redirecionada ou capturada. Ao executar somente `identity`, o terminal mostra a lista de comandos disponíveis. Use `identity --help` para consultar a mesma ajuda a qualquer momento.

## Comandos

| Comando | Resultado |
| --- | --- |
| `identity plan --project E:\caminho\projeto` | Mostra os fluxos CRUD, leituras, omissões e uma amostra das perguntas do agente sem executar testes. |
| `identity scam --project E:\caminho\projeto` | Analisa o projeto e salva `scan.json` sem iniciar testes. |
| `identity NOME_DO_PROJETO` | Carrega o projeto salvo e executa os testes. |
| `identity report [NOME_DO_PROJETO]` | Mostra o último relatório geral ou o mais recente do projeto escolhido. |
| `identity tes`, `identity rod`, `identity ag` | Mostram o modo, as rodadas e a quantidade de agentes definidos em `config`. |
| `identity tes set api` / `identity tes set agente` | Define o modo para os próximos scans e para a execução dos projetos salvos. |
| `identity ag set 100` / `identity rod set 10` | Define agentes e rodadas sem editar os arquivos manualmente. |

Quando a pasta Identity está dentro do projeto, `--project` é opcional no `scam`. Projetos salvos aparecem como comandos na ajuda do Identity. Use `uv run identity ...` se o executável `identity` ainda não estiver no PATH.

## Modo

Deixe os arquivos marcadores dentro de `Identity/config`: `agente.tes` ou `api.tes`, um arquivo numérico `.ag` (por exemplo, `100.ag`) e um arquivo numérico `.rod` (por exemplo, `10.rod`). O Identity lê e valida os três arquivos a cada execução, inclusive quando o projeto já foi salvo pelo `scam`. No modo API, `.rod` é validado, mas os testes executam uma única rodada. Os comandos `tes set`, `ag set` e `rod set` substituem o marcador anterior pelo novo.

### `agente`

O scanner lê `PEDIDOS` e `AVALIACAO` em `Agent/agente/treino_capacidades.py` e inventaria as ferramentas registradas em `Ferramenta(...)`. Ele gera várias formas de cada pergunta e aponta ferramentas sem exemplos de classificação em `scan.json` e `relatorio-agente.md`. Cada agente do Identity classifica uma pergunta usando os pesos de `Agent/modelos/capacidades_local.json.gz`, lidos diretamente em memória. **O Identity não envia essas perguntas ao endpoint de comandos da Yasmin nem executa ferramentas ou aplicativos.** O teste verifica se o modelo escolheu a capacidade esperada; não verifica a resposta nem o efeito da ferramenta. Perguntas de `AVALIACAO` são testadas sem entrar nos exemplos de treino. A cada nova rodada, o teste que falhou permanece com o mesmo agente; cada teste aprovado é trocado por uma pergunta diferente.

Após as classificações, o Identity usa os exemplos de conversa encontrados no projeto para treinar o classificador local de intenções do Yasmin. O treino mantém 1.000 exemplos, avalia o modelo e o publica em `Agent/modelos/intencoes_local.json`. Uma cópia anterior fica em `Identity/yasmin/modelo_anterior.json`; se o treino falhar, ela é restaurada. Os testes executáveis do projeto não são iniciados automaticamente, pois podem acionar ferramentas do desktop. **Reinicie o Yasmin para carregar o modelo publicado.** O modelo generativo usado para criar respostas é um componente separado e não é treinado por esse processo.

O resultado fica em `Identity/yasmin/relatorio-agente.md`, com o JSONL das classificações e o log de treino. Para outro agente, o scanner registra o mecanismo encontrado; um adaptador seguro de classificação e treino precisa ser configurado para executar seus testes.

### `api`

O scanner lê rotas FastAPI, Flask, Express e Fastify, além de arquivos `openapi.json`/`swagger.json`. Ele tenta identificar os modelos de entrada e montar um fluxo por recurso: `POST` para criar um registro com marcador único, `PUT` ou `PATCH` para atualizar, `GET` para consultar e listar, e `DELETE` para remover **o ID criado pelo próprio teste**. Cada agente recebe um fluxo próprio; recursos aninhados são criados e excluídos na ordem correta. A limpeza é tentada mesmo quando atualização ou consulta falha. Se o `POST` não devolver ID, o Identity busca o marcador na lista para tentar localizar o registro. Falhas de limpeza aparecem no relatório e fazem o comando falhar.

Rotas de leitura independentes também são testadas. Rotas de escrita sem corpo inferível, sem atualização ou sem exclusão correspondente aparecem em `scan.json` e no relatório com o motivo da omissão. Para informar corpos que o scanner não consegue deduzir, use `api_payloads` em `identity.json`, por exemplo: `{"api_payloads":{"/users":{"create":{"name":"{identity_marker}","email":"identity+{identity_marker}@example.com"},"update":{"name":"{identity_marker}_alterado"}}}}`. O corpo de criação precisa conter `{identity_marker}` para permitir a recuperação do registro. Por padrão, fluxos com alterações só são executados em `localhost`; para um servidor remoto, configure `"allow_remote_mutations": true`. A URL vem do README local quando disponível; `--url` tem prioridade.

## Arquivos de resultado

Cada projeto tem uma pasta com seu nome dentro do Identity. Ela contém `scan.json`, o JSONL dos testes e relatórios distintos: `relatorio-agente.md` ou `relatorio-api.md`. O comando `report` também lê relatórios antigos chamados `relatorio.md`. O processo retorna código 0 quando tudo passa, 1 para falhas dos testes ou treino e 2 para erro de configuração.

Durante `identity scam --project E:\caminho\projeto`, o terminal mostra o projeto e o resultado do scanner; nenhuma requisição ou treino é iniciado. Durante `identity NOME_DO_PROJETO`, aparecem o progresso geral e uma barra por capacidade (`agente`) ou método HTTP (`api`). As barras avançam com os resultados, mudando de vermelho para amarelo e verde, e são atualizadas no lugar em um terminal interativo.
