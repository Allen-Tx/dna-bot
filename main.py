# main.py
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

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

CHAT_ID_ENV = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_CHAT_ID = int(CHAT_ID_ENV) if CHAT_ID_ENV.isdigit() or (CHAT_ID_ENV and CHAT_ID_ENV[0] == "-" and CHAT_ID_ENV[1:].isdigit()) else None

DISCOVERY_INTERVAL = int(os.environ.get("DISCOVERY_INTERVAL_SECONDS", "60"))
HEALTH_PORT = int(os.environ.get("PORT", "10000"))

DEX_BASE = "https://api.dexscreener.com/latest/dex"

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
# Helpers
# -----------------
def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default

def _pair_age_minutes(p: Dict[str, Any]) -> float:
    # DexScreener sometimes returns pairCreatedAt or creationTime in ms
    ts = p.get("creationTime") or p.get("pairCreatedAt")
    if ts is None:
        return 999999.0
    try:
        ts = int(ts)
        if ts > 10**12:  # ms
            ts //= 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return 999999.0

def _activity_score(p: Dict[str, Any]) -> int:
    m5 = (p.get("txns") or {}).get("m5") or {}
    return int(_num(m5.get("buys"), 0) + _num(m5.get("sells"), 0))

def _format_pair_line(p: Dict[str, Any]) -> str:
    symbol = (p.get("baseToken") or {}).get("symbol") or "?"
    fdv = int(_num(p.get("fdv"), 0))
    liq = int(_num(((p.get("liquidity") or {}).get("usd")), 0))
    url = p.get("url") or ""
    return f"• {symbol} | FDV ${fdv:,} | LP ${liq:,}\n{url}"

# -----------------
# DexScreener fetch
# -----------------
async def fetch_latest_solana_pairs(limit: int = 120) -> List[Dict[str, Any]]:
    url = f"{DEX_BASE}/search"
    params = {"q": "solana"}
    timeout = httpx.Timeout(15.0, read=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json() or {}
        pairs = data.get("pairs", []) or []

    # Only Solana
    pairs = [p for p in pairs if p.get("chainId") == "solana"]

    # Sort: newest or most active first
    pairs.sort(key=lambda p: (int(1e9) - int(_num(p.get("pairCreatedAt") or p.get("creationTime"), 0)), _activity_score(p)))
    return pairs[:limit]

# -----------------
# DNA filters (from your PDF)
# -----------------
def pass_basic_safety(p: Dict[str, Any]) -> bool:
    # Quick protections: non-zero LP, some activity, not ancient
    liq = _num((p.get("liquidity") or {}).get("usd"), 0)
    if liq <= 0:
        return False
    if _activity_score(p) == 0:
        return False
    # treat "newish" as age < 72h for alerts; you can tighten later
    if _pair_age_minutes(p) > 72 * 60:
        return False
    return True

def dna_winner(p: Dict[str, Any]) -> bool:
    if not pass_basic_safety(p):
        return False
    fdv = _num(p.get("fdv"))
    liq = _num((p.get("liquidity") or {}).get("usd"))
    return (fdv > 0 and fdv < 250_000) and (liq >= 15_000)

def dna_tytty(p: Dict[str, Any]) -> bool:
    if not pass_basic_safety(p):
        return False
    fdv = _num(p.get("fdv"))
    liq = _num((p.get("liquidity") or {}).get("usd"))
    return (fdv > 0 and fdv < 600_000) and (liq >= 20_000)

def dna_strict(p: Dict[str, Any]) -> bool:
    if not pass_basic_safety(p):
        return False
    fdv = _num(p.get("fdv"))
    liq = _num((p.get("liquidity") or {}).get("usd"))
    # We can't guarantee "LP locked or renounced" from this endpoint,
    # but we can require stronger LP + activity as a proxy.
    socials = (p.get("info") or {}).get("socials") or []
    return (fdv > 0 and fdv < 500_000) and (liq >= 30_000) and (len(socials) > 0)

# -----------------
# Commands
# -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Yo! Bot is live. I’ll ping when I spot DNA matches. Use /dna to see criteria.")

async def dna_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "DNA screens:\n"
        "• Winner: FDV < $250k, LP ≥ $15k (early, riskier)\n"
        "• Tytty:  FDV < $600k, LP ≥ $20–30k (sweet spot)\n"
        "• Strict: FDV < $500k, LP ≥ $30k + socials\n"
        "All also require: some txns, non-zero LP, not ancient pairs."
    )
    await update.message.reply_text(text)

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong")

# -----------------
# Discovery job
# -----------------
def _dedupe_key(p: Dict[str, Any]) -> str:
    return p.get("pairAddress") or p.get("pairId") or (p.get("baseToken") or {}).get("address") or (p.get("info") or {}).get("address") or p.get("url") or str(p)

async def discovery_cycle(app: Application) -> None:
    try:
        pairs = await fetch_latest_solana_pairs(limit=150)

        winners  = [p for p in pairs if dna_winner(p)]
        tytties  = [p for p in pairs if dna_tytty(p)]
        stricts  = [p for p in pairs if dna_strict(p)]

        log.info("tick | pairs=%s | winner=%s tytty=%s strict=%s",
                 len(pairs), len(winners), len(tytties), len(stricts))

        # Dedupe: don't re-alert same pair twice
        sent: set = app.bot_data.setdefault("sent_pairs", set())
        buckets = [("🚀 Winner", winners), ("🔥 Tytty", tytties), ("🛡️ Strict", stricts)]

        if TELEGRAM_CHAT_ID:
            for label, items in buckets:
                # keep only new items not previously sent
                fresh = []
                for p in items:
                    key = _dedupe_key(p)
                    if key and key not in sent:
                        sent.add(key)
                        fresh.append(p)

                if fresh:
                    lines = [_format_pair_line(p) for p in fresh[:6]]  # cap per tick
                    msg = f"{label} hits ({len(fresh)})\n" + "\n\n".join(lines)
                    try:
                        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, disable_web_page_preview=True)
                    except Exception:
                        log.exception("send_message failed")
    except httpx.HTTPStatusError as e:
        log.error("discovery_cycle HTTP error: %s", e)
    except Exception as e:
        log.exception("discovery_cycle error: %s", e)

async def discovery_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await discovery_cycle(context.application)

# -----------------
# Lifecycle
# -----------------
async def on_post_init(app: Application) -> None:
    runner = await start_health_server(HEALTH_PORT)
    app.bot_data["health_runner"] = runner

    jq = app.job_queue
    if jq is None:
        log.error("JobQueue is None (should not happen if PTB installed with [job-queue])")
        return

    jq.run_repeating(discovery_job, interval=DISCOVERY_INTERVAL, first=5, name="discovery_cycle")
    log.info("Scheduled discovery every %ss", DISCOVERY_INTERVAL)

# -----------------
# Build & run
# -----------------
def build_app() -> Application:
    jq = JobQueue()
    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(jq)            # ensure JobQueue exists
        .post_init(on_post_init)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("dna", dna_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    return app

def main() -> None:
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None, close_loop=False)

if __name__ == "__main__":
    main()


