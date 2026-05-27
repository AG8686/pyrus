"""
Отчёт по платежам Pyrus — открывается в браузере по ссылке.

Фильтры:
  • Дата платежа (поле id = 37) — диапазон выбирается календарём,
    по умолчанию текущий календарный месяц.
  • Тип платежа = Приход — отбор на клиенте.

Учётные данные бота — в настройках приложения (Secrets), НЕ в коде:
    PYRUS_LOGIN = "bot@your-company.ru"
    PYRUS_SECURITY_KEY = "..."
"""

import time
import io
import calendar
import datetime as dt

import requests
import pandas as pd
import streamlit as st

FORM_ID = 579058
DATE_FIELD_ID = 37                       # «Дата платежа»
TYPE_FIELD_NAME = "Тип платежа"
TYPE_TARGET_VALUE = "Приход"
AUTH_URL = "https://accounts.pyrus.com/api/v4/auth"
PAGE_SIZE = 1000
ACCENT = "#008C8C"


# ---------- клиент Pyrus ----------
def _norm(s):
    return (s or "").strip().casefold()


def authenticate(login, key):
    r = requests.post(AUTH_URL, json={"login": login, "security_key": key}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Не удалось войти: {r.status_code}")
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
        raise RuntimeError(f"Ошибка запроса {path}: {r.status_code}")
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


def find_field_by_id(fields, fid):
    """Рекурсивный поиск поля по id, в т.ч. внутри table/title/multiple_choice."""
    for f in fields:
        if f.get("id") == fid:
            return f
        info = f.get("info") or {}
        for key in ("columns", "fields"):
            nested = info.get(key)
            if nested:
                hit = find_field_by_id(nested, fid)
                if hit:
                    return hit
    return None


# ---------- загрузка данных ----------
@st.cache_data(ttl=300, show_spinner=False)
def load_report(login, key, d_from, d_to, only_income):
    api_url, token = authenticate(login, key)
    schema = api_get(api_url, token, f"forms/{FORM_ID}")
    fields = schema.get("fields", [])

    date_field = find_field_by_id(fields, DATE_FIELD_ID)
    date_field_name = date_field.get("name") if date_field else None

    type_field = next((f for f in fields if _norm(f.get("name")) == _norm(TYPE_FIELD_NAME)), None)
    if not type_field:
        type_field = next((f for f in fields if "тип" in _norm(f.get("name"))), None)
    type_name = type_field.get("name") if type_field else None

    money_cols = [f.get("name") for f in fields if f.get("type") == "money"]
    out_cols = [f.get("name") for f in fields]

    rows, cursor = [], None
    while True:
        params = {
            "include_archived": "y",
            "sort": "id",
            "item_count": str(PAGE_SIZE),
            # фильтр по «Дате платежа» (поле id=37): gt = от, lt = до
            f"fld{DATE_FIELD_ID}": f"gt{d_from.isoformat()},lt{d_to.isoformat()}",
        }
        if cursor is not None:
            params["id"] = f"gt{cursor}"

        tasks = api_get(api_url, token, f"forms/{FORM_ID}/register", params).get("tasks", [])
        if not tasks:
            break

        for task in tasks:
            by_name = {f.get("name"): readable(f) for f in task.get("fields", [])}

            # клиентская подстраховка по диапазону «Дата платежа»
            if date_field_name:
                dv = by_name.get(date_field_name)
                try:
                    dd = dt.date.fromisoformat(str(dv)[:10]) if dv else None
                except ValueError:
                    dd = None
                if dd is None or not (d_from <= dd <= d_to):
                    continue

            # фильтр «Приход»
            if only_income and type_name:
                tv = by_name.get(type_name)
                if tv is None or _norm(str(tv)) != _norm(TYPE_TARGET_VALUE):
                    continue

            rows.append({"№": task.get("id"), **{n: by_name.get(n) for n in out_cols}})

        if len(tasks) < PAGE_SIZE:
            break
        cursor = max(t["id"] for t in tasks)

    df = pd.DataFrame(rows).dropna(axis=1, how="all")
    return df, {
        "date_name": date_field_name,
        "money_cols": [c for c in money_cols if c in df.columns],
        "fields": [(f.get("id"), f.get("name"), f.get("type")) for f in fields],
    }


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


# ---------- интерфейс ----------
def main():
    st.set_page_config(page_title="Отчёт по платежам", page_icon="💰", layout="wide")
    st.markdown(
        "<h1 style='margin-bottom:0'>Отчёт по платежам</h1>"
        f"<p style='color:gray;margin-top:4px'>Pyrus · форма {FORM_ID} · "
        f"фильтр по полю «Дата платежа» (id {DATE_FIELD_ID})</p>",
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

    if not (run or st.session_state.get("loaded")):
        st.info("Слева выберите период и нажмите «Построить отчёт».")
        return
    st.session_state["loaded"] = True

    if not login or not key:
        st.error("Не заданы логин и секретный ключ бота.")
        return

    try:
        with st.spinner("Загружаю данные из Pyrus…"):
            df, meta = load_report(login, key, d_from, d_to, only_income)
    except Exception as e:
        st.error(f"Ошибка: {e}")
        return

    st.caption(
        f"Период: {d_from:%d.%m.%Y} – {d_to:%d.%m.%Y} · "
        f"фильтр по полю «{meta['date_name'] or 'Дата платежа'}» (id {DATE_FIELD_ID})"
    )

    cols = st.columns(1 + len(meta["money_cols"]))
    cols[0].metric("Платежей", f"{len(df)}")
    for i, mc in enumerate(meta["money_cols"], start=1):
        total = pd.to_numeric(df[mc], errors="coerce").sum()
        cols[i].metric(f"Итого: {mc}", f"{total:,.2f}".replace(",", " "))

    if df.empty:
        st.warning("За выбранный период данных нет.")
        with st.expander("Поля формы (для проверки)"):
            st.table(pd.DataFrame(meta["fields"], columns=["id", "Поле", "Тип"]))
        return

    if meta["date_name"] in df.columns and meta["money_cols"]:
        mc = meta["money_cols"][0]
        tmp = df[[meta["date_name"], mc]].copy()
        tmp[meta["date_name"]] = pd.to_datetime(tmp[meta["date_name"]], errors="coerce").dt.date
        tmp[mc] = pd.to_numeric(tmp[mc], errors="coerce")
        daily = tmp.dropna().groupby(meta["date_name"])[mc].sum()
        if not daily.empty:
            st.subheader(f"Динамика по дням · {mc}")
            st.bar_chart(daily, color=ACCENT)

    st.subheader("Платежи")
    col_config = {
        mc: st.column_config.NumberColumn(mc, format="%.2f")
        for mc in meta["money_cols"]
    }
    st.dataframe(df, use_container_width=True, hide_index=True, column_config=col_config)

    stamp = f"{d_from:%Y%m%d}_{d_to:%Y%m%d}"
    c1, c2 = st.columns(2)
    c1.download_button(
        "Скачать CSV", df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"pyrus_{stamp}.csv", mime="text/csv",
        use_container_width=True,
    )
    c2.download_button(
        "Скачать Excel", to_excel_bytes(df),
        file_name=f"pyrus_{stamp}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    with st.expander("Поля формы (для проверки)"):
        st.table(pd.DataFrame(meta["fields"], columns=["id", "Поле", "Тип"]))


if __name__ == "__main__":
    main()
