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
TYPE_FIELD_NAME = "Тип платежа"
TYPE_TARGET_VALUE = "Приход"
AUTH_URL = "https://accounts.pyrus.com/api/v4/auth"
PAGE_SIZE = 1000
ACCENT = "#008C8C"

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
            rows.append({
                "№": task.get("id"),
                "Дата платежа": dd.isoformat(),
                "Тип поступления": "" if income_type is None else str(income_type),
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


def main():
    st.set_page_config(page_title="Отчёт по платежам", page_icon="💰", layout="wide")
    st.markdown(
        "<h1 style='margin-bottom:0'>Отчёт по платежам</h1>"
        f"<p style='color:gray;margin-top:4px'>Pyrus · форма {FORM_ID} · "
        f"поле «Дата платежа» (id {DATE_FIELD_ID})</p>",
        unsafe_allow_html=True,
    )

    login, key = get_creds()

    with st.sidebar:
        st.subheader("Параметры")
        d_from, d_to = current_month()
        period = st.date_input("Дата платежа (период)", value=(d_from, d_to), format="DD.MM.YYYY")
        only_income = st.checkbox(f"Только «{TYPE_TARGET_VALUE}»", value=True)
        if not login or not key:
            st.info("Учётные данные бота не заданы в Secrets — введите вручную:")
            login = st.text_input("Логин бота", value=login)
            key = st.text_input("Секретный ключ", value=key, type="password")
        run = st.button("Построить отчёт", type="primary", use_container_width=True)

    if isinstance(period, (tuple, list)) and len(period) == 2:
        d_from, d_to = period
    if d_from > d_to:
        st.error("Начало периода позже конца.")
        return
    if not login or not key:
        st.info("Укажите учётные данные бота.")
        return

    # ---- отчёт ----
    if not run:
        st.info("Слева выберите период и нажмите «Построить отчёт».")
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
    render_section("Отчёт по платежам", df, meta, d_from, d_to, show_table=True)

    # доп. отчёты по «Типу поступления»
    for title, values in INCOME_REPORTS.items():
        wanted = {_norm(v) for v in values}
        if "Тип поступления" in df.columns:
            mask = df["Тип поступления"].apply(lambda x: _norm(str(x)) in wanted)
            sub = df[mask].reset_index(drop=True)
        else:
            sub = df.iloc[0:0]
        st.divider()
        render_section(title, sub, meta, d_from, d_to, show_table=True)


def render_section(title, df, meta, d_from, d_to, show_table=True):
    """Рисует один отчёт: заголовок, метрики, недельный график, таблицу, выгрузку."""
    st.header(title)

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
                   .mark_bar(color=ACCENT)
                   .encode(
                       x=alt.X("week:N", sort=week_order, title="Неделя",
                               axis=alt.Axis(labelAngle=0)),
                       y=alt.Y("amount:Q", title=mc,
                               axis=alt.Axis(format=",.0f")),
                       tooltip=[
                           alt.Tooltip("week:N", title="Неделя"),
                           alt.Tooltip("amount:Q", title=mc, format=",.2f"),
                       ],
                   )
                   .properties(height=380)
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


if __name__ == "__main__":
    main()
