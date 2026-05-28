"""
Отчёт по платежам Pyrus.
Фильтр по полю «Дата платежа» (id=37), только «Приход».
По умолчанию — текущий месяц. Есть режим диагностики.
"""

import time
import io
import calendar
import datetime as dt

import requests
import pandas as pd
import altair as alt
import streamlit as st

FORM_ID = 579058
DATE_FIELD_ID = 37
INCOME_TYPE_FIELD_ID = 34          # «Тип поступления»
INCOME_TYPE_FIELD_NAME = "Тип поступления"
WIDGET_FIELD_ID = 76              # «Виджет»
WIDGET_FIELD_NAME = "Виджет"
TYPE_FIELD_NAME = "Тип платежа"
TYPE_TARGET_VALUE = "Приход"
AUTH_URL = "https://accounts.pyrus.com/api/v4/auth"
PAGE_SIZE = 1000

# ---- фирменная палитра (в стиле Delomatika AI: тёмная тема + бирюзовый акцент) ----
ACCENT = "#11C5B5"          # бирюзовый акцент
ACCENT_DARK = "#0E9E91"     # притемнённый акцент (hover)
BG = "#0B1220"              # фон страницы
BG_PANEL = "#111A2B"        # панели/карточки
BG_ELEV = "#16223A"         # приподнятые элементы
TEXT = "#E8EEF6"            # основной текст
TEXT_MUTED = "#8A9BB3"      # приглушённый текст
BORDER = "#243349"          # границы

# Доп. отчёты: название -> список допустимых значений поля «Тип поступления»
INCOME_REPORTS = {
    "Поступления IT": [
        "Мой склад доработки",
        "Мой склад внедрение",
        "РитейлСРМ услуга сопровождения",
        "Услуга внедрения Амосрм",
        "Услуга внедрения телефонии",
        "Услуга внедрения Bitrix 24",
        "Услуга внедрения Pyrus",
        "Услуга внедрения Roistat",
        "Услуга внедрения Yclients",
        "Услуги разработчиков",
        "Услуга сопровождения Б24",
        "Услуга сопровождения Амо",
        "Услуга доработка Амосрм",
    ],
    "Поступления PPC": [
        "Услуга ведения РК",
        "Услуга настройки РК",
    ],
    "Поступления МП": [
        "Продвижение МП",
    ],
    "Поступления WBOX": {
        "values": ["Амосрм виджеты"],
        "require_nonempty": "Виджет",   # поле «Виджет» должно быть заполнено
    },
}


def _norm(s):
    return (s or "").strip().casefold()


def authenticate(login, key):
    r = requests.post(AUTH_URL, json={"login": login, "security_key": key}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Не удалось войти: {r.status_code} {r.text[:200]}")
    d = r.json()
    return d["api_url"], d["access_token"]


def api_get(api_url, token, path, params=None):
    url = api_url.rstrip("/") + "/" + path.lstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(5):
        r = requests.get(url, headers=headers, params=params, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("X-RateLimit-Reset", "30")), 60) + 1)
            continue
        if r.status_code in (500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"{path}: {r.status_code} {r.text[:300]}")
    raise RuntimeError(f"Не удалось получить {path}")


def person_str(p):
    fio = " ".join(x for x in [p.get("first_name"), p.get("last_name")] if x).strip()
    return f"{fio} <{p.get('email')}>" if fio and p.get("email") else (fio or p.get("email") or "")


def readable(field):
    t, v = field.get("type"), field.get("value")
    if v is None:
        return None
    if t == "catalog" and isinstance(v, dict):
        if v.get("values"):
            return " / ".join(map(str, v["values"]))
        if v.get("rows"):
            return " | ".join(", ".join(map(str, row)) for row in v["rows"])
        return str(v)
    if t in ("person", "author") and isinstance(v, dict):
        return person_str(v)
    if t == "multiple_choice" and isinstance(v, dict):
        return " / ".join(map(str, v.get("choice_names", [])))
    if t == "form_link" and isinstance(v, dict):
        return ", ".join(map(str, v.get("task_ids", [])))
    if t == "table" and isinstance(v, list):
        return f"[таблица: строк {len(v)}]"
    return v


