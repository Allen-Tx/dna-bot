# main.py
import os
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import httpx

# ========= ENV =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()  # required for liq/price lane
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()    # optional safety lane

GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "").strip()  # purely informational for logs
# Optional: if you set up a tiny Apps Script Web App that appends rows, put that URL here
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL", "").strip()

COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "10"))

# Polling (free-tier safe)
DEX_CYCLE_SECONDS = int(os.getenv("DEX_CYCLE_SECONDS", "60"))
BIRDEYE_CYCLE_SECONDS = int(os.getenv("BIRDEYE_CYCLE_SECONDS", "15"))
HELIUS_CYCLE_SECONDS = int(os.getenv("HELIUS_CYCLE_SECONDS", "90"))

# DNA thresholds (tune yours here or load from Settings tab later)
MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", "20000"))   # example: 20k
MAX_FDV_USD = float(os.getenv("MAX_FDV_USD", "600000"))  # example: 600k

# Safety thresholds (simple starters)
TOP1_HOLDER_MAX_PCT = float(os.getenv("TOP1_HOLDER_MAX_PCT", "20.0"))
TOP5_HOLDER_MAX_PCT = float(os.getenv("TOP5_HOLDER_MAX_PCT", "45.0"))

# ========= GLOBALS =========
seen_pairs: Dict[str, float] = {}       # pair_address -> last_seen_ts
candidates: Dict[str, float] = {}       # pair_address -> ts_detected
last_birdeye_check: Dict[str, float] = {}
last_helius_check: Dict[str, float] = {}

BACKOFF_BASE = 20  # seconds


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
    """
    Journaling: if you publish a tiny Google Apps Script Web App that accepts JSON and appends
    to your sheet, put its URL in SHEET_WEBHOOK_URL. If empty, we just print.
    """
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


# ------------------ DATA LANES ------------------

async def dexscreener_latest(session: httpx.AsyncClient) -> List[Dict[str, Any]]:
    url = "https://api.dexscreener.com/latest/dex/tokens/solana"
    # Fallback endpoint (pairs) — some deployments use this:
    alt_url = "https://api.dexscreener.com/latest/dex/search?q=solana"
    for attempt in range(3):
        try:
            r = await session.get(url, timeout=20)
            if r.status_code == 429:
                wait = BACKOFF_BASE * (attempt + 1)
                print(f"[dex] 429 rate-limit, backoff {wait}s")
                await asyncio.sleep(wait)
                continue
            if r.status_code >= 300:
                print(f"[dex] {r.status_code} {r.text[:200]}; trying alt")
                r = await session.get(alt_url, timeout=20)
            data = r.json()
            # Normalize: return a list of dicts with at least token/chain/pair address
            pairs = data.get("pairs") or data.get("tokens") or []
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
    """
    Minimal safety snapshot. If HELIUS API changes, we handle errors gracefully.
    We attempt a common holders endpoint; if it fails, we return None (skip safety).
    """
    if not HELIUS_API_KEY:
        return None
    # Try a holders endpoint (if unavailable, function returns None)
    # Example (subject to change by Helius): /v0/tokens/holders?tokenMint=...&api-key=...
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
        # Sort by amount, compute simple concentration
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


# ------------------ LOGIC ------------------

