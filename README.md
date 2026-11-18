# 🔔 Price Alerts — Streamlit + yfinance + Telegram

Alert su rottura di livelli di prezzo (al rialzo o al ribasso) per qualsiasi ticker Yahoo
(`AAPL`, `QQQ`, `NQ=F`, `ES=F`, `^GSPC`, `EURUSD=X`, `BTC-USD`…), con controllo ogni ora e notifica su Telegram.

## File
| File | Cosa fa |
|---|---|
| `app.py` | Interfaccia Streamlit: crei, metti in pausa, rimuovi gli alert; controllo orario in background |
| `core.py` | Logica: prezzi yfinance, valutazione, scadenze, Telegram, salvataggio |
| `checker.py` | Un controllo da riga di comando (per cron / GitHub Actions) |
| `.github/workflows/price-alerts.yml` | Controllo orario su GitHub Actions (per Streamlit Cloud) |
| `tests/` | Test (`pytest -q tests`) |

## 1. Bot Telegram (5 minuti)
1. Su Telegram apri **@BotFather** → `/newbot` → copia il **token**.
2. Scrivi un messaggio qualsiasi al tuo nuovo bot.
3. Metti il token in `secrets.toml`, avvia l'app e nella sidebar premi **"Trova chat_id"**: copia l'id in `TELEGRAM_CHAT_ID`.
4. Premi **"Invia messaggio di prova"**.

## 2a. In locale / su un PC o server sempre acceso (la più semplice)
```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # e compila token + chat_id
streamlit run app.py
```
Il controllo orario gira dentro l'app finché il processo è acceso (non serve tenere aperto il browser).
Gli alert stanno in `alerts.json`.

## 2b. Su Streamlit Community Cloud
Su Streamlit Cloud l'app **va in sleep se nessuno la visita** e il disco **si azzera a ogni riavvio**:
il thread orario si fermerebbe e gli alert andrebbero persi. Quindi:

1. **Gist per salvare gli alert**: su gist.github.com crea un gist *secret* con un file `alerts.json` contenente `[]`.
   L'id è l'ultima parte dell'URL.
2. **Token GitHub**: Settings → Developer settings → Personal access tokens → **Tokens (classic)** → solo permesso `gist`.
3. **Streamlit Cloud → App settings → Secrets**: incolla il contenuto di `secrets.toml.example` compilato,
   con `GIST_ID`, `GITHUB_TOKEN` e `RUN_SCHEDULER = false`.
4. **GitHub Actions** (nel repo dell'app) → Settings → Secrets and variables → Actions:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GIST_ID`, `GIST_TOKEN` (lo stesso token del punto 2).
   Il workflow gira ogni ora al minuto :01; la prima volta lancialo a mano con "Run workflow".

Così l'app serve solo da pannello di controllo e il controllo orario non dipende da lei.

## Come decide se scattare
- Ogni ora scarica le candele **5 minuti** e guarda **tutto quello che è successo dall'ultimo controllo**:
  una spike alle 10:23 rientrata alle 10:40 viene presa comunque al controllo delle 11:01.
- **Al tocco** = basta il massimo/minimo della candela. **Su chiusura** = serve una chiusura 5m oltre il livello.
- **Una volta sola** (default): dopo lo scatto va nello storico. **Ri-arma**: si riattiva quando il prezzo torna
  dall'altra parte del livello, senza mandarti un messaggio ogni ora mentre resta sopra.
- **Durata**: *solo oggi*, *N giorni* (oggi incluso) o *fino a una data* scadono alle 23:59 del `TIMEZONE`;
  *infinito* resta finché non lo rimuovi. Alla scadenza arriva un messaggio (`NOTIFY_EXPIRY = false` per toglierlo).
- Se Telegram non risponde, l'alert resta attivo e riprova al giro dopo.
- Pre/after-market USA esclusi di default (`INCLUDE_PREPOST = true` per includerli). I futures (`NQ=F`) girano ~23h.

## Note
- yfinance è gratuito ma non ufficiale: ogni tanto Yahoo cambia qualcosa o limita le richieste. Con poche decine di
  ticker all'ora non è un problema; se smette di funzionare, `pip install -U yfinance` di solito risolve.
- Non attivare insieme lo scheduler dell'app **e** GitHub Actions sullo stesso Gist, altrimenti ricevi alert doppi.
