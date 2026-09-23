import json
import time
from datetime import datetime, date, time as dt_time, timedelta
import pandas as pd
import requests
import streamlit as st
from requests.auth import HTTPBasicAuth

# --- CONFIGURAÇÃO ---
BASE_URL = "https://k1.wms.ocs.oraclecloud.com:443/arcoed/wms/lgfapi/v10/entity"
BATCH_SIZE = 50  # Tamanho do lote máximo reduzido para 50 para chamadas em Bulk na API WMS

st.set_page_config(
    page_title="WMS Cycle Count Automation & Approvals",
    page_icon="📦",
    layout="wide"
)

# Estilização CSS customizada
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


# --- AUXILIAR DE FATIAMENTO EM LOTES (BATCHES DE 50) ---
def chunk_list(lst, chunk_size=BATCH_SIZE):
    """
    Divide qualquer lista em sublistas de tamanho até `chunk_size` (padrão 50).
    """
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i + chunk_size]


# --- TELA DE AUTENTICAÇÃO ---
def login_screen():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        st.title("📦 WMS Cycle Count - Automação Total & Aprovações")
        st.info("Entre com suas credenciais do Oracle WMS para acessar o painel de aprovações e comparativos.")
        with st.form("login_form"):
            col_u, col_p = st.columns(2)
            with col_u:
                username = st.text_input("Username")
            with col_p:
                password = st.text_input("Password", type="password")

            st.write("---")
            st.markdown("##### 📅 Filtro Inicial de Período das Tarefas")
            col_d1, col_d2 = st.columns(2)
            
            fixed_start = date(2026, 9, 22)
            fixed_end = date(2026, 9, 27)

            with col_d1:
                start_date_input = st.date_input("Data Inicial", value=fixed_start)
            with col_d2:
                end_date_input = st.date_input("Data Final", value=fixed_end)

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


