import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pandas as pd
from datetime import timedelta
from streamlit.testing.v1 import AppTest
import core

APP = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py")


def test_app_flow(tmp_path, monkeypatch):
    f = tmp_path / "alerts.json"
    monkeypatch.setenv("ALERTS_FILE", str(f))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("TIMEZONE", "Europe/Rome")
    sent = []
    monkeypatch.setattr(core, "last_price", lambda t, p=False: {"AAPL": 228.0, "NQ=F": 20500.0}.get(t))
    def fake_bars(t, p=False):
        now = core.utcnow()
        idx = pd.date_range(now - timedelta(minutes=10), periods=3, freq="5min", tz="UTC")
        c = pd.Series([228, 229, 231.0], index=idx)
        return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c})
    monkeypatch.setattr(core, "fetch_bars", fake_bars)
    monkeypatch.setattr(core, "send_telegram", lambda cfg, text: sent.append(text))
    # run_check ha fetch/notify come default già legati: li forzo
    orig = core.run_check
    monkeypatch.setattr(core, "run_check", lambda cfg, store, now=None, fetch=None, notify=None:
                        orig(cfg, store, now=now, fetch=fake_bars, notify=lambda c, t: sent.append(t)))

    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception

    at.text_input[0].input("aapl").run()
    assert not at.exception
    assert any("228" in m.value for m in at.metric)
    at.number_input(key="target_AAPL").set_value(230.0).run()
    at.selectbox[1].set_value("days").run()            # durata: N giorni
    at.number_input[1].set_value(3).run()
    add = next(b for b in at.button if "Aggiungi" in b.label)
    add.click().run()
    assert not at.exception, at.exception
    data = json.loads(f.read_text())
    assert len(data) == 1 and data[0]["ticker"] == "AAPL" and data[0]["target"] == 230.0
    assert data[0]["duration_label"] == "3 giorni" and data[0]["expires_at"]

    # backdate creazione così le candele finte cadono dopo
    data[0]["created_at"] = core.to_iso(core.utcnow() - timedelta(minutes=30)); f.write_text(json.dumps(data))
    btn = next(b for b in at.button if "Controlla ora" in b.label)
    btn.click().run()
    assert not at.exception, at.exception
    assert len(sent) == 1 and "AAPL" in sent[0], sent
    assert json.loads(f.read_text())[0]["status"] == "triggered"
    print("\n---- messaggio Telegram ----\n" + sent[0])
