import json
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from requests.auth import HTTPBasicAuth
from datetime import datetime, date, time

# --- CONFIGURAÇÃO DA PÁGINA ---
BASE_URL = "https://k1.wms.ocs.oraclecloud.com:443/arcoed/wms/lgfapi/v10/entity"
EXCEL_FILE = "location.xlsx"

st.set_page_config(
    page_title="Progresso do Inventário & Bateria de Corredores",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- AUTENTICAÇÃO ---
def login_screen():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        st.title("Visualizador de Progresso do Inventário WMS")
        with st.form("login_form"):
            username = st.text_input("Usuário")
            password = st.text_input("Senha", type="password")
            submit_button = st.form_submit_button("Entrar")

            if submit_button:
                if username and password:
                    st.session_state.username = username
                    st.session_state.password = password
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("Por favor, informe o usuário e a senha.")
        st.stop()

def get_session():
    session = requests.Session()
    session.auth = HTTPBasicAuth(st.session_state.username, st.session_state.password)
    session.headers.update({"Content-Type": "application/json"})
    return session

# --- CARREGAMENTO DE DADOS COM FILTRO DINÂMICO DE PERÍODO ---
@st.cache_data
def load_master_locations(filepath=EXCEL_FILE):
    """Carrega o arquivo location.xlsx e extrai área, corredor, módulo e nível."""
    try:
        df = pd.read_excel(filepath)
        parts = df['texto_exibicao'].str.split('-', expand=True)
        if parts.shape[1] >= 4:
            df['area'] = parts[0]
            df['aisle'] = parts[1]
            df['bay'] = parts[2]
            df['level'] = parts[3]
        else:
            df['area'] = 'DESCONHECIDO'
            df['aisle'] = 'DESCONHECIDO'
            df['bay'] = '0'
            df['level'] = '0'
        return df
    except Exception as e:
        st.error(f"Erro ao carregar o arquivo {filepath}: {e}")
        return pd.DataFrame()

def fetch_cc_adjustment_headers(start_ts, end_ts=None):
    """Busca os registros de cc_adjustment_hdr na instalação 4 filtrados pelo período."""
    session = get_session()
    all_results = []
    
    url = f"{BASE_URL}/cc_adjustment_hdr?facility_id=4&page_mode=paged&create_ts__gte={start_ts}"
    if end_ts:
        url += f"&create_ts__lte={end_ts}"
    
    with st.spinner("Buscando ajustes de contagem do WMS..."):
        response = session.get(url)
        if response.status_code != 200:
            st.error(f"Falha ao buscar cc_adjustment_hdr. Código de Status: {response.status_code}")
            return pd.DataFrame()
            
        data = response.json()
        all_results.extend(data.get("results", []))
        
        page_count = data.get("page_count", 1)
        for page in range(2, min(page_count + 1, 15)):
            p_url = f"{url}&page={page}"
            res = session.get(p_url)
            if res.status_code == 200:
                all_results.extend(res.json().get("results", []))
                
    if not all_results:
        return pd.DataFrame()

    df_hdr = pd.json_normalize(all_results)
    return df_hdr

# --- MOTOR DE VALIDAÇÃO ---
def evaluate_locations(df_master, df_hdr):
    if df_hdr.empty:
        df_master['count_num'] = 0
        df_master['status_list'] = "[]"
        df_master['validation_status'] = 'Não Contado'
        return df_master

    loc_col = "location_id.key" if "location_id.key" in df_hdr.columns else "location_id"
    status_col = "status_id"
    
    df_hdr['loc_key'] = df_hdr[loc_col].astype(str)

    def check_location(group):
        statuses = group[status_col].tolist()
        count_num = len(statuses)
        
        if count_num == 1:
            is_valid = 70 in statuses
        elif count_num in [2, 3]:
            is_valid = 30 in statuses
        else:
            is_valid = (30 in statuses) or (70 in statuses)

        status_label = "Validado (OK)" if is_valid else "Pendente / Rejeitado"

        return pd.Series({
            "count_num": count_num,
            "status_list": str(statuses),
            "validation_status": status_label
        })

    loc_summary = df_hdr.groupby('loc_key').apply(check_location).reset_index()

    merged = pd.merge(
        df_master, 
        loc_summary, 
        left_on='texto_exibicao', 
        right_on='loc_key', 
        how='left'
    )

    merged['validation_status'] = merged['validation_status'].fillna('Não Contado')
    merged['count_num'] = merged['count_num'].fillna(0).astype(int)

    return merged

# --- GRÁFICOS EMPILHADOS 100% (COM BASE INICIANDO EM FINALIZADAS) ---
def render_split_100pct_stacked_bars(df_eval, selected_level="Todos"):
    df_chart = df_eval.copy()
    
    if selected_level != "Todos":
        df_chart = df_chart[df_chart['level'].astype(str) == str(selected_level)]

    # Agrupa por corredor
    aisle_df = df_chart.groupby('aisle').agg(
        total=('texto_exibicao', 'count'),
        validated=('validation_status', lambda x: (x == "Validado (OK)").sum())
    ).reset_index()

    if aisle_df.empty:
        st.warning("Nenhum dado encontrado para o nível selecionado.")
        return

    # Ordena os corredores em ordem CRESCENTE (ASC)
    aisle_df['aisle_str'] = aisle_df['aisle'].astype(str)
    aisle_df = aisle_df.sort_values(by='aisle_str', ascending=True)

    aisle_df['pending'] = aisle_df['total'] - aisle_df['validated']
    aisle_df['pct_validated'] = (aisle_df['validated'] / aisle_df['total'] * 100).fillna(0)
    aisle_df['pct_pending'] = (aisle_df['pending'] / aisle_df['total'] * 100).fillna(0)

    # Divide os corredores na metade (Parte 1 e Parte 2)
    half_idx = (len(aisle_df) + 1) // 2
    top_half = aisle_df.iloc[:half_idx]
    bottom_half = aisle_df.iloc[half_idx:]

    def build_chart_figure(df_sub):
        fig = go.Figure()

        # 1. Barra de FINALIZADAS / VALIDADAS (Azul Escuro) - COMEÇA DA BASE (0%)
        fig.add_trace(go.Bar(
            x=df_sub['aisle_str'],
            y=df_sub['pct_validated'],
            name="FINALIZADAS",
            marker_color="#030f26",
            text=[f"{v:.0f}%" if v > 3 else "" for v in df_sub['pct_validated']],
            textposition="inside",
            textfont=dict(color="white", size=10, family="Arial Black"),
            insidetextanchor="middle"
        ))

        # 2. Barra de PENDENTES (Laranja) - FICA EMPILHADA NO TOPO
        fig.add_trace(go.Bar(
            x=df_sub['aisle_str'],
            y=df_sub['pct_pending'],
            name="PENDENTES",
            marker_color="#ff6a3d",
            text=[f"{v:.0f}%" if v > 3 else "" for v in df_sub['pct_pending']],
            textposition="inside",
            textfont=dict(color="white", size=10, family="Arial Black"),
            insidetextanchor="middle"
        ))

        fig.update_layout(
            barmode='stack',
            height=280,
            margin=dict(l=10, r=10, t=35, b=10),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1.0,
                font=dict(size=11, color="#000", family="Arial Black")
            ),
            xaxis=dict(
                type='category',
                tickfont=dict(size=10, color="#333", family="Arial Black"),
                showgrid=False
            ),
            yaxis=dict(
                range=[0, 100],
                ticksuffix="%",
                showgrid=True,
                gridcolor="#e5e5e5"
            ),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)"
        )
        return fig

    # Gráfico Parte 1
    title_top = f"Parte 1: Corredores ({top_half['aisle_str'].iloc[0]} - {top_half['aisle_str'].iloc[-1]})"
    st.markdown(f"##### **{title_top}**")
    fig_top = build_chart_figure(top_half)
    st.plotly_chart(fig_top, use_container_width=True)

    # Gráfico Parte 2
    if not bottom_half.empty:
        title_bottom = f"Parte 2: Corredores ({bottom_half['aisle_str'].iloc[0]} - {bottom_half['aisle_str'].iloc[-1]})"
        st.markdown(f"##### **{title_bottom}**")
        fig_bottom = build_chart_figure(bottom_half)
        st.plotly_chart(fig_bottom, use_container_width=True)

