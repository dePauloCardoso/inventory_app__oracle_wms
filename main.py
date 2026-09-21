import json
import time
from datetime import datetime, timedelta
import pandas as pd
import requests
import streamlit as st
from requests.auth import HTTPBasicAuth

# --- CONFIGURATION ---
BASE_URL = "https://tk1.wms.ocs.oraclecloud.com:443/arcoed_test/wms/lgfapi/v10/entity"

st.set_page_config(
    page_title="WMS Cycle Count Automation",
    page_icon="📦",
    layout="wide"
)

# Custom CSS for modern styling
st.markdown("""
<style>
    .metric-card {
        background-color: #f8f9fa;
        border-radius: 8px;
        padding: 12px 18px;
        border: 1px solid #e9ecef;
    }
    .stButton>button {
        font-weight: 600;
        border-radius: 6px;
    }
</style>
""", unsafe_allow_html=True)


# --- AUTHENTICATION ---
def login_screen():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        st.title("📦 WMS Cycle Count - Automação Total")
        st.info("Entre com suas credenciais do Oracle WMS e defina o período de tarefas a processar.")
        with st.form("login_form"):
            col_u, col_p = st.columns(2)
            with col_u:
                username = st.text_input("Username")
            with col_p:
                password = st.text_input("Password", type="password")

            st.write("---")
            st.markdown("##### 📅 Filtro Inicial de Período das Tarefas")
            col_d1, col_d2 = st.columns(2)
            today = datetime.now().date()
            with col_d1:
                start_date_input = st.date_input("Data Inicial", value=today - timedelta(days=3))
            with col_d2:
                end_date_input = st.date_input("Data Final", value=today)

            submit_button = st.form_submit_button("Entrar no Sistema")

            if submit_button:
                if username and password:
                    st.session_state.username = username
                    st.session_state.password = password
                    st.session_state.filter_start_date = start_date_input
                    st.session_state.filter_end_date = end_date_input
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("Por favor, forneça usuário e senha.")
        st.stop()


# --- API HELPER FUNCTIONS ---
def get_session():
    session = requests.Session()
    session.auth = HTTPBasicAuth(st.session_state.username, st.session_state.password)
    session.headers.update({"Content-Type": "application/json"})
    return session


def fetch_headers(facility_id=4, create_ts_gte=None, create_ts_lte=None):
    """
    Busca todos os cabeçalhos de ajuste de contagem cíclica (com paginação).
    """
    session = get_session()
    hdr_data = []
    params = [
        f"facility_id={facility_id}",
        "rf_screen_name=Cycle Cnt %7Blocn%7D Contents",
        "page_size=100"
    ]
    if create_ts_gte:
        params.append(f"create_ts__gte={create_ts_gte}")
    if create_ts_lte:
        params.append(f"create_ts__lte={create_ts_lte}")

    url = f"{BASE_URL}/cc_adjustment_hdr?{'&'.join(params)}"

    while url:
        resp = session.get(url)
        if resp.status_code != 200:
            st.error(f"Falha ao buscar cabeçalhos! Status: {resp.status_code} - {resp.text}")
            break
        data = resp.json()
        results = data.get("results", [])
        hdr_data.extend(results)
        url = data.get("next_page")

    return hdr_data


def fetch_details(hdr_ids):
    """
    Busca os detalhes de contagem em lotes de IDs de cabeçalho (com paginação).
    """
    session = get_session()
    dtl_data = []
    chunk_size = 50

    for i in range(0, len(hdr_ids), chunk_size):
        batch_ids = hdr_ids[i : i + chunk_size]
        url = f"{BASE_URL}/cc_adjustment_dtl?cc_adjustment_hdr_id__in={','.join(batch_ids)}&page_size=100"
        while url:
            resp = session.get(url)
            if resp.status_code == 200:
                data = resp.json()
                dtl_data.extend(data.get("results", []))
                url = data.get("next_page")
            else:
                st.warning(f"Falha ao buscar lote de detalhes: Status {resp.status_code}")
                break

    return dtl_data


