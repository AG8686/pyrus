"""
Отчёт по платежам Pyrus.
Фильтр по полю «Дата платежа» (id=37), только «Приход».
По умолчанию — текущий месяц. Есть режим диагностики.
"""

import time
import io
import calendar
import datetime as dt
import json

import requests
import pandas as pd
import streamlit as st

FORM_ID = 579058
DATE_FIELD_ID = 37
TYPE_FIELD_NAME = "Тип платежа"
TYPE_TARGET_VALUE = "Приход"
AUTH_URL = "https://accounts.pyrus.com/api/v4/auth"
PAGE_SIZE = 1000
ACCENT = "#008C8C"


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
            rows.append({
                "№": task.get("id"),
                "Дата платежа": dd.isoformat(),
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


# ---------- диагностика ----------
def run_diagnostic(login, key, d_from, d_to):
    out = {"steps": []}
    api_url, token = authenticate(login, key)
    out["api_url"] = api_url
    schema = api_get(api_url, token, f"forms/{FORM_ID}")
    top_fields = schema.get("fields", [])
    out["top_fields"] = [(f.get("id"), f.get("name"), f.get("type")) for f in top_fields]

    date_field = find_field_by_id(top_fields, DATE_FIELD_ID)
    out["date_field"] = {
        "found": bool(date_field),
        "name": date_field.get("name") if date_field else None,
        "type": date_field.get("type") if date_field else None,
        "in_table": bool(date_field) and date_field not in top_fields,
    }

    # 1. свежие 5 задач без фильтра — посмотреть формат значения поля 37
    data = api_get(api_url, token, f"forms/{FORM_ID}/register",
                   {"include_archived": "y", "item_count": "5"})
    samples = []
    for t in data.get("tasks", []):
        v = extract_date_field_value(t, DATE_FIELD_ID)
        samples.append({"id": t.get("id"), "raw_value_of_field_37": v,
                        "parsed": str(parse_date_any(v))})
    out["sample_no_filter"] = samples

    # 2. с серверным фильтром по диапазону
    params = {
        "include_archived": "y", "item_count": "5",
        f"fld{DATE_FIELD_ID}": f"gt{d_from.isoformat()},lt{d_to.isoformat()}",
    }
    data2 = api_get(api_url, token, f"forms/{FORM_ID}/register", params)
    out["server_filter"] = {
        "params": params,
        "returned": len(data2.get("tasks", [])),
        "first_ids": [t.get("id") for t in data2.get("tasks", [])[:5]],
    }
    return out


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
        st.divider()
        diag = st.button("🔎 Запустить диагностику", use_container_width=True)

    if isinstance(period, (tuple, list)) and len(period) == 2:
        d_from, d_to = period
    if d_from > d_to:
        st.error("Начало периода позже конца.")
        return
    if not login or not key:
        st.info("Укажите учётные данные бота.")
        return

    # ---- диагностика ----
    if diag:
        st.subheader("Диагностика")
        try:
            with st.spinner("Проверяю API…"):
                info = run_diagnostic(login, key, d_from, d_to)
        except Exception as e:
            st.error(f"Ошибка диагностики: {e}")
            return

        st.write("**Поле id=37 в схеме формы:**", info["date_field"])
        st.write("**Серверный фильтр по диапазону вернул задач:**",
                 info["server_filter"]["returned"])
        st.code(json.dumps(info["server_filter"], ensure_ascii=False, indent=2), language="json")
        st.write("**5 свежих задач без фильтра — как выглядит значение поля 37:**")
        st.code(json.dumps(info["sample_no_filter"], ensure_ascii=False, indent=2), language="json")
        with st.expander("Все поля верхнего уровня формы"):
            st.table(pd.DataFrame(info["top_fields"], columns=["id", "Поле", "Тип"]))
        st.info("Пришли этот вывод — по нему сразу видно, в чём дело.")
        return

    # ---- отчёт ----
    if not run:
        st.info("Слева выберите период и нажмите «Построить отчёт». "
                "Если данных нет — нажмите «Запустить диагностику».")
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

    cols = st.columns(1 + len(meta["money_cols"]))
    cols[0].metric("Платежей", f"{len(df)}")
    for i, mc in enumerate(meta["money_cols"], start=1):
        total = pd.to_numeric(df[mc], errors="coerce").sum()
        cols[i].metric(f"Итого: {mc}", f"{total:,.2f}".replace(",", " "))

    if df.empty:
        st.warning(
            "За выбранный период данных нет. Нажмите «🔎 Запустить диагностику» слева — "
            "она покажет, в каком формате API возвращает поле «Дата платежа» (id 37) "
            "и почему оно не попадает в период."
        )
        with st.expander("Поля формы (верхний уровень)"):
            st.table(pd.DataFrame(meta["fields"], columns=["id", "Поле", "Тип"]))
        return

    if "Дата платежа" in df.columns and meta["money_cols"]:
        mc = meta["money_cols"][0]
        tmp = df[["Дата платежа", mc]].copy()
        tmp["Дата платежа"] = pd.to_datetime(tmp["Дата платежа"], errors="coerce").dt.date
        tmp[mc] = pd.to_numeric(tmp[mc], errors="coerce")
        daily = tmp.dropna().groupby("Дата платежа")[mc].sum()
        if not daily.empty:
            st.subheader(f"Динамика по дням · {mc}")
            st.bar_chart(daily, color=ACCENT)

    st.subheader("Платежи")
    col_config = {mc: st.column_config.NumberColumn(mc, format="%.2f") for mc in meta["money_cols"]}
    st.dataframe(df, use_container_width=True, hide_index=True, column_config=col_config)

    stamp = f"{d_from:%Y%m%d}_{d_to:%Y%m%d}"
    c1, c2 = st.columns(2)
    c1.download_button("Скачать CSV", df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"pyrus_{stamp}.csv", mime="text/csv",
                       use_container_width=True)
    c2.download_button("Скачать Excel", to_excel_bytes(df),
                       file_name=f"pyrus_{stamp}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)


if __name__ == "__main__":
    main()
