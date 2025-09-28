# main.py
import asyncio
import logging
import os
from typing import Dict, Any, List, Optional, Tuple

import httpx
from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    JobQueue,
)

# -----------------
# Logging
# -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("memecoin-dna-bot")

# -----------------
# Config
# -----------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var.")

CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()  # can be user id or group/channel id
DISCOVERY_INTERVAL = int(os.environ.get("DISCOVERY_INTERVAL_SECONDS", "60"))
HEALTH_PORT = int(os.environ.get("PORT", "10000"))

# -----------------
# DNA thresholds (from your PDF)
# -----------------
DNA_RULES = {
    "Winner":   {"max_fdv": 250_000, "min_lp": 15_000, "extra": {}},        # early + low LP ok
    "Tytty":    {"max_fdv": 600_000, "min_lp": 20_000, "extra": {}},        # balanced
    "Strict":   {"max_fdv": 500_000, "min_lp": 30_000, "extra": {"lock": True}},  # safest
}

# -----------------
# Health server
# -----------------
async def _health_handler(_request: web.Request) -> web.Response:
    return web.Response(text="ok")

async def start_health_server(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", _health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("memecoin-dna-bot | Health server on :%s/health", port)
    return runner

# -----------------
# Telegram helpers
# -----------------
async def send_text(app: Application, text: str, parse_mode: Optional[str] = "HTML") -> None:
    if not CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID not set; skipping send.")
        return
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=parse_mode, disable_web_page_preview=False)
    except Exception as e:
        log.exception("send_text error: %s", e)

# -----------------
# DexScreener fetch
# -----------------
DEX_BASE = "https://api.dexscreener.com"

async def fetch_latest_solana_pairs(client: httpx.AsyncClient, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Pull latest Solana pairs. DexScreener endpoint returns many; we'll cap locally.
    """
    # Endpoint commonly used: /latest/dex/pairs/{chainId}
    url = f"{DEX_BASE}/latest/dex/pairs/solana"
    r = await client.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    pairs = data.get("pairs", [])[:limit]
    return pairs

# -----------------
# Rug / safety checks (basic placeholders)
# -----------------
def basic_rug_checks(p: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Super lightweight checks that are free and fast:
    - not paused (DexScreener won't show paused trades anyway)
    - some tx activity in 5m/1h windows
    - non-zero liquidity and price
    - optional: check for LP lock flag if provided by API (rare)
    Returns (ok, reasons_failed)
    """
    reasons = []

    liq = (p.get("liquidity") or {})
    lp_usd = float(liq.get("usd") or 0)

    # txns structure: {'m5': {'buys': x, 'sells': y}, 'h1': {...}}
    tx = (p.get("txns") or {})
    m5 = tx.get("m5") or {}
    h1 = tx.get("h1") or {}
    m5_buys = int(m5.get("buys") or 0)
    m5_sells = int(m5.get("sells") or 0)
    h1_buys = int(h1.get("buys") or 0)
    h1_sells = int(h1.get("sells") or 0)

    price_usd = float((p.get("priceUsd") or 0) or 0)

    if lp_usd <= 0:
        reasons.append("no_liquidity")
    if price_usd <= 0:
        reasons.append("no_price")
    if (m5_buys + m5_sells + h1_buys + h1_sells) == 0:
        reasons.append("no_tx_activity")

    # Some sources add lock flags; if present, keep it
    is_locked = bool(p.get("liquidity") and p["liquidity"].get("isLocked"))
    return (len(reasons) == 0, reasons)

# -----------------
# DNA classification
# -----------------
def classify_dna(p: Dict[str, Any]) -> Optional[str]:
    """
    Return 'Winner' / 'Tytty' / 'Strict' if pair fits any DNA; else None.
    """
    fdv = float(p.get("fdv") or 0)
    lp_usd = float((p.get("liquidity") or {}).get("usd") or 0)

    # LP lock flag if API provides it (not always available)
    is_locked = bool((p.get("liquidity") or {}).get("isLocked"))

    # Strict requires lp locked (if field exists). If field is missing, we skip the lock requirement.
    def passes(rule: Dict[str, Any]) -> bool:
        if fdv <= rule["max_fdv"] and lp_usd >= rule["min_lp"]:
            if rule["extra"].get("lock"):
                # only enforce if API exposes the flag
                if "isLocked" in (p.get("liquidity") or {}):
                    return is_locked
            return True
        return False

    if passes(DNA_RULES["Winner"]):
        return "Winner"
    if passes(DNA_RULES["Tytty"]):
        return "Tytty"
    if passes(DNA_RULES["Strict"]):
        return "Strict"
    return None

# -----------------
# Format alert
# -----------------
def pair_link(p: Dict[str, Any]) -> str:
    # Prefer pair link if present, else build with chain + pairAddress
    url = p.get("url")
    if url:
        return url
    chain = p.get("chainId", "solana")
    pair_addr = p.get("pairAddress") or p.get("pair") or ""
    return f"https://dexscreener.com/{chain}/{pair_addr}"

def format_alert(dna: str, p: Dict[str, Any]) -> str:
    name = p.get("baseToken", {}).get("name") or p.get("baseToken", {}).get("symbol") or "Unknown"
    symbol = p.get("baseToken", {}).get("symbol") or "?"
    fdv = float(p.get("fdv") or 0)
    lp = float((p.get("liquidity") or {}).get("usd") or 0)
    mcap = fdv  # DexScreener fdv often acts like mcap for new tokens
    tx = (p.get("txns") or {})
    m5 = tx.get("m5") or {}
    h1 = tx.get("h1") or {}

    link = pair_link(p)
    notes = {
        "Winner": "High risk / high reward (super early).",
        "Tytty": "Balanced early entry.",
        "Strict": "Safer (LP ≥30k; lock if available).",
    }.get(dna, "")

    msg = (
        f"<b>{dna} DNA</b> — <b>{symbol}</b> ({name})\n"
        f"FDV: <b>${mcap:,.0f}</b> | LP: <b>${lp:,.0f}</b>\n"
        f"5m buys/sells: {m5.get('buys',0)}/{m5.get('sells',0)} | "
        f"1h buys/sells: {h1.get('buys',0)}/{h1.get('sells',0)}\n"
        f"{notes}\n"
        f"{link}"
    )
    return msg

# -----------------
# Commands
# -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Yo! Bot is live.\n"
        "I scan new Solana pairs and alert on Winner / Tytty / Strict DNA.\n"
        "Use /ping to test alerts."
    )

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong")

