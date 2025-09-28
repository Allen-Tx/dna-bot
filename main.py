import os
import json
import time
import logging
import asyncio
import traceback
from datetime import datetime, timezone

import requests
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("memecoin-dna-bot")

# =========================
# Env (set these in Render → Environment)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # required
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID")  # optional (int as string). If not set, user must /start the bot
NETWORK = os.getenv("NETWORK", "solana")
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))
NEW_PAIR_MAX_AGE_MIN = int(os.getenv("NEW_PAIR_MAX_AGE_MIN", "360"))
SEEN_TTL_SEC = int(os.getenv("SEEN_TTL_SEC", "10800"))  # 3h
PORT = int(os.getenv("PORT", "10000"))  # Render uses this for web services

# DNA thresholds (override via env to tune live)
WINNER_MAX_FDV = float(os.getenv("WINNER_MAX_FDV", "250000"))
WINNER_MIN_LP  = float(os.getenv("WINNER_MIN_LP",  "15000"))

TYTTY_MAX_FDV  = float(os.getenv("TYTTY_MAX_FDV",  "600000"))
TYTTY_MIN_LP   = float(os.getenv("TYTTY_MIN_LP",   "20000"))  # prefers 30k+

STRICT_MAX_FDV = float(os.getenv("STRICT_MAX_FDV", "500000"))
STRICT_MIN_LP  = float(os.getenv("STRICT_MIN_LP",  "30000"))

# Optional: Google Sheets
SHEET_ID = os.getenv("SHEET_ID")
SHEETS_SERVICE_JSON = os.getenv("SHEETS_SERVICE_JSON")

# =========================
# Optional Google Sheets wiring (auto-disabled if libs/creds missing)
# =========================
USE_SHEETS = False
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    USE_SHEETS = bool(SHEET_ID and SHEETS_SERVICE_JSON)
except Exception:
    USE_SHEETS = False

def sheets_client_or_none():
    if not USE_SHEETS:
        return None
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds_dict = json.loads(SHEETS_SERVICE_JSON)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet("Logs")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="Logs", rows=1000, cols=18)
            ws.append_row([
                "ts_utc", "dna", "pairAddress", "base", "quote",
                "fdv", "liquidity_usd", "age_min", "url", "dex_id",
                "chain_id", "pair_createdAt", "txns_m5", "vol_24h",
                "fdv_raw", "liq_raw", "notes", "score"
            ])
        return ws
    except Exception as e:
        log.warning("Sheets init failed: %s", e)
        return None

SHEETS_WS = sheets_client_or_none()

def log_to_sheet_safe(row):
    if not SHEETS_WS:
        return
    try:
        SHEETS_WS.append_row(row, value_input_option="RAW")
    except Exception as e:
        log.warning("Sheets append failed: %s", e)

# =========================
# Runtime state
# =========================
SEEN_CACHE = {}  # pairAddress -> last_alert_ts
DYNAMIC_ALERT_CHAT_ID = None  # set by /start if ALERT_CHAT_ID not provided

# =========================
# DexScreener helpers
# =========================
def fetch_pairs():
    url = f"https://api.dexscreener.com/latest/dex/pairs/{NETWORK}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("pairs", []) or []

def calc_age_minutes(pair):
    created_ms = pair.get("pairCreatedAt")
    if not created_ms:
        return None
    now_ms = int(time.time() * 1000)
    return max(0, int((now_ms - int(created_ms)) / 1000 / 60))

def get_fdv(pair):
    v = pair.get("fullyDilutedValuation")
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

def get_liq_usd(pair):
    v = (pair.get("liquidity") or {}).get("usd")
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

def pair_url(pair):
    paddr = pair.get("pairAddress")
    chain = pair.get("chainId", NETWORK)
    if paddr:
        return f"https://dexscreener.com/{chain}/{paddr}"
    base_addr = (pair.get("baseToken") or {}).get("address")
    if base_addr:
        return f"https://dexscreener.com/{chain}/{base_addr}"
    return "https://dexscreener.com"

# =========================
# DNA classifier
# =========================
def classify_dna(pair):
    age = calc_age_minutes(pair)
    fdv = get_fdv(pair)
    lp  = get_liq_usd(pair)

    if None in (age, fdv, lp):
        return (None, None, "missing metrics")

    if age > NEW_PAIR_MAX_AGE_MIN:
        return (None, None, f"too old ({age}m)")

    txns = (pair.get("txns") or {}).get("m5") or {}
    m5_buys  = int(txns.get("buys", 0))
    m5_sells = int(txns.get("sells", 0))
    if (m5_buys + m5_sells) == 0:
        return (None, None, "no 5m activity")

    # Winner
    if fdv <= WINNER_MAX_FDV and lp >= WINNER_MIN_LP:
        score = (lp / 1000) + max(0, 30 - age) + (m5_buys * 0.5)
        return ("Winner", score, "")

    # Tytty
    if fdv <= TYTTY_MAX_FDV and lp >= TYTTY_MIN_LP:
        bonus = 5 if lp >= 30000 else 0
        score = (lp / 1500) + max(0, 30 - age) + bonus + (m5_buys * 0.3)
        return ("Tytty", score, "")

    # Strict
    if fdv <= STRICT_MAX_FDV and lp >= STRICT_MIN_LP:
        score = (lp / 2000) + max(0, 30 - age) + (m5_buys * 0.2)
        # Note: contract lock/renounce not checked here
        return ("Strict", score, "contract/lock checks TODO")

    return (None, None, "does not meet DNA")

