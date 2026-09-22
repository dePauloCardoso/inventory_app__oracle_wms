import json
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, date, time
from requests.auth import HTTPBasicAuth

# --- CONFIGURAÇÕES ---
BASE_URL = "https://k1.wms.ocs.oraclecloud.com:443/arcoed/wms/lgfapi/v10/entity"
USERS_FILE = "users.xlsx"

st.set_page_config(
    page_title="Distribuição de Tarefas WMS - Status 5",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- AUTENTICAÇÃO ---
def login_screen():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        st.title("Distribuição de Tarefas WMS - Status 5")
        with st.form("login_form"):
            username = st.text_input("Usuário WMS")
            password = st.text_input("Senha WMS", type="password")
            submit_button = st.form_submit_button("Entrar")

            if submit_button:
                if username and password:
                    st.session_state.username = username
                    st.session_state.password = password
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("Por favor, preencha usuário e senha.")
        st.stop()

def get_session():
    session = requests.Session()
    session.auth = HTTPBasicAuth(st.session_state.username, st.session_state.password)
    session.headers.update({"Content-Type": "application/json"})
    return session

# --- CARREGAMENTO DE USUÁRIOS (users.xlsx) ---
@st.cache_data
def load_users(filepath=USERS_FILE):
    try:
        df = pd.read_excel(filepath)
        df['USUARIO'] = df['USUARIO'].astype(str).str.strip()
        df['COLABORADOR'] = df['COLABORADOR'].astype(str).str.strip()
        return df
    except Exception as e:
        st.error(f"Erro ao carregar o arquivo de usuários ({filepath}): {e}")
        return pd.DataFrame()

# --- EXTRAÇÃO DE RUA E NÍVEL ---
def extract_location_parts(loc_key):
    """
    Extrai a Rua (Corredor) e Nível (Level) da chave do local.
    Exemplo: 'EST-RA2-150-2' -> Rua: 'RA2', Nível: '2'
    """
    if not loc_key or not isinstance(loc_key, str):
        return {"aisle": "OUTROS", "level": "1"}
    parts = loc_key.split("-")
    if len(parts) >= 4:
        return {
            "aisle": parts[1].strip(),
            "level": parts[3].strip()
        }
    elif len(parts) >= 2:
        return {
            "aisle": parts[1].strip(),
            "level": "1"
        }
    return {"aisle": "OUTROS", "level": "1"}

# --- BUSCA DE TAREFAS (STATUS 5 E SEM USUÁRIO) ---
def fetch_unassigned_status5_tasks(start_ts=None):
    """
    Busca tarefas do tipo 19 estritamente no status_id = 5 (Retidas) 
    e sem usuário atribuído.
    """
    session = get_session()
    url = f"{BASE_URL}/task/?facility_id=4&task_type_id=19&status_id=5&page_mode=paged"
    
    if start_ts:
        url += f"&create_ts__gte={start_ts}"

    all_results = []
    with st.spinner("Buscando tarefas retidas (Status 5) sem usuário no WMS..."):
        res = session.get(url)

        # Trata 404 como 'Nenhum registro encontrado' no WMS
        if res.status_code == 404:
            return pd.DataFrame()
        elif res.status_code != 200:
            st.error(f"Erro na requisição WMS (Status {res.status_code}): {res.text}")
            return pd.DataFrame()

        data = res.json()
        all_results.extend(data.get("results", []))

        page_count = data.get("page_count", 1)
        for page in range(2, min(page_count + 1, 30)):
            p_url = f"{url}&page={page}"
            p_res = session.get(p_url)
            if p_res.status_code == 200:
                all_results.extend(p_res.json().get("results", []))

    if not all_results:
        return pd.DataFrame()

    df_tasks = pd.json_normalize(all_results)

    # Extrai chave do local
    if "next_location_id.key" in df_tasks.columns:
        df_tasks["location_key"] = df_tasks["next_location_id.key"].astype(str)
    else:
        df_tasks["location_key"] = "DESCONHECIDO"

    # Extrai Rua (Corredor) e Nível
    loc_parts = df_tasks["location_key"].apply(extract_location_parts)
    df_tasks["aisle"] = loc_parts.apply(lambda x: x["aisle"])
    df_tasks["level"] = loc_parts.apply(lambda x: x["level"])

    # Filtro estrito: apenas tarefas SEM usuário atribuído
    if "assigned_user" in df_tasks.columns:
        df_tasks = df_tasks[
            df_tasks["assigned_user"].isna() |
            (df_tasks["assigned_user"] == "") |
            (df_tasks["assigned_user"] == "None")
        ].copy()

    return df_tasks

# --- BUSCA HISTÓRICO PARA DETECTAR CONTADORAS ANTERIORES E ETAPA ---
def fetch_location_history(location_keys):
    """
    Consulta o histórico de tarefas do local para mapear usuários prévios 
    e determinar se o local está em 1ª, 2ª ou 3ª+ contagem.
    """
    session = get_session()
    history_map = {}

    if not location_keys:
        return history_map

    unique_locs = list(set(location_keys))
    chunk_size = 30

    with st.spinner("Analisando histórico de contagens prévias dos locais..."):
        for i in range(0, len(unique_locs), chunk_size):
            chunk = unique_locs[i : i + chunk_size]
            loc_str = ",".join(chunk)
            url = f"{BASE_URL}/task/?facility_id=4&task_type_id=19&next_location_id__key__in={loc_str}"

            res = session.get(url)
            if res.status_code == 200:
                tasks = res.json().get("results", [])
                for t in tasks:
                    loc = t.get("next_location_id", {}).get("key")
                    assigned = t.get("assigned_user")
                    mod_user = t.get("mod_user")
                    create_user = t.get("create_user")
                    status = t.get("status_id")

                    if loc not in history_map:
                        history_map[loc] = set()

                    if assigned and str(assigned).strip().lower() != "none":
                        history_map[loc].add(assigned.strip().lower())
                    if status in [20, 90] and mod_user and str(mod_user).strip().lower() != "none":
                        history_map[loc].add(mod_user.strip().lower())
                    if create_user and str(create_user).strip().lower() != "none":
                        history_map[loc].add(create_user.strip().lower())

    return history_map

# --- ALGORITMO DE ATRIBUIÇÃO ---
def plan_task_assignments(df_tasks, history_map, active_users, selected_aisles=None, selected_levels=None, selected_cycles=None, tasks_per_user_per_level=10):
    df_filtered = df_tasks.copy()

    # Determina a Etapa da Contagem (1ª, 2ª ou 3ª+)
    def get_cycle_info(loc_key):
        prev_users = history_map.get(loc_key, set())
        cycle_num = len(prev_users) + 1
        if cycle_num == 1:
            stage_name = "1ª Contagem"
        elif cycle_num == 2:
            stage_name = "2ª Contagem"
        else:
            stage_name = f"⚠️ {cycle_num}ª Contagem (Especial)"
        return cycle_num, stage_name, prev_users

    cycle_info = df_filtered["location_key"].apply(get_cycle_info)
    df_filtered["cycle_num"] = [c[0] for c in cycle_info]
    df_filtered["cycle_stage"] = [c[1] for c in cycle_info]
    df_filtered["prev_users"] = [c[2] for c in cycle_info]

    # Filtro por Rua (Corredores)
    if selected_aisles:
        df_filtered = df_filtered[df_filtered["aisle"].isin(selected_aisles)]

    # Filtro por Níveis (Multiselect: aceita 1, 2 ou mais níveis simultaneamente)
    if selected_levels:
        df_filtered = df_filtered[df_filtered["level"].isin(selected_levels)]

    # Filtro por Etapa/Ciclo da Contagem
    if selected_cycles:
        # Mapeia opções amigáveis do filtro para números do ciclo
        cycle_nums_to_keep = []
        if "1ª Contagem" in selected_cycles:
            cycle_nums_to_keep.append(1)
        if "2ª Contagem" in selected_cycles:
            cycle_nums_to_keep.append(2)
        if "3ª+ Contagem (Especial)" in selected_cycles:
            df_filtered_cycles = df_filtered[
                df_filtered["cycle_num"].isin(cycle_nums_to_keep) | (df_filtered["cycle_num"] >= 3)
            ]
        else:
            df_filtered_cycles = df_filtered[df_filtered["cycle_num"].isin(cycle_nums_to_keep)]
        df_filtered = df_filtered_cycles

    user_assignments = {u: [] for u in active_users}
    conflicts_prevented = 0
    assigned_rows = []

    # Agrupa por Nível para garantir até N tarefas por usuário por nível
    grouped_levels = df_filtered.groupby("level")

    for level_val, df_level in grouped_levels:
        user_level_counts = {u: 0 for u in active_users}

        for _, task in df_level.iterrows():
            task_id = task["id"]
            task_nbr = task.get("task_nbr", "")
            loc_key = task["location_key"]
            aisle_val = task["aisle"]
            prev_users = task["prev_users"]
            cycle_stage = task["cycle_stage"]

            for user in active_users:
                user_lower = user.strip().lower()

                # Limite de tarefas por nível para o usuário
                if user_level_counts[user] >= tasks_per_user_per_level:
                    continue

                # Regra Anti-Repetição: Usuário não pode ter contado o local anteriormente
                if user_lower in prev_users:
                    conflicts_prevented += 1
                    continue

                # Atribuição aprovada
                task_info = {
                    "task_id": task_id,
                    "task_nbr": task_nbr,
                    "location_key": loc_key,
                    "aisle": aisle_val,
                    "level": level_val,
                    "cycle_stage": cycle_stage,
                    "assigned_user": user,
                    "previous_count_users": ", ".join(prev_users) if prev_users else "Nenhum (1ª Contagem)"
                }

                user_assignments[user].append(task_info)
                user_level_counts[user] += 1
                assigned_rows.append(task_info)
                break

    df_planned = pd.DataFrame(assigned_rows) if assigned_rows else pd.DataFrame()
    return user_assignments, df_planned, conflicts_prevented, df_filtered

# --- EXECUÇÃO EM LOTE (API ORACLE WMS) ---
def execute_bulk_assign(user_assignments):
    """Atribui usuários em lote via POST /entity/task/bulk_assign_user/"""
    session = get_session()
    bulk_url = f"{BASE_URL}/task/bulk_assign_user/"

    total_success = 0
    total_failures = 0
    results_details = {}
    assigned_task_ids = []

    for user, tasks in user_assignments.items():
        if not tasks:
            continue

        task_ids = [int(t["task_id"]) for t in tasks]
        payload = {
            "parameters": {
                "id__in": task_ids
            },
            "options": {
                "assigned_user": user,
                "commit_frequency": "0"
            }
        }

        res = session.post(bulk_url, json=payload)

        if res.status_code == 200:
            res_data = res.json()
            succ = res_data.get("success_count", len(task_ids))
            fail = res_data.get("failure_count", 0)
            total_success += succ
            total_failures += fail
            results_details[user] = f"Atribuição Ok: {succ}, Falhas: {fail}"
            assigned_task_ids.extend(task_ids)
        else:
            # Fallback individual caso o bulk falhe
            succ, fail = 0, 0
            for tid in task_ids:
                single_url = f"{BASE_URL}/task/assign_user/"
                single_payload = {
                    "parameters": {"id": tid},
                    "options": {"assigned_user": user}
                }
                s_res = session.post(single_url, json=single_payload)
                if s_res.status_code in [200, 204]:
                    succ += 1
                    assigned_task_ids.append(tid)
                else:
                    fail += 1

            total_success += succ
            total_failures += fail
            results_details[user] = f"Fallback -> Atribuição Ok: {succ}, Falhas: {fail}"

    return total_success, total_failures, results_details, assigned_task_ids

def execute_bulk_release(task_ids):
    """Libera tarefas em lote via POST /entity/task/bulk_release/"""
    session = get_session()
    release_url = f"{BASE_URL}/task/bulk_release/"

    payload = {
        "parameters": {
            "id__in": [int(tid) for tid in task_ids]
        },
        "options": {
            "commit_frequency": "0"
        }
    }

    res = session.post(release_url, json=payload)

    if res.status_code == 200:
        return res.json()
    else:
        success_count, failure_count = 0, 0
        details = {}

        for tid in task_ids:
            single_url = f"{BASE_URL}/task/{tid}/release/"
            single_res = session.post(single_url)
            if single_res.status_code in [200, 204]:
                success_count += 1
            else:
                failure_count += 1
                details[str(tid)] = f"Status {single_res.status_code}"

        return {
            "record_count": len(task_ids),
            "success_count": success_count,
            "failure_count": failure_count,
            "details": details if details else None
        }

# --- INTERFACE PRINCIPAL ---
def main():
    login_screen()

    st.sidebar.title(f"Usuário WMS: {st.session_state.username}")
    if st.sidebar.button("Sair (Logout)"):
        st.session_state.clear()
        st.rerun()

    st.title("Distribuição de Tarefas Retidas (Status 5)")
    st.markdown("Filtre por **Rua**, **Níveis (Múltiplos)** e **Etapa da Contagem**, garantindo a distribuição correta entre os usuários.")

    # 1. Carrega Cadastro de Usuários do Excel
    df_users = load_users()
    if df_users.empty:
        st.warning("Nenhum usuário encontrado em `users.xlsx`.")
        st.stop()

    all_usernames = df_users["USUARIO"].dropna().unique().tolist()

    # --- BARRA LATERAL: CONFIGURAÇÕES E FILTRO DE USUÁRIOS ---
    st.sidebar.write("---")
    st.sidebar.subheader("Configurações de Atribuição")

    tasks_per_user = st.sidebar.number_input(
        "Meta de Tarefas por Usuário por Nível:",
        min_value=1, max_value=50, value=10, step=1
    )

    # Filtro de Data Inicial
    use_date_filter = st.sidebar.checkbox("Aplicar Filtro de Data Inicial (`create_ts__gte`)", value=True)
    start_ts = None
    if use_date_filter:
        start_d = st.sidebar.date_input("Data Inicial", value=date.today())
        start_t = st.sidebar.time_input("Hora Inicial", value=time(0, 0, 1))
        start_ts = f"{start_d.isoformat()}T{start_t.strftime('%H:%M:%S')}"

    st.sidebar.write("---")
    st.sidebar.subheader("Operadores Ativos para Atribuição")
    selected_users = st.sidebar.multiselect(
        "Selecione os operadores elegíveis hoje:",
        options=all_usernames,
        default=all_usernames[:5] if len(all_usernames) >= 5 else all_usernames
    )

    # 2. Busca e Processamento de Tarefas
    if st.button("Buscar Tarefas Retidas e Planejar Atribuição", type="primary"):
        if not selected_users:
            st.error("Por favor, selecione ao menos um operador ativo na barra lateral.")
            st.stop()

        # Busca estritamente status_id = 5 e sem usuário
        df_tasks = fetch_unassigned_status5_tasks(start_ts=start_ts)

        if df_tasks.empty:
            st.info("Nenhuma tarefa retida (Status 5) sem usuário encontrada no WMS para os parâmetros atuais.")
            st.stop()

        st.session_state.df_tasks = df_tasks

        location_keys = df_tasks["location_key"].tolist()
        history_map = fetch_location_history(location_keys)
        st.session_state.history_map = history_map

        st.rerun()

    # 3. Exibição de Filtros Avançados e Resultados
    if "df_tasks" in st.session_state and not st.session_state.df_tasks.empty:
        df_tasks = st.session_state.df_tasks
        history_map = st.session_state.history_map

        st.write("---")
        st.subheader("Filtros de Localização e Etapa de Contagem")

        available_aisles = sorted(df_tasks["aisle"].unique().tolist())
        available_levels = sorted(df_tasks["level"].unique().tolist())

        col_f1, col_f2, col_f3 = st.columns(3)

        with col_f1:
            selected_aisles = st.multiselect(
                "Filtrar por Rua (Corredor):",
                options=available_aisles,
                default=available_aisles,
                help="Selecione as ruas desejadas para atribuição."
            )

        with col_f2:
            # Multiselect de Níveis (Permite selecionar 2 ou mais níveis simultaneamente)
            selected_levels = st.multiselect(
                "Filtrar por Níveis (Dois ou Mais):",
                options=available_levels,
                default=available_levels,
                help="Você pode selecionar um, dois ou múltiplos níveis para filtrar juntos."
            )

        with col_f3:
            cycle_options = ["1ª Contagem", "2ª Contagem", "3ª+ Contagem (Especial)"]
            selected_cycles = st.multiselect(
                "Filtrar por Etapa da Contagem:",
                options=cycle_options,
                default=cycle_options,
                help="Selecione as etapas que deseja atribuir nesta rodada."
            )

        user_assignments, df_planned, conflicts, df_filtered = plan_task_assignments(
            df_tasks, history_map, selected_users,
            selected_aisles=selected_aisles,
            selected_levels=selected_levels,
            selected_cycles=selected_cycles,
            tasks_per_user_per_level=tasks_per_user
        )

        st.write("---")
        st.subheader("Resumo das Tarefas Retidas Encontradas")

        cnt_1st = len(df_filtered[df_filtered["cycle_num"] == 1])
        cnt_2nd = len(df_filtered[df_filtered["cycle_num"] == 2])
        cnt_3rd = len(df_filtered[df_filtered["cycle_num"] >= 3])

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total Status 5 Filtrado", len(df_filtered))
        m2.metric("1ª Contagem", cnt_1st)
        m3.metric("2ª Contagem", cnt_2nd)
        m4.metric("⚠️ 3ª+ Contagem", cnt_3rd)
        m5.metric("Tarefas Planejadas", len(df_planned) if not df_planned.empty else 0)

        if cnt_3rd > 0:
            st.warning(f"Existem **{cnt_3rd} tarefas em 3ª contagem ou superior** nos filtros selecionados. Você pode filtrar apenas '3ª+ Contagem' para destiná-las a operadores específicos.")

        if not df_planned.empty:
            st.write("### Resumo de Tarefas Atribuídas por Operador")
            summary_df = df_planned.groupby(["assigned_user", "aisle", "level", "cycle_stage"]).agg(
                quantidade_tarefas=("task_id", "count")
            ).reset_index()
            st.dataframe(summary_df, use_container_width=True)

            st.write("### Detalhamento Completo do Planejamento")
            st.dataframe(
                df_planned[[
                    "assigned_user", "cycle_stage", "aisle", "level", "task_nbr", "location_key",
                    "previous_count_users"
                ]],
                use_container_width=True
            )

            st.write("---")
            if st.button("Confirmar: Atribuir e Liberar Tarefas (Bulk Assign & Release)", type="primary"):
                with st.spinner("1/2. Atribuindo tarefas aos operadores no WMS..."):
                    total_succ, total_fail, assign_details, assigned_ids = execute_bulk_assign(user_assignments)

                if assigned_ids:
                    with st.spinner("2/2. Liberando tarefas no WMS (Bulk Release de Status 5 para Status 10)..."):
                        release_res = execute_bulk_release(assigned_ids)

                    st.success(f"Processo Concluído! Tarefas Atribuídas: {total_succ} | Tarefas Liberadas: {release_res.get('success_count', 0)}")

                    col_a, col_r = st.columns(2)
                    with col_a:
                        st.write("**Detalhes da Atribuição:**")
                        st.json(assign_details)
                    with col_r:
                        st.write("**Detalhes da Liberação (Release):**")
                        st.json(release_res)
                else:
                    st.error("Nenhuma tarefa foi atribuída com sucesso. Liberação cancelada.")
        else:
            st.warning("Nenhuma tarefa pôde ser planejada para os operadores ativos com os filtros selecionados.")

if __name__ == "__main__":
    main()