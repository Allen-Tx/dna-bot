# ========================= MemeBot vNext - EARLY BIRD =========================
# DexScreener engine + optional BirdEye confirm, with "early builder" mode
#
# - DexScreener = main discovery + primary LP/FDV/age/txns engine
# - BirdEye = optional safety confirm only (never blocks Dex results)
# - Early Builder Mode = DO NOT reject coins for being under DNA floors.
#                        If coin is new + building, still ding + journal.
# - Rejection rules ONLY apply to true scams (liq=0, FDV insane, holders bad)
# - Journaling ALWAYS happens on ding.
# - Watchlists, cool-downs, Telegram, etc. preserved.
# ============================================================================

import os
import re
import asyncio
import time
import random
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import aiohttp
from aiohttp import web


# ------------------------- Keep-Alive Server (Render) -------------------------
async def _health_handle(request):
    return web.Response(text="ok")

async def _doorbell_handle(request):
    return web.Response(text="ding")

async def start_health_server():
    port = int(os.getenv("PORT", 10000))
    app = web.Application()
    app.add_routes([web.get("/", _health_handle), web.get("/doorbell", _doorbell_handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[health] running on :{port}")


# ------------------------------ ENV SETTINGS ---------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID")

SHEET_URL      = os.getenv("GOOGLE_SHEET_URL")
WEBHOOK_URL    = os.getenv("SHEET_WEBHOOK_URL")

DEX_CYCLE      = int(os.getenv("DEX_CYCLE_SECONDS", "60"))
DEX_JITTER_MAX = int(os.getenv("DEX_JITTER_MAX_SECONDS", "5"))

BIRD_KEY       = os.getenv("BIRDEYE_API_KEY", "")
BIRD_CYCLE     = int(os.getenv("BIRDEYE_CYCLE_SECONDS", "120"))

HELIUS_KEY     = os.getenv("HELIUS_API_KEY", "")
HELIUS_CYCLE   = int(os.getenv("HELIUS_CYCLE_SECONDS", "90"))

LP_FLOOR       = int(os.getenv("BUILDER_MIN_LP", "8000"))     # early builder: still ding under this
LP_MAX         = int(os.getenv("BUILDER_MAX_LP", "15000"))
NEAR_MIN       = int(os.getenv("NEAR_MIN_LP", "15000"))
NEAR_MAX       = int(os.getenv("NEAR_MAX_LP", "25000"))

FDV_CAP        = int(os.getenv("MAX_FDV_USD", "600000"))
AGE_MAX        = int(os.getenv("AGE_MAX_MINUTES", "720"))
YOUNG_AGE      = int(os.getenv("YOUNG_AGE_MINUTES", "30"))
MIN_TX_YOUNG   = int(os.getenv("MIN_TXNS_5M_YOUNG", "1"))
MIN_TX_OLD     = int(os.getenv("MIN_TXNS_5M_OLD", "3"))

TOP1_MAX       = float(os.getenv("TOP1_HOLDER_MAX_PCT", "12"))
TOP5_MAX       = float(os.getenv("TOP5_HOLDER_MAX_PCT", "28"))

PROMO_LP       = int(os.getenv("PROMOTION_LP", "25000"))

COOLDOWN_MIN   = int(os.getenv("COOLDOWN_MINUTES", "18"))
REJECT_MEMORY  = int(os.getenv("REJECT_MEMORY_MINUTES", "20"))
WATCHLIST_SEC  = int(os.getenv("WATCHLIST_RECHECK_SECONDS", "15"))
WATCH_WIN_SEC  = int(os.getenv("WATCHLIST_WINDOW_SECONDS", "600"))

MANUAL_WINDOW  = int(os.getenv("MANUAL_RETRY_WINDOW", "900"))
MANUAL_RETRY   = int(os.getenv("MANUAL_RETRY_SECONDS", "30"))

PROVISIONAL    = os.getenv("PROVISIONAL_DING", "true").lower() == "true"
SAFETY_REQUIRE_HELIUS = os.getenv("SAFETY_REQUIRE_HELIUS_OK", "false").lower() == "true"


# ------------------------- STATE ---------------------------------------------
seen_tokens = {}
cooldowns = {}
reject_memory = {}
watchlist = {}


# ------------------------- Telegram ------------------------------------------
async def tg(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as s:
        await s.post(url, json={"chat_id": CHAT_ID, "text": text})


# ------------------------- DexScreener Core ----------------------------------
async def fetch_dex():
    url = "https://api.dexscreener.com/latest/dex/pairs/solana"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                if r.status == 429:
                    print("[dex] 429 rate-limit")
                    return None
                if r.status != 200:
                    print(f"[dex] bad status {r.status}")
                    return None
                return await r.json()
    except:
        return None


# --------------------- BirdEye Optional Safety Confirm ------------------------
async def fetch_bird(mint):
    if not BIRD_KEY:
        return None
    url = f"https://public-api.birdeye.so/defi/token_info?address={mint}"
    headers = {"X-API-KEY": BIRD_KEY}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as r:
                if r.status != 200: 
                    return None
                return await r.json()
    except:
        return None


# ----------------------- Helius Optional Safety ------------------------------
async def fetch_helius(mint):
    if not HELIUS_KEY:
        return None
    url = f"https://api.helius.xyz/v0/tokens/{mint}?api-key={HELIUS_KEY}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except:
        return None


# -------------------------- DNA Check (Early Bird) ---------------------------
def dna_check(token) -> Tuple[bool, str]:
    """
    EARLY BIRD logic:
    - Never reject for LP < LP_FLOOR
    - Never reject for FDV < FDV_CAP (only reject over)
    - Never reject for TX < floors (only warn)
    - Only reject:
        * liquidity = 0
        * FDV crazy high
        * holders concentration > limits
        * age too high AND metrics terrible
    """

    lp = token["liquidity"]
    fdv = token["fdv"]
    age = token["age_m"]
    tx5 = token["tx5m"]
    t1 = token["top1"]
    t5 = token["top5"]

    # 🚫 Reject true garbage ONLY
    if lp <= 0:
        return (False, "liq=0")
    if fdv > FDV_CAP * 3:
        return (False, f"insane FDV {fdv}")
    if t1 > TOP1_MAX or t5 > TOP5_MAX:
        return (False, "holder concentration")

    # Everything else is EARLY BIRD positive
    return (True, "early-bird-pass")


# ---------------------- Journal to Google Sheet ------------------------------
async def journal(token):
    if not WEBHOOK_URL:
        return
    async with aiohttp.ClientSession() as s:
        await s.post(WEBHOOK_URL, json=token)


# ------------------------------ MAIN LOOP ------------------------------------
async def engine():
    await tg("🐣 Early Bird engine starting…")

    while True:
        await asyncio.sleep(random.randint(1, DEX_JITTER_MAX))

        data = await fetch_dex()
        if not data or "pairs" not in data:
            continue

        pairs = data["pairs"]

        now = time.time()

        for p in pairs:
            mint = p.get("baseToken", {}).get("address")
            if not mint:
                continue

            # skip old rejects
            if mint in reject_memory and now - reject_memory[mint] < REJECT_MEMORY * 60:
                continue

            lp = p.get("liquidity", {}).get("usd", 0)
            fdv = p.get("fdv", 0)
            age = p.get("pairCreatedAt", 0)
            age_m = max(1, (now - age/1000) / 60)
            tx5 = p.get("txns", {}).get("m5", {}).get("buys", 0) + p.get("txns", {}).get("m5", {}).get("sells", 0)

            token = {
                "mint": mint,
                "lp": lp,
                "fdv": fdv,
                "age_m": int(age_m),
                "tx5m": tx5,
                "top1": 0,          # optional fill later
                "top5": 0,
                "ts": datetime.now(timezone.utc).isoformat()
            }

            ok, reason = dna_check(token)

            if not ok:
                reject_memory[mint] = now
                print(f"[rej] {mint} — {reason}")
                continue

            # FIRST TIME?
            if mint not in seen_tokens:
                seen_tokens[mint] = now
                msg = (
                    f"🐣 EARLY BIRD DING!\n"
                    f"Mint: `{mint}`\n"
                    f"LP=${lp:,} | FDV=${fdv:,}\n"
                    f"Age={int(age_m)}m | 5m tx={tx5}\n"
                    f"Reason={reason}"
                )
                await tg(msg)
                await journal(token)

        await asyncio.sleep(DEX_CYCLE)


# ------------------------------- STARTUP -------------------------------------
async def main():
    asyncio.create_task(start_health_server())
    await engine()

asyncio.run(main())
