# main.py
# MemeBot (Anti-False-Negative + Recheck + /check + BirdEye/Helius)
# - Prevents early auto-fails when DS fields are null
# - Recheck queue (20s for 15m) for incomplete or near-miss pairs
# - /check <dexscreener-url|mint> uses PAIRS endpoint (exact, faster)
# - BirdEye volume/liquidity & Helius holder safety confirmation
# - Journaling: Candidates, NearMisses/Queued, Dings
# - Render keep-alive web server (PORT)

import os
import re
import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import httpx
from aiohttp import web  # keep-alive web server

# ========= Keep Alive (Render) =========
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
    print(f"[health] server running on port {port}")
# ======================================

# ========= ENV =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()
HELIUS_API_KEY  = os.getenv("HELIUS_API_KEY", "").strip()

GOOGLE_SHEET_URL  = os.getenv("GOOGLE_SHEET_URL", "").strip()
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL", "").strip()

COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "3"))

DEX_CYCLE_SECONDS     = int(os.getenv("DEX_CYCLE_SECONDS", "30"))
NEAR_RECHECK_SECONDS  = float(os.getenv("NEAR_RECHECK_SECONDS", "20"))

# DNA thresholds
MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", "20000"))
MAX_FDV_USD = float(os.getenv("MAX_FDV_USD", "600000"))

# Follow-up bands
BUILDER_MIN_LP = float(os.getenv("BUILDER_MIN_LP", "5000"))
BUILDER_MAX_LP = float(os.getenv("BUILDER_MAX_LP", "15000"))
NEAR_MIN_LP    = float(os.getenv("NEAR_MIN_LP", "15000"))
NEAR_MAX_LP    = float(os.getenv("NEAR_MAX_LP", "19900"))

AGE_MAX_MINUTES        = float(os.getenv("AGE_MAX_MINUTES", "4320"))  # 72h
YOUNG_AGE_MINUTES      = float(os.getenv("YOUNG_AGE_MINUTES", "30"))  # leniency window
MIN_TXNS_5M            = int(os.getenv("MIN_TXNS_5M", "3"))

# Recheck queue controls
RECHECK_TTL_MINUTES     = int(os.getenv("RECHECK_TTL_MINUTES", "15"))
RECHECK_INTERVAL_SECONDS= float(os.getenv("RECHECK_INTERVAL_SECONDS", "20"))

# Holder safety
TOP1_HOLDER_MAX_PCT = float(os.getenv("TOP1_HOLDER_MAX_PCT", "25.0"))
TOP5_HOLDER_MAX_PCT = float(os.getenv("TOP5_HOLDER_MAX_PCT", "45.0"))
REQUIRE_HELIUS_OK   = os.getenv("SAFETY_REQUIRE_HELIUS_OK", "false").lower() == "true"

BACKOFF_BASE = 20

# ========= GLOBALS =========
seen_pairs: Dict[str, float]   = {}
candidates: Dict[str, float]   = {}
near_watch: Dict[str, float]   = {}   # token_addr -> expire_ts
builder_watch: Dict[str, float]= {}   # token_addr -> expire_ts

# recheck queue: addr -> meta
recheck: Dict[str, Dict[str, Any]] = {}

# Telegram state
LAST_UPDATE_ID = 0

# ========= UTILS =========
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")

def _now() -> float:
    return time.time()

async def telegram_send(session: httpx.AsyncClient, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] missing token/chat_id, skipping send")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        await session.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print(f"[telegram] error: {e}")

async def telegram_get_updates(session: httpx.AsyncClient, offset: int) -> list:
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = await session.get(url, params={"timeout": 20, "offset": offset}, timeout=25)
        data = r.json() if r.status_code < 300 else {}
        return data.get("result", [])
    except Exception as e:
        print(f"[telegram] getUpdates error: {e}")
        return []

def _allowed_chat(chat_id: int) -> bool:
    if not TELEGRAM_CHAT_ID:
        return True
    try:
        return str(chat_id) == str(TELEGRAM_CHAT_ID)
    except:
        return False

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
    # fallbacks: fdv -> marketCap -> info.fdv
    try:
        return float(pair.get("fdv") or pair.get("marketCap") or (pair.get("info") or {}).get("fdv") or 0)
    except:
        return 0.0

