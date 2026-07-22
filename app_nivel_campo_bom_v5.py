# -*- coding: utf-8 -*-
"""
NÍVEL DOS SINOS — CAMPO BOM (Estação ANA 87380000) — v5
==============================================================================
Novidades da v4 (sobre a v3):
  - PDF totalmente redesenhado: faixa de cabeçalho, selo de status colorido,
    KPIs em cards arredondados, mini-cards da previsão de 7 dias.
  - O gráfico do PDF agora é desenhado com MATPLOTLIB (já vem no Colab).
    Não precisa mais de kaleido nem de Chrome — funciona sempre.

Fontes de dados:
  - Nível/chuva/vazão: API HidroWebService da ANA (estação 87380000)
  - Meteorologia: Open-Meteo (https://open-meteo.com) — gratuita, sem chave

Cada bloco "# ===== CÉLULA N =====" pode virar uma célula no Colab.
No VS Code: python app_nivel_campo_bom_v4.py
Para hospedar (Render etc.): gunicorn app_nivel_campo_bom_v4:server
"""

# ============================================================
# ===== CÉLULA 1 — Instalação (rodar só uma vez no Colab) =====
# ============================================================
# !pip install dash dash-bootstrap-components plotly pandas requests
# !pip install fpdf2 matplotlib   # <- para o Exportar PDF (kaleido NÃO é mais necessário)


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

DIAS_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


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


def carregar_dados():
    identificador, senha = ler_credenciais()
    token = gerar_token(identificador, senha)
    try:
        items = buscar_serie(token)
    except PermissionError:
        token = gerar_token(identificador, senha, forcar=True)
        items = buscar_serie(token)
    return processar_dados(items), buscar_meteo()


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
    return df


# ============================================================
# ===== CÉLULA 5D — Gráficos (tela e PDF) ====================
# ============================================================
COR_FUNDO = "#0b1220"
COR_CARD = "#141d33"
COR_CARD2 = "#1a2542"
COR_TEXTO = "#e6edf3"
COR_AGUA = "#38bdf8"
COR_CHUVA = "#60a5fa"

RANGES = {"24h": 1, "3 dias": 3, "7 dias": 7, "15 dias": 15, "30 dias": 30}


def montar_figura(dfr):
    """Gráfico combinado chuva+nível (Plotly) usado na TELA do app."""
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(
        x=dfr["data_hora"], y=dfr["chuva_mm"], name="Chuva (mm)",
        marker_color=COR_CHUVA, opacity=.55,
        hovertemplate="%{x|%d/%m %H:%M}<br>%{y:.1f} mm<extra>Chuva</extra>",
    ), secondary_y=True)
    fig.add_trace(go.Scatter(
        x=dfr["data_hora"], y=dfr["cota_m"], name="Nível (m)",
        mode="lines", line=dict(color=COR_AGUA, width=2.5),
        fill="tozeroy", fillcolor="rgba(56,189,248,.12)",
        hovertemplate="%{x|%d/%m %H:%M}<br>%{y:.2f} m<extra>Nível</extra>",
    ), secondary_y=False)

    for valor, texto, cor in [(COTA_ATENCAO, "Atenção", "#facc15"),
                              (COTA_ALERTA, "Alerta", "#fb923c"),
                              (COTA_INUNDACAO, "Inundação", "#ef4444")]:
        if valor is not None:
            fig.add_hline(y=valor, line_dash="dash", line_color=cor,
                          annotation_text=f"{texto} ({valor:.2f} m)",
                          annotation_position="top left",
                          annotation_font_color=cor, secondary_y=False)

    fig.update_layout(
        template="plotly_dark", paper_bgcolor=COR_CARD, plot_bgcolor=COR_CARD,
        height=440, margin=dict(l=45, r=45, t=30, b=40),
        hovermode="x unified", barmode="overlay",
        legend=dict(orientation="h", y=1.08, x=0),
    )
    fig.update_yaxes(title_text="Nível (m)", secondary_y=False, rangemode="tozero")
    fig.update_yaxes(title_text="Chuva (mm)", secondary_y=True,
                     showgrid=False, rangemode="tozero")
    return fig


