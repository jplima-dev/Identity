# Identity

Coloque a pasta `Identity` dentro do projeto que deseja verificar. Instale o pacote com `uv sync` dentro de `Identity` e execute `uv run identity scam` nessa pasta. Com o executável instalado no ambiente, use `identity scam`. Para verificar um projeto enquanto o Identity ainda está separado, passe `--project E:\caminho\projeto`.

## Modo

Deixe **um único arquivo vazio** de modo na raiz do Identity: `agente.tes` ou `api.tes`. O número no arquivo `.ag`, como `100.ag`, define quantos agentes serão iniciados. No modo agente, o número no arquivo `.rod`, como `10.rod`, define o máximo de rodadas. O modo API ignora `.rod` e executa uma rodada.

### `agente`

O scanner lê `PEDIDOS` e `AVALIACAO` em `Agent/agente/treino_capacidades.py` e inventaria as ferramentas registradas em `Ferramenta(...)`. Ele gera várias formas de cada pergunta e aponta ferramentas sem exemplos de classificação em `scan.json` e `relatorio.md`. Cada agente do Identity classifica uma pergunta usando os pesos de `Agent/modelos/capacidades_local.json.gz`, lidos diretamente em memória. **O Identity não envia essas perguntas ao endpoint de comandos da Yasmin nem executa ferramentas ou aplicativos.** O teste verifica se o modelo escolheu a capacidade esperada; não verifica a resposta nem o efeito da ferramenta. Perguntas de `AVALIACAO` são testadas sem entrar nos exemplos de treino. A cada nova rodada, o teste que falhou permanece com o mesmo agente; cada teste aprovado é trocado por uma pergunta diferente.

Após as classificações, o Identity usa os exemplos de conversa encontrados no projeto para treinar o classificador local de intenções do Yasmin. O treino mantém 1.000 exemplos, avalia o modelo e o publica em `Agent/modelos/intencoes_local.json`. Uma cópia anterior fica em `Identity/yasmin/modelo_anterior.json`; se o treino falhar, ela é restaurada. Os testes executáveis do projeto não são iniciados automaticamente, pois podem acionar ferramentas do desktop. **Reinicie o Yasmin para carregar o modelo publicado.** O modelo generativo usado para criar respostas é um componente separado e não é treinado por esse processo.

O resultado fica em `Identity/yasmin/relatorio.md`, com o JSONL das classificações e o log de treino. Para outro agente, o scanner registra o mecanismo encontrado; um adaptador seguro de classificação e treino precisa ser configurado para executar seus testes.

### `api`

O scanner lê rotas FastAPI, Flask, Express e Fastify, além de arquivos `openapi.json`/`swagger.json`. Ele tenta identificar os modelos de entrada e montar um fluxo por recurso: `POST` para criar um registro com marcador único, `PUT` ou `PATCH` para atualizar, `GET` para consultar e listar, e `DELETE` para remover **o ID criado pelo próprio teste**. Cada agente recebe um fluxo próprio; recursos aninhados são criados e excluídos na ordem correta. A limpeza é tentada mesmo quando atualização ou consulta falha. Se o `POST` não devolver ID, o Identity busca o marcador na lista para tentar localizar o registro. Falhas de limpeza aparecem no relatório e fazem o comando falhar.

Rotas de leitura independentes também são testadas. Rotas de escrita sem corpo inferível, sem atualização ou sem exclusão correspondente aparecem em `scan.json` e no relatório com o motivo da omissão. Para informar corpos que o scanner não consegue deduzir, use `api_payloads` em `identity.json`, por exemplo: `{"api_payloads":{"/users":{"create":{"name":"{identity_marker}","email":"identity+{identity_marker}@example.com"},"update":{"name":"{identity_marker}_alterado"}}}}`. O corpo de criação precisa conter `{identity_marker}` para permitir a recuperação do registro. Por padrão, fluxos com alterações só são executados em `localhost`; para um servidor remoto, configure `"allow_remote_mutations": true`. A URL vem do README local quando disponível; `--url` tem prioridade.

## Arquivos de resultado

Cada execução cria uma pasta com o nome do projeto dentro do Identity. Ela contém `scan.json`, o JSONL dos testes e `relatorio.md`. Falhas de classificação ou treino aparecem no relatório do modo agente; falhas de resposta ou status aparecem no modo API. O processo retorna código 0 quando tudo passa, 1 para falhas dos testes ou treino e 2 para erro de configuração.

Durante a execução, o terminal mostra o progresso geral e uma barra por capacidade testada (`agente`) ou método HTTP (`api`). As barras avançam quando cada resultado é gravado, mudando de vermelho para amarelo e verde. Em um terminal interativo, as mesmas linhas são atualizadas no lugar. Escaneamento, treino e testes automatizados mostram a etapa e o tempo decorrido.