def short_pair_text(pair, dna, score, note=""):
    base = (pair.get("baseToken") or {}).get("symbol") or "BASE"
    quote = (pair.get("quoteToken") or {}).get("symbol") or "QUOTE"
    fdv = get_fdv(pair)
    lp  = get_liq_usd(pair)
    age = calc_age_minutes(pair)
    dex = pair.get("dexId") or "dex"
    url = pair_url(pair)

    lines = [
        f"🧬 <b>{dna}</b> hit | <b>{base}</b>/<b>{quote}</b> on <b>{dex}</b>",
        f"FDV: ${int(fdv):,} | LP: ${int(lp):,} | Age: {age}m | Score: {round(score, 2)}",
        f"{url}",
    ]
    if note:
        lines.append(f"Note: {note}")
    return "\n".join(lines)

def should_alert(pair):
    addr = pair.get("pairAddress") or (pair.get("baseToken") or {}).get("address")
    if not addr:
        return True
    now = time.time()
    last = SEEN_CACHE.get(addr, 0)
    if now - last < SEEN_TTL_SEC:
        return False
    SEEN_CACHE[addr] = now
    return True

# =========================
# Telegram commands
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DYNAMIC_ALERT_CHAT_ID
    DYNAMIC_ALERT_CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        "Yo! Bot online ✅\n"
        "I’ll alert you here when a pair matches your DNA.\n"
        "Tip: set ALERT_CHAT_ID in Render to send alerts to a fixed chat/group."
    )

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def dna_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🧬 <b>DNA Settings</b>\n"
        f"Winner: FDV ≤ ${int(WINNER_MAX_FDV):,}, LP ≥ ${int(WINNER_MIN_LP):,}\n"
        f"Tytty:  FDV ≤ ${int(TYTTY_MAX_FDV):,}, LP ≥ ${int(TYTTY_MIN_LP):,}\n"
        f"Strict: FDV ≤ ${int(STRICT_MAX_FDV):,}, LP ≥ ${int(STRICT_MIN_LP):,}\n"
        f"Max Age: {NEW_PAIR_MAX_AGE_MIN} min | Network: {NETWORK.upper()}\n"
        f"Scan every: {SCAN_INTERVAL_SEC}s"
    )
    await update.message.reply_html(txt)

# =========================
# Alert helper
# =========================
async def send_alert(app: Application, text_html: str):
    chat_id = None
    if ALERT_CHAT_ID:
        try:
            chat_id = int(ALERT_CHAT_ID)
        except Exception:
            chat_id = ALERT_CHAT_ID
    elif DYNAMIC_ALERT_CHAT_ID:
        chat_id = DYNAMIC_ALERT_CHAT_ID

    if not chat_id:
        log.info("No ALERT_CHAT_ID or /start yet — skipping alert.")
        return
    try:
        await app.bot.send_message(chat_id=chat_id, text=text_html, parse_mode="HTML")
    except Exception as e:
        log.warning("send_alert failed: %s", e)

# =========================
# Discovery cycle (scheduled)
# =========================
async def discovery_cycle(app: Application):
    try:
        pairs = fetch_pairs()
    except Exception as e:
        log.error("DexScreener fetch failed: %s", e)
        return

    winners = []
    for p in pairs:
        try:
            dna, score, note = classify_dna(p)
            if not dna:
                continue
            if not should_alert(p):
                continue
            winners.append((score, p, dna, note))
        except Exception as inner:
            log.warning("pair processing error: %s", inner)

    winners.sort(key=lambda x: x[0], reverse=True)
    for score, p, dna, note in winners[:5]:
        msg = short_pair_text(p, dna, score, note)
        await send_alert(app, msg)

        # log to sheet (optional)
        try:
            fdv = get_fdv(p)
            lp = get_liq_usd(p)
            age = calc_age_minutes(p)
            row = [
                datetime.now(timezone.utc).isoformat(),
                dna,
                p.get("pairAddress"),
                (p.get("baseToken") or {}).get("symbol"),
                (p.get("quoteToken") or {}).get("symbol"),
                int(fdv) if fdv else "",
                int(lp) if lp else "",
                age if age is not None else "",
                pair_url(p),
                p.get("dexId"),
                p.get("chainId"),
                p.get("pairCreatedAt"),
                (p.get("txns") or {}).get("m5", {}),
                (p.get("volume") or {}).get("h24"),
                fdv,
                lp,
                note,
                float(score),
            ]
            log_to_sheet_safe(row)
        except Exception as e:
            log.warning("sheet log error: %s", e)

# =========================
# Health server (keeps Render happy & lets UptimeRobot ping)
# =========================
async def health_handler(request):
    return web.Response(text="ok")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Health server on :{PORT}/health")

# =========================
# Post-init hook: where we can safely use job_queue
# (this FIXES the 'NoneType' job_queue/run_repeating error)
# =========================
async def on_post_init(app: Application):
    log.info("post_init: setting schedules & health server")
    # Start health server in background
    asyncio.create_task(start_health_server())

    # Now job_queue is guaranteed to exist
    jq = app.job_queue
    jq.run_repeating(lambda c: asyncio.create_task(discovery_cycle(app)),
                     interval=SCAN_INTERVAL_SEC, first=10)
    jq.run_repeating(lambda c: log.info("heartbeat ✅ running"),
                     interval=300, first=15)

# =========================
# Main
# =========================
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not found in env. Set it in Render → Environment.")

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .post_init(on_post_init)  # <— schedule after app is fully built
        .build()
    )

    # Commands
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("ping",  ping_cmd))
    application.add_handler(CommandHandler("dna",   dna_cmd))

    # Run polling (blocks until stop). post_init already set schedules.
    try:
        application.run_polling()
    except KeyboardInterrupt:
        log.info("Shutting down…")
    except Exception:
        log.error("Fatal:\n%s", traceback.format_exc())

if __name__ == "__main__":
    main()