def grafico_png_pdf(dfr):
    """Mesmo gráfico, em matplotlib (tema claro), como PNG para o PDF.
    Não depende de kaleido nem de navegador."""
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

    # --- cotas de referência (rótulos à esquerda, como na tela) ---
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
# Paleta do PDF (RGB, impressão em fundo claro)
PDF_AZUL_ESCURO = (13, 27, 62)     # faixa do cabeçalho
PDF_AZUL = (2, 132, 199)           # número do nível
PDF_TEXTO = (15, 23, 42)
PDF_CINZA = (100, 116, 139)
PDF_CARD = (241, 245, 249)         # fundo dos cards
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
    L = pdf.w - pdf.l_margin - pdf.r_margin   # largura útil (~190 mm)
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

    # selo (pílula) colorido central
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
        # quebra de página se não couber (cards + rodapé)
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

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.CYBORG],
                title=f"Nível do {NOME_RIO} - {CIDADE}")
server = app.server   # necessário para gunicorn/hospedagem


def card(children, **kw):
    style = {"backgroundColor": COR_CARD, "border": "none",
             "borderRadius": "14px"}
    style.update(kw.pop("style", {}))
    return dbc.Card(dbc.CardBody(children), style=style, **kw)


def kpi(titulo, id_valor, sufixo=""):
    return card([
        html.Div(titulo, style={"fontSize": ".8rem", "opacity": .65, **CENTRO}),
        html.Div([
            html.Span(id=id_valor, style={"fontSize": "1.7rem", "fontWeight": 700}),
            html.Span(f" {sufixo}", style={"fontSize": ".95rem", "opacity": .65}),
        ], style=CENTRO),
    ])


