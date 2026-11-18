import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import pytest
import core

T0 = datetime(2026, 11, 18, 14, 30, tzinfo=timezone.utc)  # 9:30 NY


def bars(prices, start=T0, highs=None, lows=None):
    idx = pd.date_range(start, periods=len(prices), freq="5min", tz="UTC")
    c = pd.Series(prices, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": highs if highs is not None else c + 0.1,
                         "Low": lows if lows is not None else c - 0.1, "Close": c}, index=idx)


class Fake:
    def __init__(self): self.data = {}; self.sent = []; self.fail = False
    def fetch(self, t, prepost=False):
        if t not in self.data: raise RuntimeError("404")
        return self.data[t]
    def notify(self, cfg, text):
        if self.fail: raise RuntimeError("telegram down")
        self.sent.append(text)


@pytest.fixture
def env(tmp_path):
    cfg = core.Config(telegram_token="x", telegram_chat_id="1", alerts_file=str(tmp_path / "a.json"))
    return cfg, core.JsonStore(cfg.alerts_file), Fake()


def mk(store, **kw):
    d = dict(ticker="AAPL", direction=core.ABOVE, target=230, expires_at=None, duration_label="Infinito",
             created_at=core.to_iso(T0))
    d.update(kw); a = core.Alert(**d); store.add(a); return a


def run(cfg, store, f, now):
    return core.run_check(cfg, store, now=now, fetch=f.fetch, notify=f.notify)


def get(store, i): return next(a for a in store.load() if a.id == i)


def test_above_touch_triggers_once(env):
    cfg, store, f = env
    a = mk(store)
    f.data["AAPL"] = bars([228, 229, 230.0, 229.5])  # high 230.1 >= 230
    s = run(cfg, store, f, T0 + timedelta(minutes=30))
    assert len(f.sent) == 1 and "AAPL" in f.sent[0] and s["triggered"]
    assert get(store, a.id).status == core.ST_TRIGGERED
    run(cfg, store, f, T0 + timedelta(minutes=90))
    assert len(f.sent) == 1  # non più attivo


def test_no_trigger_below_target(env):
    cfg, store, f = env
    a = mk(store)
    f.data["AAPL"] = bars([228, 229, 229.5])
    run(cfg, store, f, T0 + timedelta(minutes=20))
    x = get(store, a.id)
    assert not f.sent and x.status == core.ST_ACTIVE and x.last_price == 229.5 and x.last_check


def test_spike_before_creation_ignored(env):
    cfg, store, f = env
    a = mk(store, created_at=core.to_iso(T0 + timedelta(minutes=30)))
    f.data["AAPL"] = bars([231, 231, 228, 228, 228, 228, 228, 228])  # sopra solo prima della creazione
    run(cfg, store, f, T0 + timedelta(minutes=45))
    assert not f.sent


def test_spike_between_checks_caught(env):
    cfg, store, f = env
    a = mk(store)
    f.data["AAPL"] = bars([228] * 12)
    run(cfg, store, f, T0 + timedelta(minutes=60))
    assert not f.sent
    # nell'ora successiva tocca 230.5 e rientra: al controllo il prezzo è 228 ma l'alert scatta
    f.data["AAPL"] = bars([228] * 12 + [229, 230.5, 228] + [228] * 9)
    run(cfg, store, f, T0 + timedelta(minutes=120))
    assert len(f.sent) == 1 and "230.60" in f.sent[0]


def test_below_close_mode(env):
    cfg, store, f = env
    a = mk(store, ticker="NQ=F", direction=core.BELOW, target=20000, mode=core.MODE_CLOSE)
    # il minimo buca 20000 ma nessuna chiusura sotto
    f.data["NQ=F"] = bars([20010, 20005], lows=pd.Series([19990.0, 19995.0]).values)
    run(cfg, store, f, T0 + timedelta(minutes=10))
    assert not f.sent
    f.data["NQ=F"] = bars([20010, 20005, 19990])
    run(cfg, store, f, T0 + timedelta(minutes=15))
    assert len(f.sent) == 1 and "ribasso" in f.sent[0]


def test_below_touch_mode(env):
    cfg, store, f = env
    a = mk(store, ticker="QQQ", direction=core.BELOW, target=500)
    f.data["QQQ"] = bars([501, 500.05])  # low 499.95
    run(cfg, store, f, T0 + timedelta(minutes=10))
    assert len(f.sent) == 1


