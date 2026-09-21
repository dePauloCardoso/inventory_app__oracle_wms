# Walkthrough: Automação Total do Ciclo de Contagens no Oracle WMS

Implementamos a automação completa do fluxo de aprovações, rejeições e retenções de contagens cíclicas, removendo os botões manuais e processando tudo em sequência direta com a geração dos DataFrames atualizados.

---

## 🎯 O que foi alterado e implementado

### 1. Eliminação dos Processos Manuais
- Removidos os botões manuais `Execute Bulk Approve`, `Execute Bulk Reject` e `Reter Todas as Tarefas`.
- Agora existe um único acionador principal:
  **`🚀 Sincronizar & Executar Fluxo Automático`**
- Ao acionar o botão, a aplicação executa automaticamente todo o ciclo:
  1. Busca de cabeçalhos (`cc_adjustment_hdr`) e paginação completa.
  2. Busca de detalhes (`cc_adjustment_dtl`) em lotes.
  3. Checagem detalhada (Item + Quantidade + LPN/Container) e sequenciamento de contagens por posição.
  4. Envio de aprovação em lote (`bulk_approve`) para contagens válidas.
  5. Envio de rejeição em lote (`bulk_reject`) para contagens divergentes.
  6. Consulta imediata e retenção em lote (`bulk_hold`) das novas tarefas criadas pelo WMS para as posições rejeitadas.
  7. Atualização instantânea dos DataFrames com métricas visuais no painel.

---

### 2. Filtro de Datas Interativo Definido pelo Usuário
- **Na Tela de Login**:
  - Campos adicionados no formulário inicial: `Data Inicial` e `Data Final` das tarefas.
  - Assim como usuário e senha, os filtros já podem ser inicializados no momento do login.
- **No Painel Principal**:
  - Bloco visual de filtros com:
    - `Data Inicial` (`st.date_input`) e `Hora Inicial` (`st.time_input`)
    - `Data Final` (`st.date_input`) e `Hora Final` (`st.time_input`)
    - `Facility ID` (`st.number_input`)
  - Geração dinâmica dos parâmetros ISO com fuso horário (`create_ts__gte` e `create_ts__lte`).
  - Permite ao usuário alterar as datas a qualquer momento antes de clicar em `🚀 Sincronizar & Executar Fluxo Automático`.

---

### 3. Tradução dos Status das Tarefas para Português
Os status numéricos do Oracle WMS agora são traduzidos e exibidos com suas descrições oficiais em português nos DataFrames e nos cards de acompanhamento:
- **`10`**: **Em Andamento** (*In Progress*)
- **`20`**: **Pendente** (*Pending*)
- **`30`**: **Aprovado** (*Approved*)
- **`50`**: **Rejeitado** (*Rejected*)
- **`70`**: **Sem Divergência** (*No Variance*)
- **`99`**: **Cancelado** (*Cancelled*)

Exibido na coluna **`Status da Tarefa (WMS)`** tanto na aba de detalhamento quanto na aba de tarefas retidas.

---

### 4. Filtros Dinâmicos em Todas as Colunas dos DataFrames
- **Busca Global**: Campo de texto rápido que pesquisa simultaneamente em todas as colunas do DataFrame (ex: digitar parte de uma localização, SKU, LPN ou status).
- **Filtros por Colunas Específicas**:
  - Seleção de qualquer coluna do DataFrame via `st.multiselect`.
  - **Colunas Textuais / Categóricas**: Multiselect com valores únicos ou campo de busca contextual.
  - **Colunas Numéricas**: Slider de intervalo (`st.slider`) com limites mínimo e máximo dinâmicos.
  - **Colunas de Data/Hora**: Seletor de período de datas (`st.date_input`).
- **Contador Dinâmico de Registros**: Exibe a contagem de registros visíveis vs totais em tempo real.
- **Exportação CSV**: Botão `📥 Exportar CSV` que permite baixar o conjunto de dados filtrado.
- Disponível nas 3 visões: *Detalhamento Completo*, *Tabela Comparativa* e *Tarefas em Retenção*.

---

### 4. Motor de Regras Multi-Contagem (Item + Quantidade + LPN)

| Rodada de Contagem | Condição | Ação do Sistema | Ação Consequente |
| :--- | :--- | :--- | :--- |
| **1ª Contagem** | Divergência com o estoque esperado (`counted_qty != expected_qty`) | **Recusa (Auto-Reject)** | WMS gera nova tarefa; sistema retém automaticamente (**Hold**) |
| **1ª Contagem** | Bate exatamente com o esperado (`counted_qty == expected_qty`) | **Aprova (Auto-Approve)** | Ajuste aprovado no WMS |
| **2ª Contagem** | Bate com a 1ª contagem (`Item`, `Qtd` e `LPN` idênticos) | **Aprova (Auto-Approve)** | Ajuste aprovado no WMS |
| **2ª Contagem** | Diverge da 1ª contagem | **Recusa (Auto-Reject)** | WMS gera nova tarefa; sistema retém automaticamente (**Hold**) |
| **3ª Contagem** | Bate com a 1ª contagem **OU** com a 2ª contagem | **Aprova (Auto-Approve)** | Ajuste aprovado no WMS |
| **3ª Contagem** | Diverge de ambas | **Recusa (Auto-Reject)** | Tarefa retida / enviada para revisão |

> [!NOTE]
> Se o campo `lpn_id` for nulo ou vazio, é padronizado como `"SEM LPN"` para comparação uniforme entre as contagens.

---

### 3. Visualização Aprimorada em 3 DataFrames

1. **📋 1. Detalhamento Completo (`cc_adjustment_dtl`)**:
   - Traz todas as colunas de detalhe: *Localização*, *LPN / Container*, *Item*, *Qtd Esperada*, *Qtd Contada*, *Diferença*, *Rodada (Seq)*, *Status Inicial WMS*, *Ação Definida*, *Resultado Execução*, *Grupo Ajuste*, *ID Cabeçalho*.
2. **📊 2. Tabela Comparativa (1ª, 2ª e 3ª Contagens)**:
   - Visão matricial lado a lado mostrando o histórico de cada contagem por *Localização*, *Item* e *LPN*.
3. **🔒 3. Novas Tarefas Retidas em Hold**:
   - Apresenta as tarefas de contagem que foram automaticamente identificadas e retidas no WMS com *ID*, *Número da Tarefa*, *Localização*, *Data de Criação* e *Usuário*.

---

## 🧪 Verificação e Validação

- **Compilação de Sintaxe**: Validada com sucesso (`py_compile main.py`).
- **Testes de Lógica Multi-Contagem**:
  - Testado cenário com 1ª contagem igual ao esperado -> `Auto-Approve`.
  - Testado cenário com 1ª contagem divergente -> `Auto-Reject`.
  - Testado cenário com 2ª contagem igual à 1ª -> `Auto-Approve`.
  - Testado cenário com 2ª contagem diferente da 1ª -> `Auto-Reject`.
  - Testado cenário com 3ª contagem igual à 2ª -> `Auto-Approve`.
  - Testado cenário onde a quantidade bate mas o LPN muda -> Detectado como divergente -> `Auto-Reject`.
  - Tabela comparativa renderizou 100% dos registros sem omissão de linhas.