app.layout = dbc.Container([
    # ---------- Cabeçalho (centralizado) ----------
    html.Div([
        html.H2(f"Nível do {NOME_RIO} - {CIDADE}",
                style={"marginBottom": 0, "fontWeight": 800}),
        html.Div(f"Monitoramento em tempo quase-real · "
                 f"Estação {NOME_ESTACAO} (ANA {CODIGO_ESTACAO})",
                 style={"opacity": .65}),
    ], style={"padding": "1.1rem 0 .6rem", **CENTRO}),

    html.Div(id="banner-erro"),

    # ---------- Destaque: nível + status (centralizado) ----------
    card([
        dbc.Row([
            dbc.Col([
                html.Div("Nível atual", style={"opacity": .65}),
                html.Div([
                    html.Span(id="hero-nivel",
                              style={"fontSize": "3.4rem", "fontWeight": 800,
                                     "color": COR_AGUA}),
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
    ], style={"backgroundColor": COR_CARD2}),

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

    # ---------- Tendências (centralizado) ----------
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

    # ---------- Seletor de período (centralizado) + gráfico ----------
    html.Div(
        dbc.ButtonGroup(
            [dbc.Button(r, id={"type": "btn-range", "index": r},
                        color="info", outline=True, size="sm") for r in RANGES],
            className="mb-2"),
        className="d-flex justify-content-center"),
    dcc.Store(id="range-sel", data="7 dias"),

    card(dcc.Graph(id="grafico-serie", config={"displayModeBar": False})),

    html.Div(style={"height": "1rem"}),

    # ---------- Meteorologia (títulos centralizados) ----------
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

    # ---------- Ações (centralizadas) ----------
    html.Div([
        dbc.Button("Atualizar agora", id="btn-atualizar", color="info",
                   className="me-2"),
        dcc.Loading(
            dbc.Button("Exportar PDF", id="btn-pdf", color="secondary",
                       outline=True),
            type="circle", color=COR_AGUA,
            style={"display": "inline-block"}),
        dcc.Download(id="download-pdf"),
    ], className="d-flex justify-content-center align-items-center"),
    html.Div(id="pdf-aviso", style={"fontSize": ".8rem", "opacity": .7,
                                    "marginTop": ".4rem", **CENTRO}),

    # ---------- Rodapé (centralizado) ----------
    html.Hr(style={"opacity": .2, "marginTop": "1.5rem"}),
    html.Div([
        html.Div([html.B("Realização: "), REALIZACAO,
                  f" {PARCERIA}" if PARCERIA else ""]),
        html.Div(["Fonte dos dados hidrológicos: ",
                  html.A("ANA / HidroWebService",
                         href="https://www.snirh.gov.br/hidroweb-mobile/mapa/historico-estacao/87380000",
                         target="_blank", style={"color": COR_AGUA}),
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
], fluid=True, style={"maxWidth": "1050px", "color": COR_TEXTO,
                      "backgroundColor": COR_FUNDO, "minHeight": "100vh",
                      "paddingTop": ".2rem"})


# ============================================================
# ===== CÉLULA 6B — Callbacks ================================
# ============================================================
@app.callback(
    Output("store-dados", "data"),
    Input("intervalo", "n_intervals"),
    Input("btn-atualizar", "n_clicks"),
)
def atualizar_store(_i, _c):
    try:
        df, meteo = carregar_dados()
        return {"ok": True, "erro": None, "meteo": meteo,
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
    seta, cor = ("▲", "#f87171") if taxa > 0.2 else \
                (("▼", "#4ade80") if taxa < -0.2 else ("▬", "#9ca3af"))
    return html.Div([
        html.Div(rotulo, style={"opacity": .6, "fontSize": ".8rem"}),
        html.Div(f"{seta} {taxa:+.1f} cm/h",
                 style={"fontSize": "1.2rem", "fontWeight": 700, "color": cor}),
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
        cor_chuva = "#f87171" if (chuva or 0) >= 50 else \
                    ("#facc15" if (chuva or 0) >= 20 else COR_CHUVA)
        cartoes.append(dbc.Col(card([
            html.Div(nome, style={"fontWeight": 700, "textAlign": "center"}),
            html.Div(f"{d['temperature_2m_max'][i]:.0f}°",
                     style={"fontSize": "1.3rem", "fontWeight": 700,
                            "textAlign": "center"}),
            html.Div(f"{d['temperature_2m_min'][i]:.0f}°",
                     style={"opacity": .6, "textAlign": "center"}),
            html.Div(f"{chuva:.1f} mm", style={"color": cor_chuva,
                                               "textAlign": "center",
                                               "fontWeight": 600}),
            html.Div(f"{prob:.0f}%", style={"opacity": .6, "fontSize": ".8rem",
                                            "textAlign": "center"}),
            html.Div(f"💨 {d['windspeed_10m_max'][i]:.0f} km/h "
                     f"{direcao_pt(d['winddirection_10m_dominant'][i])}",
                     style={"opacity": .7, "fontSize": ".72rem",
                            "textAlign": "center", "marginTop": ".2rem"}),
        ], style={"backgroundColor": COR_CARD2, "padding": "0"}),
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
    Output("banner-erro", "children"),
    Input("store-dados", "data"),
    Input("range-sel", "data"),
)
def render(payload, range_nome):
    vazio = go.Figure().update_layout(
        template="plotly_dark", paper_bgcolor=COR_CARD, plot_bgcolor=COR_CARD,
        height=430, annotations=[dict(text="Aguardando dados...",
                                      showarrow=False,
                                      font=dict(color=COR_TEXTO, size=16))])
    padrao = (vazio, "—", "", "", "", f"{COTA_INUNDACAO:.2f}", "—", "—", "—",
              "—", linha_taxa("Últimas 3h", None), linha_taxa("Últimas 6h", None),
              linha_taxa("Últimas 12h", None), cartoes_meteo(None), None, None)

    if not payload:
        return padrao
    if not payload.get("ok"):
        banner = dbc.Alert([html.B("Não foi possível carregar os dados. "),
                            html.Span(payload.get("erro", ""))],
                           color="danger", className="mb-3")
        return padrao[:-1] + (banner,)

    df = pd.read_json(io.StringIO(payload["df"]), orient="split")
    df["data_hora"] = pd.to_datetime(df["data_hora"], errors="coerce")
    meteo = payload.get("meteo")

    if df.empty:
        return padrao[:-3] + (cartoes_meteo(meteo), bloco_vento(meteo), None)

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
    fig = montar_figura(df[df["data_hora"] >= ini])

    return (fig, fmt(nivel), f"Atualizado em {quando} · {len(df)} registros/30d",
            badge, proj, f"{COTA_INUNDACAO:.2f}", fmt(media),
            f"{fmt(minimo)} / {fmt(maximo)}", fmt(chuva_hoje, 1), fmt(vazao, 1),
            linha_taxa("Últimas 3h", tx3), linha_taxa("Últimas 6h", tx6),
            linha_taxa("Últimas 12h", tx12),
            cartoes_meteo(meteo), bloco_vento(meteo), None)


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