# --- FUNÇÕES AUXILIARES DA API REST ORACLE WMS ---
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

    for batch_ids in chunk_list(hdr_ids, chunk_size=BATCH_SIZE):
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
    Executa POST para /cc_adjustment_hdr/bulk_approve/ fatiando em lotes de 50
    com fallback individual apenas para itens do lote que falharem.
    """
    session = get_session()
    approve_url = f"{BASE_URL}/cc_adjustment_hdr/bulk_approve/"
    group_numbers = df_approve["group_nbr"].dropna().astype(int).unique().tolist()

    if not group_numbers:
        return None

    total_success = 0
    total_failures = 0
    all_details = {}

    for batch_groups in chunk_list(group_numbers, chunk_size=BATCH_SIZE):
        payload = {
            "parameters": {
                "facility_id": facility_id,
                "group_nbr__in": batch_groups
            },
            "options": {
                "comment": "Aprovado automaticamente pelo sistema (Contagens coincidem)",
                "commit_frequency": 1
            }
        }

        response = session.post(approve_url, json=payload)

        if response.status_code == 200:
            res_data = response.json()
            total_success += res_data.get("success_count", len(batch_groups))
            total_failures += res_data.get("failure_count", 0)
        else:
            st.warning(f"Lote de aprovação (tamanho {len(batch_groups)}) falhou no bulk. Executando fallback individual...")
            batch_df = df_approve[df_approve["group_nbr"].isin(batch_groups)].drop_duplicates(subset=["group_nbr"])

            for _, row in batch_df.iterrows():
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
                    total_success += 1
                else:
                    total_failures += 1
                    all_details[str(row["group_nbr"])] = f"Status {single_res.status_code}"

    mock_response = requests.Response()
    mock_response.status_code = 200
    mock_payload = {
        "record_count": len(group_numbers),
        "success_count": total_success,
        "failure_count": total_failures,
        "details": all_details if all_details else None
    }
    mock_response._content = json.dumps(mock_payload).encode('utf-8')
    return mock_response


def bulk_reject_headers(df_reject, facility_id=4):
    """
    Executa POST para /cc_adjustment_hdr/bulk_reject/ fatiando em lotes de 50
    com fallback individual por lote se necessário.
    """
    session = get_session()
    reject_url = f"{BASE_URL}/cc_adjustment_hdr/bulk_reject/"
    group_numbers = df_reject["group_nbr"].dropna().astype(int).unique().tolist()

    if not group_numbers:
        return None

    total_success = 0
    total_failures = 0
    all_details = {}

    for batch_groups in chunk_list(group_numbers, chunk_size=BATCH_SIZE):
        payload = {
            "parameters": {
                "facility_id": facility_id,
                "group_nbr__in": batch_groups
            },
            "options": {
                "comment": "Rejeitado automaticamente pelo sistema",
                "commit_frequency": 1
            }
        }

        response = session.post(reject_url, json=payload)

        if response.status_code == 200:
            res_data = response.json()
            total_success += res_data.get("success_count", len(batch_groups))
            total_failures += res_data.get("failure_count", 0)
        else:
            st.warning(f"Lote de rejeição (tamanho {len(batch_groups)}) falhou no bulk. Executando fallback individual...")
            batch_df = df_reject[df_reject["group_nbr"].isin(batch_groups)].drop_duplicates(subset=["group_nbr"])

            for _, row in batch_df.iterrows():
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
                    total_success += 1
                else:
                    total_failures += 1
                    all_details[str(row["group_nbr"])] = f"Status {single_res.status_code}"

    mock_response = requests.Response()
    mock_response.status_code = 200
    mock_payload = {
        "record_count": len(group_numbers),
        "success_count": total_success,
        "failure_count": total_failures,
        "details": all_details if all_details else None
    }
    mock_response._content = json.dumps(mock_payload).encode('utf-8')
    return mock_response


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


def bulk_hold_tasks(task_ids, batch_size=BATCH_SIZE):
    """
    Coloca tarefas em retenção usando POST /task/bulk_hold/ em lotes de 50 com fallback.
    """
    session = get_session()
    hold_url = f"{BASE_URL}/task/bulk_hold/"

    if not task_ids:
        return None

    total_success = 0
    total_failures = 0
    all_details = {}

    for batch_tasks in chunk_list(task_ids, chunk_size=batch_size):
        payload = {
            "parameters": {
                "id__in": [int(tid) for tid in batch_tasks]
            },
            "options": {
                "commit_frequency": 1
            }
        }

        response = session.post(hold_url, json=payload)

        if response.status_code == 200:
            res_data = response.json()
            total_success += res_data.get("success_count", len(batch_tasks))
            total_failures += res_data.get("failure_count", 0)
        else:
            st.warning(f"Lote de retenção/hold (tamanho {len(batch_tasks)}) falhou no bulk. Executando fallback individual...")
            for tid in batch_tasks:
                single_url = f"{BASE_URL}/task/{tid}/hold/"
                single_res = session.post(single_url)
                if single_res.status_code in [200, 204]:
                    total_success += 1
                else:
                    total_failures += 1
                    all_details[str(tid)] = f"Status {single_res.status_code}"

    mock_response = requests.Response()
    mock_response.status_code = 200
    mock_payload = {
        "record_count": len(task_ids),
        "success_count": total_success,
        "failure_count": total_failures,
        "details": all_details if all_details else None
    }
    mock_response._content = json.dumps(mock_payload).encode('utf-8')
    return mock_response


# --- TRADUÇÃO DE STATUS ---
STATUS_MAP_PT = {
    10: "Em Andamento",
    20: "Pendente",
    30: "Aprovado",
    50: "Rejeitado",
    70: "Sem Divergência",
    99: "Cancelado"
}

def translate_status_id(val):
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


# --- PREPARAÇÃO DE DADOS & REGRAS MULTI-CONTAGEM ---
def evaluate_multi_count_dataset(df):
    if df.empty:
        return df, pd.DataFrame()

    df = df.copy()

    df["expected_qty"] = pd.to_numeric(df.get("expected_qty", 0), errors="coerce").fillna(0)
    df["counted_qty"] = pd.to_numeric(df.get("counted_qty", 0), errors="coerce").fillna(0)
    df["qty_diff"] = df["counted_qty"] - df["expected_qty"]

    def get_row_lpn(row):
        for col in ["lpn", "lpn_id.key", "lpn_id", "container_nbr", "container_id.key"]:
            if col in row and pd.notna(row[col]):
                val = str(row[col]).strip()
                if val not in ("", "None", "nan", "null", "0", "0.0"):
                    return val
        return "SEM LPN"

    df["lpn"] = df.apply(get_row_lpn, axis=1)

    def get_row_item(row):
        for col in ["item_id.key", "item_id", "item_key", "item_nbr"]:
            if col in row and pd.notna(row[col]):
                val = str(row[col]).strip()
                if val not in ("", "None", "nan"):
                    return val
        return "ITEM_DESCONHECIDO"

    df["item_id.key"] = df.apply(get_row_item, axis=1)

    def get_row_loc(row):
        for col in ["location_id.key", "location_id", "location_barcode"]:
            if col in row and pd.notna(row[col]):
                val = str(row[col]).strip()
                if val not in ("", "None", "nan"):
                    return val
        return "LOC_DESCONHECIDA"

    df["location_id.key"] = df.apply(get_row_loc, axis=1)

    if "create_ts_hdr" in df.columns:
        df["create_ts_hdr_dt"] = pd.to_datetime(df["create_ts_hdr"], errors="coerce")
    else:
        df["create_ts_hdr_dt"] = pd.to_datetime(df.get("create_ts", pd.Timestamp.now()))

    hdr_order = (
        df[["location_id.key", "hdr_id", "create_ts_hdr_dt"]]
        .drop_duplicates()
        .sort_values(by=["location_id.key", "create_ts_hdr_dt", "hdr_id"])
    )
    hdr_order["count_sequence"] = hdr_order.groupby("location_id.key").cumcount() + 1
    hdr_seq_map = dict(zip(hdr_order["hdr_id"], hdr_order["count_sequence"]))
    df["count_sequence"] = df["hdr_id"].map(hdr_seq_map).fillna(1).astype(int)

    if "status_id_hdr" in df.columns:
        df["status_wms_pt"] = df["status_id_hdr"].apply(translate_status_id)
    else:
        df["status_wms_pt"] = df.get("status_id", 0).apply(translate_status_id)

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

            counted_profile = {}
            expected_profile = {}
            for _, d in h_dtls.iterrows():
                k = (str(d["item_id.key"]), str(d["lpn"]))
                counted_profile[k] = float(d["counted_qty"])
                expected_profile[k] = float(d["expected_qty"])

            profiles[seq] = counted_profile
            expected_profiles[seq] = expected_profile

            if status == 30:
                hdr_action_map[h_id] = "Já Aprovado"
                continue
            elif status == 50:
                hdr_action_map[h_id] = "Já Rejeitado"
                continue
            elif status != 20:
                hdr_action_map[h_id] = translate_status_id(status)
                continue

            # Status 20 (Pendente):
            if seq == 1:
                # 1ª Contagem: Se divergente do sistema -> Rejeita; Se igual -> Aprova
                has_diff = any(
                    counted_profile.get(k, 0.0) != expected_profile.get(k, 0.0)
                    for k in set(counted_profile.keys()) | set(expected_profile.keys())
                )
                if has_diff:
                    hdr_action_map[h_id] = "Rejeição Automática"
                else:
                    hdr_action_map[h_id] = "Aprovação Automática"
            elif seq == 2:
                # 2ª Contagem: Se bater com a 1ª contagem -> Aprova; Senão -> Rejeita
                profile_seq1 = profiles.get(1, {})
                if profile_seq1 and counted_profile == profile_seq1:
                    hdr_action_map[h_id] = "Aprovação Automática"
                else:
                    hdr_action_map[h_id] = "Rejeição Automática"
            else:
                # Demais contagens: Aprova se bater com qualquer contagem anterior
                matched_previous = False
                for prev_seq in range(1, seq):
                    if profiles.get(prev_seq) == counted_profile:
                        matched_previous = True
                        break

                if matched_previous:
                    hdr_action_map[h_id] = "Aprovação Automática"
                else:
                    hdr_action_map[h_id] = "Rejeição Automática"

    df["system_action"] = df["hdr_id"].map(hdr_action_map).fillna("Revisão Necessária")

    # Tabela comparativa (Posição x 7 contagens)
    pivot_df = df.pivot_table(
        index=["location_id.key", "item_id.key", "lpn"],
        columns="count_sequence",
        values="counted_qty",
        aggfunc="first"
    ).reset_index()

    for i in range(1, 8):
        c_label = f"{i}ª Contagem"
        if i in pivot_df.columns:
            pivot_df.rename(columns={i: c_label}, inplace=True)
        else:
            pivot_df[c_label] = None

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

    comp_df = comp_df.rename(columns={
        "location_id.key": "Localização",
        "item_id.key": "Item",
        "lpn": "LPN / Container",
        "expected_qty": "Qtd Esperada",
        "system_action": "Última Ação Recomendada"
    })

    final_cols = [
        "Localização", "Item", "LPN / Container", "Qtd Esperada",
        "1ª Contagem", "2ª Contagem", "3ª Contagem", "4ª Contagem",
        "5ª Contagem", "6ª Contagem", "7ª Contagem", "Última Ação Recomendada"
    ]
    comp_df = comp_df[[c for c in final_cols if c in comp_df.columns]]

    return df, comp_df


def fetch_all_counts_data(facility_id, create_ts_gte, create_ts_lte=None):
    hdr_data = fetch_headers(facility_id, create_ts_gte, create_ts_lte)
    if not hdr_data:
        return pd.DataFrame(), pd.DataFrame()

    hdr_ids = [str(item["id"]) for item in hdr_data]
    dtl_data = fetch_details(hdr_ids)

    df_hdr = pd.json_normalize(hdr_data).rename(columns={"id": "hdr_id"})
    df_dtl = pd.json_normalize(dtl_data) if dtl_data else pd.DataFrame()

    if df_dtl.empty:
        return df_hdr, pd.DataFrame()

    df = pd.merge(
        df_dtl,
        df_hdr,
        left_on="cc_adjustment_hdr_id.id",
        right_on="hdr_id",
        how="left",
        suffixes=('_dtl', '_hdr')
    )

    return evaluate_multi_count_dataset(df)


# --- AUTOMATED PIPELINE PROCESSANDO EM BATCHES DE 50 E AÇÃO ASSÍNCRONA DE HOLD POR LOTE ---
def execute_actions_pipeline(df_evaluated, facility_id, create_ts_gte):
    """
    Executa o pipeline em lotes de 50:
    1. Aprovações em lotes de 50.
    2. Rejeições em lotes de 50 com busca e retenção (Hold) assíncrona IMEDIATAMENTE após cada lote recusado.
    """
    pending_df = df_evaluated[df_evaluated["status_id_hdr"] == 20]
    df_to_approve = pending_df[pending_df["system_action"].isin(["Aprovação Automática", "Auto-Approve"])]
    df_to_reject = pending_df[pending_df["system_action"].isin(["Rejeição Automática", "Auto-Reject"])]

    log = {
        "approved_count": 0,
        "approved_groups": [],
        "rejected_count": 0,
        "rejected_groups": [],
        "held_count": 0,
        "held_tasks": [],
        "df_held_list": []
    }

    # 1. Aprovações em Lotes de 50
    if not df_to_approve.empty:
        appr_res = bulk_approve_headers(df_to_approve, facility_id=facility_id)
        if appr_res and appr_res.status_code == 200:
            appr_data = appr_res.json()
            log["approved_count"] = appr_data.get("success_count", len(df_to_approve["group_nbr"].unique()))
            log["approved_groups"] = df_to_approve["group_nbr"].dropna().unique().tolist()

    # 2. Rejeições em Lotes de 50 com Hold Assíncrono por Lote
    if not df_to_reject.empty:
        reject_groups = df_to_reject["group_nbr"].dropna().astype(int).unique().tolist()

        for chunk_groups in chunk_list(reject_groups, chunk_size=BATCH_SIZE):
            chunk_df_reject = df_to_reject[df_to_reject["group_nbr"].isin(chunk_groups)]
            chunk_locations = set(chunk_df_reject["location_id.key"].dropna().unique())

            # Marca o timestamp de disparo deste lote específico
            batch_ts = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.000000-03:00")

            # Executa a rejeição do lote de 50
            rej_res = bulk_reject_headers(chunk_df_reject, facility_id=facility_id)

            if rej_res and rej_res.status_code == 200:
                rej_data = rej_res.json()
                succ_rej = rej_data.get("success_count", len(chunk_groups))
                log["rejected_count"] += succ_rej
                log["rejected_groups"].extend(chunk_groups)

                # Pausa para garantir que o WMS processou a criação das novas tarefas para o lote
                time.sleep(1.2)

                # Busca as tarefas geradas para este lote específico de posições recusadas
                df_new_tasks = fetch_ready_tasks(create_ts_gte=batch_ts, facility_id=facility_id)

                if not df_new_tasks.empty:
                    loc_col = "next_location_id.key" if "next_location_id.key" in df_new_tasks.columns else "location_barcode"
                    if loc_col in df_new_tasks.columns:
                        target_tasks = df_new_tasks[df_new_tasks[loc_col].isin(chunk_locations)]
                    else:
                        target_tasks = df_new_tasks

                    if not target_tasks.empty:
                        task_ids = target_tasks["id"].dropna().tolist()
                        # Executa o Hold para as novas tarefas do lote de 50
                        hold_res = bulk_hold_tasks(task_ids, batch_size=BATCH_SIZE)
                        if hold_res and hold_res.status_code == 200:
                            hold_data = hold_res.json()
                            log["held_count"] += hold_data.get("success_count", len(task_ids))
                            log["held_tasks"].extend(task_ids)
                            log["df_held_list"].append(target_tasks)

    # Consolida DataFrames de tarefas retidas
    if log["df_held_list"]:
        log["df_held"] = pd.concat(log["df_held_list"], ignore_index=True).drop_duplicates(subset=["id"])
    else:
        log["df_held"] = pd.DataFrame()

    # Atualiza visualização de execução
    def get_exec_status(row):
        action = row["system_action"]
        grp = row.get("group_nbr")
        if action in ("Aprovação Automática", "Auto-Approve"):
            if grp in log["approved_groups"]:
                return "✅ Aprovado com Sucesso"
            return "⚠️ Aguardando / Falha na Aprovação"
        elif action in ("Rejeição Automática", "Auto-Reject"):
            if grp in log["rejected_groups"]:
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
        "execution_log": log
    }


# --- FILTRO DINÂMICO INTERATIVO ---
def render_interactive_filter(df, key_prefix):
    if df.empty:
        return df

    df_filtered = df.copy()

    with st.expander(f"🔍 Filtrar Colunas deste DataFrame ({len(df)} registros)", expanded=False):
        c1, c2 = st.columns([1.5, 2])
        with c1:
            q = st.text_input("Busca Rápida:", placeholder="Ex: SKU, LPN, Localização...", key=f"{key_prefix}_q")
        with c2:
            cols = st.multiselect("Filtrar por Coluna Específica:", options=df.columns.tolist(), key=f"{key_prefix}_cols")

        if q:
            q_str = str(q).strip().lower()
            mask = df_filtered.astype(str).apply(lambda r: r.str.lower().str.contains(q_str, regex=False)).any(axis=1)
            df_filtered = df_filtered[mask]

        if cols:
            grid = st.columns(min(len(cols), 3))
            for idx, col in enumerate(cols):
                with grid[idx % len(grid)]:
                    series = df[col].dropna()
                    if pd.api.types.is_numeric_dtype(df[col]) and not series.empty:
                        min_v, max_v = float(series.min()), float(series.max())
                        if min_v < max_v:
                            v = st.slider(f"`{col}`:", min_v, max_v, (min_v, max_v), key=f"{key_prefix}_s_{col}")
                            df_filtered = df_filtered[df_filtered[col].between(v[0], v[1])]
                    else:
                        opts = sorted([str(x) for x in series.unique() if str(x).strip() != ""])
                        if len(opts) <= 50:
                            chosen = st.multiselect(f"`{col}`:", options=opts, key=f"{key_prefix}_m_{col}")
                            if chosen:
                                df_filtered = df_filtered[df_filtered[col].astype(str).isin(chosen)]

    st.caption(f"Mostrando **{len(df_filtered)}** de **{len(df)}** registros.")
    return df_filtered


# --- INTERFACE PRINCIPAL ---
def main():
    login_screen()

    st.title("📦 Automação de Aprovação de Contagens Cíclicas")
    st.caption("Painel automatizado com análise por rodadas de contagem e comparativo geral integrado ao Oracle WMS (em lotes de 50).")

    # Filtros Globais de Data (Fixado de 22/09/2026 até 27/09/2026)
    st.markdown("### 📅 Filtro de Período do Processamento")
    f_col1, f_col2, f_col3, f_col4, f_col5 = st.columns([2, 1.3, 2, 1.3, 1.2])

    fixed_start = date(2026, 9, 22)
    fixed_end = date(2026, 9, 27)

    with f_col1:
        sel_start_date = st.date_input("Data Inicial", value=fixed_start, key="global_start_date")
    with f_col2:
        sel_start_time = st.time_input("Hora Inicial", value=dt_time(0, 0, 0), key="global_start_time")
    with f_col3:
        sel_end_date = st.date_input("Data Final", value=fixed_end, key="global_end_date")
    with f_col4:
        sel_end_time = st.time_input("Hora Final", value=dt_time(23, 59, 59), key="global_end_time")
    with f_col5:
        facility_id = st.number_input("Facility ID", value=4, step=1, key="global_facility_id")

    create_ts_gte = f"{sel_start_date.isoformat()}T{sel_start_time.strftime('%H:%M:%S')}.000000-03:00"
    create_ts_lte = f"{sel_end_date.isoformat()}T{sel_end_time.strftime('%H:%M:%S')}.999999-03:00"

    # Botão Principal de Sincronização
    col_btn, _ = st.columns([2, 3])
    with col_btn:
        sync_clicked = st.button("🚀 Sincronizar Dados do WMS", type="primary", use_container_width=True)

    if sync_clicked:
        with st.spinner("Buscando e avaliando contagens no WMS..."):
            df_eval, comp_df = fetch_all_counts_data(facility_id, create_ts_gte, create_ts_lte)
            st.session_state.df_evaluated = df_eval
            st.session_state.comp_df = comp_df

    # Abas Principais
    tab_blocks, tab_comparative = st.tabs([
        "🥇 1. Blocos de Aprovações por Contagem",
        "📊 2. Comparativo Geral de Contagens (1ª a 7ª)"
    ])

    # ABA 1: BLOCOS DE APROVAÇÕES
    with tab_blocks:
        st.markdown("### 📋 Análise e Ações por Nível/Rodada de Contagem (Em Lotes de 50)")

        if "df_evaluated" not in st.session_state or st.session_state.df_evaluated.empty:
            st.info("Clique no botão **'🚀 Sincronizar Dados do WMS'** acima para carregar o painel de aprovações.")
        else:
            df_eval = st.session_state.df_evaluated

            c_auto1, c_auto2 = st.columns([2.5, 2.5])
            with c_auto1:
                if st.button("⚡ Executar Aprovações & Rejeições Automáticas (em Lotes de 50)", type="primary", use_container_width=True):
                    with st.spinner("Processando lotes de até 50 registros na API..."):
                        exec_res = execute_actions_pipeline(df_eval, facility_id, create_ts_gte)
                        log = exec_res["execution_log"]
                        st.session_state.exec_log = log
                        
                        # Recarrega a base atualizada
                        df_eval, comp_df = fetch_all_counts_data(facility_id, create_ts_gte, create_ts_lte)
                        st.session_state.df_evaluated = df_eval
                        st.session_state.comp_df = comp_df
                        st.success("Fluxo em lote executado com sucesso!")
                        st.rerun()

            if "exec_log" in st.session_state:
                log = st.session_state.exec_log
                st.info(f"**Última Automação:** Aprovados: `{log['approved_count']}` grupos | Rejeitados: `{log['rejected_count']}` grupos | Tarefas Retidas em Hold: `{log['held_count']}`")

            st.write("---")

            sequences = sorted(df_eval["count_sequence"].unique().tolist())

            if not sequences:
                st.warning("Nenhuma contagem encontrada.")
            else:
                sub_tab_labels = [f"{s}ª Contagem" for s in sequences]
                sub_tabs = st.tabs(sub_tab_labels)

                for idx, seq in enumerate(sequences):
                    with sub_tabs[idx]:
                        df_seq = df_eval[df_eval["count_sequence"] == seq]

                        st.markdown(f"#### Contagens Pendentes/Analisadas na **{seq}ª Contagem**")
                        
                        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
                        col_m1.metric("Total de Registros", len(df_seq))
                        col_m2.metric("Pendentes de Ação", len(df_seq[df_seq["status_id_hdr"] == 20]))
                        col_m3.metric("Recomendado Aprovar", len(df_seq[df_seq["system_action"] == "Aprovação Automática"]))
                        col_m4.metric("Recomendado Rejeitar", len(df_seq[df_seq["system_action"] == "Rejeição Automática"]))

                        if seq == 1:
                            st.info("💡 **Regras da 1ª Contagem:** Se divergente do sistema -> Rejeição e retenção da nova tarefa. Se igual -> Aprovação.")
                        elif seq == 2:
                            st.info("💡 **Regras da 2ª Contagem:** Se a 2ª contagem for igual à 1ª -> Aprovação. Senão -> Rejeição e retenção da nova tarefa.")
                        elif seq == 3:
                            st.info("💡 **Regras da 3ª Contagem:** Comparativo das contagens. Se a 3ª bater com a 1ª OU com a 2ª -> Aprovação.")
                        else:
                            st.info(f"💡 **Regras da {seq}ª Contagem:** Se a {seq}ª contagem bater com QUALQUER contagem anterior (1..{seq-1}) -> Aprovação.")

                        df_seq_display = render_interactive_filter(df_seq, key_prefix=f"block_{seq}")

                        cols_show = [
                            "hdr_id", "group_nbr", "location_id.key", "item_id.key", "lpn",
                            "expected_qty", "counted_qty", "qty_diff", "status_wms_pt", "system_action"
                        ]
                        cols_valid = [c for c in cols_show if c in df_seq_display.columns]

                        st.dataframe(df_seq_display[cols_valid], use_container_width=True, height=350)

    # ABA 2: COMPARATIVO GERAL DE CONTAGENS (1ª a 7ª)
    with tab_comparative:
        st.markdown("### 📊 Comparativo Geral de Contagens por Posição/Item/LPN")
        st.caption("Linha única por posição trazendo as 7 comparações de contagens históricas.")

        col_comp_btn, _ = st.columns([2.5, 2.5])
        with col_comp_btn:
            comp_refresh_clicked = st.button("🔄 Atualizar Tabela Comparativa de Contagens", type="secondary", use_container_width=True)

        if comp_refresh_clicked:
            with st.spinner("Atualizando tabela comparativa diretamente do WMS..."):
                _, comp_df = fetch_all_counts_data(facility_id, create_ts_gte, create_ts_lte)
                st.session_state.comp_df = comp_df
                st.success("Tabela comparativa atualizada com sucesso!")

        if "comp_df" in st.session_state and not st.session_state.comp_df.empty:
            comp_df = st.session_state.comp_df
            filtered_comp_df = render_interactive_filter(comp_df, key_prefix="comp_tab")
            st.dataframe(filtered_comp_df, use_container_width=True, height=500)
        else:
            st.info("Nenhum dado comparativo carregado. Clique no botão de atualização acima ou sincronize os dados do WMS.")


if __name__ == "__main__":
    main()