def bulk_approve_headers(df_approve, facility_id=4):
    """
    Executa POST para /cc_adjustment_hdr/bulk_approve/ com fallback individual.
    """
    session = get_session()
    approve_url = f"{BASE_URL}/cc_adjustment_hdr/bulk_approve/"
    group_numbers = df_approve["group_nbr"].dropna().astype(int).unique().tolist()

    if not group_numbers:
        return None

    payload = {
        "parameters": {
            "facility_id": facility_id,
            "group_nbr__in": group_numbers
        },
        "options": {
            "comment": "Aprovado automaticamente pelo sistema (Contagens coincidem)",
            "commit_frequency": 1
        }
    }

    response = session.post(approve_url, json=payload)

    # Fallback individual caso o endpoint em lote retorne erro
    if response.status_code != 200:
        st.warning("Bulk approve falhou. Executando aprovação individual por group_nbr...")
        success_count, failure_count, details = 0, 0, {}
        unique_groups_df = df_approve.drop_duplicates(subset=["group_nbr"])

        for _, row in unique_groups_df.iterrows():
            single_url = f"{BASE_URL}/cc_adjustment_hdr/approve/"
            single_payload = {
                "parameters": {
                    "facility_id": int(row.get("facility_id.id", facility_id)),
                    "location_barcode": str(row.get("location_id.key", "")),
                    "group_nbr": int(row["group_nbr"])
                },
                "options": {
                    "comment": "Aprovado automaticamente pelo sistema"
                }
            }
            single_res = session.post(single_url, json=single_payload)
            if single_res.status_code in [200, 204]:
                success_count += 1
            else:
                failure_count += 1
                details[str(row["group_nbr"])] = f"Status {single_res.status_code}"

        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_payload = {
            "record_count": len(unique_groups_df),
            "success_count": success_count,
            "failure_count": failure_count,
            "details": details if details else None
        }
        mock_response._content = json.dumps(mock_payload).encode('utf-8')
        return mock_response

    return response


def bulk_reject_headers(df_reject, facility_id=4):
    """
    Executa POST para /cc_adjustment_hdr/bulk_reject/ com fallback individual.
    """
    session = get_session()
    reject_url = f"{BASE_URL}/cc_adjustment_hdr/bulk_reject/"
    group_numbers = df_reject["group_nbr"].dropna().astype(int).unique().tolist()

    if not group_numbers:
        return None

    payload = {
        "parameters": {
            "facility_id": facility_id,
            "group_nbr__in": group_numbers
        },
        "options": {
            "comment": "Rejeitado automaticamente pelo sistema",
            "commit_frequency": 1
        }
    }

    response = session.post(reject_url, json=payload)

    # Fallback individual caso o endpoint em lote retorne erro
    if response.status_code != 200:
        st.warning("Bulk reject falhou. Executando rejeição individual por group_nbr...")
        success_count, failure_count, details = 0, 0, {}
        unique_groups_df = df_reject.drop_duplicates(subset=["group_nbr"])

        for _, row in unique_groups_df.iterrows():
            single_url = f"{BASE_URL}/cc_adjustment_hdr/reject/"
            single_payload = {
                "parameters": {
                    "facility_id": int(row.get("facility_id.id", facility_id)),
                    "location_barcode": str(row.get("location_id.key", "")),
                    "group_nbr": int(row["group_nbr"])
                },
                "options": {
                    "comment": "Rejeitado automaticamente pelo sistema"
                }
            }
            single_res = session.post(single_url, json=single_payload)
            if single_res.status_code in [200, 204]:
                success_count += 1
            else:
                failure_count += 1
                details[str(row["group_nbr"])] = f"Status {single_res.status_code}"

        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_payload = {
            "record_count": len(unique_groups_df),
            "success_count": success_count,
            "failure_count": failure_count,
            "details": details if details else None
        }
        mock_response._content = json.dumps(mock_payload).encode('utf-8')
        return mock_response

    return response


def fetch_ready_tasks(create_ts_gte, facility_id=4, create_ts_lte=None):
    """
    Busca tarefas CC em status Pronto (status_id=10, task_type_id=19).
    """
    session = get_session()
    tasks_data = []
    params = [
        f"facility_id={facility_id}",
        "task_type_id=19",
        "status_id=10",
        f"create_ts__gte={create_ts_gte}",
        "page_size=100"
    ]
    if create_ts_lte:
        params.append(f"create_ts__lte={create_ts_lte}")

    url = f"{BASE_URL}/task/?{'&'.join(params)}"

    while url:
        resp = session.get(url)
        if resp.status_code != 200:
            break
        data = resp.json()
        tasks_data.extend(data.get("results", []))
        url = data.get("next_page")

    if not tasks_data:
        return pd.DataFrame()

    return pd.json_normalize(tasks_data)