def walk_fields(fields):
    """Все поля рекурсивно (включая колонки таблиц и вложенные)."""
    for f in fields or []:
        yield f
        info = f.get("info") or {}
        for key in ("columns", "fields"):
            yield from walk_fields(info.get(key))


def find_field_by_id(fields, fid):
    for f in walk_fields(fields):
        if f.get("id") == fid:
            return f
    return None


def extract_date_field_value(task, target_id):
    """Достаёт значение поля по id из задачи, в т.ч. из таблиц."""
    def visit(items):
        for f in items or []:
            if f.get("id") == target_id:
                return f.get("value")
            v = f.get("value")
            if isinstance(v, list):              # table
                for row in v:
                    found = visit(row.get("cells") or [])
                    if found is not None:
                        return found
            if isinstance(v, dict) and v.get("fields"):  # title/multiple_choice
                found = visit(v["fields"])
                if found is not None:
                    return found
        return None
    return visit(task.get("fields", []))


def extract_readable_by_id(task, target_id):
    """Извлекает читаемое значение поля по id (в т.ч. из таблицы, каталога)."""
    def visit(items):
        for f in items or []:
            if f.get("id") == target_id:
                return readable(f)
            v = f.get("value")
            if isinstance(v, list):
                for row in v:
                    found = visit(row.get("cells") or [])
                    if found is not None:
                        return found
            if isinstance(v, dict) and v.get("fields"):
                found = visit(v["fields"])
                if found is not None:
                    return found
        return None
    return visit(task.get("fields", []))


def parse_date_any(v):
    """Парсит дату из разных форматов: '2026-05-14', '14.05.2026', ISO с временем."""
    if v is None:
        return None
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return dt.datetime.strptime(s[:len(fmt)+0 if "%" in fmt else 10], fmt).date()
        except ValueError:
            continue
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


# ---------- основной режим ----------
def run_report(login, key, d_from, d_to, only_income):
    api_url, token = authenticate(login, key)
    schema = api_get(api_url, token, f"forms/{FORM_ID}")
    top_fields = schema.get("fields", [])

    date_field = find_field_by_id(top_fields, DATE_FIELD_ID)
    date_field_name = date_field.get("name") if date_field else None
    date_in_table = bool(date_field) and date_field not in top_fields

    type_field = next((f for f in top_fields if _norm(f.get("name")) == _norm(TYPE_FIELD_NAME)), None)
    type_name = type_field.get("name") if type_field else None

    money_cols = [f.get("name") for f in top_fields if f.get("type") == "money"]
    out_cols = [f.get("name") for f in top_fields]

    # Серверный фильтр fld37 работает и для поля внутри таблицы
    # (подтверждено диагностикой). Используем его всегда.
    use_server_filter = bool(date_field)

    rows, cursor = [], None
    seen, kept = 0, 0
    while True:
        params = {
            "include_archived": "y",
            "sort": "id",
            "item_count": str(PAGE_SIZE),
        }
        if use_server_filter:
            params[f"fld{DATE_FIELD_ID}"] = f"gt{d_from.isoformat()},lt{d_to.isoformat()}"
        if cursor is not None:
            params["id"] = f"gt{cursor}"

        data = api_get(api_url, token, f"forms/{FORM_ID}/register", params)
        tasks = data.get("tasks", [])
        if not tasks:
            break

        for task in tasks:
            seen += 1
            dv = extract_date_field_value(task, DATE_FIELD_ID)
            dd = parse_date_any(dv)
            if dd is None or not (d_from <= dd <= d_to):
                continue

            by_name = {f.get("name"): readable(f) for f in task.get("fields", [])}
            if only_income and type_name:
                tv = by_name.get(type_name)
                if tv is None or _norm(str(tv)) != _norm(TYPE_TARGET_VALUE):
                    continue

            kept += 1
            income_type = extract_readable_by_id(task, INCOME_TYPE_FIELD_ID)
            widget_val = (extract_readable_by_id(task, WIDGET_FIELD_ID)
                          if WIDGET_FIELD_ID is not None else None)
            rows.append({
                "№": task.get("id"),
                "Дата платежа": dd.isoformat(),
                "Тип поступления": "" if income_type is None else str(income_type),
                "Виджет": "" if widget_val is None else str(widget_val),
                **{n: by_name.get(n) for n in out_cols},
            })

        if len(tasks) < PAGE_SIZE:
            break
        cursor = max(t["id"] for t in tasks)

    df = pd.DataFrame(rows).dropna(axis=1, how="all")
    return df, {
        "date_name": date_field_name,
        "date_in_table": date_in_table,
        "use_server_filter": use_server_filter,
        "money_cols": [c for c in money_cols if c in df.columns],
        "seen": seen, "kept": kept,
        "fields": [(f.get("id"), f.get("name"), f.get("type")) for f in top_fields],
    }