def test_repeat_rearms(env):
    cfg, store, f = env
    a = mk(store, repeat=True)
    seq = [228, 231]                                   # 1) scatta
    f.data["AAPL"] = bars(seq); run(cfg, store, f, T0 + timedelta(minutes=10))
    assert len(f.sent) == 1 and get(store, a.id).armed is False
    seq += [231, 232]                                  # 2) resta sopra: niente spam
    f.data["AAPL"] = bars(seq); run(cfg, store, f, T0 + timedelta(minutes=20))
    assert len(f.sent) == 1
    seq += [229]                                       # 3) torna sotto (low 228.9): si ri-arma
    f.data["AAPL"] = bars(seq); run(cfg, store, f, T0 + timedelta(minutes=25))
    assert len(f.sent) == 1 and get(store, a.id).armed is True
    seq += [230.5]                                     # 4) riscatta
    f.data["AAPL"] = bars(seq); run(cfg, store, f, T0 + timedelta(minutes=30))
    x = get(store, a.id)
    assert len(f.sent) == 2 and x.trigger_count == 2 and x.status == core.ST_ACTIVE


def test_expiry(env):
    cfg, store, f = env
    a = mk(store, expires_at=core.to_iso(T0 + timedelta(minutes=20)), duration_label="Solo oggi")
    # tocca il livello solo DOPO la scadenza -> non deve scattare, deve scadere
    f.data["AAPL"] = bars([228, 228, 228, 228, 228, 231])
    run(cfg, store, f, T0 + timedelta(minutes=40))
    x = get(store, a.id)
    assert x.status == core.ST_EXPIRED and len(f.sent) == 1 and "scaduto" in f.sent[0]


def test_trigger_at_expiry_check_wins(env):
    cfg, store, f = env
    a = mk(store, expires_at=core.to_iso(T0 + timedelta(minutes=20)))
    f.data["AAPL"] = bars([228, 231, 228, 228, 228, 228])
    run(cfg, store, f, T0 + timedelta(minutes=40))
    x = get(store, a.id)
    assert x.status == core.ST_TRIGGERED and len(f.sent) == 1 and "superato" in f.sent[0]


def test_telegram_failure_retries(env):
    cfg, store, f = env
    a = mk(store)
    f.data["AAPL"] = bars([228, 231]); f.fail = True
    run(cfg, store, f, T0 + timedelta(minutes=10))
    x = get(store, a.id)
    assert x.status == core.ST_ACTIVE and "Telegram" in x.last_error
    f.fail = False
    run(cfg, store, f, T0 + timedelta(minutes=70))
    assert len(f.sent) == 1 and get(store, a.id).status == core.ST_TRIGGERED


def test_bad_ticker_error(env):
    cfg, store, f = env
    a = mk(store, ticker="NONEXIST")
    s = run(cfg, store, f, T0 + timedelta(minutes=10))
    assert s["errors"] and get(store, a.id).last_error


def test_paused_ignored(env):
    cfg, store, f = env
    a = mk(store, status=core.ST_PAUSED)
    f.data["AAPL"] = bars([231])
    run(cfg, store, f, T0 + timedelta(minutes=10))
    assert not f.sent


def test_user_remove_during_check_not_resurrected(env):
    cfg, store, f = env
    a = mk(store); b = mk(store, ticker="QQQ", target=1e9)
    f.data["AAPL"] = bars([228]); f.data["QQQ"] = bars([500])
    orig = f.fetch
    def fetch_and_delete(t, p=False):
        store.remove(a.id); return orig(t, p)
    core.run_check(cfg, store, now=T0 + timedelta(minutes=10), fetch=fetch_and_delete, notify=f.notify)
    ids = [x.id for x in store.load()]
    assert a.id not in ids and b.id in ids


def test_compute_expiry():
    tz = ZoneInfo("Europe/Rome")
    assert core.compute_expiry("forever", tz) == (None, "Infinito")
    iso, lbl = core.compute_expiry("today", tz)
    e = core.from_iso(iso).astimezone(tz)
    assert e.date() == datetime.now(tz).date() and (e.hour, e.minute) == (23, 59)
    iso, lbl = core.compute_expiry("days", tz, days=3)
    assert core.from_iso(iso).astimezone(tz).date() == datetime.now(tz).date() + timedelta(days=2)


def test_next_run_time():
    n = core.next_run_time(datetime(2026, 11, 18, 14, 20, tzinfo=timezone.utc), 60)
    assert n == datetime(2026, 11, 18, 15, 1, tzinfo=timezone.utc)
    n = core.next_run_time(datetime(2026, 11, 18, 14, 0, 30, tzinfo=timezone.utc), 60)
    assert n == datetime(2026, 11, 18, 14, 1, tzinfo=timezone.utc)


def test_old_json_fields_tolerated(env):
    cfg, store, f = env
    store._write([{**core.Alert("AAPL", "above", 1, None, "x").to_dict(), "campo_vecchio": 1}])
    assert store.load()[0].ticker == "AAPL"
