"""
app.py — Price Alerts (Streamlit + yfinance + Telegram)

Avvio locale:   streamlit run app.py
Segreti:        .streamlit/secrets.toml (vedi secrets.toml.example) oppure variabili d'ambiente
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

import core
from core import ABOVE, BELOW, MODE_CLOSE, MODE_TOUCH, ST_ACTIVE, ST_EXPIRED, ST_PAUSED, ST_TRIGGERED

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
st.set_page_config(page_title="Price Alerts", page_icon="🔔", layout="wide")


# --------------------------------------------------------------------------------------
# Config & store
# --------------------------------------------------------------------------------------
def _get(key, default):
    if key in os.environ:
        return os.environ[key]
    try:
        return st.secrets.get(key, default)
    except Exception:  # nessun secrets.toml
        return default


cfg = core.Config.from_mapping(_get)
TZ = cfg.tz


@st.cache_resource
def get_store(_cfg_key: tuple) -> core.Store:
    return core.make_store(cfg)


store = get_store((cfg.gist_id, cfg.alerts_file))


# --------------------------------------------------------------------------------------
# Scheduler in background (un solo thread per processo, sopravvive ai rerun)
# --------------------------------------------------------------------------------------
class Scheduler:
    def __init__(self, cfg: core.Config, store: core.Store):
        self.cfg, self.store = cfg, store
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.last_run: datetime | None = None
        self.last_summary: dict | None = None
        self.next_run: datetime | None = None
        self.running = False
        self.thread = threading.Thread(target=self._loop, daemon=True, name="price-alert-scheduler")
        self.thread.start()

    def run_now(self) -> dict:
        with self.lock:
            self.running = True
            try:
                s = core.run_check(self.cfg, self.store)
            except Exception as e:  # noqa: BLE001
                logging.exception("check fallito")
                s = {"time": core.to_iso(core.utcnow()), "checked": 0, "triggered": [], "expired": [],
                     "errors": [f"check fallito: {e}"]}
            finally:
                self.running = False
            self.last_run, self.last_summary = core.utcnow(), s
            logging.info("check: %s", s)
            return s

    def _loop(self):
        while True:
            self.next_run = core.next_run_time(core.utcnow(), self.cfg.check_interval_minutes)
            wait = (self.next_run - core.utcnow()).total_seconds()
            self.wake.wait(timeout=max(1.0, wait))
            self.wake.clear()
            self.run_now()


@st.cache_resource
def get_scheduler() -> Scheduler:
    return Scheduler(cfg, store)


scheduler = get_scheduler() if cfg.run_scheduler else None


# --------------------------------------------------------------------------------------
# Helper UI
# --------------------------------------------------------------------------------------
@st.cache_data(ttl=60, show_spinner=False)
def cached_price(ticker: str) -> tuple[float | None, str | None]:
    try:
        p = core.last_price(ticker, cfg.include_prepost)
        return p, None if p is not None else "nessun dato"
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def dist_pct(a: core.Alert) -> str:
    if not a.last_price:
        return "—"
    d = (a.target / a.last_price - 1) * 100
    return f"{d:+.2f}%"


STATUS_LBL = {ST_ACTIVE: "🟢 attivo", ST_PAUSED: "⏸️ in pausa", ST_TRIGGERED: "✅ scattato", ST_EXPIRED: "⌛ scaduto"}


# --------------------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Stato")
    st.write(f"**Telegram:** {'✅ configurato' if cfg.telegram_ok else '❌ mancano token / chat_id'}")
    st.write(f"**Salvataggio:** {store.kind}")
    st.write(f"**Fuso orario:** {cfg.timezone}")
    st.write(f"**Pre/after-market:** {'inclusi' if cfg.include_prepost else 'esclusi'}")

    if scheduler:
        st.write(f"**Controllo automatico:** ogni {cfg.check_interval_minutes} min")
        st.caption(
            f"Ultimo: {core.fmt_dt(core.to_iso(scheduler.last_run), TZ)} · "
            f"Prossimo: {core.fmt_dt(core.to_iso(scheduler.next_run), TZ)}"
        )
    else:
        st.write("**Controllo automatico:** esterno (checker.py / GitHub Actions)")

    if st.button("🔄 Controlla ora", width="stretch"):
        with st.spinner("Scarico i prezzi e controllo gli alert…"):
            summ = scheduler.run_now() if scheduler else core.run_check(cfg, store)
        st.session_state["last_manual"] = summ

    if summ := st.session_state.get("last_manual"):
        st.caption(f"Controllati {summ['checked']} · scattati {len(summ['triggered'])} · "
                   f"scaduti {len(summ['expired'])}")
        for t in summ["triggered"]:
            st.success(t)
        for e in summ["errors"]:
            st.warning(e)

    st.divider()
    st.subheader("📨 Telegram")
    if st.button("Invia messaggio di prova", width="stretch", disabled=not cfg.telegram_ok):
        try:
            core.send_telegram(cfg, "✅ Price Alerts: Telegram funziona.")
            st.success("Inviato!")
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
    with st.expander("Non so il mio chat_id"):
        st.caption("Scrivi un messaggio qualsiasi al tuo bot su Telegram, poi premi il pulsante.")
        if st.button("Trova chat_id", disabled=not cfg.telegram_token):
            try:
                ids = core.telegram_find_chat_ids(cfg.telegram_token)
                st.write(ids or "Nessun messaggio trovato: scrivi prima al bot.")
            except Exception as e:  # noqa: BLE001
                st.error(str(e))


# --------------------------------------------------------------------------------------
# Nuovo alert
# --------------------------------------------------------------------------------------
st.title("🔔 Price Alerts")
st.caption("Alert su rottura di livelli di prezzo, dati yfinance, notifiche Telegram.")

with st.container(border=True):
    st.subheader("Nuovo alert")
    c1, c2, c3 = st.columns([1.2, 1, 1])
    with c1:
        raw = st.text_input("Strumento (ticker Yahoo)", placeholder="AAPL, QQQ, NQ=F, ES=F, ^GSPC, EURUSD=X, BTC-USD")
        ticker = core.normalize_ticker(raw) if raw else ""
        cur_price, price_err = cached_price(ticker) if ticker else (None, None)
        if ticker:
            if cur_price is not None:
                st.metric(f"Ultimo prezzo {ticker}", core.fmt_price(cur_price))
            else:
                st.warning(f"Non trovo dati per **{ticker}** ({price_err}). Controlla il ticker su finance.yahoo.com")
    with c2:
        direction = st.radio("Direzione", [ABOVE, BELOW], horizontal=True,
                             format_func=lambda d: "⬆️ Rottura al rialzo" if d == ABOVE else "⬇️ Rottura al ribasso")
        target = st.number_input("Livello di prezzo", min_value=0.0, step=0.01, format="%.4f",
                                 value=float(round(cur_price, 2)) if cur_price else 0.0,
                                 key=f"target_{ticker}")
        mode = st.selectbox("Quando scatta", [MODE_TOUCH, MODE_CLOSE],
                            format_func=lambda m: "Al tocco (massimo/minimo candele 5m)" if m == MODE_TOUCH
                            else "Su chiusura di una candela 5m oltre il livello")
    with c3:
        dkind = st.selectbox("Durata", ["once", "today", "days", "until", "forever"],
                             format_func={"once": "Fino al primo scatto (poi si disattiva)",
                                          "today": "Solo oggi", "days": "Per N giorni",
                                          "until": "Fino a una data",
                                          "forever": "Infinito (finché non lo rimuovo)"}.get)
        ndays, until = 1, None
        if dkind == "days":
            ndays = st.number_input("Numero di giorni (oggi incluso)", min_value=1, max_value=3650, value=5)
        elif dkind == "until":
            until = st.date_input("Scade a fine giornata del", value=date.today() + timedelta(days=7),
                                  min_value=date.today(), format="DD/MM/YYYY")
        if dkind == "once":
            repeat = False
            st.caption("L'alert resta attivo senza scadenza e, appena scatta, si disattiva.")
        else:
            repeat = st.checkbox("Ri-arma dopo lo scatto", value=False,
                                 help="Se attivo, dopo l'alert si ri-arma quando il prezzo torna dall'altra parte "
                                      "del livello e può scattare di nuovo. Se disattivo, scatta una volta sola "
                                      "e si disattiva.")
        note = st.text_input("Nota (opzionale)", placeholder="es. breakout massimi settimanali")

    expires_at, dlabel = core.compute_expiry(dkind, TZ, int(ndays), until)
    st.caption(f"Scadenza: **{core.fmt_dt(expires_at, TZ, with_year=True) if expires_at else 'mai'}** ({cfg.timezone})")

    if cur_price and target > 0:
        if direction == ABOVE and cur_price >= target:
            st.warning("Il prezzo è **già sopra** il livello: l'alert scatterà al primo controllo.")
        elif direction == BELOW and cur_price <= target:
            st.warning("Il prezzo è **già sotto** il livello: l'alert scatterà al primo controllo.")

    if st.button("➕ Aggiungi alert", type="primary", disabled=not ticker or target <= 0):
        a = core.Alert(ticker=ticker, direction=direction, target=float(target), expires_at=expires_at,
                       duration_label=dlabel, mode=mode, repeat=repeat, note=note.strip(),
                       ref_price=cur_price, last_price=cur_price)
        store.add(a)
        st.toast(f"Alert aggiunto: {ticker} {a.direction_label} {core.fmt_price(target)}", icon="✅")
        st.rerun()


# --------------------------------------------------------------------------------------
# Elenco alert (si aggiorna da solo ogni minuto per mostrare i risultati del controllo orario)
# --------------------------------------------------------------------------------------
@st.fragment(run_every=timedelta(seconds=60))
def alerts_panel():
    try:
        alerts = store.load()
    except Exception as e:  # noqa: BLE001
        st.error(f"Non riesco a leggere gli alert: {e}")
        return

    live = [a for a in alerts if a.status in (ST_ACTIVE, ST_PAUSED)]
    done = [a for a in alerts if a.status in (ST_TRIGGERED, ST_EXPIRED)]

    st.subheader(f"Alert attivi ({len(live)})")
    if not live:
        st.info("Nessun alert attivo.")
    else:
        hdr = st.columns([1.1, 1, 1, 1, 0.8, 1.4, 1.2, 1.4])
        for h, t in zip(hdr, ["Strumento", "Direzione", "Livello", "Ultimo", "Distanza", "Scadenza", "Stato", ""]):
            h.markdown(f"**{t}**")
        for a in sorted(live, key=lambda x: (x.ticker, x.target)):
            c = st.columns([1.1, 1, 1, 1, 0.8, 1.4, 1.2, 1.4])
            c[0].write(f"**{a.ticker}**" + (f"  \n<small>{a.note}</small>" if a.note else ""), unsafe_allow_html=True)
            c[1].write(a.direction_label + ("  \n<small>chiusura 5m</small>" if a.mode == MODE_CLOSE else ""),
                       unsafe_allow_html=True)
            c[2].write(core.fmt_price(a.target))
            c[3].write(core.fmt_price(a.last_price))
            c[4].write(dist_pct(a))
            c[5].write(core.fmt_dt(a.expires_at, TZ) if a.expires_at else "∞ infinito")
            stato = STATUS_LBL[a.status]
            if a.repeat:
                stato += f"  \n<small>ri-arma · {'armato' if a.armed else 'in attesa ritorno'} · {a.trigger_count}×</small>"
            if a.last_error:
                stato += f"  \n<small>⚠️ {a.last_error[:80]}</small>"
            c[6].write(stato, unsafe_allow_html=True)
            b1, b2 = c[7].columns(2)
            if a.status == ST_ACTIVE:
                if b1.button("⏸️", key=f"p_{a.id}", help="Metti in pausa"):
                    store.patch(a.id, status=ST_PAUSED)
                    st.rerun(scope="fragment")
            else:
                if b1.button("▶️", key=f"r_{a.id}", help="Riprendi"):
                    # riparto da adesso: non considero quello che è successo durante la pausa
                    store.patch(a.id, status=ST_ACTIVE, last_check=core.to_iso(core.utcnow()))
                    st.rerun(scope="fragment")
            if b2.button("🗑️", key=f"d_{a.id}", help="Rimuovi"):
                store.remove(a.id)
                st.rerun(scope="fragment")

    st.subheader(f"Storico ({len(done)})")
    if done:
        df = pd.DataFrame([{
            "Strumento": a.ticker, "Direzione": a.direction_label, "Livello": a.target,
            "Stato": STATUS_LBL[a.status], "Scattato il": core.fmt_dt(a.last_triggered_at, TZ),
            "Ultimo prezzo": a.last_price, "Durata": a.duration_label, "Nota": a.note, "id": a.id,
        } for a in sorted(done, key=lambda x: x.last_triggered_at or x.expires_at or "", reverse=True)])
        st.dataframe(df.drop(columns="id"), hide_index=True, width="stretch")

        c1, c2, c3 = st.columns([2, 1, 1])
        opts = {a.id: f"{a.ticker} {a.direction_label} {core.fmt_price(a.target)} ({STATUS_LBL[a.status]})" for a in done}
        sel = c1.selectbox("Seleziona", list(opts), format_func=opts.get, label_visibility="collapsed")
        if c2.button("↩️ Riattiva", width="stretch", help="Riattiva con la stessa scadenza se ancora valida, altrimenti infinito"):
            a = next(x for x in done if x.id == sel)
            exp = a.expires_at if (a.expires_at and not a.is_expired(core.utcnow())) else None
            store.patch(sel, status=ST_ACTIVE, armed=True, last_check=core.to_iso(core.utcnow()),
                        expires_at=exp, duration_label=a.duration_label if exp else "Infinito", last_error=None)
            st.rerun(scope="fragment")
        if c3.button("🧹 Svuota storico", width="stretch"):
            store.update(lambda al: [x for x in al if x.status not in (ST_TRIGGERED, ST_EXPIRED)])
            st.rerun(scope="fragment")
    else:
        st.caption("Niente nello storico.")


alerts_panel()

with st.expander("ℹ️ Come funziona"):
    st.markdown(f"""
- Ogni **{cfg.check_interval_minutes} minuti** (al minuto :01) scarico da yfinance le candele a 5 minuti e guardo
  **tutto quello che è successo dall'ultimo controllo**, non solo il prezzo di quell'istante: se il prezzo
  ha toccato il livello alle 10:23 ed è rientrato alle 10:40, l'alert delle 11:01 scatta comunque.
- **Al tocco**: basta il massimo (rialzo) o il minimo (ribasso) di una candela. **Su chiusura**: serve
  che una candela a 5 minuti chiuda oltre il livello (filtra le spike).
- **Durata**: *solo oggi* e *N giorni* scadono alle 23:59 ({cfg.timezone}) dell'ultimo giorno;
  *infinito* resta finché non lo rimuovi. Alla scadenza ricevi un messaggio (impostabile con `NOTIFY_EXPIRY`).
- I dati yfinance delle azioni possono avere fino a ~15 min di ritardo su alcune borse; per USA sono quasi realtime.
""")
