# -*- coding: utf-8 -*-
"""
NÍVEL DOS SINOS — CAMPO BOM (Estação ANA 87380000) — v7
==============================================================================
Novidades da v7 (sobre a v6):
  - Layout ainda mais largo (1750px).
  - Datas dos avisos do INMET em formato brasileiro legível
    (ex.: "22/07/2026" em vez de "2026-07-22T00:00:00.000Z").
    Se início e fim caírem no mesmo instante, mostra a data só uma vez.
  - MODO CLARO / MODO ESCURO com botão no canto superior direito.
    A preferência fica salva no navegador (localStorage) e o gráfico
    também troca de tema junto.

Fontes de dados:
  - Nível/chuva/vazão: API HidroWebService da ANA (estação 87380000)
  - Meteorologia: Open-Meteo (https://open-meteo.com) — gratuita, sem chave
  - Avisos meteorológicos: INMET (https://alertas.inmet.gov.br)

Cada bloco "# ===== CÉLULA N =====" pode virar uma célula no Colab.
No VS Code: python app_nivel_campo_bom_v7.py
Para hospedar (Render etc.): gunicorn app_nivel_campo_bom_v7:server
"""

# ============================================================
# ===== CÉLULA 1 — Instalação (rodar só uma vez no Colab) =====
# ============================================================
# !pip install dash dash-bootstrap-components plotly pandas requests
# !pip install fpdf2 matplotlib   # <- para o Exportar PDF

# ============================================================
# ===== CÉLULA 2 — Imports e configurações ===================
# ============================================================
import os
import io
import time
from datetime import datetime, timedelta

import requests
import pandas as pd

import dash
from dash import dcc, html, Input, Output, State, callback_context
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ---- Projeto ----
CODIGO_ESTACAO = "87380000"
NOME_ESTACAO = "Campo Bom"
NOME_RIO = "Rio dos Sinos"
CIDADE = "Campo Bom/RS"
LAT, LON = -29.678, -51.058          # Campo Bom (para a previsão do tempo)
REALIZACAO = "Yuri Georg Pedde e Jeferson Timm"   # aparece no rodapé
PARCERIA = ""                        # ex.: "em parceria com a Prefeitura de Campo Bom"

# Credenciais: 1º tenta variáveis de ambiente (ideal para hospedagem),
# depois o TXT do Drive (Colab).
CAMINHO_CREDENCIAIS = "/content/drive/MyDrive/Colab Notebooks/APP Nível dos Sinos/ID_SENHA_ANA_API.txt"

# ---- API ANA ----
BASE_URL = "https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas"
URL_TOKEN = f"{BASE_URL}/OAUth/v1"
URL_SERIE = f"{BASE_URL}/HidroinfoanaSerieTelemetricaAdotada/v1"
RANGE_BUSCA = "DIAS_30"   # buscamos sempre 30 dias; o seletor filtra localmente

# ---- Cotas de referência (m) — CONFIRME com a Defesa Civil de Campo Bom ----
COTA_ATENCAO   = 5.8
COTA_ALERTA    = 6.5
COTA_INUNDACAO = 7.2
PICO_HISTORICO = None   # ex.: 9.20 (preencha se souber; aparece no rodapé)

# ---- Open-Meteo ----
URL_METEO = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
    "precipitation_probability_max,windspeed_10m_max,winddirection_10m_dominant"
    "&hourly=windspeed_10m,winddirection_10m,windgusts_10m"
    "&current_weather=true&timezone=America%2FSao_Paulo&forecast_days=7"
)

# ---- INMET (avisos meteorológicos oficiais) ----
GEOCODIGO_IBGE = "4303905"          # Campo Bom/RS
URL_ALERTAS_INMET = "https://apiprevmet3.inmet.gov.br/avisos/ativos"

DIAS_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

LARGURA_MAX = "1750px"   # <- largura do painel (era 1400px na v6)


# ============================================================
# ===== CÉLULA 3 — Drive e credenciais =======================
# ============================================================
def montar_drive():
    try:
        from google.colab import drive
        drive.mount("/content/drive")
        print("Drive montado.")
    except ImportError:
        print("Não está no Colab — pulando montagem do Drive.")


def ler_credenciais(caminho=CAMINHO_CREDENCIAIS):
    """Ordem: variáveis de ambiente ANA_ID/ANA_SENHA -> TXT do Drive."""
    ident = os.environ.get("ANA_ID")
    senha = os.environ.get("ANA_SENHA")
    if ident and senha:
        return ident, senha

    if not os.path.exists(caminho):
        raise FileNotFoundError(
            f"Credenciais não encontradas. Defina as variáveis de ambiente "
            f"ANA_ID e ANA_SENHA, ou coloque o TXT em:\n{caminho}"
        )
    identificador, senha = None, None
    with open(caminho, "r", encoding="utf-8") as f:
        for linha in f:
            if "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            chave = chave.strip().lower()
            valor = valor.strip().strip('"').strip("'").strip('"').strip()
            if "identificador" in chave:
                identificador = valor
            elif "senha" in chave:
                senha = valor
    if not identificador or not senha:
        raise ValueError("Formato do TXT inválido (IDENTIFICADOR = \"...\" / senha = \"...\").")
    return identificador, senha


# ============================================================
# ===== CÉLULA 4 — Token ANA (com cache) =====================
# ============================================================
_TOKEN_CACHE = {"token": None, "obtido_em": 0.0, "validade_seg": 55 * 60}


def gerar_token(identificador, senha, forcar=False):
    agora = time.time()
    if (not forcar and _TOKEN_CACHE["token"]
            and (agora - _TOKEN_CACHE["obtido_em"]) < _TOKEN_CACHE["validade_seg"]):
        return _TOKEN_CACHE["token"]
    resp = requests.get(URL_TOKEN, headers={"Identificador": identificador,
                                            "Senha": senha}, timeout=30)
    resp.raise_for_status()
    token = (resp.json().get("items") or {}).get("tokenautenticacao")
    if not token:
        raise RuntimeError(f"Autenticação sem token. Resposta: {resp.json()}")
    _TOKEN_CACHE.update({"token": token, "obtido_em": agora})
    return token


# ============================================================
# ===== CÉLULA 5 — Dados ANA + Open-Meteo ====================
# ============================================================
def buscar_serie(token, codigo_estacao=CODIGO_ESTACAO, range_busca=RANGE_BUSCA):
    """Nomes de parâmetros LITERAIS do Swagger (com espaços e acentos)."""
    params = {
        "Código da Estação": codigo_estacao,
        "Tipo Filtro Data": "DATA_LEITURA",
        "Data de Busca (yyyy-MM-dd)": datetime.now().strftime("%Y-%m-%d"),
        "Range Intervalo de busca": range_busca,
    }
    resp = requests.get(URL_SERIE, headers={"Authorization": f"Bearer {token}"},
                        params=params, timeout=60)
    if resp.status_code in (401, 403):
        raise PermissionError(f"Token expirado/inválido (HTTP {resp.status_code}).")
    if not resp.ok:
        try:
            detalhe = resp.json().get("message", resp.text[:300])
        except Exception:
            detalhe = resp.text[:300]
        raise RuntimeError(f"HTTP {resp.status_code} na consulta: {detalhe}")
    return resp.json().get("items", []) or []


def processar_dados(items):
    colunas = ["data_hora", "cota_m", "cota_cm", "chuva_mm", "vazao_m3s"]
    if not items:
        return pd.DataFrame(columns=colunas)
    df = pd.DataFrame(items)

    def num(col):
        return pd.to_numeric(df.get(col), errors="coerce")

    out = pd.DataFrame()
    out["data_hora"] = pd.to_datetime(df.get("Data_Hora_Medicao"), errors="coerce")
    out["cota_cm"] = num("Cota_Adotada")
    out["cota_m"] = out["cota_cm"] / 100.0
    out["chuva_mm"] = num("Chuva_Adotada")
    out["vazao_m3s"] = num("Vazao_Adotada")
    out = (out.dropna(subset=["data_hora"])
              .drop_duplicates(subset=["data_hora"])
              .sort_values("data_hora").reset_index(drop=True))
    return out


