# Treino da LLM de conversa da Yasmin

O modo `agente` executa uma rodada de classificação local e, em seguida, chama o pacote `Agent/treino_llm` da Yasmin. O classificador de intenções e a LLM de conversa têm relatórios separados. Acertos de classificação não são contados como treino LoRA.

## Dados revisados

O arquivo `projects/yasmin/conversas/dados_gerados.jsonl` é preservado nos próximos escaneamentos. A primeira rodada de uma base vazia acrescenta pelo menos 40 pares novos; as próximas acrescentam pelo menos 10, mesmo quando começam em outra execução do Identity. Os pares têm `capability: "conversa"`, duas mensagens (`user` e `assistant`) e `reviewed: true` somente depois de verificar a saída de um programa Python determinístico em dois processos isolados. A saída da Yasmin nunca é usada como gabarito. O leitor da Yasmin seleciona treino e avaliação reservada por uma divisão estável dos pedidos.

## Ambiente e espaço

Antes da instalação, o Identity verifica os volumes que receberão as dependências, os pesos Safetensors originais, o modelo mesclado e a importação no Ollama. A estimativa reserva espaço adicional para evitar esgotar o disco durante a publicação. O ambiente e o cache do modelo ficam por padrão em `Identity/.llm-venv/` e `Identity/.llm-cache/`; é possível definir `IDENTITY_LLM_VENV` e `IDENTITY_LLM_CACHE_DIR`. O ambiente da aplicação da Yasmin permanece separado. Dependências de `Agent/requirements-treino-llm.txt` são instaladas apenas quando faltarem. O Identity também prepara o conversor GGUF oficial do llama.cpp em `Identity/.llm-tools/`, com as dependências necessárias no mesmo ambiente separado.

Se o espaço ou a instalação falhar, o relatório marca a LLM como `bloqueado`, registra o motivo e **zero passos de otimização**. O Identity interrompe as próximas rodadas nesse caso. Os pares revisados continuam salvos.

## Treino e publicação

Em cada rodada, o Python do ambiente de treino executa `python -m treino_llm --dry-run` e depois `python -m treino_llm`, a partir de `Agent`. O Identity passa o caminho explícito do JSONL e do cache por argumentos. O pacote da Yasmin começa com o adaptador ativo, mede a perda reservada, otimiza pesos LoRA, mede a perda candidata e só publica se ela melhorar. A publicação mescla os pesos, converte o modelo Qwen3 para GGUF, importa no Ollama local e testa uma resposta JSON antes de trocar `Agent/modelos/llm_ativa.json`.

O treino LoRA usa a CPU quando CUDA não está disponível. A quantidade de agentes controla as classificações, não a duração de cada passo da LLM. Para evitar 100 passos fixos mesmo com poucos exemplos novos, o Identity usa no mínimo 16 passos e acrescenta uma pequena repetição de exemplos anteriores. O treinador apresenta todos os pares novos primeiro e alterna a parte do histórico repetida em cada publicação. A avaliação reservada continua completa; se a perda não melhorar, o modelo ativo é preservado.

O Identity confirma que o processo informou passos positivos, perdas finitas com melhora, `adapter_model.safetensors` existente, novo modelo ativo e mensagem de publicação. Em falha, restaura o ponteiro anterior se ele tiver mudado. Cada execução salva `llm-rodada-N.json`, o log do `dry-run`, o log do treino e o relatório completo em `projects/yasmin/runs/ID_DA_EXECUCAO/`.

Para consultar o estado resumido, use `identity report yasmin`. O arquivo completo distingue o treino da LLM do treino do classificador de intenções.

No Windows, o carregamento dos pesos usa `low_cpu_mem_usage=True` para reduzir o pico de RAM. O Identity mantém uma trava por projeto: se outra campanha já estiver treinando a mesma LLM, a nova rodada é bloqueada antes de carregar o modelo. O erro 1455 indica falta de memória virtual disponível naquele momento; o modelo ativo permanece preservado e o relatório não deve contar passos que não ocorreram.