def _liq_usd(pair: Dict[str, Any]) -> float:
    liq = (pair.get("liquidity") or {})
    v = liq.get("usd")
    if v is None:
        v = pair.get("liquidityUsd") or pair.get("lp")
    try:
        return float(v or 0)
    except:
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
    except:
        return 999999.0

def _txns_bucket_total(pair: Dict[str, Any], key: str) -> int:
    buck = (pair.get("txns") or {}).get(key) or {}
    return int(buck.get("buys") or 0) + int(buck.get("sells") or 0)

def _activity_any(pair: Dict[str, Any]) -> int:
    # use m5, else m1/h1 for young pairs
    return _txns_bucket_total(pair, "m5") or _txns_bucket_total(pair, "m1") or _txns_bucket_total(pair, "h1")

def _addr_of(pair: Dict[str, Any]) -> Optional[str]:
    return (pair.get("baseToken") or {}).get("address") or pair.get("tokenAddress") or pair.get("pairAddress")

def best_by_token(pairs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep highest-LP pool per token to avoid dust pools."""
    by_token = {}
    for p in pairs:
        t = _addr_of(p)
        if not t:
            continue
        liq = _liq_usd(p)
        if t not in by_token or liq > by_token[t][0]:
            by_token[t] = (liq, p)
    return [v[1] for v in by_token.values()]

# ========= Data Lanes =========
async def ds_search(session: httpx.AsyncClient, q: str) -> List[Dict[str, Any]]:
    url = "https://api.dexscreener.com/latest/dex/search"
    try:
        r = await session.get(url, params={"q": q}, timeout=20)
        if r.status_code == 429:
            print("[dex] 429 on search; sleeping 20s")
            await asyncio.sleep(20)
            return []
        data = r.json() if r.status_code < 300 else {}
        pairs = data.get("pairs") or data.get("tokens") or []
        return [p for p in pairs if (p.get("chainId") or p.get("chain")) == "solana"]
    except Exception as e:
        print(f"[dex] search error: {e}")
        return []

async def ds_pairs_all(session: httpx.AsyncClient) -> List[Dict[str, Any]]:
    # broader sweep
    url = "https://api.dexscreener.com/latest/dex/pairs/solana"
    try:
        r = await session.get(url, timeout=20)
        if r.status_code == 429:
            print("[dex] 429 on pairs/all; sleeping 20s")
            await asyncio.sleep(20)
            return []
        data = r.json() if r.status_code < 300 else {}
        pairs = data.get("pairs") or []
        return [p for p in pairs if (p.get("chainId") or p.get("chain")) == "solana"]
    except Exception as e:
        print(f"[dex] pairs/all error: {e}")
        return []

async def ds_pair_exact(session: httpx.AsyncClient, chain: str, pair_id: str) -> Optional[Dict[str, Any]]:
    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_id}"
    try:
        r = await session.get(url, timeout=20)
        if r.status_code == 429:
            print("[dex] 429 on pairs/exact; sleeping 20s")
            await asyncio.sleep(20)
            return None
        data = r.json() if r.status_code < 300 else {}
        pairs = data.get("pairs") or []
        if pairs and isinstance(pairs, list):
            return pairs[0]
        return None
    except Exception as e:
        print(f"[dex] pair/exact error: {e}")
        return None

async def dexscreener_latest(session: httpx.AsyncClient) -> List[Dict[str, Any]]:
    # combine pairs/all + search
    all_pairs = await ds_pairs_all(session)
    search_pairs = await ds_search(session, "solana")
    collected = all_pairs + search_pairs
    # de-dupe by token addr
    def _key(p: Dict[str, Any]) -> str:
        return (p.get("baseToken") or {}).get("address") or p.get("tokenAddress") or p.get("pairAddress") or ""
    seen = set(); out = []
    for p in collected:
        k = _key(p)
        if not k or k in seen:
            continue
        seen.add(k); out.append(p)
    print(f"[dex] fetched {len(out)} items (raw={len(collected)})")
    return out

async def birdeye_price_liq(session: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    if not BIRDEYE_API_KEY:
        return None
    url = "https://public-api.birdeye.so/defi/price"
    headers = {"X-API-KEY": BIRDEYE_API_KEY, "accept": "application/json"}
    params = {"chain": "solana", "address": token_address, "include_liquidity": "true"}
    try:
        r = await session.get(url, headers=headers, params=params, timeout=20)
        if r.status_code == 429:
            print("[birdeye] 429; backoff 20s")
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
            print("[helius] 429; backoff 20s")
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

# ========= DNA + Verdict =========
def dna_verdict(pair: Dict[str, Any]) -> Dict[str, Any]:
    """Return verdict plus completeness score and reject reason."""
    fdv = _fdv_usd(pair)
    liq = _liq_usd(pair)
    age_min = _pair_age_minutes(pair)
    tx_any = _activity_any(pair)
    tx5    = _txns_bucket_total(pair, "m5")

    # data completeness score (rough, 0-100)
    completeness = 0
    completeness += 35 if fdv > 0 else 0
    completeness += 35 if liq > 0 else 0
    completeness += 30 if (tx5 > 0 or tx_any > 0) else 0

    if age_min > AGE_MAX_MINUTES:
        return {"status": "reject", "why": f"too old {age_min:.1f}m", "fdv": fdv, "liq": liq,
                "age": age_min, "tx5": tx5, "tx_any": tx_any, "completeness": completeness}

    # Young leniency: within first 30m accept m1/h1 as activity and allow "near bands"
    young = age_min <= YOUNG_AGE_MINUTES

    # Activity check with leniency
    if not (tx5 > 0 or (young and tx_any > 0)):
        return {"status": "incomplete", "why": "low activity (waiting)", "fdv": fdv, "liq": liq,
                "age": age_min, "tx5": tx5, "tx_any": tx_any, "completeness": completeness}

    # Critical fields missing? queue instead of hard fail
    if fdv <= 0 or liq <= 0:
        return {"status": "incomplete", "why": "missing fdv/liq", "fdv": fdv, "liq": liq,
                "age": age_min, "tx5": tx5, "tx_any": tx_any, "completeness": completeness}

    # Pass bands
    if fdv <= MAX_FDV_USD and liq >= MIN_LIQ_USD:
        return {"status": "pass", "why": "meets floor", "fdv": fdv, "liq": liq,
                "age": age_min, "tx5": tx5, "tx_any": tx_any, "completeness": completeness}

    # Near/builder bands (young pairs get extra grace)
    if fdv <= MAX_FDV_USD and NEAR_MIN_LP <= liq <= NEAR_MAX_LP:
        return {"status": "near", "why": "near-miss band", "fdv": fdv, "liq": liq,
                "age": age_min, "tx5": tx5, "tx_any": tx_any, "completeness": completeness}
    if BUILDER_MIN_LP <= liq <= BUILDER_MAX_LP:
        return {"status": "builder", "why": "early-builder band", "fdv": fdv, "liq": liq,
                "age": age_min, "tx5": tx5, "tx_any": tx_any, "completeness": completeness}

    return {"status": "reject", "why": "outside bands", "fdv": fdv, "liq": liq,
            "age": age_min, "tx5": tx5, "tx_any": tx_any, "completeness": completeness}

# ========= Recheck Queue =========
def queue_recheck(addr: str, reason: str):
    now = _now()
    recheck[addr] = {
        "since": now,
        "last": 0.0,
        "until": now + RECHECK_TTL_MINUTES * 60,
        "reason": reason
    }

def dequeue(addr: str):
    recheck.pop(addr, None)

async def recheck_worker(session: httpx.AsyncClient):
    print("[recheck] worker started")
    while True:
        now = _now()
        addrs = list(recheck.keys())
        for addr in addrs:
            meta = recheck.get(addr) or {}
            if now >= meta.get("until", 0):
                recheck.pop(addr, None)
                continue
            if now - meta.get("last", 0) < RECHECK_INTERVAL_SECONDS:
                continue

            # refresh from DS pairs endpoint; also BirdEye LP to see promotion
            p = await ds_pair_exact(session, "solana", addr) or {}
            bi = await birdeye_price_liq(session, addr)
            liq_be = 0.0
            if bi:
                l = bi.get("liquidity", {})
                liq_be = (l.get("usd") if isinstance(l, dict) else l) or 0
                try: liq_be = float(liq_be)
                except: liq_be = 0.0

            verdict = dna_verdict(p) if p else {"status": "incomplete", "why": "no pair payload", "fdv":0, "liq":0, "age":999999, "tx5":0, "tx_any":0, "completeness":0}
            recheck[addr]["last"] = now

            # Promotion path: BirdEye LP crosses floor OR DNA passes
            if verdict["status"] == "pass" or liq_be >= MIN_LIQ_USD:
                sym = (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?"
                url = p.get("url") or p.get("pairLink") or f"https://dexscreener.com/solana/{addr}"
                await telegram_send(session, f"🟩 Recheck PASS: {sym} (LP≈${int(max(_liq_usd(p), liq_be)):,})\n{url}")
                await process_candidate(session, p if p else {"baseToken":{"address":addr,"symbol":"?"},"url":url})
                dequeue(addr)
        await asyncio.sleep(2)

# ========= Telegram: /check helper =========
def _extract_pair_or_mint(text: str) -> Optional[str]:
    text = text.strip()
    # direct mint/pair id (base58 32-44)
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", text):
        return text
    m = re.search(r"dexscreener\.com/solana/([A-Za-z0-9]+)", text)
    if m:
        return m.group(1)
    return None

async def scan_one(session: httpx.AsyncClient, mint_or_url: str):
    addr = _extract_pair_or_mint(mint_or_url)
    if not addr:
        await telegram_send(session, "⚠️ /check needs a Solana mint or DexScreener URL.")
        return

    # exact pair lookup first (fastest)
    p = await ds_pair_exact(session, "solana", addr)
    if not p:
        # fallback: search by mint
        pairs = await ds_search(session, addr)
        if not pairs:
            await telegram_send(session, "❔ Not on DexScreener yet. Queued recheck.")
            queue_recheck(addr, "not indexed yet")
            return
        p = max(pairs, key=lambda x: (_liq_usd(x) or 0))

    sym = (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?"
    liq = _liq_usd(p); fdv = _fdv_usd(p); age_m = round(_pair_age_minutes(p), 1)
    tx5 = _txns_bucket_total(p, "m5"); tx_any = _activity_any(p)
    await telegram_send(session, f"🔎 Check {sym}: LP=${int(liq):,} | FDV=${int(fdv):,} | age={age_m}m | tx5={tx5} (any={tx_any})")

    verdict = dna_verdict(p)
    await telegram_send(session, f"🧪 DNA: {verdict['status']} — {verdict['why']} (complete={verdict['completeness']})")

    if verdict["status"] == "pass":
        await process_candidate(session, p)
        return

    if verdict["status"] in ("incomplete", "near", "builder"):
        addr2 = _addr_of(p) or addr
        queue_recheck(addr2, verdict["why"])
        url = p.get("url") or p.get("pairLink") or f"https://dexscreener.com/solana/{addr2}"
        await telegram_send(session, f"⏳ Queued for recheck: {sym} — {verdict['why']}\n{url}")
        await sheet_append(session, "NearMisses", {
            "ts": utcnow_iso(), "pair_address": addr2, "symbol": sym,
            "fdv": verdict["fdv"], "lp": verdict["liq"], "status": f"queued:{verdict['status']}",
            "reason": verdict["why"], "complete": verdict["completeness"], "url": url
        })
        return

    # hard reject stays logged (for learning)
    url = p.get("url") or p.get("pairLink") or f"https://dexscreener.com/solana/{addr}"
    await sheet_append(session, "NearMisses", {
        "ts": utcnow_iso(), "pair_address": _addr_of(p) or addr, "symbol": sym,
        "fdv": verdict["fdv"], "lp": verdict["liq"], "status": "reject",
        "reason": verdict["why"], "complete": verdict["completeness"], "url": url
    })

# ========= Core Processing =========
async def process_candidate(session: httpx.AsyncClient, pair: Dict[str, Any]):
    address = _addr_of(pair) or ""
    symbol  = (pair.get("baseToken") or {}).get("symbol") or pair.get("symbol") or "?"
    chain   = pair.get("chainId") or pair.get("chain") or "solana"
    dex_url = pair.get("url") or pair.get("pairLink") or f"https://dexscreener.com/{chain}/{address}"

    ts = utcnow_iso()
    row = {
        "ts_detected": ts,
        "pair_address": address,
        "symbol": symbol,
        "chain": chain,
        "dex_url": dex_url,
    }

    bi = await birdeye_price_liq(session, address) if address else None
    if bi:
        row.update({
            "birdeye_price": bi.get("valueUsd"),
            "birdeye_liq": bi.get("liquidity", {}).get("usd") if isinstance(bi.get("liquidity"), dict) else bi.get("liquidity"),
            "birdeye_success": True
        })
    else:
        row["birdeye_success"] = False

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
            "mc_at_ding": pair.get("fdv") or pair.get("marketCap"),
            "liq_at_ding": row.get("birdeye_liq"),
            "dna_flavor": "TTYL",
            "safety_snapshot": "OK" if safety_ok else "WARN",
            "dex_url": dex_url
        })
    else:
        await telegram_send(session, f"👀 Watchlist: {symbol}\n(birdeye:{'OK' if bi else 'MISS'} / helius:{'OK' if safety_ok else 'WARN'})\n{dex_url}")

    await sheet_append(session, "Candidates", {**row, "status": "candidate"})

# ========= Telegram Commands =========
async def telegram_command_loop(session: httpx.AsyncClient):
    global LAST_UPDATE_ID
    print("[tg] command loop started")
    while True:
        updates = await telegram_get_updates(session, LAST_UPDATE_ID)
        for u in updates:
            try:
                LAST_UPDATE_ID = max(LAST_UPDATE_ID, int(u.get("update_id", 0)) + 1)
                msg  = u.get("message") or u.get("edited_message") or {}
                chat = msg.get("chat", {}) or {}
                chat_id = chat.get("id")
                text = (msg.get("text") or "").strip()
                if not chat_id or not text or not _allowed_chat(chat_id):
                    continue
                low = text.lower()
                if low.startswith("/start"):
                    await telegram_send(session,
                        "👋 I’m live.\n"
                        f"Sheet: {GOOGLE_SHEET_URL or 'not set'}\n"
                        "Commands: /ping /dna /check <mint-or-url> /help")
                elif low.startswith("/ping"):
                    await telegram_send(session, f"🏓 pong — {utcnow_iso()}")
                elif low.startswith("/dna"):
                    await telegram_send(session,
                        "🧬 DNA settings:\n"
                        f"- LP floor: ${int(MIN_LIQ_USD):,}\n"
                        f"- FDV cap:  ${int(MAX_FDV_USD):,}\n"
                        f"- Near band: ${int(NEAR_MIN_LP):,}–${int(NEAR_MAX_LP):,}\n"
                        f"- Builder band: ${int(BUILDER_MIN_LP):,}–${int(BUILDER_MAX_LP):,}\n"
                        f"- Age ≤ {int(AGE_MAX_MINUTES/60)}h; young leniency ≤ {int(YOUNG_AGE_MINUTES)}m\n"
                        f"- Recheck: {int(RECHECK_INTERVAL_SECONDS)}s for {int(RECHECK_TTL_MINUTES)}m\n"
                        f"- Holders: Top1 ≤ {TOP1_HOLDER_MAX_PCT}%, Top5 ≤ {TOP5_HOLDER_MAX_PCT}%")
                elif low.startswith("/help"):
                    await telegram_send(session, "Commands: /ping /dna /check <mint-or-url> /help")
                elif low.startswith("/scan") or low.startswith("/check"):
                    parts = text.split(maxsplit=1)
                    if len(parts) == 2:
                        await scan_one(session, parts[1])
                    else:
                        await telegram_send(session, "Usage: /check <mint-or-dexscreener-url>")
            except Exception as e:
                print(f"[tg] handle error: {e}")
                continue
        await asyncio.sleep(2)

# ========= Discovery Loop =========
async def discovery_cycle():
    async with httpx.AsyncClient(timeout=30) as session:
        await start_health_server()
        asyncio.create_task(telegram_command_loop(session))
        asyncio.create_task(recheck_worker(session))  # start recheck engine
        await telegram_send(session, f"✅ Bot live @ {utcnow_iso()}\nSheet: {GOOGLE_SHEET_URL or 'not set'}")
        backoff = 0

        while True:
            started = _now()
            try:
                pairs  = await dexscreener_latest(session)
                pairs  = best_by_token(pairs)
                now_ts = _now()
                print(f"[loop] {utcnow_iso()} ok pairs={len(pairs)} queued={len(recheck)}")

                for p in pairs:
                    addr = _addr_of(p)
                    if not addr:
                        continue

                    last = seen_pairs.get(addr, 0)
                    if now_ts - last < COOLDOWN_MINUTES * 60:
                        continue

                    verdict = dna_verdict(p)
                    status  = verdict["status"]
                    sym = (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?"
                    url = p.get("url") or p.get("pairLink") or f"https://dexscreener.com/solana/{addr}"

                    if status == "pass":
                        seen_pairs[addr] = now_ts
                        candidates[addr] = now_ts
                        await sheet_append(session, "Candidates", {
                            "ts_detected": utcnow_iso(),
                            "pair_address": addr,
                            "symbol": sym,
                            "chain": p.get("chainId") or p.get("chain") or "solana",
                            "dex_url": url,
                            "mc_at_detect": verdict["fdv"],
                            "liq_at_detect": verdict["liq"],
                            "dna_pass_type": "TTYL",
                            "why_candidate": "FDV≤cap & liq≥floor",
                            "status": "candidate"
                        })
                        await process_candidate(session, p)
                        await asyncio.sleep(1.5)
                        # if it passed, no need to track in near/builder
                        builder_watch.pop(addr, None); near_watch.pop(addr, None)
                        dequeue(addr)
                        continue

                    if status in ("incomplete", "near", "builder"):
                        # queue for recheck instead of dropping
                        if addr not in recheck:
                            queue_recheck(addr, verdict["why"])
                            await sheet_append(session, "NearMisses", {
                                "ts": utcnow_iso(), "pair_address": addr, "symbol": sym,
                                "fdv": verdict["fdv"], "lp": verdict["liq"], "status": f"queued:{status}",
                                "reason": verdict["why"], "complete": verdict["completeness"], "url": url
                            })
                        continue

                    # hard reject (outside bands / too old) — log for learning
                    await sheet_append(session, "NearMisses", {
                        "ts": utcnow_iso(), "pair_address": addr, "symbol": sym,
                        "fdv": verdict["fdv"], "lp": verdict["liq"], "status": "reject",
                        "reason": verdict["why"], "complete": verdict["completeness"], "url": url
                    })

                backoff = 0  # success path

            except Exception as e:
                print(f"[loop] error: {e}")
                backoff = min(backoff + 1, 3)
                sleep_for = BACKOFF_BASE * backoff
                print(f"[loop] backing off {sleep_for}s")
                await asyncio.sleep(sleep_for)

            elapsed = _now() - started
            sleep_left = max(DEX_CYCLE_SECONDS - elapsed, 5)
            await asyncio.sleep(sleep_left)

def main():
    print("Starting MemeBot (Anti-False-Negative)…")
    print(f"Keys: BirdEye={'yes' if BIRDEYE_API_KEY else 'no'} | Helius={'yes' if HELIUS_API_KEY else 'no'}")
    print(f"Sheet: {GOOGLE_SHEET_URL or 'not set'}  (webhook: {'yes' if SHEET_WEBHOOK_URL else 'no'})")
    print(f"Cycle: Dex={DEX_CYCLE_SECONDS}s | Recheck={RECHECK_INTERVAL_SECONDS}s x {RECHECK_TTL_MINUTES}m window")
    print(f"DNA: MIN_LIQ_USD={MIN_LIQ_USD} | MAX_FDV_USD={MAX_FDV_USD} | Young≤{int(YOUNG_AGE_MINUTES)}m")
    print(f"Holders: TOP1≤{TOP1_HOLDER_MAX_PCT}% TOP5≤{TOP5_HOLDER_MAX_PCT}% (require_ok={REQUIRE_HELIUS_OK})")
    asyncio.run(discovery_cycle())

if __name__ == "__main__":
    main()