def bulk_hold_tasks(task_ids):
    """
    Coloca tarefas em retenção usando POST /task/bulk_hold/ com fallback individual.
    """
    session = get_session()
    hold_url = f"{BASE_URL}/task/bulk_hold/"

    if not task_ids:
        return None

    payload = {
        "parameters": {
            "id__in": [int(tid) for tid in task_ids]
        },
        "options": {
            "commit_frequency": 1
        }
    }

    response = session.post(hold_url, json=payload)

    if response.status_code != 200:
        st.warning("Bulk hold falhou. Executando retenção individual de tarefas...")
        success_count, failure_count, details = 0, 0, {}
        for tid in task_ids:
            single_url = f"{BASE_URL}/task/{tid}/hold/"
            single_res = session.post(single_url)
            if single_res.status_code in [200, 204]:
                success_count += 1
            else:
                failure_count += 1
                details[str(tid)] = f"Status {single_res.status_code}"

        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_payload = {
            "record_count": len(task_ids),
            "success_count": success_count,
            "failure_count": failure_count,
            "details": details if details else None
        }
        mock_response._content = json.dumps(mock_payload).encode('utf-8')
        return mock_response

    return response


# --- STATUS TRANSLATION CONSTANTS ---
STATUS_MAP_PT = {
    10: "Em Andamento",    # In Progress
    20: "Pendente",        # Pending
    30: "Aprovado",        # Approved
    50: "Rejeitado",       # Rejected
    70: "Sem Divergência", # No Variance
    99: "Cancelado"        # Cancelled
}

def translate_status_id(val):
    """
    Traduz o status numérico do Oracle WMS para a descrição oficial em português.
    """
    try:
        if pd.isna(val) or val is None:
            return "Não Informado"
        s = int(float(str(val).strip()))
        return STATUS_MAP_PT.get(s, f"Status {s}")
    except (ValueError, TypeError):
        mapping = {
            "in progress": "Em Andamento",
            "pending": "Pendente",
            "approved": "Aprovado",
            "rejected": "Rejeitado",
            "no variance": "Sem Divergência",
            "cancelled": "Cancelado",
            "canceled": "Cancelado"
        }
        return mapping.get(str(val).strip().lower(), str(val))


# --- DATA PREPARATION & MULTI-COUNT LOGIC ---
def normalize_lpn(val):
    if pd.isna(val) or val is None:
        return "SEM LPN"
    s = str(val).strip()
    if s in ("", "None", "nan", "null", "0", "0.0"):
        return "SEM LPN"
    return s