# ---------- вспомогательные ----------
def to_excel_bytes(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Платежи")
    return buf.getvalue()


def current_month():
    t = dt.date.today()
    return t.replace(day=1), t.replace(day=calendar.monthrange(t.year, t.month)[1])


def get_creds():
    login = key = ""
    try:
        login = st.secrets.get("PYRUS_LOGIN", "")
        key = st.secrets.get("PYRUS_SECURITY_KEY", "")
    except Exception:
        pass
    return login, key


def inject_theme():
    css = f"""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

      :root {{
        --accent: {ACCENT};
        --accent-dark: {ACCENT_DARK};
        --bg: {BG};
        --panel: {BG_PANEL};
        --elev: {BG_ELEV};
        --text: {TEXT};
        --muted: {TEXT_MUTED};
        --border: {BORDER};
      }}

      .stApp {{ background: var(--bg); color: var(--text); }}
      html, body, [class*="css"] {{
        font-family: 'Inter', -apple-system, system-ui, sans-serif !important;
      }}

      /* заголовки */
      h1, h2, h3, h4 {{ color: var(--text) !important; font-weight: 700 !important; letter-spacing:-.01em; }}
      h1 {{ font-weight: 800 !important; }}

      /* акцентная плашка-бренд */
      .brand-badge {{
        display:inline-block; padding:4px 12px; border-radius:999px;
        background:rgba(17,197,181,.12); color:var(--accent);
        font-size:13px; font-weight:600; margin-bottom:14px;
        border:1px solid rgba(17,197,181,.3);
      }}

      /* заголовок отчёта-секции */
      .section-head {{
        display:flex; align-items:center; gap:12px;
        margin:0 0 18px; padding:14px 20px;
        background:linear-gradient(90deg, rgba(17,197,181,.16), rgba(17,197,181,.02));
        border-left:5px solid var(--accent); border-radius:12px;
      }}
      .section-head h2 {{ margin:0 !important; font-size:1.5rem !important; }}
      .section-gap {{ height:40px; }}

      /* сайдбар */
      section[data-testid="stSidebar"] {{
        background: var(--panel); border-right:1px solid var(--border);
      }}

      /* кнопки */
      .stButton > button {{
        background: var(--accent) !important; color:#06231F !important;
        border:none !important; border-radius:10px !important;
        font-weight:700 !important; padding:.55rem 1rem !important;
        transition:all .15s ease !important;
      }}
      .stButton > button:hover {{ background: var(--accent-dark) !important; transform:translateY(-1px); }}
      .stDownloadButton > button {{
        background: transparent !important; color: var(--accent) !important;
        border:1px solid var(--accent) !important; border-radius:10px !important;
        font-weight:600 !important;
      }}
      .stDownloadButton > button:hover {{ background:rgba(17,197,181,.1) !important; }}

      /* метрики-карточки */
      div[data-testid="stMetric"] {{
        background: var(--panel); border:1px solid var(--border);
        border-radius:14px; padding:16px 18px;
      }}
      div[data-testid="stMetricValue"] {{ color: var(--text) !important; font-weight:700; }}
      div[data-testid="stMetricLabel"] {{ color: var(--muted) !important; }}

      /* инпуты */
      div[data-baseweb="input"], div[data-baseweb="select"] > div {{
        background: var(--elev) !important; border-color: var(--border) !important;
        border-radius:10px !important;
      }}
      .stCheckbox, .stDateInput label, .stTextInput label {{ color: var(--text) !important; }}

      /* таблица */
      div[data-testid="stDataFrame"] {{
        border:1px solid var(--border); border-radius:12px; overflow:hidden;
      }}

      /* карточка-секция отчёта (st.container border=True) */
      div[data-testid="stVerticalBlockBorderWrapper"] {{
        background: var(--panel);
        border:1px solid var(--border) !important;
        border-radius:18px !important;
        padding:8px 22px 18px !important;
        box-shadow:0 6px 24px rgba(0,0,0,.25);
      }}

      /* подписи/caption */
      div[data-testid="stCaptionContainer"], .stCaption {{ color: var(--muted) !important; }}
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def main():
    st.set_page_config(page_title="Деломатика · Отчёт по поступлениям",
                       page_icon="📊", layout="wide")
    inject_theme()
    st.markdown(
        "<h1 style='margin-bottom:2px'>Отчёт по поступлениям</h1>"
        f"<p style='color:var(--muted);margin-top:4px'>Pyrus · форма {FORM_ID} · "
        f"поле «Дата платежа» (id {DATE_FIELD_ID})</p>",
        unsafe_allow_html=True,
    )

    login, key = get_creds()

    with st.sidebar:
        st.subheader("Параметры для отчёта по поступлениям")
        d_from, d_to = current_month()
        period = st.date_input("Дата платежа (период)", value=(d_from, d_to),
                               format="DD.MM.YYYY", key="main_period")
        if not login or not key:
            st.info("Учётные данные бота не заданы в Secrets — введите вручную:")
            login = st.text_input("Логин бота", value=login)
            key = st.text_input("Секретный ключ", value=key, type="password")
        if st.button("Построить отчёт", type="primary", use_container_width=True,
                     key="run_main"):
            st.session_state["show_main"] = True
            st.session_state["show_compare"] = False

        st.markdown("<div class='section-gap'></div>", unsafe_allow_html=True)
        st.subheader("Параметры для сравнения периодов")
        a_from, a_to = current_month()
        prev_first = (a_from - dt.timedelta(days=1)).replace(day=1)
        prev_last = a_from - dt.timedelta(days=1)
        period_a = st.date_input("Даты периода A", value=(a_from, a_to),
                                 format="DD.MM.YYYY", key="period_a")
        period_b = st.date_input("Даты периода B", value=(prev_first, prev_last),
                                 format="DD.MM.YYYY", key="period_b")
        if st.button("Построить отчёт", type="primary", use_container_width=True,
                     key="run_compare"):
            st.session_state["show_compare"] = True
            st.session_state["show_main"] = False

    if not login or not key:
        st.info("Укажите учётные данные бота.")
        return

    only_income = True  # фильтр «Приход» включён всегда

    # ---- режим сравнения периодов ----
    if st.session_state.get("show_compare"):
        a = period_a if isinstance(period_a, (tuple, list)) and len(period_a) == 2 else None
        b = period_b if isinstance(period_b, (tuple, list)) and len(period_b) == 2 else None
        if not a or not b:
            st.error("Выберите обе даты в периодах A и B.")
            return
        if a[0] > a[1] or b[0] > b[1]:
            st.error("В одном из периодов начало позже конца.")
            return
        try:
            with st.spinner("Загружаю данные за оба периода…"):
                df_a, meta_a = run_report(login, key, a[0], a[1], only_income)
                df_b, meta_b = run_report(login, key, b[0], b[1], only_income)
        except Exception as e:
            st.error(f"Ошибка: {e}")
            return
        render_comparison(df_a, df_b, meta_a, a, b)
        return

    # ---- обычный отчёт по поступлениям ----
    if not st.session_state.get("show_main"):
        st.info("Слева выберите период и нажмите «Построить отчёт».")
        return

    if isinstance(period, (tuple, list)) and len(period) == 2:
        d_from, d_to = period
    if d_from > d_to:
        st.error("Начало периода позже конца.")
        return

    try:
        with st.spinner("Загружаю данные из Pyrus…"):
            df, meta = run_report(login, key, d_from, d_to, only_income)
    except Exception as e:
        st.error(f"Ошибка: {e}")
        return

    note = (
        f"Период: {d_from:%d.%m.%Y} – {d_to:%d.%m.%Y} · "
        f"поле «{meta['date_name'] or '?'}» (id {DATE_FIELD_ID}"
        f"{', в таблице' if meta['date_in_table'] else ''}) · "
        f"проверено задач: {meta['seen']}, оставлено: {meta['kept']}"
    )
    st.caption(note)

    # основной отчёт — все приходы
    with st.container(border=True):
        render_section("Отчёт по поступлениям", df, meta, d_from, d_to, show_table=True)

    # доп. отчёты по «Типу поступления»
    for title, sub in split_by_categories(df)[1:]:
        st.markdown("<div class='section-gap'></div>", unsafe_allow_html=True)
        with st.container(border=True):
            render_section(title, sub, meta, d_from, d_to, show_table=True)


def split_by_categories(df):
    """Возвращает список (название, под-df): общий отчёт + группы по «Типу поступления»."""
    result = [("Отчёт по поступлениям", df)]
    for title, spec in INCOME_REPORTS.items():
        if isinstance(spec, dict):
            values = spec.get("values", [])
            require_nonempty = spec.get("require_nonempty")
        else:
            values = spec
            require_nonempty = None

        wanted = {_norm(v) for v in values}
        if "Тип поступления" in df.columns:
            mask = df["Тип поступления"].apply(lambda x: _norm(str(x)) in wanted)
            sub = df[mask]
            # доп. условие: указанное поле должно быть непустым
            if require_nonempty and require_nonempty in sub.columns:
                nonempty = sub[require_nonempty].apply(
                    lambda x: str(x).strip() not in ("", "None", "nan")
                )
                sub = sub[nonempty]
            sub = sub.reset_index(drop=True)
        else:
            sub = df.iloc[0:0]
        result.append((title, sub))
    return result


def money_total(df, mc):
    if df.empty or mc not in df.columns:
        return 0.0
    return float(pd.to_numeric(df[mc], errors="coerce").sum())


def render_section(title, df, meta, d_from, d_to, show_table=True):
    """Рисует один отчёт: заголовок, метрики, недельный график, таблицу, выгрузку."""
    st.markdown(
        f"<div class='section-head'><h2>{title}</h2></div>",
        unsafe_allow_html=True,
    )

    cols = st.columns(1 + len(meta["money_cols"]))
    cols[0].metric("Платежей", f"{len(df)}")
    for i, mc in enumerate(meta["money_cols"], start=1):
        total = pd.to_numeric(df[mc], errors="coerce").sum() if not df.empty else 0
        cols[i].metric(f"Итого: {mc}", f"{total:,.2f}".replace(",", " "))

    if df.empty:
        st.warning("За выбранный период данных нет.")
        return

    # недельный график
    if "Дата платежа" in df.columns and meta["money_cols"]:
        mc = meta["money_cols"][0]
        tmp = df[["Дата платежа", mc]].copy()
        tmp["Дата платежа"] = pd.to_datetime(tmp["Дата платежа"], errors="coerce")
        tmp[mc] = pd.to_numeric(tmp[mc], errors="coerce")
        tmp = tmp.dropna(subset=["Дата платежа", mc])
        if not tmp.empty:
            tmp["_week_start"] = tmp["Дата платежа"].dt.to_period("W-SUN").dt.start_time
            weekly = (
                tmp.groupby("_week_start", as_index=False)[mc].sum()
                   .sort_values("_week_start")
                   .reset_index(drop=True)
            )
            weekly["week"] = weekly["_week_start"].apply(
                lambda w: f"{w:%d.%m} – {(w + pd.Timedelta(days=6)):%d.%m}"
            )
            chart_df = weekly[["week", mc]].rename(columns={mc: "amount"})
            week_order = chart_df["week"].tolist()

            st.subheader(f"Динамика по неделям · {mc}")
            chart = (
                alt.Chart(chart_df)
                   .mark_bar(color=ACCENT, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                   .encode(
                       x=alt.X("week:N", sort=week_order, title="Неделя",
                               axis=alt.Axis(labelAngle=0, labelColor=TEXT_MUTED,
                                             titleColor=TEXT_MUTED, domainColor=BORDER,
                                             tickColor=BORDER)),
                       y=alt.Y("amount:Q", title=mc,
                               axis=alt.Axis(format=",.0f", labelColor=TEXT_MUTED,
                                             titleColor=TEXT_MUTED, domainColor=BORDER,
                                             gridColor=BORDER, tickColor=BORDER)),
                       tooltip=[
                           alt.Tooltip("week:N", title="Неделя"),
                           alt.Tooltip("amount:Q", title=mc, format=",.2f"),
                       ],
                   )
                   .properties(height=380)
                   .configure(background="transparent")
                   .configure_view(strokeWidth=0)
                   .configure_axis(grid=True)
            )
            st.altair_chart(chart, use_container_width=True)

    if not show_table:
        return

    # таблица: чистим типы под Arrow
    df_display = df.copy()
    money_set = set(meta["money_cols"])
    for col in df_display.columns:
        if col in money_set:
            df_display[col] = pd.to_numeric(df_display[col], errors="coerce")
        else:
            df_display[col] = df_display[col].fillna("").astype(str).replace("nan", "")

    st.subheader("Платежи")
    col_config = {mc: st.column_config.NumberColumn(mc, format="%.2f") for mc in meta["money_cols"]}
    st.dataframe(df_display, use_container_width=True, hide_index=True, column_config=col_config)

    stamp = f"{d_from:%Y%m%d}_{d_to:%Y%m%d}"
    safe = title.replace(" ", "_")
    c1, c2 = st.columns(2)
    c1.download_button("Скачать CSV", df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"{safe}_{stamp}.csv", mime="text/csv",
                       use_container_width=True, key=f"csv_{safe}")
    c2.download_button("Скачать Excel", to_excel_bytes(df),
                       file_name=f"{safe}_{stamp}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True, key=f"xlsx_{safe}")


def _fmt_money(v):
    return f"{v:,.2f}".replace(",", " ")


def render_comparison(df_a, df_b, meta, period_a, period_b):
    st.markdown(
        "<h1 style='margin-bottom:2px'>Сравнение периодов по поступлению</h1>",
        unsafe_allow_html=True,
    )
    a_label = f"{period_a[0]:%d.%m.%Y} – {period_a[1]:%d.%m.%Y}"
    b_label = f"{period_b[0]:%d.%m.%Y} – {period_b[1]:%d.%m.%Y}"
    st.caption(f"Период A: {a_label}  ·  Период B: {b_label}")

    mc = meta["money_cols"][0] if meta["money_cols"] else None
    if not mc:
        st.warning("В форме не найдено денежного поля для сравнения.")
        return

    cats_a = dict(split_by_categories(df_a))
    cats_b = dict(split_by_categories(df_b))

    for cat_name in cats_a.keys():
        sub_a, sub_b = cats_a[cat_name], cats_b[cat_name]
        total_a = money_total(sub_a, mc)
        total_b = money_total(sub_b, mc)
        delta = total_a - total_b
        pct = (delta / total_b * 100) if total_b else (100.0 if total_a else 0.0)
        sign = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
        pct_txt = "—" if (total_b == 0 and total_a == 0) else f"{sign} {pct:+.1f}%"

        st.markdown("<div class='section-gap'></div>", unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown(
                f"<div class='section-head'><h2>{cat_name} – сравнение периодов A / B</h2></div>",
                unsafe_allow_html=True,
            )
            c1, c2, c3, c4 = st.columns(4)
            c1.metric(f"Период A · {mc}", _fmt_money(total_a))
            c2.metric(f"Период B · {mc}", _fmt_money(total_b))
            c3.metric("Прирост, ₽", _fmt_money(delta))
            c4.metric("Прирост, %", pct_txt)

            # сгруппированный график: два столбца A и B
            comp_df = pd.DataFrame({
                "Период": [f"A\n{a_label}", f"B\n{b_label}"],
                "amount": [total_a, total_b],
                "key": ["A", "B"],
            })
            bars = (
                alt.Chart(comp_df)
                   .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                   .encode(
                       x=alt.X("Период:N", title=None, sort=["A", "B"],
                               axis=alt.Axis(labelAngle=0, labelColor=TEXT_MUTED,
                                             domainColor=BORDER, tickColor=BORDER)),
                       y=alt.Y("amount:Q", title=mc,
                               axis=alt.Axis(format=",.0f", labelColor=TEXT_MUTED,
                                             titleColor=TEXT_MUTED, domainColor=BORDER,
                                             gridColor=BORDER, tickColor=BORDER)),
                       color=alt.Color("key:N",
                                       scale=alt.Scale(domain=["A", "B"],
                                                       range=[ACCENT, "#5B7290"]),
                                       legend=None),
                       tooltip=[alt.Tooltip("key:N", title="Период"),
                                alt.Tooltip("amount:Q", title=mc, format=",.2f")],
                   )
            )
            labels = (
                alt.Chart(comp_df)
                   .mark_text(dy=-8, color=TEXT, fontWeight="bold")
                   .encode(x=alt.X("Период:N", sort=["A", "B"]),
                           y="amount:Q",
                           text=alt.Text("amount:Q", format=",.0f"))
            )
            chart = (
                (bars + labels).properties(height=340)
                .configure(background="transparent")
                .configure_view(strokeWidth=0)
            )
            st.altair_chart(chart, use_container_width=True)

            # наглядная плашка прироста/падения
            if delta > 0:
                color, word = ACCENT, "Прирост"
            elif delta < 0:
                color, word = "#FF6B6B", "Падение"
            else:
                color, word = TEXT_MUTED, "Без изменений"
            st.markdown(
                f"<div style='padding:14px 18px;border-radius:12px;"
                f"background:rgba(17,197,181,.08);border-left:5px solid {color};'>"
                f"<span style='font-size:18px;font-weight:700;color:{color}'>"
                f"{word}: {_fmt_money(delta)} ₽ ({pct_txt})</span>"
                f"<br><span style='color:{TEXT_MUTED}'>A ({a_label}): {_fmt_money(total_a)} ₽ "
                f"&nbsp;·&nbsp; B ({b_label}): {_fmt_money(total_b)} ₽</span></div>",
                unsafe_allow_html=True,
            )


if __name__ == "__main__":
    main()