# --- APLICAÇÃO PRINCIPAL ---
def main():
    login_screen()

    st.sidebar.title("Monitor de Locais WMS")
    if st.sidebar.button("Sair (Logout)"):
        st.session_state.clear()
        st.rerun()

    # --- FILTRO DE PERÍODO NA BARRA LATERAL ---
    st.sidebar.write("---")
    st.sidebar.subheader("Filtro de Período da API (`create_ts`)")
    
    start_d = st.sidebar.date_input("Data Inicial (`create_ts__gte`)", value=date(2026, 9, 15))
    start_t = st.sidebar.time_input("Hora Inicial", value=time(0, 0, 51))
    
    use_end_date = st.sidebar.checkbox("Aplicar Filtro de Data Final (`create_ts__lte`)")
    end_d = None
    if use_end_date:
        end_d = st.sidebar.date_input("Data Final", value=date(2026, 9, 21))
        end_t = st.sidebar.time_input("Hora Final", value=time(23, 59, 59))
        end_ts = f"{end_d.isoformat()}T{end_t.strftime('%H:%M:%S')}-03:00"
    else:
        end_ts = None

    start_ts = f"{start_d.isoformat()}T{start_t.strftime('%H:%M:%S')}-03:00"

    # 1. Carrega o cadastro mestre de locais
    df_master = load_master_locations()
    if df_master.empty:
        st.stop()

    st.title("Painel de Validação de Inventário")

    # 2. Busca Dados do WMS
    if "df_evaluated" not in st.session_state:
        if st.button("Buscar e Calcular Progresso", type="primary"):
            df_hdr = fetch_cc_adjustment_headers(start_ts, end_ts)
            df_eval = evaluate_locations(df_master, df_hdr)
            st.session_state.df_evaluated = df_eval
            st.session_state.df_hdr = df_hdr
            st.rerun()
        else:
            st.info(f"Clique em 'Buscar e Calcular Progresso' para consultar o Oracle WMS a partir de `{start_ts}`.")
            st.stop()
    else:
        if st.sidebar.button("Atualizar Dados"):
            del st.session_state["df_evaluated"]
            st.rerun()

    df_eval = st.session_state.df_evaluated

    # 3. Métricas Principais
    total_locs = len(df_eval)
    validated_locs = len(df_eval[df_eval['validation_status'] == "Validado (OK)"])
    pending_locs = len(df_eval[df_eval['validation_status'] == "Pendente / Rejeitado"])
    not_counted = len(df_eval[df_eval['validation_status'] == "Não Contado"])
    overall_progress = (validated_locs / total_locs * 100) if total_locs > 0 else 0

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total de Locais", f"{total_locs:,}")
    col2.metric("Validados (Aprovados)", f"{validated_locs:,}")
    col3.metric("Pendentes / Rejeitados", f"{pending_locs:,}")
    col4.metric("Não Contados", f"{not_counted:,}")
    col5.metric("Taxa de Conclusão", f"{overall_progress:.1f}%")

    st.progress(overall_progress / 100.0)

    # 4. Gráficos Empilhados Divididos
    st.write("---")
    st.subheader("Detalhamento de Progresso por Corredor (Gráfico 100% Empilhado)")

    available_levels = sorted([str(l) for l in df_eval['level'].dropna().unique()])
    level_options = ["Todos"] + available_levels
    
    selected_level = st.radio(
        "Filtrar por Nível:",
        options=level_options,
        horizontal=True,
        index=0
    )

    # Renderiza os dois gráficos (Parte 1 e Parte 2)
    render_split_100pct_stacked_bars(df_eval, selected_level=selected_level)

    # 5. Visão de Grid Interativa do Corredor
    st.write("---")
    st.subheader("Grid Interativo do Corredor (Módulos vs Níveis)")

    sorted_aisles = sorted(df_eval['aisle'].dropna().unique())
    selected_aisle = st.selectbox("Selecione o Corredor para Inspecionar:", sorted_aisles)

    if selected_aisle:
        df_aisle = df_eval[df_eval['aisle'] == selected_aisle].copy()

        status_map = {
            "Validado (OK)": 2,
            "Pendente / Rejeitado": 1,
            "Não Contado": 0
        }
        df_aisle['status_code'] = df_aisle['validation_status'].map(status_map)

        grid = df_aisle.pivot_table(
            index='level', 
            columns='bay', 
            values='status_code', 
            aggfunc='first'
        ).fillna(-1)

        fig_grid = px.imshow(
            grid,
            labels=dict(x="Módulo (Bay)", y="Nível (Level)", color="Status"),
            x=grid.columns,
            y=grid.index,
            color_continuous_scale=[
                [0.0, "#E0E0E0"],   # Não Contado (Cinza)
                [0.5, "#FF9800"],   # Pendente / Rejeitado (Laranja)
                [1.0, "#4CAF50"]    # Validado (Verde)
            ],
            title=f"Corredor {selected_aisle} — Leiaute da Estrutura"
        )
        fig_grid.update_layout(coloraxis_showscale=False)
        st.plotly_chart(fig_grid, use_container_width=True)

        with st.expander(f"Ver Detalhes dos Locais do Corredor {selected_aisle}"):
            st.dataframe(
                df_aisle[[
                    'texto_exibicao', 'bay', 'level', 'count_num', 
                    'validation_status', 'status_list'
                ]],
                use_container_width=True
            )

if __name__ == "__main__":
    main()