def buscar_meteo():
    """Previsão 7 dias + vento (Open-Meteo). Falha aqui não derruba o app."""
    try:
        r = requests.get(URL_METEO, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("Open-Meteo indisponível:", e)
        return None


def buscar_alertas_inmet():
    """Avisos meteorológicos oficiais do INMET vigentes para Campo Bom.
    Retorna: lista de avisos (pode ser vazia) ou None se a consulta falhar."""
    try:
        r = requests.get(URL_ALERTAS_INMET, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0 (nivel-sinos)"})
        r.raise_for_status()
        dados = r.json()
    except Exception as e:
        print("INMET indisponível:", e)
        return None

    if isinstance(dados, dict):
        avisos = list(dados.get("hoje") or []) + list(dados.get("futuro") or [])
    elif isinstance(dados, list):
        avisos = dados
    else:
        avisos = []

    achados = []
    for av in avisos:
        if not isinstance(av, dict):
            continue
        blob = " ".join(str(av.get(campo, "")) for campo in
                        ("geocodes", "municipios", "municipio", "cidades"))
        if GEOCODIGO_IBGE in blob or "Campo Bom" in blob:
            riscos = av.get("riscos") or av.get("descricao_riscos") or ""
            if isinstance(riscos, (list, tuple)):
                riscos = "; ".join(str(x) for x in riscos)
            achados.append({
                "evento": (av.get("descricao") or av.get("evento")
                           or "Aviso meteorológico"),
                "severidade": str(av.get("severidade") or ""),
                "inicio": str(av.get("data_inicio") or ""),
                "fim": str(av.get("data_fim") or ""),
                "riscos": str(riscos),
            })
    return achados


def carregar_dados():
    identificador, senha = ler_credenciais()
    token = gerar_token(identificador, senha)
    try:
        items = buscar_serie(token)
    except PermissionError:
        token = gerar_token(identificador, senha, forcar=True)
        items = buscar_serie(token)
    return processar_dados(items), buscar_meteo(), buscar_alertas_inmet()


# ============================================================
# ===== CÉLULA 5B — Análises (tendência, status, projeção) ===
# ============================================================
def cota_em(df, horas_atras):
    """Cota (cm) na leitura mais próxima de N horas atrás, ou None."""
    alvo = df["data_hora"].iloc[-1] - timedelta(hours=horas_atras)
    antes = df[df["data_hora"] <= alvo]
    if antes.empty:
        return None
    ref = antes.iloc[-1]
    if (alvo - ref["data_hora"]) > timedelta(hours=max(2, horas_atras)):
        return None
    return ref["cota_cm"] if pd.notna(ref["cota_cm"]) else None


def taxa_cm_h(df, horas):
    """Variação média em cm/h nas últimas N horas (positivo = subindo)."""
    if df.empty or pd.isna(df["cota_cm"].iloc[-1]):
        return None
    ref = cota_em(df, horas)
    if ref is None:
        return None
    dt_h = (df["data_hora"].iloc[-1]
            - df[df["cota_cm"] == ref]["data_hora"].iloc[-1]).total_seconds() / 3600
    if dt_h <= 0:
        return None
    return (df["cota_cm"].iloc[-1] - ref) / dt_h


def status_nivel(nivel_m):
    if nivel_m is None:
        return "Sem dados", "secondary"
    if COTA_INUNDACAO and nivel_m >= COTA_INUNDACAO:
        return "INUNDAÇÃO", "danger"
    if COTA_ALERTA and nivel_m >= COTA_ALERTA:
        return "Alerta", "danger"
    if COTA_ATENCAO and nivel_m >= COTA_ATENCAO:
        return "Atenção", "warning"
    return "Normal", "success"


def projecao_texto(nivel_m, taxa1h):
    """Se estiver subindo, estima tempo até a próxima cota de referência."""
    if nivel_m is None or taxa1h is None or taxa1h <= 0.1:
        return None
    for cota, nome in [(COTA_ATENCAO, "atenção"), (COTA_ALERTA, "alerta"),
                       (COTA_INUNDACAO, "inundação")]:
        if cota and nivel_m < cota:
            horas = (cota - nivel_m) * 100.0 / taxa1h
            if horas > 24 * 15:
                return None
            d, h = int(horas // 24), int(round(horas % 24))
            if d > 0:
                return f"No ritmo atual, pode atingir a cota de {nome} em {d} dia(s) e {h} h"
            return f"No ritmo atual, pode atingir a cota de {nome} em {h} h"
    return None


def direcao_pt(graus):
    if graus is None:
        return "—"
    pontos = ["Norte", "Nordeste", "Leste", "Sudeste",
              "Sul", "Sudoeste", "Oeste", "Noroeste"]
    return pontos[int((graus + 22.5) % 360 // 45)]


def direcao_abrev(graus):
    if graus is None:
        return "-"
    pontos = ["N", "NE", "L", "SE", "S", "SO", "O", "NO"]
    return pontos[int((graus + 22.5) % 360 // 45)]


def fmt(v, casas=2):
    return f"{v:.{casas}f}" if v is not None and pd.notna(v) else "—"


def formatar_data_aviso(valor):
    """Converte as datas do INMET para formato brasileiro legível.

    Aceita tanto ISO com 'Z' ('2026-07-22T00:00:00.000Z') quanto
    '2026-07-22 10:00:00'. Removemos o 'Z' de propósito (sem converter
    fuso), porque nesses avisos o horário costuma vir zerado e o que
    interessa é a DATA — converter UTC->BRT jogaria a data um dia
    para trás. Se o horário for meia-noite, mostra só a data.
    """
    s = str(valor or "").strip()
    if not s or s.lower() in ("none", "nan"):
        return "?"
    try:
        ts = pd.to_datetime(s.replace("Z", "").replace("z", ""))
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        if ts.hour == 0 and ts.minute == 0:
            return ts.strftime("%d/%m/%Y")
        return ts.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return s   # se não der para interpretar, mostra como veio


def texto_vigencia(inicio, fim):
    """'Vigência: 22/07/2026 até 24/07/2026' — ou só uma data se forem iguais."""
    ini_f = formatar_data_aviso(inicio)
    fim_f = formatar_data_aviso(fim)
    if ini_f == "?" and fim_f == "?":
        return None
    if ini_f == fim_f or fim_f == "?":
        return f"Vigência: {ini_f}"
    if ini_f == "?":
        return f"Vigência: até {fim_f}"
    return f"Vigência: {ini_f} até {fim_f}"


# ============================================================
# ===== CÉLULA 5C — DIAGNÓSTICO ==============================
# ============================================================
def diagnostico():
    ident, senha = ler_credenciais()
    print(f"credenciais ok ({ident[:3]}***)")
    token = gerar_token(ident, senha, forcar=True)
    print("token ok")
    items = buscar_serie(token)
    df = processar_dados(items)
    print(f"{len(df)} registros | última: {df['data_hora'].iloc[-1] if not df.empty else '—'}")
    meteo = buscar_meteo()
    print("open-meteo:", "ok" if meteo else "falhou")
    alertas = buscar_alertas_inmet()
    if alertas is None:
        print("inmet: falhou")
    else:
        print(f"inmet: ok ({len(alertas)} aviso(s) para {NOME_ESTACAO})")
        for a in (alertas or []):
            print("  vigência formatada:", texto_vigencia(a["inicio"], a["fim"]))
    return df


# ============================================================
# ===== CÉLULA 5D — Temas e gráficos (tela e PDF) ============
# ============================================================
# Paletas do gráfico Plotly (a interface usa variáveis CSS; o Plotly
# não entende CSS vars, então a figura recebe o tema por parâmetro).
TEMAS_FIG = {
    "dark": dict(template="plotly_dark",
                 paper="#141d33", plot="#141d33", texto="#e6edf3",
                 agua="#38bdf8", chuva="#60a5fa",
                 fill="rgba(56,189,248,.12)",
                 atencao="#facc15", alerta="#fb923c", inund="#ef4444"),
    "light": dict(template="plotly_white",
                  paper="#ffffff", plot="#ffffff", texto="#0f172a",
                  agua="#0284c7", chuva="#3b82f6",
                  fill="rgba(2,132,199,.10)",
                  atencao="#b45309", alerta="#c2410c", inund="#b91c1c"),
}

RANGES = {"24h": 1, "3 dias": 3, "7 dias": 7, "15 dias": 15, "30 dias": 30}


def montar_figura(dfr, tema="dark"):
    """Gráfico combinado chuva+nível (Plotly) usado na TELA do app."""
    t = TEMAS_FIG.get(tema, TEMAS_FIG["dark"])
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(
        x=dfr["data_hora"], y=dfr["chuva_mm"], name="Chuva (mm)",
        marker_color=t["chuva"], opacity=.55,
        hovertemplate="%{x|%d/%m %H:%M}<br>%{y:.1f} mm<extra>Chuva</extra>",
    ), secondary_y=True)
    fig.add_trace(go.Scatter(
        x=dfr["data_hora"], y=dfr["cota_m"], name="Nível (m)",
        mode="lines", line=dict(color=t["agua"], width=2.5),
        fill="tozeroy", fillcolor=t["fill"],
        hovertemplate="%{x|%d/%m %H:%M}<br>%{y:.2f} m<extra>Nível</extra>",
    ), secondary_y=False)

    for valor, texto, cor in [(COTA_ATENCAO, "Atenção", t["atencao"]),
                              (COTA_ALERTA, "Alerta", t["alerta"]),
                              (COTA_INUNDACAO, "Inundação", t["inund"])]:
        if valor is not None:
            fig.add_hline(y=valor, line_dash="dash", line_color=cor,
                          annotation_text=f"{texto} ({valor:.2f} m)",
                          annotation_position="top left",
                          annotation_font_color=cor, secondary_y=False)

    fig.update_layout(
        template=t["template"], paper_bgcolor=t["paper"], plot_bgcolor=t["plot"],
        font=dict(color=t["texto"]),
        height=440, margin=dict(l=45, r=45, t=30, b=40),
        hovermode="x unified", barmode="overlay",
        legend=dict(orientation="h", y=1.08, x=0),
    )
    fig.update_yaxes(title_text="Nível (m)", secondary_y=False, rangemode="tozero")
    fig.update_yaxes(title_text="Chuva (mm)", secondary_y=True,
                     showgrid=False, rangemode="tozero")
    return fig


def figura_vazia(tema="dark", texto="Aguardando dados..."):
    t = TEMAS_FIG.get(tema, TEMAS_FIG["dark"])
    return go.Figure().update_layout(
        template=t["template"], paper_bgcolor=t["paper"], plot_bgcolor=t["plot"],
        height=430, annotations=[dict(text=texto, showarrow=False,
                                      font=dict(color=t["texto"], size=16))])


def grafico_png_pdf(dfr):
    """Mesmo gráfico, em matplotlib (tema claro), como PNG para o PDF."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.transforms as mtransforms

    fig, ax = plt.subplots(figsize=(10.6, 4.4), dpi=160)
    ax2 = ax.twinx()

    # --- chuva (barras, eixo direito) ---
    tem_chuva = dfr["chuva_mm"].notna().any()
    if tem_chuva:
        if len(dfr) > 1:
            passo = dfr["data_hora"].diff().median()
            largura = max(passo.total_seconds() / 86400 * 0.9, 0.004)
        else:
            largura = 0.02
        ax2.bar(dfr["data_hora"], dfr["chuva_mm"].fillna(0), width=largura,
                color="#3b82f6", alpha=.45, label="Chuva (mm)", zorder=1)

    # --- nível (linha, eixo esquerdo) ---
    ax.plot(dfr["data_hora"], dfr["cota_m"], color="#0284c7", lw=2.2,
            label="Nível (m)", zorder=3)
    ax.fill_between(dfr["data_hora"], dfr["cota_m"].fillna(0),
                    color="#0284c7", alpha=.08, zorder=2)

    # --- cotas de referência ---
    trans = mtransforms.blended_transform_factory(ax.transAxes, ax.transData)
    for valor, texto, cor in [(COTA_ATENCAO, "Atenção", "#b45309"),
                              (COTA_ALERTA, "Alerta", "#c2410c"),
                              (COTA_INUNDACAO, "Inundação", "#b91c1c")]:
        if valor is not None:
            ax.axhline(valor, ls="--", lw=1.3, color=cor, zorder=2)
            ax.text(0.01, valor, f"{texto} ({valor:.2f} m)", transform=trans,
                    va="bottom", ha="left", fontsize=8.5, color=cor, zorder=4)

    # --- escalas e eixos ---
    topo = max([v for v in [dfr["cota_m"].max(skipna=True),
                            COTA_INUNDACAO, COTA_ALERTA, COTA_ATENCAO]
                if v is not None and pd.notna(v)] + [1])
    ax.set_ylim(0, topo * 1.10)
    ax.set_ylabel("Nível (m)", fontsize=10)
    ax2.set_ylabel("Chuva (mm)", fontsize=10)
    ax2.set_ylim(bottom=0)
    ax.margins(x=0.01)
    ax.grid(alpha=.25, lw=.6)
    ax.set_axisbelow(True)

    dias_periodo = (dfr["data_hora"].iloc[-1] - dfr["data_hora"].iloc[0]).days
    formato = "%d/%m %Hh" if dias_periodo < 2 else "%d/%m"
    ax.xaxis.set_major_formatter(mdates.DateFormatter(formato))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
    ax.tick_params(labelsize=9)
    ax2.tick_params(labelsize=9)
    for lado in ("top",):
        ax.spines[lado].set_visible(False)
        ax2.spines[lado].set_visible(False)

    # --- legenda combinada ---
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=9,
              frameon=False, bbox_to_anchor=(0, 1.10), ncol=2)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight",
                facecolor="white", pad_inches=0.15)
    plt.close(fig)
    return buf.getvalue()


# ============================================================
# ===== CÉLULA 5E — Geração do PDF ===========================
# ============================================================
PDF_AZUL_ESCURO = (13, 27, 62)
PDF_AZUL = (2, 132, 199)
PDF_TEXTO = (15, 23, 42)
PDF_CINZA = (100, 116, 139)
PDF_CARD = (241, 245, 249)
PDF_CARD_BORDA = (226, 232, 240)
PDF_STATUS = {"success": (34, 197, 94), "warning": (234, 179, 8),
              "danger": (239, 68, 68), "secondary": (148, 163, 184)}


def _lat(texto):
    """fpdf2 com fontes padrão só aceita latin-1; troca o que não couber."""
    trocas = {"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
              "·": "-", "▲": "^", "▼": "v", "▬": "=", "💨": ""}
    s = str(texto)
    for a, b in trocas.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def gerar_pdf_bytes(df, range_nome, meteo):
    """Monta o relatório em PDF (fpdf2 + matplotlib) e devolve os bytes."""
    from fpdf import FPDF

    dias = RANGES.get(range_nome, 7)
    dfr = df[df["data_hora"] >= df["data_hora"].iloc[-1] - timedelta(days=dias)]

    ultimo = df.iloc[-1]
    nivel = ultimo["cota_m"] if pd.notna(ultimo["cota_m"]) else None
    quando = ultimo["data_hora"].strftime("%d/%m/%Y %H:%M")
    tx1, tx3, tx6, tx12 = (taxa_cm_h(df, h) for h in (1, 3, 6, 12))
    rot_status, cor_status = status_nivel(nivel)
    proj = projecao_texto(nivel, tx1)

    hoje = df[df["data_hora"].dt.date == datetime.now().date()]
    chuva_hoje = hoje["chuva_mm"].sum(skipna=True) if not hoje.empty else None
    media = df["cota_m"].mean(skipna=True)
    minimo, maximo = df["cota_m"].min(skipna=True), df["cota_m"].max(skipna=True)
    vazao = ultimo["vazao_m3s"] if pd.notna(ultimo["vazao_m3s"]) else None

    try:
        png = grafico_png_pdf(dfr)
    except Exception as e:
        print("Falha ao desenhar o gráfico do PDF:", e)
        png = None

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    L = pdf.w - pdf.l_margin - pdf.r_margin
    X0 = pdf.l_margin

    # ---------- faixa de cabeçalho ----------
    pdf.set_fill_color(*PDF_AZUL_ESCURO)
    pdf.rect(0, 0, pdf.w, 30, style="F")
    pdf.set_y(7)
    pdf.set_font("Helvetica", "B", 19)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(L, 9, _lat(f"Nível do {NOME_RIO} - {CIDADE}"), align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(170, 190, 220)
    pdf.cell(L, 5, _lat(f"Estação {NOME_ESTACAO} (ANA {CODIGO_ESTACAO})  -  "
                        f"Período do gráfico: {range_nome}  -  "
                        f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}"),
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(36)

    # ---------- destaque: nível + selo de status ----------
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*PDF_CINZA)
    pdf.cell(L, 5, "Nível atual", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 30)
    pdf.set_text_color(*PDF_AZUL)
    pdf.cell(L, 13, _lat(f"{fmt(nivel)} m"), align="C",
             new_x="LMARGIN", new_y="NEXT")

    tend = ""
    if tx1 is not None:
        rotulo = ("Subindo" if tx1 > 0.2 else
                  ("Baixando" if tx1 < -0.2 else "Estável"))
        tend = f" - {rotulo} {abs(tx1):.1f} cm/h"
    selo_txt = _lat(f"{rot_status}{tend}")
    pdf.set_font("Helvetica", "B", 11)
    selo_w = pdf.get_string_width(selo_txt) + 12
    selo_h = 8.5
    selo_x = X0 + (L - selo_w) / 2
    selo_y = pdf.get_y() + 1
    pdf.set_fill_color(*PDF_STATUS.get(cor_status, PDF_STATUS["secondary"]))
    pdf.rect(selo_x, selo_y, selo_w, selo_h, style="F",
             round_corners=True, corner_radius=selo_h / 2)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(selo_x, selo_y)
    pdf.cell(selo_w, selo_h, selo_txt, align="C")
    pdf.set_y(selo_y + selo_h + 2)

    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*PDF_CINZA)
    if proj:
        pdf.cell(L, 5, _lat(proj), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(L, 5, _lat(f"Última leitura: {quando}"), align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ---------- KPIs em cards arredondados (2 linhas x 3) ----------
    def linha_cards(trios, altura=17):
        gap = 4
        w = (L - gap * (len(trios) - 1)) / len(trios)
        y = pdf.get_y()
        for i, (rotulo, valor) in enumerate(trios):
            x = X0 + i * (w + gap)
            pdf.set_fill_color(*PDF_CARD)
            pdf.set_draw_color(*PDF_CARD_BORDA)
            pdf.rect(x, y, w, altura, style="FD",
                     round_corners=True, corner_radius=2.5)
            pdf.set_xy(x, y + 2.5)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(*PDF_CINZA)
            pdf.cell(w, 4, _lat(rotulo), align="C")
            pdf.set_xy(x, y + 8)
            pdf.set_font("Helvetica", "B", 12.5)
            pdf.set_text_color(*PDF_TEXTO)
            pdf.cell(w, 6, _lat(valor), align="C")
        pdf.set_y(y + altura + 3)

    linha_cards([
        ("Cota de inundação", f"{COTA_INUNDACAO:.2f} m"),
        ("Média do período (30d)", f"{fmt(media)} m"),
        ("Mín / Máx (30d)", f"{fmt(minimo)} / {fmt(maximo)} m"),
    ])
    linha_cards([
        ("Chuva hoje (estação)", f"{fmt(chuva_hoje, 1)} mm"),
        ("Vazão (última)", f"{fmt(vazao, 1)} m³/s"),
        ("Variação 3h / 6h / 12h (cm/h)",
         " / ".join(f"{t:+.1f}" if t is not None else "-"
                    for t in (tx3, tx6, tx12))),
    ])
    pdf.ln(1)

    # ---------- gráfico ----------
    if png:
        pdf.image(io.BytesIO(png), x=X0, w=L)
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*PDF_CINZA)
        pdf.cell(L, 6, _lat("(Gráfico indisponível: instale o pacote "
                            "'matplotlib' e tente de novo.)"),
                 align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ---------- previsão 7 dias em mini-cards ----------
    if meteo and "daily" in meteo:
        d = meteo["daily"]
        n = len(d["time"])
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*PDF_TEXTO)
        pdf.cell(L, 6, _lat(f"Previsão do tempo em {NOME_ESTACAO} - 7 dias"),
                 align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)

        gap = 2.5
        w = (L - gap * (n - 1)) / n
        h = 27
        y = pdf.get_y()
        if y + h + 20 > pdf.h - pdf.b_margin:
            pdf.add_page()
            y = pdf.get_y()
        for i in range(n):
            x = X0 + i * (w + gap)
            dt = datetime.strptime(d["time"][i], "%Y-%m-%d")
            nome = "Hoje" if i == 0 else DIAS_PT[dt.weekday()]
            chuva = d["precipitation_sum"][i] or 0
            cor_ch = (185, 28, 28) if chuva >= 50 else \
                     ((180, 83, 9) if chuva >= 20 else (37, 99, 235))

            pdf.set_fill_color(*PDF_CARD)
            pdf.set_draw_color(*PDF_CARD_BORDA)
            pdf.rect(x, y, w, h, style="FD",
                     round_corners=True, corner_radius=2.5)

            pdf.set_xy(x, y + 2)
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*PDF_TEXTO)
            pdf.cell(w, 4, _lat(nome), align="C")

            pdf.set_xy(x, y + 7)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(w, 4.5, _lat(f"{d['temperature_2m_max'][i]:.0f}° / "
                                  f"{d['temperature_2m_min'][i]:.0f}°"),
                     align="C")

            pdf.set_xy(x, y + 12.5)
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*cor_ch)
            pdf.cell(w, 4, _lat(f"{chuva:.1f} mm"), align="C")

            pdf.set_xy(x, y + 17)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*PDF_CINZA)
            pdf.cell(w, 3.5, _lat(f"{d['precipitation_probability_max'][i]:.0f}%"),
                     align="C")

            pdf.set_xy(x, y + 21)
            pdf.cell(w, 3.5,
                     _lat(f"{d['windspeed_10m_max'][i]:.0f} km/h "
                          f"{direcao_abrev(d['winddirection_10m_dominant'][i])}"),
                     align="C")
        pdf.set_y(y + h + 4)

    # ---------- rodapé ----------
    pdf.set_draw_color(*PDF_CARD_BORDA)
    pdf.line(X0, pdf.get_y(), X0 + L, pdf.get_y())
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*PDF_CINZA)
    rodape = (f"Realização: {REALIZACAO}{(' ' + PARCERIA) if PARCERIA else ''}  "
              "-  Fonte hidrológica: ANA / HidroWebService  -  "
              "Meteorologia: Open-Meteo\n"
              "Projeto informativo e experimental. As projeções são estimativas "
              "aritméticas simples e NÃO substituem os alertas oficiais. "
              "Consulte sempre a Defesa Civil.")
    if PICO_HISTORICO:
        rodape = (f"Pico histórico de referência: {PICO_HISTORICO:.2f} m\n"
                  + rodape)
    pdf.multi_cell(L, 3.8, _lat(rodape), align="C")

    return bytes(pdf.output())


# ============================================================
# ===== CÉLULA 6 — Aplicativo Dash ===========================
# ============================================================
CENTRO = {"textAlign": "center"}

# A interface agora usa VARIÁVEIS CSS para as cores, e o tema
# (dark/light) é trocado adicionando data-theme="light" no <html>.
# Assim quase nenhum componente precisa ser reconstruído na troca.
CSS_TEMAS = """
:root, [data-theme="dark"] {
  --fundo: #0b1220;   --card: #141d33;  --card2: #1a2542;
  --texto: #e6edf3;   --agua: #38bdf8;  --borda: rgba(255,255,255,.06);
  --sombra: none;
  --chuva-baixa: #60a5fa; --chuva-media: #facc15; --chuva-alta: #f87171;
  --sobe: #f87171; --desce: #4ade80; --estavel: #9ca3af;
}
[data-theme="light"] {
  --fundo: #eef2f7;   --card: #ffffff;  --card2: #f1f5f9;
  --texto: #0f172a;   --agua: #0284c7;  --borda: rgba(15,23,42,.10);
  --sombra: 0 1px 4px rgba(15,23,42,.08);
  --chuva-baixa: #2563eb; --chuva-media: #b45309; --chuva-alta: #dc2626;
  --sobe: #dc2626; --desce: #16a34a; --estavel: #64748b;
}
html, body { background-color: var(--fundo); }
body, .accordion-body { color: var(--texto); transition: background-color .25s, color .25s; }
/* O tema BOOTSTRAP define texto escuro dentro de .card — aqui fazemos o
   card herdar a cor do tema atual (dinâmica), senão as letras "somem"
   sobre o fundo escuro. Redefinir a variável --bs-card-color não afeta
   as cores específicas (.cor-agua, alertas, taxas, chuva etc.). */
.card { --bs-card-color: var(--texto); color: var(--texto); }
.painel-card { background-color: var(--card) !important; border: 1px solid var(--borda) !important;
               border-radius: 14px; box-shadow: var(--sombra); transition: background-color .25s; }
/* Botões cinza (Modo claro / Exportar PDF) legíveis no fundo escuro */
[data-theme="dark"] .btn-outline-secondary {
  color: var(--texto); border-color: rgba(255,255,255,.35); }
[data-theme="dark"] .btn-outline-secondary:hover {
  color: #0b1220; background-color: var(--texto); }
.painel-card2 { background-color: var(--card2) !important; }
.cor-agua { color: var(--agua) !important; }
.chuva-baixa { color: var(--chuva-baixa); font-weight: 600; }
.chuva-media { color: var(--chuva-media); font-weight: 600; }
.chuva-alta  { color: var(--chuva-alta);  font-weight: 600; }
.taxa-sobe    { color: var(--sobe); }
.taxa-desce   { color: var(--desce); }
.taxa-estavel { color: var(--estavel); }
/* Acordeão (FAQ) acompanhando o tema */
.accordion, .accordion-item { background-color: var(--card) !important;
                              border-color: var(--borda) !important; }
.accordion-body { background-color: var(--card) !important; }
.accordion-button { background-color: var(--card) !important;
                    color: var(--texto) !important; box-shadow: none !important; }
.accordion-button:not(.collapsed) { color: var(--agua) !important; }
[data-theme="dark"] .accordion-button::after {
  filter: invert(1) grayscale(100%) brightness(200%); }
"""

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP],
                title=f"Nível do {NOME_RIO} - {CIDADE}")
server = app.server   # necessário para gunicorn/hospedagem

app.index_string = f"""<!DOCTYPE html>
<html>
  <head>
    {{%metas%}}
    <title>{{%title%}}</title>
    {{%favicon%}}
    {{%css%}}
    <style>{CSS_TEMAS}</style>
  </head>
  <body>
    {{%app_entry%}}
    <footer>{{%config%}}{{%scripts%}}{{%renderer%}}</footer>
  </body>
</html>"""


def card(children, **kw):
    classes = ("painel-card " + kw.pop("className", "")).strip()
    style = kw.pop("style", {})
    return dbc.Card(dbc.CardBody(children), className=classes,
                    style=style, **kw)


def kpi(titulo, id_valor, sufixo=""):
    return card([
        html.Div(titulo, style={"fontSize": ".8rem", "opacity": .65, **CENTRO}),
        html.Div([
            html.Span(id=id_valor, style={"fontSize": "1.7rem", "fontWeight": 700}),
            html.Span(f" {sufixo}", style={"fontSize": ".95rem", "opacity": .65}),
        ], style=CENTRO),
    ])


app.layout = dbc.Container([
    # ---------- Tema: preferência salva no navegador + aplicador ----------
    dcc.Store(id="tema-sel", data="dark", storage_type="local"),
    html.Div(id="tema-dummy", style={"display": "none"}),

    # ---------- Botão de tema (canto superior direito) ----------
    html.Div(
        dbc.Button(id="btn-tema", size="sm", outline=True, color="secondary"),
        style={"position": "absolute", "top": "14px", "right": "18px",
               "zIndex": 10}),

    # ---------- Cabeçalho ----------
    html.Div([
        html.H2(f"Nível do {NOME_RIO} - {CIDADE}",
                style={"marginBottom": 0, "fontWeight": 800}),
        html.Div(f"Monitoramento em tempo quase-real · "
                 f"Estação {NOME_ESTACAO} (ANA {CODIGO_ESTACAO})",
                 style={"opacity": .65}),
    ], style={"padding": "1.1rem 0 .6rem", **CENTRO}),

    html.Div(id="banner-erro"),

    # ---------- Avisos meteorológicos (INMET) ----------
    html.Div(id="alertas-inmet"),

    # ---------- Destaque: nível + status ----------
    card([
        dbc.Row([
            dbc.Col([
                html.Div("Nível atual", style={"opacity": .65}),
                html.Div([
                    html.Span(id="hero-nivel", className="cor-agua",
                              style={"fontSize": "3.4rem", "fontWeight": 800}),
                    html.Span(" m", style={"fontSize": "1.4rem", "opacity": .6}),
                ]),
                html.Div(id="hero-atualizado", style={"opacity": .6,
                                                      "fontSize": ".85rem"}),
            ], md=4, style=CENTRO),
            dbc.Col([
                html.Div(id="hero-status", className="mb-2"),
                html.Div(id="hero-projecao", style={"opacity": .8,
                                                    "fontSize": ".92rem"}),
            ], md=8,
                className="d-flex flex-column justify-content-center "
                          "align-items-center", style=CENTRO),
        ]),
    ], className="painel-card2"),

    html.Div(style={"height": ".6rem"}),

    # ---------- KPIs ----------
    dbc.Row([
        dbc.Col(kpi("Cota de inundação", "kpi-cota-inund", "m"), md=2, xs=6),
        dbc.Col(kpi("Média do período", "kpi-media", "m"), md=2, xs=6),
        dbc.Col(kpi("Mín / Máx do período", "kpi-minmax", "m"), md=3, xs=6),
        dbc.Col(kpi("Chuva hoje (estação)", "kpi-chuva-hoje", "mm"), md=2, xs=6),
        dbc.Col(kpi("Vazão (última)", "kpi-vazao", "m³/s"), md=3, xs=6),
    ], className="g-2"),

    html.Div(style={"height": ".6rem"}),

    # ---------- Tendências ----------
    card([
        html.Div("Variação média do nível",
                 style={"fontWeight": 700, "marginBottom": ".4rem", **CENTRO}),
        dbc.Row([
            dbc.Col(html.Div(id="tx-3h"), md=4, xs=4),
            dbc.Col(html.Div(id="tx-6h"), md=4, xs=4),
            dbc.Col(html.Div(id="tx-12h"), md=4, xs=4),
        ]),
    ]),

    html.Div(style={"height": ".8rem"}),

    # ---------- Seletor de período + gráfico ----------
    html.Div(
        dbc.ButtonGroup(
            [dbc.Button(r, id={"type": "btn-range", "index": r},
                        color="info", outline=True, size="sm") for r in RANGES],
            className="mb-2"),
        className="d-flex justify-content-center"),
    dcc.Store(id="range-sel", data="7 dias"),

    card(dcc.Graph(id="grafico-serie", config={"displayModeBar": False})),

    html.Div(style={"height": "1rem"}),

    # ---------- Meteorologia ----------
    card([
        html.Div(f"Previsão do tempo em {NOME_ESTACAO} — 7 dias",
                 style={"fontWeight": 700, "marginBottom": ".5rem", **CENTRO}),
        html.Div(id="meteo-cards"),
        html.Div(id="meteo-vento", className="mt-3", style=CENTRO),
        html.Div("Fonte: Open-Meteo (open-meteo.com)",
                 style={"opacity": .5, "fontSize": ".78rem",
                        "marginTop": ".5rem", **CENTRO}),
    ]),

    html.Div(style={"height": "1rem"}),

    # ---------- FAQ (respostas dinâmicas) ----------
    card([
        html.Div("Perguntas Frequentes",
                 style={"fontWeight": 700, "fontSize": "1.15rem",
                        "marginBottom": ".8rem", **CENTRO}),
        dbc.Accordion([
            dbc.AccordionItem(
                html.Div(id="faq-nivel"), item_id="faq-1",
                title=f"Qual é o nível atual do {NOME_RIO} em {NOME_ESTACAO}?"),
            dbc.AccordionItem(
                html.Div(id="faq-chuva"), item_id="faq-2",
                title=f"Vai chover em {NOME_ESTACAO} hoje?"),
            dbc.AccordionItem(
                html.Div(id="faq-enchente"), item_id="faq-3",
                title=f"A chuva pode causar enchente em {NOME_ESTACAO}?"),
            dbc.AccordionItem(
                html.Div(id="faq-alerta"), item_id="faq-4",
                title="Há algum alerta meteorológico vigente para a região?"),
            dbc.AccordionItem(
                html.Div([
                    "Os dados hidrológicos vêm da telemetria da estação "
                    f"{NOME_ESTACAO} (código {CODIGO_ESTACAO}), operada pela "
                    "rede da Agência Nacional de Águas e Saneamento Básico "
                    "(ANA) com o Serviço Geológico do Brasil (SGB/CPRM). "
                    "O painel busca novos dados automaticamente a cada "
                    "15 minutos; a estação costuma registrar leituras de "
                    "15 em 15 minutos ou de hora em hora."
                ]), item_id="faq-5",
                title="Com que frequência os dados são atualizados?"),
            dbc.AccordionItem(
                html.Div([
                    html.Div([html.B("Normal: "),
                              f"abaixo da cota de atenção ({COTA_ATENCAO:.2f} m)."]),
                    html.Div([html.B("Atenção: "),
                              f"a partir de {COTA_ATENCAO:.2f} m — acompanhe a evolução."]),
                    html.Div([html.B("Alerta: "),
                              f"a partir de {COTA_ALERTA:.2f} m — o rio se aproxima "
                              "da cota de inundação."]),
                    html.Div([html.B("Inundação: "),
                              f"a partir de {COTA_INUNDACAO:.2f} m — o nível "
                              "ultrapassou a cota de inundação."]),
                    html.Div("As cotas são valores de referência; siga sempre "
                             "as orientações da Defesa Civil.",
                             style={"opacity": .7, "marginTop": ".4rem",
                                    "fontSize": ".85rem"}),
                ]), item_id="faq-6",
                title="O que significam os status de nível?"),
            dbc.AccordionItem(
                html.Div([
                    "Sim. O gráfico interativo acima permite visualizar o "
                    "histórico de 24 horas até 30 dias. Selecione o período "
                    "nos botões logo acima do gráfico. Você também pode "
                    "exportar um relatório em PDF com o botão no fim da página."
                ]), item_id="faq-7",
                title="Posso ver o histórico do nível?"),
            dbc.AccordionItem(
                html.Div([
                    "Nível, chuva e vazão: ANA / HidroWebService (estação "
                    f"{CODIGO_ESTACAO}). Previsão do tempo e vento: Open-Meteo. "
                    "Avisos meteorológicos: INMET (alertas.inmet.gov.br). "
                    "Este é um projeto informativo e experimental — em situação "
                    "de risco, consulte a Defesa Civil (telefone 199)."
                ]), item_id="faq-8",
                title="De onde vêm os dados?"),
        ], start_collapsed=True, flush=True),
    ]),

    html.Div(style={"height": "1rem"}),

    # ---------- Ações ----------
    html.Div([
        dbc.Button("Atualizar agora", id="btn-atualizar", color="info",
                   className="me-2"),
        dcc.Loading(
            dbc.Button("Exportar PDF", id="btn-pdf", color="secondary",
                       outline=True),
            type="circle", color="#38bdf8",
            style={"display": "inline-block"}),
        dcc.Download(id="download-pdf"),
    ], className="d-flex justify-content-center align-items-center"),
    html.Div(id="pdf-aviso", style={"fontSize": ".8rem", "opacity": .7,
                                    "marginTop": ".4rem", **CENTRO}),

    # ---------- Rodapé ----------
    html.Hr(style={"opacity": .2, "marginTop": "1.5rem"}),
    html.Div([
        html.Div([html.B("Realização: "), REALIZACAO,
                  f" {PARCERIA}" if PARCERIA else ""]),
        html.Div(["Fonte dos dados hidrológicos: ",
                  html.A("ANA / HidroWebService",
                         href="https://www.snirh.gov.br/hidroweb-mobile/mapa/historico-estacao/87380000",
                         target="_blank", className="cor-agua"),
                  " · Meteorologia: Open-Meteo"]),
        html.Div(f"Pico histórico de referência: {PICO_HISTORICO:.2f} m"
                 if PICO_HISTORICO else "",
                 style={"marginTop": ".2rem"}),
        html.Div("Projeto informativo e experimental. As projeções são estimativas "
                 "aritméticas simples e NÃO substituem os alertas oficiais. "
                 "Consulte sempre a Defesa Civil.",
                 style={"opacity": .6, "fontSize": ".8rem", "marginTop": ".4rem"}),
    ], style={"opacity": .85, "fontSize": ".85rem",
              "paddingBottom": "2rem", **CENTRO}),

    dcc.Interval(id="intervalo", interval=15 * 60 * 1000, n_intervals=0),
    dcc.Store(id="store-dados"),
], fluid=True, style={"maxWidth": LARGURA_MAX, "position": "relative",
                      "color": "var(--texto)",
                      "backgroundColor": "var(--fundo)", "minHeight": "100vh",
                      "paddingTop": ".2rem"})


# ============================================================
# ===== CÉLULA 6B — Callbacks ================================
# ============================================================
# --- Tema: aplica o atributo data-theme no <html> (clientside = instantâneo)
app.clientside_callback(
    """
    function(tema) {
        document.documentElement.setAttribute('data-theme', tema || 'dark');
        return '';
    }
    """,
    Output("tema-dummy", "children"),
    Input("tema-sel", "data"),
)


@app.callback(
    Output("tema-sel", "data"),
    Input("btn-tema", "n_clicks"),
    State("tema-sel", "data"),
    prevent_initial_call=True,
)
def alternar_tema(_n, atual):
    return "light" if atual == "dark" else "dark"


@app.callback(Output("btn-tema", "children"), Input("tema-sel", "data"))
def rotulo_tema(tema):
    return "☀️ Modo claro" if tema == "dark" else "🌙 Modo escuro"


@app.callback(
    Output("store-dados", "data"),
    Input("intervalo", "n_intervals"),
    Input("btn-atualizar", "n_clicks"),
)
def atualizar_store(_i, _c):
    try:
        df, meteo, alertas = carregar_dados()
        return {"ok": True, "erro": None, "meteo": meteo, "alertas": alertas,
                "df": df.to_json(date_format="iso", orient="split")}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print("Erro ao carregar dados:", msg)
        return {"ok": False, "erro": msg, "df": None, "meteo": None}


@app.callback(
    Output("range-sel", "data"),
    Input({"type": "btn-range", "index": dash.ALL}, "n_clicks"),
    State("range-sel", "data"),
    prevent_initial_call=True,
)
def escolher_range(_cliques, atual):
    trig = callback_context.triggered_id
    return trig["index"] if trig else atual


@app.callback(
    Output("download-pdf", "data"),
    Output("pdf-aviso", "children"),
    Input("btn-pdf", "n_clicks"),
    State("store-dados", "data"),
    State("range-sel", "data"),
    prevent_initial_call=True,
)
def exportar_pdf(_n, payload, range_nome):
    if not payload or not payload.get("ok") or not payload.get("df"):
        return None, "Sem dados carregados para exportar."
    try:
        df = pd.read_json(io.StringIO(payload["df"]), orient="split")
        df["data_hora"] = pd.to_datetime(df["data_hora"], errors="coerce")
        if df.empty:
            return None, "Sem dados carregados para exportar."
        pdf_bytes = gerar_pdf_bytes(df, range_nome, payload.get("meteo"))
        nome = (f"nivel_sinos_campo_bom_"
                f"{datetime.now().strftime('%Y-%m-%d_%H%M')}.pdf")
        return dcc.send_bytes(pdf_bytes, nome), ""
    except ImportError:
        return None, ("Para exportar, instale os pacotes: "
                      "pip install fpdf2 matplotlib")
    except Exception as e:
        print("Erro ao gerar PDF:", e)
        return None, f"Erro ao gerar o PDF: {type(e).__name__}: {e}"


def linha_taxa(rotulo, taxa):
    if taxa is None:
        return html.Div([html.Div(rotulo, style={"opacity": .6,
                                                 "fontSize": ".8rem"}),
                         html.Div("—", style={"fontSize": "1.2rem"})],
                        style=CENTRO)
    seta, classe = ("▲", "taxa-sobe") if taxa > 0.2 else \
                   (("▼", "taxa-desce") if taxa < -0.2 else ("▬", "taxa-estavel"))
    return html.Div([
        html.Div(rotulo, style={"opacity": .6, "fontSize": ".8rem"}),
        html.Div(f"{seta} {taxa:+.1f} cm/h", className=classe,
                 style={"fontSize": "1.2rem", "fontWeight": 700}),
    ], style=CENTRO)


def cartoes_meteo(meteo):
    if not meteo or "daily" not in meteo:
        return html.Div("Previsão indisponível no momento.",
                        style={"opacity": .6, **CENTRO})
    d = meteo["daily"]
    cartoes = []
    for i, dia in enumerate(d["time"]):
        dt = datetime.strptime(dia, "%Y-%m-%d")
        nome = "Hoje" if i == 0 else DIAS_PT[dt.weekday()]
        chuva = d["precipitation_sum"][i]
        prob = d["precipitation_probability_max"][i]
        classe_chuva = "chuva-alta" if (chuva or 0) >= 50 else \
                       ("chuva-media" if (chuva or 0) >= 20 else "chuva-baixa")
        cartoes.append(dbc.Col(card([
            html.Div(nome, style={"fontWeight": 700, "textAlign": "center"}),
            html.Div(f"{d['temperature_2m_max'][i]:.0f}°",
                     style={"fontSize": "1.3rem", "fontWeight": 700,
                            "textAlign": "center"}),
            html.Div(f"{d['temperature_2m_min'][i]:.0f}°",
                     style={"opacity": .6, "textAlign": "center"}),
            html.Div(f"{chuva:.1f} mm", className=classe_chuva,
                     style={"textAlign": "center"}),
            html.Div(f"{prob:.0f}%", style={"opacity": .6, "fontSize": ".8rem",
                                            "textAlign": "center"}),
            html.Div(f"💨 {d['windspeed_10m_max'][i]:.0f} km/h "
                     f"{direcao_pt(d['winddirection_10m_dominant'][i])}",
                     style={"opacity": .7, "fontSize": ".72rem",
                            "textAlign": "center", "marginTop": ".2rem"}),
        ], className="painel-card2", style={"padding": "0"}),
            xs=6, md=True))
    return dbc.Row(cartoes, className="g-2")


def bloco_vento(meteo):
    cw = (meteo or {}).get("current_weather")
    if not cw:
        return None
    return html.Div([
        html.B("Vento agora: "),
        f"{cw['windspeed']:.0f} km/h, vindo de {direcao_pt(cw['winddirection'])}",
    ])


LINK_INMET = html.A("alertas.inmet.gov.br",
                    href="https://alertas.inmet.gov.br", target="_blank",
                    className="cor-agua")


def _cor_severidade(sev):
    s = (sev or "").lower()
    if "grande" in s or "extreme" in s:
        return "danger"
    if s.startswith("perigo") and "potencial" not in s or "severe" in s:
        return "danger"
    return "warning"          # Perigo Potencial / Moderate / desconhecido


def bloco_alertas(alertas):
    """Card de avisos do INMET. alertas: lista, [] (sem avisos) ou None (falha)."""
    rodape_dc = html.Div(
        ["Fonte: INMET (", LINK_INMET, "). Em emergência, Defesa Civil: 199."],
        style={"opacity": .55, "fontSize": ".78rem", "marginTop": ".4rem",
               **CENTRO})

    if alertas is None:
        corpo = html.Div(
            ["Não foi possível consultar os avisos do INMET agora. ",
             "Verifique diretamente em ", LINK_INMET, "."],
            style={"opacity": .75, **CENTRO})
        return card([corpo, rodape_dc], className="mb-2")

    if not alertas:
        corpo = dbc.Alert(
            html.Div([html.B("Sem avisos meteorológicos vigentes "),
                      f"do INMET para {NOME_ESTACAO} no momento."],
                     style=CENTRO),
            color="success", className="mb-0 py-2")
        return card([corpo, rodape_dc], className="mb-2")

    itens = []
    for av in alertas:
        detalhes = []
        if av.get("severidade"):
            detalhes.append(f"Severidade: {av['severidade']}")
        vig = texto_vigencia(av.get("inicio"), av.get("fim"))
        if vig:
            detalhes.append(vig)
        itens.append(dbc.Alert([
            html.Div([html.B(f"⚠ {av.get('evento', 'Aviso meteorológico')}")],
                     style=CENTRO),
            html.Div(" · ".join(detalhes),
                     style={"fontSize": ".85rem", **CENTRO}) if detalhes else None,
            html.Div(av["riscos"], style={"fontSize": ".82rem", "opacity": .85,
                                          "marginTop": ".25rem", **CENTRO})
            if av.get("riscos") else None,
        ], color=_cor_severidade(av.get("severidade")), className="mb-2 py-2"))
    itens.append(rodape_dc)
    return card(itens, className="mb-2")


def faq_dinamico(df, meteo, alertas):
    """Respostas dinâmicas do FAQ (nível, chuva, enchente, alerta)."""
    aguardando = html.Div("Aguardando dados da estação...",
                          style={"opacity": .6})

    # --- nível atual ---
    if df is None or df.empty or pd.isna(df["cota_m"].iloc[-1]):
        r_nivel = aguardando
        nivel = None
    else:
        ultimo = df.iloc[-1]
        nivel = ultimo["cota_m"]
        r_nivel = html.Div([
            "A última medição registrada foi de ",
            html.B(f"{nivel:.2f} metros"),
            f", em {ultimo['data_hora'].strftime('%d/%m/%Y às %H:%M')}. "
            f"A cota de inundação é de {COTA_INUNDACAO:.2f} metros. "
            f"Os dados são da estação telemétrica da ANA "
            f"(código {CODIGO_ESTACAO}).",
        ])

    # --- chuva prevista ---
    if meteo and "daily" in meteo:
        d = meteo["daily"]
        chuva_hoje = d["precipitation_sum"][0] or 0
        chuva_7d = sum(v or 0 for v in d["precipitation_sum"])
        r_chuva = html.Div([
            "A previsão indica ", html.B(f"{chuva_hoje:.1f} mm"),
            " de chuva para hoje. Nos próximos 7 dias, são esperados ",
            html.B(f"{chuva_7d:.1f} mm"), " acumulados. (Fonte: Open-Meteo)",
        ])
    else:
        r_chuva = html.Div("Previsão indisponível no momento.",
                           style={"opacity": .6})

    # --- risco de enchente ---
    if nivel is None:
        r_enchente = aguardando
    elif nivel < COTA_INUNDACAO:
        r_enchente = html.Div([
            f"O {NOME_RIO} está atualmente a ",
            html.B(f"{COTA_INUNDACAO - nivel:.2f} metros"),
            f" abaixo da cota de inundação de {COTA_INUNDACAO:.2f} metros. "
            "Chuvas intensas na bacia hidrográfica podem elevar o nível do "
            "rio. Acompanhe o gráfico de chuva e a tendência do nível nesta "
            "página.",
        ])
    else:
        r_enchente = html.Div([
            f"O {NOME_RIO} ultrapassou a cota de inundação em ",
            html.B(f"{nivel - COTA_INUNDACAO:.2f} metros"),
            ". Siga as orientações da Defesa Civil (telefone 199).",
        ])

    # --- alertas ---
    if alertas is None:
        r_alerta = html.Div(["Não foi possível consultar os avisos do INMET "
                             "agora. Verifique em ", LINK_INMET, "."])
    elif not alertas:
        r_alerta = html.Div([
            "Não. No momento ", html.B("não há avisos meteorológicos"),
            f" do INMET vigentes para {NOME_ESTACAO}. Confira também a "
            "Defesa Civil do seu município.",
        ])
    else:
        nomes = "; ".join(a.get("evento", "aviso") for a in alertas)
        r_alerta = html.Div([
            "Sim. Há ", html.B(f"{len(alertas)} aviso(s)"),
            f" do INMET vigente(s) que incluem {NOME_ESTACAO}: {nomes}. "
            "Veja os detalhes no card de avisos no topo da página.",
        ])

    return r_nivel, r_chuva, r_enchente, r_alerta


@app.callback(
    Output("grafico-serie", "figure"),
    Output("hero-nivel", "children"),
    Output("hero-atualizado", "children"),
    Output("hero-status", "children"),
    Output("hero-projecao", "children"),
    Output("kpi-cota-inund", "children"),
    Output("kpi-media", "children"),
    Output("kpi-minmax", "children"),
    Output("kpi-chuva-hoje", "children"),
    Output("kpi-vazao", "children"),
    Output("tx-3h", "children"),
    Output("tx-6h", "children"),
    Output("tx-12h", "children"),
    Output("meteo-cards", "children"),
    Output("meteo-vento", "children"),
    Output("alertas-inmet", "children"),
    Output("faq-nivel", "children"),
    Output("faq-chuva", "children"),
    Output("faq-enchente", "children"),
    Output("faq-alerta", "children"),
    Output("banner-erro", "children"),
    Input("store-dados", "data"),
    Input("range-sel", "data"),
    Input("tema-sel", "data"),       # <- gráfico troca de tema junto
)
def render(payload, range_nome, tema):
    tema = tema or "dark"
    vazio = figura_vazia(tema)

    def saida(fig=vazio, nivel_txt="—", atualizado="", badge="", proj="",
              media="—", minmax="—", chuva="—", vazao="—",
              taxas=(None, None, None), meteo=None, alertas=None,
              df=None, banner=None):
        faq = faq_dinamico(df, meteo, alertas)
        return (fig, nivel_txt, atualizado, badge, proj,
                f"{COTA_INUNDACAO:.2f}", media, minmax, chuva, vazao,
                linha_taxa("Últimas 3h", taxas[0]),
                linha_taxa("Últimas 6h", taxas[1]),
                linha_taxa("Últimas 12h", taxas[2]),
                cartoes_meteo(meteo), bloco_vento(meteo),
                bloco_alertas(alertas), *faq, banner)

    if not payload:
        return saida()
    if not payload.get("ok"):
        banner = dbc.Alert([html.B("Não foi possível carregar os dados. "),
                            html.Span(payload.get("erro", ""))],
                           color="danger", className="mb-3")
        return saida(banner=banner)

    df = pd.read_json(io.StringIO(payload["df"]), orient="split")
    df["data_hora"] = pd.to_datetime(df["data_hora"], errors="coerce")
    meteo = payload.get("meteo")
    alertas = payload.get("alertas")

    if df.empty:
        return saida(meteo=meteo, alertas=alertas)

    # ----- números principais -----
    ultimo = df.iloc[-1]
    nivel = ultimo["cota_m"] if pd.notna(ultimo["cota_m"]) else None
    quando = ultimo["data_hora"].strftime("%d/%m/%Y %H:%M")

    tx1, tx3, tx6, tx12 = (taxa_cm_h(df, h) for h in (1, 3, 6, 12))
    rot_status, cor_status = status_nivel(nivel)
    if tx1 is not None:
        tend = "Subindo" if tx1 > 0.2 else ("Baixando" if tx1 < -0.2 else "Estável")
        badge_txt = f"{rot_status} — {tend} {abs(tx1):.1f} cm/h"
    else:
        badge_txt = rot_status
    badge = dbc.Badge(badge_txt, color=cor_status, pill=True,
                      style={"fontSize": "1rem", "padding": ".55rem .9rem"})
    proj = projecao_texto(nivel, tx1) or ""

    hoje = df[df["data_hora"].dt.date == datetime.now().date()]
    chuva_hoje = hoje["chuva_mm"].sum(skipna=True) if not hoje.empty else None

    media = df["cota_m"].mean(skipna=True)
    minimo, maximo = df["cota_m"].min(skipna=True), df["cota_m"].max(skipna=True)
    vazao = ultimo["vazao_m3s"] if pd.notna(ultimo["vazao_m3s"]) else None

    # ----- gráfico do período escolhido -----
    dias = RANGES.get(range_nome, 7)
    ini = df["data_hora"].iloc[-1] - timedelta(days=dias)
    fig = montar_figura(df[df["data_hora"] >= ini], tema)

    return saida(fig=fig, nivel_txt=fmt(nivel),
                 atualizado=f"Atualizado em {quando} · {len(df)} registros/30d",
                 badge=badge, proj=proj, media=fmt(media),
                 minmax=f"{fmt(minimo)} / {fmt(maximo)}",
                 chuva=fmt(chuva_hoje, 1), vazao=fmt(vazao, 1),
                 taxas=(tx3, tx6, tx12), meteo=meteo, alertas=alertas, df=df)


# ============================================================
# ===== CÉLULA 7 — Rodar =====================================
# ============================================================
if __name__ == "__main__":
    # No Colab: montar_drive(); diagnostico(); e então rode esta célula.
    try:
        app.run(debug=False, jupyter_mode="inline", jupyter_height=1200)
    except TypeError:
        app.run(debug=False, host="0.0.0.0",
                port=int(os.environ.get("PORT", 8050)))
