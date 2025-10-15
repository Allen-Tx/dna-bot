# main.py
# MemeBot (Tytty + Follow-up)
# Full version with Render keep-alive web server built in

import os
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
import httpx
from aiohttp import web  # keep-alive server

# ========= Render Keep Alive =========
async def _health_handle(request):
    return web.Response(text="ok")

async def _start_health_server():
    port = int(os.getenv("PORT", 10000))
    app = web.Application()
    app.add_routes([web.get("/", _health_handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[health] dummy server running on port {port}")

asyncio.create_task(_start_health_server())
# =====================================

# ========= ENV =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "").strip()
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL", "").strip()

COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "3"))
DEX_CYCLE_SECONDS = int(os.getenv("DEX_CYCLE_SECONDS", "30"))
BIRDEYE_CYCLE_SECONDS = int(os.getenv("BIRDEYE_CYCLE_SECONDS", "15"))
HELIUS_CYCLE_SECONDS = int(os.getenv("HELIUS_CYCLE_SECONDS", "90"))

# DNA thresholds
MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", "20000"))
MAX_FDV_USD = float(os.getenv("MAX_FDV_USD", "600000"))

# follow-up bands
BUILDER_MIN_LP = float(os.getenv("BUILDER_MIN_LP", "5000"))
BUILDER_MAX_LP = float(os.getenv("BUILDER_MAX_LP", "9000"))
NEAR_MIN_LP = float(os.getenv("NEAR_MIN_LP", "15000"))
NEAR_MAX_LP = float(os.getenv("NEAR_MAX_LP", "19900"))
AGE_MAX_MINUTES = float(os.getenv("AGE_MAX_MINUTES", "4320"))  # 72h
MIN_TXNS_5M = int(os.getenv("MIN_TXNS_5M", "3"))
NEAR_WINDOW_SECONDS = float(os.getenv("NEAR_WINDOW_SECONDS", "300"))
NEAR_RECHECK_SECONDS = float(os.getenv("NEAR_RECHECK_SECONDS", "15"))

# holder safety
TOP1_HOLDER_MAX_PCT = float(os.getenv("TOP1_HOLDER_MAX_PCT", "25.0"))
TOP5_HOLDER_MAX_PCT = float(os.getenv("TOP5_HOLDER_MAX_PCT", "45.0"))
REQUIRE_HELIUS_OK = os.getenv("SAFETY_REQUIRE_HELIUS_OK", "false").lower() == "true"

# ========= GLOBALS =========
seen_pairs: Dict[str, float] = {}
candidates: Dict[str, float] = {}
near_watch: Dict[str, float] = {}
builder_watch: Dict[str, float] = {}
BACKOFF_BASE = 20

# ========= UTILITIES =========
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")


async def telegram_send(session: httpx.AsyncClient, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] missing token/chat_id, skipping")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        await session.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print(f"[telegram] error: {e}")


async def sheet_append(session: httpx.AsyncClient, tab: str, row: Dict[str, Any]):
    payload = {"tab": tab, "row": row, "sheet_url": GOOGLE_SHEET_URL}
    if not SHEET_WEBHOOK_URL:
        print(f"[sheet:{tab}] {row}")
        return
    try:
        r = await session.post(SHEET_WEBHOOK_URL, json=payload, timeout=15)
        if r.status_code >= 300:
            print(f"[sheet] non-200: {r.status_code}")
    except Exception as e:
        print(f"[sheet] error: {e}")


def _fdv_usd(pair: Dict[str, Any]) -> float:
    return float(pair.get("fdv") or pair.get("marketCap") or (pair.get("info") or {}).get("fdv") or 0)


def _liq_usd(pair: Dict[str, Any]) -> float:
    liq = (pair.get("liquidity") or {})
    v = liq.get("usd")
    if v is None:
        v = pair.get("liquidityUsd") or pair.get("lp")
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _pair_age_minutes(pair: Dict[str, Any]) -> float:
    ts = pair.get("creationTime") or pair.get("pairCreatedAt")
    try:
        ts = int(ts) if ts else 0
        if ts > 10**12:
            ts //= 1000
        if ts <= 0:
            return 999999.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return 999999.0


def _txns_m5(pair: Dict[str, Any]) -> int:
    m5 = (pair.get("txns") or {}).get("m5") or {}
    return int((m5.get("buys") or 0)) + int((m5.get("sells") or 0))


def _addr_of(pair: Dict[str, Any]) -> Optional[str]:
    return (pair.get("baseToken") or {}).get("address") or pair.get("tokenAddress") or pair.get("pairAddress")


def best_by_token(pairs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_token = {}
    for p in pairs:
        t = _addr_of(p)
        if not t:
            continue
        liq = _liq_usd(p)
        if t not in by_token or liq > by_token[t][0]:
            by_token[t] = (liq, p)
    return [v[1] for v in by_token.values()]


# ========= DATA LANES =========
async def dexscreener_latest(session: httpx.AsyncClient) -> List[Dict[str, Any]]:
    urls = [
        "https://api.dexscreener.com/latest/dex/search?q=solana",
        "https://api.dexscreener.com/latest/dex/tokens/solana",
    ]
    for attempt in range(3):
        for url in urls:
            try:
                r = await session.get(url, timeout=20)
                if r.status_code == 429:
                    wait = BACKOFF_BASE * (attempt + 1)
                    print(f"[dex] 429, wait {wait}s")
                    await asyncio.sleep(wait)
                    continue
                data = r.json()
                pairs = data.get("pairs") or data.get("tokens") or []
                pairs = [p for p in pairs if (p.get("chainId") or p.get("chain")) == "solana"]
                return pairs
            except Exception as e:
                print(f"[dex] error: {e}")
                await asyncio.sleep(2)
    return []


async def birdeye_price_liq(session: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    if not BIRDEYE_API_KEY:
        return None
    url = "https://public-api.birdeye.so/defi/price"
    headers = {"X-API-KEY": BIRDEYE_API_KEY, "accept": "application/json"}
    params = {"chain": "solana", "address": token_address, "include_liquidity": "true"}
    try:
        r = await session.get(url, headers=headers, params=params, timeout=20)
        if r.status_code >= 300:
            return None
        return r.json().get("data")
    except Exception:
        return None


async def helius_holder_safety(session: httpx.AsyncClient, token_mint: str) -> Optional[Dict[str, Any]]:
    if not HELIUS_API_KEY:
        return None
    url = f"https://api.helius.xyz/v0/tokens/holders?tokenMint={token_mint}&api-key={HELIUS_API_KEY}"
    try:
        r = await session.get(url, timeout=25)
        if r.status_code >= 300:
            return None
        data = r.json()
        holders = data.get("holders") or []
        total = sum([h.get("amount", 0) for h in holders]) or 1
        holders_sorted = sorted(holders, key=lambda x: x.get("amount", 0), reverse=True)
        top1 = (holders_sorted[0]["amount"] / total * 100) if holders_sorted else 0.0
        top5 = (sum([h.get("amount", 0) for h in holders_sorted[:5]]) / total * 100) if holders_sorted else 0.0
        risk = "ok"
        if top1 > TOP1_HOLDER_MAX_PCT or top5 > TOP5_HOLDER_MAX_PCT:
            risk = "warn"
        return {"top1_pct": round(top1, 2), "top5_pct": round(top5, 2), "risk": risk}
    except Exception:
        return None


# ========= DNA GATE =========
def dna_pass_stub(pair: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fdv = _fdv_usd(pair)
    liq = _liq_usd(pair)
    if fdv <= 0 or liq <= 0:
        return None
    if _pair_age_minutes(pair) > AGE_MAX_MINUTES:
        return None
    if _txns_m5(pair) < MIN_TXNS_5M:
        return None
    if fdv <= MAX_FDV_USD and liq >= MIN_LIQ_USD:
        return {"fdv": fdv, "liq": liq, "status": "pass"}
    if fdv <= MAX_FDV_USD and NEAR_MIN_LP <= liq <= NEAR_MAX_LP:
        return {"fdv": fdv, "liq": liq, "status": "near"}
    if BUILDER_MIN_LP <= liq <= BUILDER_MAX_LP:
        return {"fdv": fdv, "liq": liq, "status": "builder"}
    return None


# ========= CORE LOGIC =========
async def process_candidate(session: httpx.AsyncClient, pair: Dict[str, Any]):
    addr = _addr_of(pair)
    sym = (pair.get("baseToken") or {}).get("symbol") or pair.get("symbol") or "?"
    url = pair.get("url") or pair.get("pairLink") or ""
    bi = await birdeye_price_liq(session, addr) if addr else None
    hel = await helius_holder_safety(session, addr) if addr else None
    safety_ok = (hel is None) or (hel.get("risk") == "ok")
    if bi and (safety_ok or not REQUIRE_HELIUS_OK):
        await telegram_send(session, f"🧬 STRONG DING: {sym}\n{url}")
    else:
        await telegram_send(session, f"👀 Watchlist: {sym}\n{url}")


async def discovery_cycle():
    async with httpx.AsyncClient(timeout=30) as session:
        await telegram_send(session, f"✅ Bot live @ {utcnow_iso()}")
        while True:
            pairs = await dexscreener_latest(session)
            pairs = best_by_token(pairs)
            for p in pairs:
                dna = dna_pass_stub(p)
                if dna:
                    await process_candidate(session, p)
            await asyncio.sleep(DEX_CYCLE_SECONDS)


def main():
    print("Starting MemeBot (Tytty + Follow-up)…")
    asyncio.run(discovery_cycle())


if __name__ == "__main__":
    main()


          
          
                       
