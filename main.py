# main.py
# MemeBot (Tytty + Follow-up) — FULL VERSION
# ✔ Builders + Near-Miss + Promotion follow-ups
# ✔ BirdEye + Helius safety
# ✔ Google Sheets journaling: NearMisses, Candidates, Dings
# ✔ Best-pool per token (avoid dust pools)
# ✔ Age ≤ 72h + activity ≥ 3 txns/5m sanity
# ✔ Render keep-alive web server (PORT) so service stays up 24/7

import os
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import httpx
from aiohttp import web  # keep-alive web server

# ========= Render Keep Alive (starts once) =========
async def _health_handle(request):
    return web.Response(text="ok")

async def start_health_server():
    port = int(os.getenv("PORT", 10000))
    app = web.Application()
    app.add_routes([web.get("/", _health_handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[health] dummy server running on port {port}")
# ===================================================

# ========= ENV =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()  # required for liq/price lane
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()    # optional safety lane

GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "").strip()  # purely informational for logs
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL", "").strip()  # Apps Script Web App URL (optional)

COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "3"))

# Polling
DEX_CYCLE_SECONDS = int(os.getenv("DEX_CYCLE_SECONDS", "30"))
BIRDEYE_CYCLE_SECONDS = int(os.getenv("BIRDEYE_CYCLE_SECONDS", "15"))
HELIUS_CYCLE_SECONDS = int(os.getenv("HELIUS_CYCLE_SECONDS", "90"))

# DNA thresholds (Tytty)
MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", "20000"))
MAX_FDV_USD = float(os.getenv("MAX_FDV_USD", "600000"))

# Follow-up bands
BUILDER_MIN_LP = float(os.getenv("BUILDER_MIN_LP", "5000"))
BUILDER_MAX_LP = float(os.getenv("BUILDER_MAX_LP", "9000"))
NEAR_MIN_LP = float(os.getenv("NEAR_MIN_LP", "15000"))
NEAR_MAX_LP = float(os.getenv("NEAR_MAX_LP", "19900"))

AGE_MAX_MINUTES = float(os.getenv("AGE_MAX_MINUTES", "4320"))  # ≤72h
MIN_TXNS_5M = int(os.getenv("MIN_TXNS_5M", "3"))

NEAR_WINDOW_SECONDS = float(os.getenv("NEAR_WINDOW_SECONDS", "300"))   # 5m watch window
NEAR_RECHECK_SECONDS = float(os.getenv("NEAR_RECHECK_SECONDS", "15"))  # fast follow-up

# Holder safety (Top1 bumped to 25% as requested)
TOP1_HOLDER_MAX_PCT = float(os.getenv("TOP1_HOLDER_MAX_PCT", "25.0"))
TOP5_HOLDER_MAX_PCT = float(os.getenv("TOP5_HOLDER_MAX_PCT", "45.0"))
REQUIRE_HELIUS_OK = os.getenv("SAFETY_REQUIRE_HELIUS_OK", "false").lower() == "true"

# ========= GLOBALS =========
seen_pairs: Dict[str, float] = {}       # pair_address -> last_seen_ts
candidates: Dict[str, float] = {}       # pair_address -> ts_detected
near_watch: Dict[str, float] = {}       # token_addr -> expire_ts (LP 15–19.9k)
builder_watch: Dict[str, float] = {}    # token_addr -> expire_ts (LP 5–9k, new+active)
BACKOFF_BASE = 20  # seconds


# ========= UTILS =========
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
            print(f"[sheet] webhook non-200: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[sheet] webhook error: {e}")


def _fdv_usd(pair: Dict[str, Any]) -> float:
    return float(
        pair.get("fdv")
        or pair.get("marketCap")
        or (pair.get("info") or {}).get("fdv")
        or 0
    )

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
        ts = int(ts) if ts is not None else 0
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
    buys = int((m5.get("buys") or 0))
    sells = int((m5.get("sells") or 0))
    return buys + sells

def _addr_of(pair: Dict[str, Any]) -> Optional[str]:
    return (pair.get("baseToken") or {}).get("address") or pair.get("tokenAddress") or pair.get("pairAddress")

