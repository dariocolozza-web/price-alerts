"""
core.py — logica condivisa tra l'app Streamlit (app.py) e il checker da riga di comando (checker.py).

Contiene:
  - modello dati degli alert
  - storage (file JSON locale oppure GitHub Gist, per avere persistenza anche su Streamlit Cloud)
  - download prezzi da yfinance
  - valutazione degli alert (rottura al rialzo / al ribasso, scadenza, ri-armo)
  - invio messaggi Telegram
"""
from __future__ import annotations

import html
import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

log = logging.getLogger("price_alerts")

ABOVE = "above"   # prezzo sale sopra il livello
BELOW = "below"   # prezzo scende sotto il livello

MODE_TOUCH = "touch"  # basta che il massimo/minimo di una candela tocchi il livello
MODE_CLOSE = "close"  # serve una chiusura (candela 5m) oltre il livello

ST_ACTIVE = "active"
ST_PAUSED = "paused"
ST_TRIGGERED = "triggered"
ST_EXPIRED = "expired"

BAR_INTERVAL = "5m"
BAR_MINUTES = 5


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
@dataclass
class Config:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    timezone: str = "Europe/Rome"
    alerts_file: str = "alerts.json"
    gist_id: str = ""
    github_token: str = ""
    include_prepost: bool = False      # includi pre/after-market per le azioni USA
    notify_expiry: bool = True         # messaggio Telegram quando un alert scade senza scattare
    check_interval_minutes: int = 60
    run_scheduler: bool = True         # thread interno all'app Streamlit

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def telegram_ok(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @classmethod
    def from_mapping(cls, get: Callable[[str, object], object]) -> "Config":
        """Costruisce la config leggendo chiavi MAIUSCOLE da una funzione get(nome, default)."""

        def b(v) -> bool:
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in ("1", "true", "yes", "si", "sì", "on")

        return cls(
            telegram_token=str(get("TELEGRAM_BOT_TOKEN", "") or ""),
            telegram_chat_id=str(get("TELEGRAM_CHAT_ID", "") or ""),
            timezone=str(get("TIMEZONE", "Europe/Rome") or "Europe/Rome"),
            alerts_file=str(get("ALERTS_FILE", "alerts.json") or "alerts.json"),
            gist_id=str(get("GIST_ID", "") or ""),
            github_token=str(get("GITHUB_TOKEN", "") or ""),
            include_prepost=b(get("INCLUDE_PREPOST", False)),
            notify_expiry=b(get("NOTIFY_EXPIRY", True)),
            check_interval_minutes=int(get("CHECK_INTERVAL_MINUTES", 60) or 60),
            run_scheduler=b(get("RUN_SCHEDULER", True)),
        )

    @classmethod
    def from_env(cls) -> "Config":
        return cls.from_mapping(lambda k, d: os.environ.get(k, d))


# --------------------------------------------------------------------------------------
# Modello dati
# --------------------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def from_iso(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def normalize_ticker(t: str) -> str:
    return t.strip().upper().replace(" ", "")


@dataclass
class Alert:
    ticker: str
    direction: str                 # ABOVE / BELOW
    target: float
    expires_at: Optional[str]      # ISO UTC, None = infinito
    duration_label: str
    mode: str = MODE_TOUCH
    repeat: bool = False           # True = dopo lo scatto si ri-arma quando il prezzo torna dall'altra parte
    note: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: str = field(default_factory=lambda: to_iso(utcnow()))
    status: str = ST_ACTIVE
    armed: bool = True
    ref_price: Optional[float] = None      # prezzo al momento della creazione
    last_price: Optional[float] = None
    last_check: Optional[str] = None       # fine dell'ultima finestra analizzata (ISO UTC)
    last_triggered_at: Optional[str] = None
    trigger_count: int = 0
    last_error: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Alert":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def direction_label(self) -> str:
        return "rialzo ↑" if self.direction == ABOVE else "ribasso ↓"

    def is_expired(self, now: datetime) -> bool:
        exp = from_iso(self.expires_at)
        return exp is not None and now >= exp


def compute_expiry(kind: str, tz: ZoneInfo, days: int = 1, until: Optional[date] = None) -> tuple[Optional[str], str]:
    """Ritorna (expires_at ISO UTC | None, etichetta leggibile).

    kind: "today" | "days" | "until" | "forever"
    "days": N giorni di calendario, oggi incluso -> scade a fine giornata del giorno N.
    """
    today = datetime.now(tz).date()
    if kind == "forever":
        return None, "Infinito"
    if kind == "today":
        end_day, label = today, "Solo oggi"
    elif kind == "days":
        days = max(1, int(days))
        end_day, label = today + timedelta(days=days - 1), f"{days} giorni"
    elif kind == "until":
        if until is None:
            raise ValueError("data mancante")
        end_day, label = until, f"Fino al {until.strftime('%d/%m/%Y')}"
    else:
        raise ValueError(kind)
    end = datetime.combine(end_day, time(23, 59, 59), tzinfo=tz)
    return to_iso(end), label


# --------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------
class Store:
    """Interfaccia: load/save + update atomico (load -> fn -> save sotto lock)."""

    _lock = threading.RLock()
    kind = "base"

    def _read(self) -> list[dict]:
        raise NotImplementedError

    def _write(self, data: list[dict]) -> None:
        raise NotImplementedError

    def load(self) -> list[Alert]:
        with self._lock:
            return [Alert.from_dict(d) for d in self._read()]

    def update(self, fn: Callable[[list[Alert]], Optional[list[Alert]]]) -> list[Alert]:
        with self._lock:
            alerts = [Alert.from_dict(d) for d in self._read()]
            res = fn(alerts)
            if res is not None:
                alerts = res
            self._write([a.to_dict() for a in alerts])
            return alerts

    # scorciatoie usate dalla UI
    def add(self, alert: Alert) -> None:
        self.update(lambda al: al + [alert])

    def remove(self, alert_id: str) -> None:
        self.update(lambda al: [a for a in al if a.id != alert_id])

    def patch(self, alert_id: str, **changes) -> None:
        def fn(al):
            for a in al:
                if a.id == alert_id:
                    for k, v in changes.items():
                        setattr(a, k, v)
            return al
        self.update(fn)


class JsonStore(Store):
    kind = "file JSON"

    def __init__(self, path: str):
        self.path = path

    def _read(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        return json.loads(txt) if txt else []

    def _write(self, data: list[dict]) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)  # scrittura atomica


class GistStore(Store):
    """Salva gli alert in un GitHub Gist (segreto): persiste anche se Streamlit Cloud riavvia l'app,
    ed è condiviso con il checker che gira su GitHub Actions."""

    kind = "GitHub Gist"
    FILENAME = "alerts.json"

    def __init__(self, gist_id: str, token: str):
        self.url = f"https://api.github.com/gists/{gist_id}"
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def _read(self) -> list[dict]:
        r = requests.get(self.url, headers=self.headers, timeout=20)
        r.raise_for_status()
        files = r.json().get("files", {})
        f = files.get(self.FILENAME)
        if not f:
            return []
        content = f.get("content", "")
        if f.get("truncated"):
            content = requests.get(f["raw_url"], timeout=20).text
        content = content.strip()
        return json.loads(content) if content else []

    def _write(self, data: list[dict]) -> None:
        body = {"files": {self.FILENAME: {"content": json.dumps(data, indent=2, ensure_ascii=False)}}}
        r = requests.patch(self.url, headers=self.headers, json=body, timeout=20)
        r.raise_for_status()


def make_store(cfg: Config) -> Store:
    if cfg.gist_id and cfg.github_token:
        return GistStore(cfg.gist_id, cfg.github_token)
    return JsonStore(cfg.alerts_file)


# --------------------------------------------------------------------------------------
# Prezzi (yfinance)
# --------------------------------------------------------------------------------------
def fetch_bars(ticker: str, include_prepost: bool = False, period: str = "5d") -> pd.DataFrame:
    """Candele a 5 minuti degli ultimi giorni, indice in UTC. DataFrame vuoto se nessun dato."""
    import yfinance as yf

    df = yf.Ticker(ticker).history(period=period, interval=BAR_INTERVAL, prepost=include_prepost,
                                   auto_adjust=False, actions=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df[["Open", "High", "Low", "Close"]].dropna()
    idx = df.index
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return df


def last_price(ticker: str, include_prepost: bool = False) -> Optional[float]:
    df = fetch_bars(ticker, include_prepost, period="5d")
    return float(df["Close"].iloc[-1]) if not df.empty else None


# --------------------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------------------
def send_telegram(cfg: Config, text: str) -> None:
    if not cfg.telegram_ok:
        raise RuntimeError("Telegram non configurato (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    r = requests.post(
        f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
        data={"chat_id": cfg.telegram_chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=20,
    )
    if r.status_code != 200 or not r.json().get("ok"):
        raise RuntimeError(f"Telegram ha risposto {r.status_code}: {r.text[:200]}")


def telegram_find_chat_ids(token: str) -> list[dict]:
    """Legge gli ultimi messaggi ricevuti dal bot per scoprire il chat_id."""
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    r.raise_for_status()
    out = {}
    for u in r.json().get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            out[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    return [{"chat_id": k, "nome": v} for k, v in out.items()]


def fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x:,.2f}" if abs(x) >= 1 else f"{x:.5f}"


def fmt_dt(iso: Optional[str], tz: ZoneInfo, with_year: bool = False) -> str:
    if not iso:
        return "—"
    return from_iso(iso).astimezone(tz).strftime("%d/%m/%Y %H:%M" if with_year else "%d/%m %H:%M")


def trigger_message(a: Alert, price: float, extreme: float, when: datetime, tz: ZoneInfo) -> str:
    verb = "ha superato al <b>rialzo</b>" if a.direction == ABOVE else "ha rotto al <b>ribasso</b>"
    icon = "🟢⬆️" if a.direction == ABOVE else "🔴⬇️"
    ext_lbl = "massimo" if a.direction == ABOVE else "minimo"
    lines = [
        f"{icon} <b>{html.escape(a.ticker)}</b> {verb} {fmt_price(a.target)}",
        f"Prezzo attuale: <b>{fmt_price(price)}</b>  ({ext_lbl} nella finestra: {fmt_price(extreme)})",
        f"Orario: {when.astimezone(tz).strftime('%d/%m %H:%M')} ({tz.key})",
        f"Durata alert: {html.escape(a.duration_label)}"
        + ("  · si ri-arma" if a.repeat else "  · disattivato dopo lo scatto"),
    ]
    if a.note:
        lines.append(f"📝 {html.escape(a.note)}")
    return "\n".join(lines)


def expiry_message(a: Alert) -> str:
    side = "sopra" if a.direction == ABOVE else "sotto"
    return (f"⌛ Alert scaduto: <b>{html.escape(a.ticker)}</b> {side} {fmt_price(a.target)} "
            f"({html.escape(a.duration_label)}). Ultimo prezzo: {fmt_price(a.last_price)}"
            + (f"\n📝 {html.escape(a.note)}" if a.note else ""))


# --------------------------------------------------------------------------------------
# Valutazione
# --------------------------------------------------------------------------------------
@dataclass
class Evaluation:
    hit: bool = False
    rearm: bool = False
    expired: bool = False
    price: Optional[float] = None
    extreme: Optional[float] = None
    window_end: Optional[datetime] = None


def evaluate(a: Alert, bars: pd.DataFrame, now: datetime) -> Evaluation:
    """Guarda le candele arrivate dall'ultimo controllo (o dalla creazione) e decide se l'alert scatta."""
    ev = Evaluation()
    exp = from_iso(a.expires_at)
    start = from_iso(a.last_check) or from_iso(a.created_at)

    if bars is not None and not bars.empty:
        ev.price = float(bars["Close"].iloc[-1])
        bar_end = bars.index + pd.Timedelta(minutes=BAR_MINUTES)
        mask = bar_end > pd.Timestamp(start)
        if exp is not None:
            mask &= bars.index < pd.Timestamp(exp)
        win = bars[mask]
        if not win.empty:
            ev.window_end = min(win.index[-1].to_pydatetime() + timedelta(minutes=BAR_MINUTES), now)
            if a.direction == ABOVE:
                col = win["High"] if a.mode == MODE_TOUCH else win["Close"]
                ev.extreme = float(col.max())
                crossed = ev.extreme >= a.target
                back = float(win["Close"].iloc[-1]) < a.target
            else:
                col = win["Low"] if a.mode == MODE_TOUCH else win["Close"]
                ev.extreme = float(col.min())
                crossed = ev.extreme <= a.target
                back = float(win["Close"].iloc[-1]) > a.target

            if a.armed and crossed:
                ev.hit = True
            elif not a.armed and back:
                ev.rearm = True

    ev.expired = a.is_expired(now)
    return ev


def run_check(cfg: Config, store: Store, now: Optional[datetime] = None,
              fetch: Callable[..., pd.DataFrame] = fetch_bars,
              notify: Callable[[Config, str], None] = send_telegram) -> dict:
    """Un giro completo: scarica i prezzi, valuta, manda gli alert, salva.
    Ritorna un riepilogo per la UI / i log."""
    now = now or utcnow()
    tz = cfg.tz
    summary = {"time": to_iso(now), "checked": 0, "triggered": [], "expired": [], "errors": []}

    snapshot = [a for a in store.load() if a.status == ST_ACTIVE]
    if not snapshot:
        return summary

    # 1) download (una volta per ticker)
    bars_by_ticker: dict[str, pd.DataFrame] = {}
    fetch_err: dict[str, str] = {}
    for t in sorted({a.ticker for a in snapshot}):
        try:
            bars_by_ticker[t] = fetch(t, cfg.include_prepost)
            if bars_by_ticker[t].empty:
                fetch_err[t] = "nessun dato da yfinance (ticker errato o mercato senza dati)"
        except Exception as e:  # noqa: BLE001
            fetch_err[t] = f"errore download: {e}"
            bars_by_ticker[t] = pd.DataFrame()

    # 2) valutazione + notifiche (fuori dal lock, così la UI non si blocca)
    updates: dict[str, dict] = {}
    for a in snapshot:
        summary["checked"] += 1
        ev = evaluate(a, bars_by_ticker.get(a.ticker), now)
        upd: dict = {"last_error": fetch_err.get(a.ticker)}
        if ev.price is not None:
            upd["last_price"] = ev.price

        if ev.hit:
            try:
                notify(cfg, trigger_message(a, ev.price, ev.extreme, now, tz))
                upd.update(last_triggered_at=to_iso(now), trigger_count=a.trigger_count + 1, armed=False)
                if not a.repeat:
                    upd["status"] = ST_TRIGGERED
                upd["last_check"] = to_iso(ev.window_end)
                summary["triggered"].append(f"{a.ticker} {a.direction_label} {fmt_price(a.target)}")
            except Exception as e:  # noqa: BLE001
                # non avanzo last_check: al prossimo giro riprova a mandarlo
                upd["last_error"] = f"invio Telegram fallito: {e}"
                summary["errors"].append(f"{a.ticker}: {upd['last_error']}")
        else:
            if ev.rearm:
                upd["armed"] = True
            if ev.window_end:
                upd["last_check"] = to_iso(ev.window_end)

        if ev.expired and upd.get("status") != ST_TRIGGERED:
            upd["status"] = ST_EXPIRED
            summary["expired"].append(f"{a.ticker} {fmt_price(a.target)}")
            if cfg.notify_expiry and cfg.telegram_ok:
                try:
                    tmp = Alert.from_dict({**a.to_dict(), **upd})
                    notify(cfg, expiry_message(tmp))
                except Exception as e:  # noqa: BLE001
                    log.warning("notifica scadenza fallita: %s", e)

        if upd.get("last_error") and upd["last_error"] not in summary["errors"]:
            summary["errors"].append(f"{a.ticker}: {upd['last_error']}")
        updates[a.id] = upd

    # 3) salvataggio: rileggo lo stato attuale e applico solo agli alert ancora attivi
    #    (se nel frattempo l'utente ne ha rimosso o messo in pausa uno, vince l'utente)
    def apply(alerts: list[Alert]):
        for al in alerts:
            if al.id in updates and al.status == ST_ACTIVE:
                for k, v in updates[al.id].items():
                    setattr(al, k, v)
        return alerts

    store.update(apply)
    summary["errors"] = list(dict.fromkeys(summary["errors"]))
    return summary


def next_run_time(now: datetime, interval_minutes: int, offset_seconds: int = 60) -> datetime:
    """Prossimo multiplo dell'intervallo (es. ogni ora alle hh:01, così l'ultima candela è chiusa)."""
    step = interval_minutes * 60
    ts = now.timestamp()
    nxt = (int(ts - offset_seconds) // step + 1) * step + offset_seconds
    return datetime.fromtimestamp(nxt, tz=timezone.utc)