def evaluate_multi_count(df):
    """
    Executa a checagem no nível de detalhe (Item, Quantidade, LPN/Container)
    e define as ações automáticas de 1ª, 2ª e 3ª contagens.
    """
    if df.empty:
        return df, pd.DataFrame()

    df = df.copy()
    if "expected_qty" not in df.columns:
        df["expected_qty"] = 0
    else:
        df["expected_qty"] = pd.to_numeric(df["expected_qty"], errors="coerce").fillna(0)

    if "counted_qty" not in df.columns:
        df["counted_qty"] = 0
    else:
        df["counted_qty"] = pd.to_numeric(df["counted_qty"], errors="coerce").fillna(0)

    df["qty_diff"] = df["counted_qty"] - df["expected_qty"]

    # Extrai e normaliza LPN / Container de forma robusta por linha
    def get_row_lpn(row):
        for col in ["lpn", "lpn_id.key", "lpn_id", "container_nbr", "container_id.key"]:
            if col in row and pd.notna(row[col]):
                val = str(row[col]).strip()
                if val not in ("", "None", "nan", "null", "0", "0.0"):
                    return val
        return "SEM LPN"

    df["lpn"] = df.apply(get_row_lpn, axis=1)

    # Extrai e normaliza Item
    def get_row_item(row):
        for col in ["item_id.key", "item_id", "item_key", "item_nbr"]:
            if col in row and pd.notna(row[col]):
                val = str(row[col]).strip()
                if val not in ("", "None", "nan"):
                    return val
        return "ITEM_DESCONHECIDO"

    df["item_id.key"] = df.apply(get_row_item, axis=1)

    # Extrai e normaliza Localização
    def get_row_loc(row):
        for col in ["location_id.key", "location_id", "location_barcode"]:
            if col in row and pd.notna(row[col]):
                val = str(row[col]).strip()
                if val not in ("", "None", "nan"):
                    return val
        return "LOC_DESCONHECIDA"

    df["location_id.key"] = df.apply(get_row_loc, axis=1)

    # Ordena data de criação dos cabeçalhos
    if "create_ts_hdr" in df.columns:
        df["create_ts_hdr_dt"] = pd.to_datetime(df["create_ts_hdr"], errors="coerce")
    else:
        df["create_ts_hdr_dt"] = pd.to_datetime(df.get("create_ts", pd.Timestamp.now()))

    # Mapeia a sequência cronológica de contagem por Localização (1ª, 2ª, 3ª...)
    hdr_order = (
        df[["location_id.key", "hdr_id", "create_ts_hdr_dt"]]
        .drop_duplicates()
        .sort_values(by=["location_id.key", "create_ts_hdr_dt", "hdr_id"])
    )
    hdr_order["count_sequence"] = hdr_order.groupby("location_id.key").cumcount() + 1
    hdr_seq_map = dict(zip(hdr_order["hdr_id"], hdr_order["count_sequence"]))
    df["count_sequence"] = df["hdr_id"].map(hdr_seq_map).fillna(1).astype(int)

    # Adiciona status traduzido em português
    if "status_id_hdr" in df.columns:
        df["status_wms_pt"] = df["status_id_hdr"].apply(translate_status_id)
    elif "status_id" in df.columns:
        df["status_wms_pt"] = df["status_id"].apply(translate_status_id)
    else:
        df["status_wms_pt"] = "Não Informado"

    # Avaliação das Regras Multi-Contagem no nível de detalhe (Item, Qtd, LPN)
    hdr_action_map = {}

    for loc, loc_df in df.groupby("location_id.key"):
        loc_hdrs = (
            loc_df[["hdr_id", "count_sequence", "create_ts_hdr_dt", "status_id_hdr"]]
            .drop_duplicates()
            .sort_values(by="count_sequence")
        )

        profiles = {}
        expected_profiles = {}

        for _, h_row in loc_hdrs.iterrows():
            h_id = h_row["hdr_id"]
            seq = h_row["count_sequence"]
            status = int(h_row.get("status_id_hdr", 0) or 0)

            h_dtls = loc_df[loc_df["hdr_id"] == h_id]

            # Constrói o perfil de detalhes: {(item, lpn): quantidade}
            counted_profile = {}
            expected_profile = {}
            for _, d in h_dtls.iterrows():
                key = (str(d["item_id.key"]), str(d["lpn"]))
                counted_profile[key] = float(d["counted_qty"])
                expected_profile[key] = float(d["expected_qty"])

            profiles[seq] = counted_profile
            expected_profiles[seq] = expected_profile

            # Status prévio no WMS (em português)
            if status == 30:
                hdr_action_map[h_id] = "Já Aprovado"
                continue
            elif status == 50:
                hdr_action_map[h_id] = "Já Rejeitado"
                continue
            elif status != 20:
                hdr_action_map[h_id] = translate_status_id(status)
                continue

            # Status 20 (Pendente) -> Aplicação das Regras:
            if seq == 1:
                # 1ª CONTAGEM: Se der divergência com o esperado, recusa. Se bater, aprova.
                has_diff = any(
                    counted_profile.get(k, 0.0) != expected_profile.get(k, 0.0)
                    for k in set(counted_profile.keys()) | set(expected_profile.keys())
                )
                if has_diff:
                    hdr_action_map[h_id] = "Rejeição Automática"
                else:
                    hdr_action_map[h_id] = "Aprovação Automática"

            elif seq == 2:
                # 2ª CONTAGEM: Se a segunda bater com a primeira (Item, Qtd, LPN), aprova. Senão, recusa.
                p1 = profiles.get(1, {})
                matches_p1 = (counted_profile == p1)
                if matches_p1:
                    hdr_action_map[h_id] = "Aprovação Automática"
                else:
                    hdr_action_map[h_id] = "Rejeição Automática"

            elif seq == 3:
                # 3ª CONTAGEM: Se a terceira bater com a 1ª OU com a 2ª, aprova. Senão, recusa.
                p1 = profiles.get(1, {})
                p2 = profiles.get(2, {})
                matches_p1 = (counted_profile == p1)
                matches_p2 = (counted_profile == p2)
                if matches_p1 or matches_p2:
                    hdr_action_map[h_id] = "Aprovação Automática"
                else:
                    hdr_action_map[h_id] = "Rejeição Automática"
            else:
                hdr_action_map[h_id] = "Revisão Necessária"

    df["system_action"] = df["hdr_id"].map(hdr_action_map).fillna("Revisão Necessária")

    # Tabela comparativa (Localização, Item, LPN x 1ª, 2ª, 3ª contagens)
    pivot_df = df.pivot_table(
        index=["location_id.key", "item_id.key", "lpn"],
        columns="count_sequence",
        values="counted_qty",
        aggfunc="first"
    ).reset_index()

    col_rename = {1: "1ª Contagem", 2: "2ª Contagem", 3: "3ª Contagem"}
    pivot_df = pivot_df.rename(columns={c: col_rename[c] for c in pivot_df.columns if c in col_rename})

    for c in ["1ª Contagem", "2ª Contagem", "3ª Contagem"]:
        if c not in pivot_df.columns:
            pivot_df[c] = None

    latest_df = (
        df.sort_values(by=["location_id.key", "count_sequence"])
        .groupby(["location_id.key", "item_id.key", "lpn"])
        .last()
        .reset_index()
    )

    comp_df = pd.merge(
        pivot_df,
        latest_df[["location_id.key", "item_id.key", "lpn", "expected_qty", "system_action"]],
        on=["location_id.key", "item_id.key", "lpn"],
        how="left"
    )

    # Formata nome das colunas comparativas
    comp_df = comp_df.rename(columns={
        "location_id.key": "Localização",
        "item_id.key": "Item",
        "lpn": "LPN / Container",
        "expected_qty": "Qtd Esperada",
        "system_action": "Ação Final"
    })

    return df, comp_df