def best_by_token(pairs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the highest-LP pool per token to avoid dust pools."""
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
                    print(f"[dex] 429 rate-limit, backoff {wait}s")
                    await asyncio.sleep(wait)
                    continue
                if r.status_code >= 300:
                    print(f"[dex] {r.status_code} {r.text[:200]}")
                    continue
                data = r.json() or {}
                pairs = data.get("pairs") or data.get("tokens") or []
                pairs = [p for p in pairs if (p.get("chainId") or p.get("chain")) == "solana"]
                return pairs
            except Exception as e:
                print(f"[dex] error: {e}; retrying…")
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
        if r.status_code == 429:
            print("[birdeye] 429; backing off 20s")
            await asyncio.sleep(20)
            return None
        if r.status_code >= 300:
            print(f"[birdeye] {r.status_code} {r.text[:200]}")
            return None
        return r.json().get("data")
    except Exception as e:
        print(f"[birdeye] error: {e}")
        return None


async def helius_holder_safety(session: httpx.AsyncClient, token_mint: str) -> Optional[Dict[str, Any]]:
    if not HELIUS_API_KEY:
        return None
    url = f"https://api.helius.xyz/v0/tokens/holders?tokenMint={token_mint}&api-key={HELIUS_API_KEY}"
    try:
        r = await session.get(url, timeout=25)
        if r.status_code == 429:
            print("[helius] 429; backing off 20s")
            await asyncio.sleep(20)
            return None
        if r.status_code >= 300:
            print(f"[helius] {r.status_code} {r.text[:200]}")
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
    except Exception as e:
        print(f"[helius] error: {e}")
        return None


# ========= DNA GATE =========
def dna_pass_stub(pair: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Tytty DNA gate with builder/near bands + age/activity sanity.
    Returns one of: {"status": "pass" | "near" | "builder"} or None.
    """
    fdv = _fdv_usd(pair)
    liq = _liq_usd(pair)
    if fdv <= 0 or liq <= 0:
        return None

    # sanity: recent + alive
    if _pair_age_minutes(pair) > AGE_MAX_MINUTES:
        return None
    if _txns_m5(pair) < MIN_TXNS_5M:
        return None

    # 1) confirmed Tytty
    if fdv <= MAX_FDV_USD and liq >= MIN_LIQ_USD:
        return {"fdv": fdv, "liq": liq, "dna_type": "TTYL", "status": "pass"}

    # 2) near-miss band
    if fdv <= MAX_FDV_USD and NEAR_MIN_LP <= liq <= NEAR_MAX_LP:
        return {"fdv": fdv, "liq": liq, "dna_type": "TTYL", "status": "near"}

    # 3) early builder band (already checked age+txns)
    if BUILDER_MIN_LP <= liq <= BUILDER_MAX_LP:
        return {"fdv": fdv, "liq": liq, "dna_type": "TTYL", "status": "builder"}

    return None


# ========= CORE LOGIC =========
async def process_candidate(session: httpx.AsyncClient, pair: Dict[str, Any]):
    """After DNA pass, validate on BirdEye + (optional) Helius, then ding/journal."""
    address = _addr_of(pair)
    symbol = (pair.get("baseToken") or {}).get("symbol") or pair.get("symbol") or "?"
    chain = pair.get("chainId") or pair.get("chain") or "solana"
    dex_url = pair.get("url") or pair.get("pairLink") or ""

    ts = utcnow_iso()
    row = {
        "ts_detected": ts,
        "pair_address": address,
        "symbol": symbol,
        "chain": chain,
        "dex_url": dex_url,
    }

    # Birdeye lane
    bi = await birdeye_price_liq(session, address) if address else None
    if bi:
        row.update({
            "birdeye_price": bi.get("valueUsd"),
            "birdeye_liq": bi.get("liquidity", {}).get("usd") if isinstance(bi.get("liquidity"), dict) else bi.get("liquidity"),
            "birdeye_success": True
        })
    else:
        row["birdeye_success"] = False

    # Helius lane
    hel = await helius_holder_safety(session, address) if address else None
    if hel:
        row.update({
            "top1_holder_pct": hel.get("top1_pct"),
            "top5_holder_pct": hel.get("top5_pct"),
            "helius_risk": hel.get("risk"),
            "helius_success": True
        })
    else:
        row["helius_success"] = False

    # Decide ding level
    safety_ok = (hel is None) or (hel and hel.get("risk") == "ok")
    if REQUIRE_HELIUS_OK and not safety_ok:
        await telegram_send(session, f"👀 Watchlist (holder risk): {symbol}\nTop1:{row.get('top1_holder_pct','?')}% Top5:{row.get('top5_holder_pct','?')}%\n{dex_url}")
        await sheet_append(session, "Candidates", {**row, "status": "holder_warn"})
        return

    if bi:
        await telegram_send(session, f"🧬 STRONG DING: {symbol}\nLiq≈${row.get('birdeye_liq')}\nTop1:{row.get('top1_holder_pct','?')}% Top5:{row.get('top5_holder_pct','?')}%\n{dex_url}")
        await sheet_append(session, "Dings", {
            "ts_ding": ts,
            "pair_address": address,
            "symbol": symbol,
            "ding_tier": "Strong",
            "mc_at_ding": pair.get("fdv"),
            "liq_at_ding": row.get("birdeye_liq"),
            "dna_flavor": "TTYL",
            "safety_snapshot": "OK" if safety_ok else "WARN",
            "dex_url": dex_url
        })
    else:
        await telegram_send(session, f"👀 Watchlist: {symbol}\n(birdeye:{'OK' if bi else 'MISS'} / helius:{'OK' if safety_ok else 'WARN'})\n{dex_url}")

    # Always record candidate
    await sheet_append(session, "Candidates", {
        **row,
        "status": "candidate"
    })


async def discovery_cycle():
    async with httpx.AsyncClient(timeout=30) as session:
        # Start the keep-alive server once
        await start_health_server()

        # Startup ping
        await telegram_send(session, f"✅ Bot live @ {utcnow_iso()}\nSheet: {GOOGLE_SHEET_URL or 'not set'}")
        backoff = 0

        while True:
            started = time.time()
            try:
                pairs = await dexscreener_latest(session)
                pairs = best_by_token(pairs)  # highest-LP pool per token
                print(f"[dex] fetched {len(pairs)} items")
                now_ts = time.time()

                for p in pairs:
                    addr = _addr_of(p)
                    if not addr:
                        continue

                    # Cooldown / dedupe
                    last = seen_pairs.get(addr, 0)
                    if now_ts - last < COOLDOWN_MINUTES * 60:
                        continue

                    dna = dna_pass_stub(p)
                    if not dna:
                        builder_watch.pop(addr, None)
                        near_watch.pop(addr, None)
                        continue

                    sym = (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?"
                    url = p.get("url") or p.get("pairLink") or ""

                    if dna["status"] == "pass":
                        seen_pairs[addr] = now_ts
                        candidates[addr] = now_ts
                        # journal detection
                        await sheet_append(session, "Candidates", {
                            "ts_detected": utcnow_iso(),
                            "pair_address": addr,
                            "symbol": sym,
                            "chain": p.get("chainId") or p.get("chain") or "solana",
                            "dex_url": url,
                            "mc_at_detect": dna["fdv"],
                            "liq_at_detect": dna["liq"],
                            "dna_pass_type": dna["dna_type"],
                            "why_candidate": "FDV≤cap & liq≥floor",
                            "status": "candidate"
                        })
                        await process_candidate(session, p)
                        await asyncio.sleep(2)
                        builder_watch.pop(addr, None)
                        near_watch.pop(addr, None)
                        continue

                    elif dna["status"] == "near":
                        exp = now_ts + NEAR_WINDOW_SECONDS
                        if addr not in near_watch:
                            near_watch[addr] = exp
                            await telegram_send(session, f"🟨 Near-miss: {sym} LP≈${int(dna['liq']):,} (needs ≥ ${int(MIN_LIQ_USD):,})\nWatching ~{int(exp-now_ts)}s\n{url}")
                            await sheet_append(session, "NearMisses", {
                                "ts": utcnow_iso(), "pair_address": addr, "symbol": sym,
                                "fdv": dna["fdv"], "lp": dna["liq"], "status": "near", "url": url
                            })
                        else:
                            near_watch[addr] = exp
                        continue

                    elif dna["status"] == "builder":
                        exp = now_ts + NEAR_WINDOW_SECONDS
                        if addr not in builder_watch:
                            builder_watch[addr] = exp
                            await telegram_send(session, f"🟧 Early Builder: {sym} LP≈${int(dna['liq']):,} | Active + New\nWatching ~{int(exp-now_ts)}s\n{url}")
                            await sheet_append(session, "NearMisses", {
                                "ts": utcnow_iso(), "pair_address": addr, "symbol": sym,
                                "fdv": dna["fdv"], "lp": dna["liq"], "status": "builder", "url": url
                            })
                        else:
                            builder_watch[addr] = exp
                        continue

                # -------- follow-up rechecks (builders + near) --------
                if builder_watch or near_watch:
                    still_builder, still_near = {}, {}
                    # builders
                    for addr, exp in list(builder_watch.items()):
                        if time.time() > exp:
                            continue
                        bi = await birdeye_price_liq(session, addr)
                        liq_now = 0.0
                        if bi:
                            liq = bi.get("liquidity", {})
                            liq_now = (liq.get("usd") if isinstance(liq, dict) else liq) or 0
                            try: liq_now = float(liq_now)
                            except: liq_now = 0.0
                        if liq_now >= MIN_LIQ_USD:
                            await telegram_send(session, f"🟩 Promoted: LP now ≈ ${int(liq_now):,} (≥ ${int(MIN_LIQ_USD):,}) — validating…")
                            pairs_now = await dexscreener_latest(session)
                            pairs_now = best_by_token(pairs_now)
                            for p2 in pairs_now:
                                if _addr_of(p2) == addr:
                                    seen_pairs[addr] = time.time()
                                    candidates[addr] = time.time()
                                    await process_candidate(session, p2)
                                    await asyncio.sleep(2)
                                    break
                            continue
                        still_builder[addr] = exp
                    # near
                    for addr, exp in list(near_watch.items()):
                        if time.time() > exp:
                            continue
                        bi = await birdeye_price_liq(session, addr)
                        liq_now = 0.0
                        if bi:
                            liq = bi.get("liquidity", {})
                            liq_now = (liq.get("usd") if isinstance(liq, dict) else liq) or 0
                            try: liq_now = float(liq_now)
                            except: liq_now = 0.0
                        if liq_now >= MIN_LIQ_USD:
                            await telegram_send(session, f"🟩 Promoted: LP now ≈ ${int(liq_now):,} (≥ ${int(MIN_LIQ_USD):,}) — validating…")
                            pairs_now = await dexscreener_latest(session)
                            pairs_now = best_by_token(pairs_now)
                            for p2 in pairs_now:
                                if _addr_of(p2) == addr:
                                    seen_pairs[addr] = time.time()
                                    candidates[addr] = time.time()
                                    await process_candidate(session, p2)
                                    await asyncio.sleep(2)
                                    break
                            continue
                        still_near[addr] = exp
                    builder_watch = still_builder
                    near_watch = still_near
                    await asyncio.sleep(NEAR_RECHECK_SECONDS)
                    continue

                # reset backoff on success
                backoff = 0

            except Exception as e:
                print(f"[loop] error: {e}")
                backoff = min(backoff + 1, 3)
                sleep_for = BACKOFF_BASE * backoff
                print(f"[loop] backing off {sleep_for}s")
                await asyncio.sleep(sleep_for)

            # Respect DEX cycle cadence
            elapsed = time.time() - started
            sleep_left = max(DEX_CYCLE_SECONDS - elapsed, 5)
            await asyncio.sleep(sleep_left)


def main():
    print("Starting MemeBot (Tytty + Follow-up)…")
    print(f"Keys: Birdeye={'yes' if BIRDEYE_API_KEY else 'no'} | Helius={'yes' if HELIUS_API_KEY else 'no'}")
    print(f"Sheet: {GOOGLE_SHEET_URL or 'not set'}  (webhook: {'yes' if SHEET_WEBHOOK_URL else 'no'})")
    print(f"Cycle: Dex={DEX_CYCLE_SECONDS}s | NearRecheck={NEAR_RECHECK_SECONDS}s | Window={NEAR_WINDOW_SECONDS}s")
    print(f"DNA: MIN_LIQ_USD={MIN_LIQ_USD} | MAX_FDV_USD={MAX_FDV_USD}")
    print(f"Holders: TOP1≤{TOP1_HOLDER_MAX_PCT}% TOP5≤{TOP5_HOLDER_MAX_PCT}% (require_ok={REQUIRE_HELIUS_OK})")
    asyncio.run(discovery_cycle())


if __name__ == "__main__":
    main()

          
          
                       
