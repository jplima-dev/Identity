# Identity

Coloque a pasta `Identity` dentro do projeto que deseja verificar. Instale o pacote com `uv sync` dentro de `Identity` e execute `uv run identity scam` nessa pasta. Com o executável instalado no ambiente, use `identity scam`. Para verificar um projeto enquanto o Identity ainda está separado, passe `--project E:\caminho\projeto`.

## Modo

Deixe **um único arquivo vazio** de modo na raiz do Identity: `agente.tes` ou `api.tes`. O número no arquivo `.ag`, como `100.ag`, define quantos agentes serão iniciados. No modo agente, o número no arquivo `.rod`, como `10.rod`, define o máximo de rodadas. O modo API ignora `.rod` e executa uma rodada.

### `agente`

O scanner lê `PEDIDOS` em `Agent/agente/treino_capacidades.py` e gera `perguntas_geradas.json` para todas as capacidades, inclusive abrir aplicativos. Cada agente do Identity classifica uma pergunta usando os pesos de `Agent/modelos/capacidades_local.json.gz`, lidos diretamente em memória. **O Identity não envia essas perguntas ao endpoint de comandos da Yasmin nem executa ferramentas ou aplicativos.** O teste verifica se o modelo escolheu a capacidade esperada; não verifica a resposta nem o efeito da ferramenta. A cada nova rodada, o teste que falhou permanece com o mesmo agente; cada teste aprovado é trocado por uma pergunta diferente.

Após os testes, o Identity usa os exemplos de conversa encontrados no projeto para treinar o classificador local de intenções do Yasmin. O treino mantém 1.000 exemplos, avalia o modelo e o publica em `Agent/modelos/intencoes_local.json`. Uma cópia anterior fica em `Identity/yasmin/modelo_anterior.json`; se o treino ou os testes automatizados falharem, ela é restaurada. **Reinicie o Yasmin para carregar o modelo publicado.** O modelo generativo usado para criar respostas é um componente separado e não é treinado por esse processo.

O resultado fica em `Identity/yasmin/relatorio.md`, com o JSONL das classificações e os logs de treino e testes. Para outro agente, o scanner registra o mecanismo encontrado; um adaptador seguro de classificação e treino precisa ser configurado para executar seus testes.

### `api`

O scanner lê o código e procura rotas HTTP, inclusive prefixos de `APIRouter` e caminhos aninhados. Ele executa rotas de leitura (`GET`, `HEAD`, `OPTIONS`), distribuindo-as entre os agentes. Para caminhos com parâmetros, usa o ID `0` e espera `404`, verificando a resposta para recurso inexistente sem criar dados. A URL é extraída do README do projeto quando houver um endereço local; `--url` tem prioridade. As rotas encontradas e as que fazem alterações aparecem em `scan.json`; as de alteração exigem cenários explícitos antes de serem executadas.

## Arquivos de resultado

Cada execução cria uma pasta com o nome do projeto dentro do Identity. Ela contém `scan.json`, o JSONL dos testes e `relatorio.md`. Falhas de resposta, status e treino aparecem no relatório. O processo retorna código 0 quando tudo passa, 1 para falhas dos testes ou treino e 2 para erro de configuração.

Durante a execução, o terminal mostra o progresso geral e uma barra por capacidade testada (`agente`) ou método HTTP (`api`). As barras avançam quando cada resultado é gravado, mudando de vermelho para amarelo e verde. Em um terminal interativo, as mesmas linhas são atualizadas no lugar. Escaneamento, treino e testes automatizados mostram a etapa e o tempo decorrido.
