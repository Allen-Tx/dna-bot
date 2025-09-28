# main.py
import asyncio
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from aiohttp import ClientSession, ClientTimeout, web
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
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
# Config from env
# -----------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var.")

DISCOVERY_INTERVAL = int(os.environ.get("DISCOVERY_INTERVAL_SECONDS", "60"))
HEALTH_PORT = int(os.environ.get("PORT", "10000"))
NETWORK = os.environ.get("NETWORK", "solana").lower().strip() or "solana"
DNA_MODE = os.environ.get("DNA_MODE", "tytty").lower().strip()  # winner|tytty|strict

# WATCHLIST can contain token mints, search words, or full dexscreener URLs
WATCHLIST = [w.strip() for w in os.environ.get("WATCHLIST", "").split(",") if w.strip()]

DEX_API = "https://api.dexscreener.com/latest/dex"

# -----------------
# DNA thresholds
# -----------------
def dna_thresholds(mode: str) -> Tuple[int, int]:
    """
    Returns (fdv_usd_max, lp_min_usd) by mode.
    winner: FDV < 250k, LP >= 15k
    tytty:  FDV < 600k, LP >= 20-30k (we'll use 20k min, you can raise to 30k)
    strict: FDV < 500k, LP >= 30k + stronger social/lock checks (best-effort here)
    """
    mode = (mode or "tytty").lower()
    if mode == "winner":
        return (250_000, 15_000)
    if mode == "strict":
        return (500_000, 30_000)
    # default tytty
    return (600_000, 20_000)

FDV_MAX, LP_MIN = dna_thresholds(DNA_MODE)

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
# DexScreener client
# -----------------
async def ds_search(session: ClientSession, query: str) -> List[dict]:
    """
    Accepts: token mint, pair URL, or search text.
    Returns: list of pair dicts (might be empty).
    """
    # Accept direct pair URL
    m = re.search(r"dexscreener\.com/([a-zA-Z0-9_-]+)/([A-Za-z0-9]+)", query)
    if m:
        chain, pair = m.group(1), m.group(2)
        url = f"{DEX_API}/pairs/{chain}/{pair}"
        async with session.get(url, timeout=ClientTimeout(total=10)) as r:
            data = await r.json()
            return data.get("pairs", []) or []

    # Accept token address lookup
    token_like = re.fullmatch(r"[A-Za-z0-9]{25,64}", query)
    if token_like:
        url = f"{DEX_API}/tokens/{query}"
        async with session.get(url, timeout=ClientTimeout(total=10)) as r:
            data = await r.json()
            return data.get("pairs", []) or []

    # Generic search
    url = f"{DEX_API}/search?q={query}"
    async with session.get(url, timeout=ClientTimeout(total=10)) as r:
        data = await r.json()
        return data.get("pairs", []) or []

def extract_pair_fields(p: dict) -> dict:
    # Defensive parsing; DexScreener fields can be missing
    liq_usd = 0
    if p.get("liquidity", {}).get("usd") is not None:
        liq_usd = float(p["liquidity"]["usd"])
    fdv = p.get("fdv")
    if fdv is None and p.get("fullyDilutedValuation") is not None:
        fdv = p["fullyDilutedValuation"]
    if fdv is None:
        fdv = 0
    base = p.get("baseToken", {})
    quote = p.get("quoteToken", {})
    tx = p.get("txns", {})
    buys = tx.get("m5", {}).get("buys", 0)
    sells = tx.get("m5", {}).get("sells", 0)
    price = p.get("priceUsd") or p.get("price") or "?"
    url = p.get("url") or p.get("pairUrl")
    chain = p.get("chainId") or p.get("chain")
    pair_addr = p.get("pairAddress") or p.get("pairCreatedAt")
    mcap = p.get("marketCap") or 0  # sometimes provided
    change_m5 = p.get("priceChange", {}).get("m5", 0)
    return {
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "?",
        "base": base.get("address") or "?",
        "quote": quote.get("symbol") or "?",
        "chain": chain or "?",
        "pair": pair_addr or "?",
        "price": price,
        "lp_usd": float(liq_usd or 0),
        "fdv": float(fdv or 0),
        "mcap": float(mcap or 0),
        "buys5": int(buys or 0),
        "sells5": int(sells or 0),
        "chg5": float(change_m5 or 0),
        "url": url or "",
    }

def passes_basic_safety(x: dict) -> Tuple[bool, List[str]]:
    """
    Lightweight checks that help avoid obvious rugs:
    - LP >= LP_MIN
    - FDV within cap
    - Not zero buys & only sells in last 5m
    - Not absurdly thin LP (< 10k) even if DNA says 15k (guard rail)
    """
    reasons = []
    ok = True
    if x["lp_usd"] < LP_MIN:
        ok = False
        reasons.append(f"LP too low (${x['lp_usd']:.0f} < ${LP_MIN:,})")
    if x["fdv"] <= 0 or x["fdv"] > FDV_MAX:
        ok = False
        reasons.append(f"FDV out of range (${x['fdv']:.0f} > ${FDV_MAX:,})")
    if x["buys5"] == 0 and x["sells5"] > 0:
        ok = False
        reasons.append("Only sells in last 5m")
    if x["lp_usd"] < 10_000:
        ok = False
        reasons.append("LP under $10k (extra guard rail)")
    return ok, reasons

def dna_name() -> str:
    return {"winner": "Winner", "strict": "Strict"}.get(DNA_MODE, "Tytty")