# --- AUTOMATED PIPELINE ---
def run_automated_pipeline(facility_id, create_ts_gte, create_ts_lte=None):
    """
    Executa o pipeline completo:
    1. Busca cabeçalhos e detalhes.
    2. Avalia as contagens com base em Item, Quantidade e LPN.
    3. Executa bulk_approve para os elegíveis.
    4. Executa bulk_reject para os divergentes.
    5. Ao rejeitar, busca novas tarefas geradas e executa bulk_hold automaticamente.
    6. Atualiza os DataFrames com os resultados da execução.
    """
    # 1. Busca cabeçalhos
    hdr_data = fetch_headers(facility_id, create_ts_gte, create_ts_lte)
    if not hdr_data:
        return {
            "status": "empty",
            "message": "Nenhum ajuste de contagem encontrado para os critérios informados."
        }

    hdr_ids = [str(item["id"]) for item in hdr_data]
    dtl_data = fetch_details(hdr_ids)

    df_hdr = pd.json_normalize(hdr_data).rename(columns={"id": "hdr_id"})
    df_dtl = pd.json_normalize(dtl_data) if dtl_data else pd.DataFrame()

    if df_dtl.empty:
        return {
            "status": "empty",
            "message": "Cabeçalhos encontrados, mas nenhum registro de detalhe retornado."
        }

    # Merge cabeçalhos e detalhes
    df = pd.merge(
        df_dtl,
        df_hdr,
        left_on="cc_adjustment_hdr_id.id",
        right_on="hdr_id",
        how="left",
        suffixes=('_dtl', '_hdr')
    )

    # 2. Avaliação multi-contagem
    df_evaluated, comp_df = evaluate_multi_count(df)

    # 3. Filtra apenas pendentes (status_id_hdr == 20)
    pending_df = df_evaluated[df_evaluated["status_id_hdr"] == 20]
    df_to_approve = pending_df[pending_df["system_action"].isin(["Aprovação Automática", "Auto-Approve"])]
    df_to_reject = pending_df[pending_df["system_action"].isin(["Rejeição Automática", "Auto-Reject"])]

    execution_log = {
        "approved_count": 0,
        "approved_groups": [],
        "rejected_count": 0,
        "rejected_groups": [],
        "held_count": 0,
        "held_tasks": [],
        "df_held": pd.DataFrame()
    }

    # 4. Executa Aprovações Automáticas
    if not df_to_approve.empty:
        appr_res = bulk_approve_headers(df_to_approve, facility_id=facility_id)
        if appr_res and appr_res.status_code == 200:
            appr_data = appr_res.json()
            execution_log["approved_count"] = appr_data.get("success_count", len(df_to_approve["group_nbr"].unique()))
            execution_log["approved_groups"] = df_to_approve["group_nbr"].dropna().unique().tolist()

    # 5. Executa Rejeições Automáticas e Retenção de Novas Tarefas
    if not df_to_reject.empty:
        # Timestamp de referência pouco antes da rejeição para capturar as tarefas recém-criadas
        rejection_start_ts = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.000000-03:00")

        rej_res = bulk_reject_headers(df_to_reject, facility_id=facility_id)
        if rej_res and rej_res.status_code == 200:
            rej_data = rej_res.json()
            execution_log["rejected_count"] = rej_data.get("success_count", len(df_to_reject["group_nbr"].unique()))
            execution_log["rejected_groups"] = df_to_reject["group_nbr"].dropna().unique().tolist()

        # Pequena pausa para garantir que o WMS concluiu a criação da nova tarefa de recontagem
        time.sleep(1.5)

        # Busca novas tarefas em status 'Pronto' (status_id = 10)
        df_new_tasks = fetch_ready_tasks(create_ts_gte=rejection_start_ts, facility_id=facility_id)
        rejected_locations = set(df_to_reject["location_id.key"].dropna().unique())

        if not df_new_tasks.empty:
            loc_col = "next_location_id.key" if "next_location_id.key" in df_new_tasks.columns else "location_barcode"
            if loc_col in df_new_tasks.columns:
                target_tasks = df_new_tasks[df_new_tasks[loc_col].isin(rejected_locations)]
            else:
                target_tasks = df_new_tasks

            if target_tasks.empty and not df_new_tasks.empty:
                target_tasks = df_new_tasks

            if not target_tasks.empty:
                task_ids = target_tasks["id"].dropna().tolist()
                hold_res = bulk_hold_tasks(task_ids)
                if hold_res and hold_res.status_code == 200:
                    hold_data = hold_res.json()
                    execution_log["held_count"] = hold_data.get("success_count", len(task_ids))
                    execution_log["held_tasks"] = task_ids
                    execution_log["df_held"] = target_tasks

    # 6. Atualiza o status de execução no DataFrame de Detalhes
    def get_exec_status(row):
        action = row["system_action"]
        grp = row.get("group_nbr")
        if action in ("Aprovação Automática", "Auto-Approve"):
            if grp in execution_log["approved_groups"]:
                return "✅ Aprovado com Sucesso"
            return "⚠️ Aguardando / Falha na Aprovação"
        elif action in ("Rejeição Automática", "Auto-Reject"):
            if grp in execution_log["rejected_groups"]:
                return "❌ Rejeitado & Nova Tarefa Retida"
            return "⚠️ Aguardando / Falha na Rejeição"
        elif action in ("Já Aprovado", "Already Approved"):
            return "Concluído (Já Aprovado)"
        elif action in ("Já Rejeitado", "Already Rejected"):
            return "Concluído (Já Rejeitado)"
        return action

    df_evaluated["resultado_execucao"] = df_evaluated.apply(get_exec_status, axis=1)

    return {
        "status": "success",
        "df_details": df_evaluated,
        "comp_df": comp_df,
        "execution_log": execution_log
    }