def dna_pass_stub(pair: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Simple DNA gate using fields commonly present in DexScreener results.
    You can replace with your Strict/TTYL numbers.
    """
    # Try to read fdv, liquidity, and basic info safely
    fdv = float(pair.get("fdv", 0) or 0)
    liq = 0.0
    try:
        liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    except Exception:
        pass

    if fdv <= 0 or liq <= 0:
        return None

    if fdv <= MAX_FDV_USD and liq >= MIN_LIQ_USD:
        return {"fdv": fdv, "liq": liq, "dna_type": "TTYL"}  # tag as your DNA flavor
    return None


async def process_candidate(session: httpx.AsyncClient, pair: Dict[str, Any]):
    """
    After DNA pass on Dex, we validate on Birdeye (price/liquidity),
    then optional safety on Helius. If all green → soft/strong ding + journal.
    """
    # token/pair addresses differ depending on endpoint shape; try to extract safely
    address = pair.get("baseToken", {}).get("address") or pair.get("tokenAddress") or pair.get("pairAddress")
    symbol = pair.get("baseToken", {}).get("symbol") or pair.get("symbol") or "?"
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

    # Birdeye lane (liquidity/price/spread)
    bi = await birdeye_price_liq(session, address) if address else None
    if bi:
        row.update({
            "birdeye_price": bi.get("valueUsd"),
            "birdeye_liq": bi.get("liquidity", {}).get("usd") if isinstance(bi.get("liquidity"), dict) else bi.get("liquidity"),
            "birdeye_success": True
        })
    else:
        row["birdeye_success"] = False

    # Helius lane (holder/safety)
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
    soft_ok = bi is not None
    safety_ok = (hel is None) or (hel and hel.get("risk") == "ok")  # if helius missing, don't block

    if soft_ok and safety_ok:
        # STRONG DING if we got Birdeye AND (no risk or Helius ok)
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
        # CANDIDATE / WATCHLIST only
        await telegram_send(session, f"👀 Watchlist: {symbol}\n(birdeye:{'OK' if bi else 'MISS'} / helius:{'OK' if safety_ok else 'WARN'})\n{dex_url}")

    # Always record candidate
    await sheet_append(session, "Candidates", {
        **row,
        "status": "watching" if soft_ok else "pending"
    })


async def discovery_cycle():
    """
    Runs every DEX_CYCLE_SECONDS:
    - fetch latest pairs
    - run DNA
    - enqueue candidates for birdeye/helius validation
    """
    async with httpx.AsyncClient(timeout=30) as session:
        # Startup ping
        await telegram_send(session, f"✅ Bot live @ {utcnow_iso()}\nSheet: {GOOGLE_SHEET_URL or 'not set'}")
        backoff = 0

        while True:
            started = time.time()
            try:
                pairs = await dexscreener_latest(session)
                print(f"[dex] fetched {len(pairs)} items")
                now_ts = time.time()

                for p in pairs:
                    addr = p.get("baseToken", {}).get("address") or p.get("tokenAddress") or p.get("pairAddress")
                    if not addr:
                        continue

                    # Cooldown / dedupe
                    last = seen_pairs.get(addr, 0)
                    if now_ts - last < COOLDOWN_MINUTES * 60:
                        continue

                    # DNA gate
                    dna = dna_pass_stub(p)
                    if not dna:
                        continue

                    # record seen + candidate
                    seen_pairs[addr] = now_ts
                    candidates[addr] = now_ts

                    # add DNA info for journaling
                    await sheet_append(session, "Candidates", {
                        "ts_detected": utcnow_iso(),
                        "pair_address": addr,
                        "symbol": p.get("baseToken", {}).get("symbol") or p.get("symbol") or "?",
                        "chain": p.get("chainId") or p.get("chain") or "solana",
                        "dex_url": p.get("url") or p.get("pairLink") or "",
                        "mc_at_detect": dna["fdv"],
                        "liq_at_detect": dna["liq"],
                        "dna_pass_type": dna["dna_type"],
                        "why_candidate": "FDV≤cap & liq≥floor",
                        "status": "candidate"
                    })

                    # Validate lanes (Birdeye/Helius) staggered to avoid bursts
                    await process_candidate(session, p)
                    await asyncio.sleep(3)  # small stagger between candidates

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
    print("Starting MemeBot Phase-1…")
    print(f"Keys: Birdeye={'yes' if BIRDEYE_API_KEY else 'no'} | Helius={'yes' if HELIUS_API_KEY else 'no'}")
    print(f"Sheet: {GOOGLE_SHEET_URL or 'not set'}  (webhook: {'yes' if SHEET_WEBHOOK_URL else 'no'})")
    print(f"Cycle: Dex={DEX_CYCLE_SECONDS}s | Birdeye={BIRDEYE_CYCLE_SECONDS}s | Helius={HELIUS_CYCLE_SECONDS}s")
    asyncio.run(discovery_cycle())


if __name__ == "__main__":
    main()