def fmt_pair(x: dict) -> str:
    return (
        f"{x['symbol']} ({x['chain']})\n"
        f"Price: ${x['price']} | LP: ${x['lp_usd']:.0f} | FDV: ${x['fdv']:.0f}\n"
        f"5m Buys/Sells: {x['buys5']}/{x['sells5']} | Δ5m: {x['chg5']}%\n"
        f"{x['url']}"
    )

# -----------------
# Telegram Handlers
# -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    subs = context.application.bot_data.setdefault("subs", set())
    subs.add(chat_id)
    await update.message.reply_text(
        f"Yo! Subscribed.\n"
        f"DNA mode: {dna_name()} (FDV<{FDV_MAX:,}, LP>={LP_MIN:,}).\n"
        f"Use /check <addr|url|word> to scan manually. Use /stop to unsubscribe."
    )

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    subs = context.application.bot_data.setdefault("subs", set())
    subs.discard(chat_id)
    await update.message.reply_text("Unsubscribed. /start to subscribe again.")

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong")

async def dna_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"{dna_name()} DNA:\n"
        f"- FDV < ${FDV_MAX:,}\n"
        f"- LP >= ${LP_MIN:,}\n"
        f"- Basic safety: no 0-buys/only-sells in last 5m, LP not ultra-thin\n"
        f"Change by env with DNA_MODE=winner|tytty|strict"
    )

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /check <addr|dexscreener url|search term>")
        return
    query = " ".join(context.args).strip()
    session: ClientSession = context.application.bot_data["http"]
    try:
        pairs = await ds_search(session, query)
        if not pairs:
            await update.message.reply_text("No pairs found.")
            return
        # Prefer current network first
        ranked = sorted(
            (extract_pair_fields(p) for p in pairs),
            key=lambda z: (z["chain"] != NETWORK, -z["lp_usd"]),
        )
        lines = []
        shown = 0
        for x in ranked:
            ok, reasons = passes_basic_safety(x)
            tag = "✅ PASS" if ok else f"❌ FAIL ({'; '.join(reasons)})"
            lines.append(f"{tag}\n{fmt_pair(x)}")
            shown += 1
            if shown >= 5:
                break
        await update.message.reply_text("\n\n".join(lines))
    except Exception as e:
        log.exception("/check error: %s", e)
        await update.message.reply_text("Error while checking. Try another query.")

# Optional text fallback: quick /check on pasted URL or address
async def plain_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    # Auto-check if message looks like a URL or token
    if "dexscreener.com" in text or re.fullmatch(r"[A-Za-z0-9]{25,64}", text):
        update.message.text = f"/check {text}"
        await check_cmd(update, context)

# -----------------
# Discovery job
# -----------------
async def discovery_cycle(app: Application) -> None:
    try:
        session: ClientSession = app.bot_data["http"]
        subs = app.bot_data.setdefault("subs", set())
        if not subs:
            return  # nobody to notify yet

        queries = WATCHLIST or []
        if not queries:
            return

        # simple de-dup throttle: don't repeat same pair within 30 min
        seen: Dict[str, float] = app.bot_data.setdefault("seen_pairs", {})

        results: List[str] = []
        for q in queries:
            pairs = await ds_search(session, q)
            for p in pairs:
                x = extract_pair_fields(p)
                # Focus current chain
                if (x["chain"] or "").lower() != NETWORK:
                    continue
                ok, reasons = passes_basic_safety(x)
                if not ok:
                    continue
                key = x["url"] or f"{x['chain']}/{x['pair']}"
                # throttle
                if key in seen:
                    continue
                # Mark as seen for 30 minutes
                seen[key] = asyncio.get_event_loop().time() + 1800
                results.append(
                    f"🔥 {dna_name()} match\n{fmt_pair(x)}"
                )

        # purge expired seen
        now = asyncio.get_event_loop().time()
        for k, exp in list(seen.items()):
            if exp <= now:
                seen.pop(k, None)

        if not results:
            return

        text = "\n\n".join(results[:8])  # avoid spamming
        for chat_id in list(subs):
            try:
                await app.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                log.warning("notify failed to %s: %s", chat_id, e)

    except Exception as e:
        log.exception("discovery_cycle error: %s", e)

async def discovery_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await discovery_cycle(context.application)

# -----------------
# Lifecycle
# -----------------
async def on_post_init(app: Application) -> None:
    # HTTP client
    session = ClientSession(timeout=ClientTimeout(total=12))
    app.bot_data["http"] = session

    # Health server
    runner = await start_health_server(HEALTH_PORT)
    app.bot_data["health_runner"] = runner

    # Schedule discovery
    jq = app.job_queue
    if jq is None:
        log.error("JobQueue is None. Skipping schedule.")
    else:
        jq.run_repeating(
            discovery_job,
            interval=DISCOVERY_INTERVAL,
            first=5,
            name="discovery_cycle",
        )
        log.info(
            "Scheduled discovery job every %ss | DNA=%s FDV<%s LP>=%s | NETWORK=%s",
            DISCOVERY_INTERVAL, dna_name(), FDV_MAX, LP_MIN, NETWORK
        )

async def on_shutdown(app: Application) -> None:
    # Close HTTP + health runner
    session: ClientSession = app.bot_data.get("http")
    if session:
        await session.close()
    runner: web.AppRunner = app.bot_data.get("health_runner")
    if runner:
        await runner.cleanup()

def build_app() -> Application:
    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_post_init)
        .post_shutdown(on_shutdown)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("dna", dna_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text_handler))

    return app

def main() -> None:
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None, close_loop=False)

if __name__ == "__main__":
    main()