# --- DATAFRAME FILTER UTILITY ---
def filter_dataframe(df: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    """
    Componente interativo para filtragem dinâmica de todas as colunas de um DataFrame.
    Inclui busca rápida global por texto e filtros individuais por coluna selecionada.
    """
    if df.empty:
        return df

    df_filtered = df.copy()

    with st.expander(f"🔍 Filtrar Colunas deste DataFrame ({len(df)} registros totais)", expanded=False):
        col_global, col_select = st.columns([1.5, 2])

        with col_global:
            search_term = st.text_input(
                "Busca Rápida (em todas as colunas):",
                placeholder="Ex: SKU, LPN, Localização, Status...",
                key=f"{key_prefix}_global_search"
            )

        with col_select:
            selected_columns = st.multiselect(
                "Selecione colunas para aplicar filtros específicos:",
                options=df.columns.tolist(),
                default=[],
                key=f"{key_prefix}_selected_cols"
            )

        # 1. Aplicação da busca global
        if search_term:
            q = str(search_term).strip().lower()
            mask = df_filtered.astype(str).apply(
                lambda row: row.str.lower().str.contains(q, regex=False)
            ).any(axis=1)
            df_filtered = df_filtered[mask]

        # 2. Aplicação de filtros específicos por coluna
        if selected_columns:
            st.write("---")
            grid_cols = st.columns(min(len(selected_columns), 3))

            for idx, col in enumerate(selected_columns):
                target_col = grid_cols[idx % len(grid_cols)]
                with target_col:
                    series = df[col].dropna()

                    # Caso 1: Coluna Numérica
                    if pd.api.types.is_numeric_dtype(df[col]):
                        if not series.empty:
                            min_val = float(series.min())
                            max_val = float(series.max())
                            if min_val < max_val:
                                step = 1.0 if pd.api.types.is_integer_dtype(df[col]) else 0.1
                                num_range = st.slider(
                                    f"Intervalo de `{col}`:",
                                    min_value=min_val,
                                    max_value=max_val,
                                    value=(min_val, max_val),
                                    step=step,
                                    key=f"{key_prefix}_num_{col}"
                                )
                                df_filtered = df_filtered[
                                    df_filtered[col].between(num_range[0], num_range[1])
                                ]
                            else:
                                st.caption(f"`{col}`: valor único ({min_val})")

                    # Caso 2: Coluna Datetime
                    elif pd.api.types.is_datetime64_any_dtype(df[col]):
                        if not series.empty:
                            min_d = series.min().date()
                            max_d = series.max().date()
                            if min_d < max_d:
                                dt_range = st.date_input(
                                    f"Período de `{col}`:",
                                    value=(min_d, max_d),
                                    key=f"{key_prefix}_dt_{col}"
                                )
                                if len(dt_range) == 2:
                                    df_filtered = df_filtered[
                                        (df_filtered[col].dt.date >= dt_range[0]) &
                                        (df_filtered[col].dt.date <= dt_range[1])
                                    ]
                            else:
                                st.caption(f"`{col}`: data única ({min_d})")

                    # Caso 3: Coluna Textual / Categórica
                    else:
                        unique_options = sorted([str(x) for x in series.unique() if str(x).strip() != ""])
                        if len(unique_options) <= 50:
                            chosen = st.multiselect(
                                f"Opções de `{col}`:",
                                options=unique_options,
                                default=[],
                                key=f"{key_prefix}_cat_{col}"
                            )
                            if chosen:
                                df_filtered = df_filtered[df_filtered[col].astype(str).isin(chosen)]
                        else:
                            txt_filter = st.text_input(
                                f"Contém em `{col}`:",
                                key=f"{key_prefix}_txt_{col}"
                            )
                            if txt_filter:
                                df_filtered = df_filtered[
                                    df_filtered[col].astype(str).str.contains(
                                        str(txt_filter), case=False, na=False, regex=False
                                    )
                                ]

    # Linha com contagem de registros e botão de exportação
    badge_col, dl_col = st.columns([4, 1])
    with badge_col:
        st.caption(f"Mostrando **{len(df_filtered)}** de **{len(df)}** registros.")
    with dl_col:
        csv_data = df_filtered.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Exportar CSV",
            data=csv_data,
            file_name=f"{key_prefix}_export.csv",
            mime="text/csv",
            key=f"{key_prefix}_dl_btn"
        )

    return df_filtered