# -----------------
# Discovery loop
# -----------------
async def discovery_cycle(app: Application) -> None:
    """
    1) Pull latest Solana pairs from DexScreener.
    2) Run basic rug/sanity checks.
    3) Classify DNA by FDV/LP thresholds.
    4) De-dupe so we don't spam same pair repeatedly.
    5) Send Telegram alerts.
    """
    try:
        # keep a simple seen cache in memory (resets on deploy)
        seen: set = app.bot_data.setdefault("seen_pairs", set())

        async with httpx.AsyncClient(timeout=15) as client:
            pairs = await fetch_latest_solana_pairs(client, limit=80)

        new_alerts = 0
        for p in pairs:
            pair_addr = p.get("pairAddress") or p.get("pair") or ""
            if not pair_addr:
                continue

            # Skip if we already alerted this pair+DNA once
            dna = classify_dna(p)
            if not dna:
                continue

            key = f"{dna}:{pair_addr}"
            if key in seen:
                continue

            ok, reasons = basic_rug_checks(p)
            if not ok:
                # Skip very obvious bad ones, but we don't mark as seen
                continue

            # Mark as seen and alert
            seen.add(key)
            msg = format_alert(dna, p)
            await send_text(app, msg)
            new_alerts += 1

        log.info("discovery_cycle tick — %d alerts", new_alerts)

    except Exception as e:
        log.exception("discovery_cycle error: %s", e)

async def discovery_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await discovery_cycle(context.application)

# -----------------
# Lifecycle
# -----------------
async def on_post_init(app: Application) -> None:
    # Start health server
    runner = await start_health_server(HEALTH_PORT)
    app.bot_data["health_runner"] = runner

    # Schedule discovery job
    jq = app.job_queue
    if jq is None:
        log.error("JobQueue is None. Skipping schedule.")
        return

    jq.run_repeating(
        discovery_job,
        interval=DISCOVERY_INTERVAL,
        first=5,
        name="discovery_cycle",
    )
    log.info("Scheduled discovery job every %ss", DISCOVERY_INTERVAL)

# -----------------
# Build & run
# -----------------
def build_app() -> Application:
    jq = JobQueue()
    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(jq)            # force JobQueue (prevents None)
        .post_init(on_post_init)  # start health + schedule jobs
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    return app

def main() -> None:
    app = build_app()
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        stop_signals=None,
        close_loop=False,
    )

if __name__ == "__main__":
    main()

