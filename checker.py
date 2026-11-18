"""
checker.py — esegue UN controllo degli alert e termina.
Da usare con cron / GitHub Actions / Task Scheduler quando l'app Streamlit non è sempre accesa.

    python checker.py            # un giro
    python checker.py --loop     # gira per sempre, ogni CHECK_INTERVAL_MINUTES (alternativa a cron)

Legge la config dalle variabili d'ambiente (stessi nomi di secrets.toml).
"""
import argparse
import logging
import sys
import time

import core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def once(cfg: core.Config) -> int:
    store = core.make_store(cfg)
    s = core.run_check(cfg, store)
    logging.info("controllati=%s scattati=%s scaduti=%s", s["checked"], s["triggered"], s["expired"])
    for e in s["errors"]:
        logging.warning(e)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true")
    args = p.parse_args()
    cfg = core.Config.from_env()
    if not cfg.telegram_ok:
        logging.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID mancanti")
        return 1
    if not args.loop:
        return once(cfg)
    while True:
        nxt = core.next_run_time(core.utcnow(), cfg.check_interval_minutes)
        time.sleep(max(1, (nxt - core.utcnow()).total_seconds()))
        try:
            once(cfg)
        except Exception:  # noqa: BLE001
            logging.exception("controllo fallito")


if __name__ == "__main__":
    sys.exit(main())