# --- MAIN INTERFACE ---
def main():
    login_screen()

    st.title("📦 Automação de Contagem Cíclica - Oracle WMS")
    st.caption("Pipeline automatizado: Busca -> Validação (Item + Qtd + LPN) -> Aprovação -> Rejeição -> Retenção de Tarefas.")

    # Filtros de Data & Unidade definidos pelo usuário via Input
    st.markdown("### 📅 Filtro de Datas e Unidade (Tasks & Contagens)")
    with st.container():
        f_col1, f_col2, f_col3, f_col4, f_col5 = st.columns([2, 1.3, 2, 1.3, 1.2])

        today = datetime.now().date()
        init_start = st.session_state.get("filter_start_date", today - timedelta(days=3))
        init_end = st.session_state.get("filter_end_date", today)

        with f_col1:
            sel_start_date = st.date_input("Data Inicial", value=init_start, key="main_start_date")
        with f_col2:
            sel_start_time = st.time_input("Hora Inicial", value=datetime.strptime("00:00:00", "%H:%M:%S").time(), key="main_start_time")
        with f_col3:
            sel_end_date = st.date_input("Data Final", value=init_end, key="main_end_date")
        with f_col4:
            sel_end_time = st.time_input("Hora Final", value=datetime.strptime("23:59:59", "%H:%M:%S").time(), key="main_end_time")
        with f_col5:
            facility_id = st.number_input("Facility ID", value=4, step=1, key="main_facility_id")

    create_ts_gte = f"{sel_start_date.isoformat()}T{sel_start_time.strftime('%H:%M:%S')}.000000-03:00"
    create_ts_lte = f"{sel_end_date.isoformat()}T{sel_end_time.strftime('%H:%M:%S')}.999999-03:00"

    st.caption(f"Filtro ativo no WMS: `create_ts >= {create_ts_gte}` e `create_ts <= {create_ts_lte}` | Facility: `{facility_id}`")

    # Sidebar: User info & shortcuts
    st.sidebar.markdown(f"### 👤 Usuário: `{st.session_state.username}`")
    st.sidebar.markdown(f"**Facility ID:** `{facility_id}`")
    st.sidebar.markdown(f"**Período:**\n- Início: `{sel_start_date} {sel_start_time.strftime('%H:%M')}`\n- Fim: `{sel_end_date} {sel_end_time.strftime('%H:%M')}`")
    if st.sidebar.button("🚪 Logout"):
        st.session_state.clear()
        st.rerun()

    # Main Action Button
    col_btn, _ = st.columns([2, 3])
    with col_btn:
        execute_clicked = st.button("🚀 Sincronizar & Executar Fluxo Automático", type="primary", use_container_width=True)

    if execute_clicked:
        with st.spinner(f"Executando pipeline automático ({sel_start_date} até {sel_end_date})..."):
            pipeline_result = run_automated_pipeline(facility_id, create_ts_gte, create_ts_lte)
            st.session_state.pipeline_result = pipeline_result

    # Render results if available
    if "pipeline_result" in st.session_state:
        res = st.session_state.pipeline_result

        if res["status"] == "empty":
            st.warning(res.get("message", "Nenhum registro encontrado."))
            return

        df_details = res["df_details"]
        comp_df = res["comp_df"]
        log = res["execution_log"]

        # Execution Metrics Cards
        st.write("---")
        m_col1, m_col2, m_col3, m_col4, m_col5 = st.columns(5)
        m_col1.metric("Total Detalhes", len(df_details))
        m_col2.metric(
            "Aprovados Auto",
            f"{log['approved_count']} grupos",
            delta=f"{len(df_details[df_details['system_action'].isin(['Aprovação Automática', 'Auto-Approve'])])} linhas"
        )
        m_col3.metric(
            "Rejeitados Auto",
            f"{log['rejected_count']} grupos",
            delta=f"{len(df_details[df_details['system_action'].isin(['Rejeição Automática', 'Auto-Reject'])])} linhas"
        )
        m_col4.metric("Tarefas em Retenção", f"{log['held_count']} tarefas")
        m_col5.metric(
            "Revisão Necessária",
            len(df_details[df_details['system_action'].isin(['Revisão Necessária', 'Review Needed'])])
        )

        # Execution Feedback Messages
        if log["approved_count"] > 0:
            st.success(f"✅ {log['approved_count']} grupo(s) de ajuste aprovados automaticamente com sucesso no WMS!")
        if log["rejected_count"] > 0:
            st.warning(f"❌ {log['rejected_count']} grupo(s) de ajuste rejeitados automaticamente pelo WMS.")
        if log["held_count"] > 0:
            st.info(f"🔒 {log['held_count']} nova(s) tarefa(s) de recontagem gerada(s) foram colocadas em Retenção (Hold) automaticamente!")

        # Tabs for Visualizing DataFrames
        tab_dtl, tab_comp, tab_held = st.tabs([
            "📋 1. Detalhamento Completo (cc_adjustment_dtl)",
            "📊 2. Tabela Comparativa (1ª, 2ª e 3ª Contagens)",
            "🔒 3. Novas Tarefas Retidas em Hold"
        ])

        with tab_dtl:
            st.write("### Detalhamento das Contagens no WMS")
            display_cols = [
                "location_id.key", "lpn", "item_id.key", "expected_qty", "counted_qty",
                "qty_diff", "count_sequence", "status_wms_pt", "system_action",
                "resultado_execucao", "group_nbr", "hdr_id"
            ]
            cols_to_show = [c for c in display_cols if c in df_details.columns]
            rename_dict = {
                "location_id.key": "Localização",
                "lpn": "LPN / Container",
                "item_id.key": "Item",
                "expected_qty": "Qtd Esperada",
                "counted_qty": "Qtd Contada",
                "qty_diff": "Diferença",
                "count_sequence": "Rodada (Seq)",
                "status_wms_pt": "Status da Tarefa (WMS)",
                "system_action": "Ação Definida",
                "resultado_execucao": "Resultado Execução",
                "group_nbr": "Grupo Ajuste",
                "hdr_id": "ID Cabeçalho"
            }
            df_display = df_details[cols_to_show].rename(columns=rename_dict)
            df_display_filtered = filter_dataframe(df_display, key_prefix="dtl")
            st.dataframe(df_display_filtered, use_container_width=True, height=450)

        with tab_comp:
            st.write("### Tabela Comparativa de Contagens (Posição vs Item vs LPN)")
            comp_df_filtered = filter_dataframe(comp_df, key_prefix="comp")
            st.dataframe(comp_df_filtered, use_container_width=True, height=450)

        with tab_held:
            st.write("### Tarefas Recém-Criadas Colocadas em Retenção (Hold)")
            df_held = log.get("df_held", pd.DataFrame())
            if not df_held.empty:
                df_held = df_held.copy()
                if "status_id" in df_held.columns:
                    df_held["status_pt"] = df_held["status_id"].apply(translate_status_id)
                elif "status" in df_held.columns:
                    df_held["status_pt"] = df_held["status"].apply(translate_status_id)
                else:
                    df_held["status_pt"] = "Pendente (10)"

                task_cols = [
                    "id", "task_nbr", "next_location_id.key", "task_type_id.key",
                    "status_pt", "create_ts", "assigned_user"
                ]
                cols_held_show = [c for c in task_cols if c in df_held.columns]
                rename_held = {
                    "id": "ID Tarefa",
                    "task_nbr": "Número Tarefa",
                    "next_location_id.key": "Localização",
                    "task_type_id.key": "Tipo Tarefa",
                    "status_pt": "Status da Tarefa (WMS)",
                    "create_ts": "Data Criação",
                    "assigned_user": "Usuário Atribuído"
                }
                df_held_display = df_held[cols_held_show].rename(columns=rename_held)
                df_held_filtered = filter_dataframe(df_held_display, key_prefix="held")
                st.dataframe(df_held_filtered, use_container_width=True)
            else:
                st.info("Nenhuma nova tarefa colocada em retenção nesta execução.")


if __name__ == "__main__":
    main()