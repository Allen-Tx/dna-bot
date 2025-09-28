import os
import json
import time
import logging
import asyncio
import traceback
from datetime import datetime, timezone

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === Optional: Google Sheets (only used if you provide creds) ===
USE_SHEETS = False
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    USE_SHEETS = True
except Exception:
    USE_SHEETS = False

# === Logging ===
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("memecoin-dna-bot")

# =========================
# ENV VARIABLES YOU SET IN RENDER
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # required
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID")  # optional (int as string). If not set, bot will DM whoever sends /start
NETWORK = os.getenv("NETWORK", "solana")  # default solana
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))  # how often to scan
NEW_PAIR_MAX_AGE_MIN = int(os.getenv("NEW_PAIR_MAX_AGE_MIN", "360"))  # consider pairs launched in last X minutes

# DNA thresholds (defaults based on your PDF)
# You can override these per env var if you want to tune without editing code
WINNER_MAX_FDV = float(os.getenv("WINNER_MAX_FDV", "250000"))
WINNER_MIN_LP = float(os.getenv("WINNER_MIN_LP", "15000"))

TYTTY_MAX_FDV = float(os.getenv("TYTTY_MAX_FDV", "600000"))
TYTTY_MIN_LP = float(os.getenv("TYTTY_MIN_LP", "20000"))  # note: pdf says 20k-30k+
# we’ll treat 20k as minimum and prefer >30k in the score

STRICT_MAX_FDV = float(os.getenv("STRICT_MAX_FDV", "500000"))
STRICT_MIN_LP = float(os.getenv("STRICT_MIN_LP", "30000"))

# Optional: Google Sheets
SHEET_ID = os.getenv("SHEET_ID")  # the spreadsheet ID
SHEETS_SERVICE_JSON = os.getenv("SHEETS_SERVICE_JSON")  # service account JSON string

# =========================
# GLOBAL STATE
# =========================
# To avoid spamming, we keep a memory of alerted pair addresses for a little while
SEEN_CACHE = {}  # {pairAddress: last_alert_ts}
SEEN_TTL_SEC = int(os.getenv("SEEN_TTL_SEC", "10800"))  # 3 hours default

# If ALERT_CHAT_ID isn't set, we’ll store a dynamic fallback from /start
DYNAMIC_ALERT_CHAT_ID = None

# =========================
# GOOGLE SHEETS HELPER
# =========================
def sheets_client_or_none():
    if not (USE_SHEETS and SHEET_ID and SHEETS_SERVICE_JSON):
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
        # Create or open a worksheet named "Logs"
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
# DEXSCREENER FETCH
# =========================
def fetch_pairs_solana():
    """
    Pull a large batch of pairs for Solana from DexScreener.
    Docs: https://docs.dexscreener.com/
    Endpoint used: /latest/dex/pairs/:chainId
    """
    url = f"https://api.dexscreener.com/latest/dex/pairs/{NETWORK}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("pairs", []) or []

def calc_age_minutes(pair):
    # DexScreener returns "pairCreatedAt" as ms epoch (not always present)
    created_ms = pair.get("pairCreatedAt")
    if not created_ms:
        return None
    now_ms = int(time.time() * 1000)
    delta_min = (now_ms - int(created_ms)) / 1000 / 60
    return max(0, int(delta_min))

def get_fdv(pair):
    # DexScreener returns fullyDilutedValuation (in USD) sometimes as float/int
    fdv = pair.get("fullyDilutedValuation")
    if fdv is None:
        return None
    try:
        return float(fdv)
    except Exception:
        return None

def get_liquidity_usd(pair):
    # path: pair["liquidity"]["usd"]
    liq = pair.get("liquidity") or {}
    usd = liq.get("usd")
    if usd is None:
        return None
    try:
        return float(usd)
    except Exception:
        return None

def pair_url(pair):
    # Create a human URL to share in Telegram
    # Prefer pairAddress if present
    paddr = pair.get("pairAddress")
    chain_id = pair.get("chainId", NETWORK)
    if paddr:
        return f"https://dexscreener.com/{chain_id}/{paddr}"
    # fallback: token address if baseToken exists
    base_token = (pair.get("baseToken") or {}).get("address")
    if base_token:
        return f"https://dexscreener.com/{chain_id}/{base_token}"
    return "https://dexscreener.com"

# =========================
# DNA FILTERS
# =========================
def classify_dna(pair):
    """
    Returns (dna_name, score, note) or (None, None, None)
    We keep it simple & strict:
      - Must have FDV & liquidity
      - Must be newer than NEW_PAIR_MAX_AGE_MIN
    """
    age_min = calc_age_minutes(pair)
    fdv = get_fdv(pair)
    lp = get_liquidity_usd(pair)

    if fdv is None or lp is None or age_min is None:
        return (None, None, "missing metrics")

    if age_min > NEW_PAIR_MAX_AGE_MIN:
        return (None, None, f"too old ({age_min}m)")

    # Basic sanity cut (avoid dead listings)
    # e.g., require at least a few txns in last 5m (if available)
    txns = (pair.get("txns") or {}).get("m5") or {}
    m5_buys = int(txns.get("buys", 0))
    m5_sells = int(txns.get("sells", 0))
    if (m5_buys + m5_sells) == 0:
        return (None, None, "no 5m activity")

    # Now classify
    # Winner DNA
    if fdv <= WINNER_MAX_FDV and lp >= WINNER_MIN_LP:
        # simple score = more LP + younger + some activity
        score = (lp / 1000) + max(0, 30 - age_min) + (m5_buys * 0.5)
        return ("Winner", score, "")

    # Tytty DNA (balanced)
    if fdv <= TYTTY_MAX_FDV and lp >= TYTTY_MIN_LP:
        bonus = 5 if lp >= 30000 else 0  # prefer 30k+ like your PDF
        score = (lp / 1500) + max(0, 30 - age_min) + bonus + (m5_buys * 0.3)
        return ("Tytty", score, "")

    # Strict DNA (safer)
    if fdv <= STRICT_MAX_FDV and lp >= STRICT_MIN_LP:
        score = (lp / 2000) + max(0, 30 - age_min) + (m5_buys * 0.2)
        # NOTE: In your PDF, Strict also wants LP locked OR ownership renounced + socials active
        # We can’t verify that from DexScreener alone. You can wire a separate check later (Helius/Birdeye/TokenSniffer/etc.)
        return ("Strict", score, "contract/lock checks TODO")

    return (None, None, "does not meet DNA thresholds")

def short_pair_text(pair, dna, score, reason_note=""):
    base = (pair.get("baseToken") or {}).get("symbol") or "BASE"
    quote = (pair.get("quoteToken") or {}).get("symbol") or "QUOTE"
    fdv = get_fdv(pair)
    lp = get_liquidity_usd(pair)
    age = calc_age_minutes(pair)
    dex_id = pair.get("dexId") or "dex"
    url = pair_url(pair)

    lines = [
        f"🧬 <b>{dna}</b> hit | <b>{base}</b>/<b>{quote}</b> on <b>{dex_id}</b>",
        f"FDV: ${int(fdv):,} | LP: ${int(lp):,} | Age: {age}m | Score: {round(score, 2)}",
        f"{url}"
    ]
    if reason_note:
        lines.append(f"Note: {reason_note}")
    return "\n".join(lines)

def should_alert(pair):
    """avoid double alerts within a TTL"""
    addr = pair.get("pairAddress") or pair.get("url") or pair.get("info", {}).get("address")
    if not addr:
        return True
    now = time.time()
    last = SEEN_CACHE.get(addr, 0)
    if now - last < SEEN_TTL_SEC:
        return False
    SEEN_CACHE[addr] = now
    return True

# =========================
# TELEGRAM COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DYNAMIC_ALERT_CHAT_ID
    DYNAMIC_ALERT_CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        "Yo! Bot online ✅\n"
        "I’ll alert you here when a pair matches your DNA.\n"
        "Tip: set ALERT_CHAT_ID in Render to force alerts to a specific chat/group."
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
# ALERT SEND
# =========================
async def send_alert(application: Application, text_html: str):
    chat_id = None
    if ALERT_CHAT_ID:
        try:
            chat_id = int(ALERT_CHAT_ID)
        except Exception:
            chat_id = ALERT_CHAT_ID
    elif DYNAMIC_ALERT_CHAT_ID:
        chat_id = DYNAMIC_ALERT_CHAT_ID

    if not chat_id:
        log.info("No ALERT_CHAT_ID or /start fallback yet — skipping alert.")
        return
    try:
        await application.bot.send_message(chat_id=chat_id, text=text_html, parse_mode="HTML", disable_web_page_preview=False)
    except Exception as e:
        log.warning("send_alert failed: %s", e)

# =========================
# DISCOVERY CYCLE (runs on schedule)
# =========================
async def discovery_cycle(application: Application):
    try:
        pairs = fetch_pairs_solana()
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

            msg = short_pair_text(p, dna, score, note)
            winners.append((score, msg, p, dna, note))
        except Exception as inner:
            log.warning("pair processing error: %s", inner)

    # Sort by score (desc) and alert top few to avoid spam (tuneable)
    winners.sort(key=lambda x: x[0], reverse=True)
    top_n = winners[:5]

    for score, msg, p, dna, note in top_n:
        await send_alert(application, msg)
        # Log to Sheets if configured
        try:
            fdv = get_fdv(p)
            lp = get_liquidity_usd(p)
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
                float(score)
            ]
            log_to_sheet_safe(row)
        except Exception as e:
            log.warning("sheet log error: %s", e)

# =========================
# HEALTH SERVER (simple)
# =========================
# Render free plans like a web process; UptimeRobot can ping this path
from aiohttp import web

async def health_handler(request):
    return web.Response(text="ok")

def make_health_app():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    return app

# =========================
# MAIN
# =========================
async def on_startup(app: Application):
    log.info("Bot starting…")

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not found in env. Set it in Render → Environment.")

    application = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    # Commands
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("dna", dna_cmd))

    # Schedule repeating discovery
    jq = application.job_queue
    jq.run_repeating(lambda c: asyncio.create_task(discovery_cycle(application)), interval=SCAN_INTERVAL_SEC, first=10)
    jq.run_repeating(lambda c: log.info("heartbeat ✅ running"), interval=300, first=15)

    # Run Telegram + Health server together
    # We’ll spin up aiohttp on PORT if provided (Render sets PORT)
    port = int(os.getenv("PORT", "10000"))

    async def runner():
        # Start health server
        health_app = make_health_app()
        runner = web.AppRunner(health_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info(f"Health server on :{port}/health")

        # Start Telegram polling (blocks until stop)
        await application.run_polling()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        log.info("Shutting down…")
    except Exception:
        log.error("Fatal:\n%s", traceback.format_exc())

if __name__ == "__main__":
    main()
