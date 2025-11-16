
# ===================== MemeBot vNext — BIRD DROP + EARLY BIRD =====================
# Dex engine + BirdEye optional confirm (with fallback) + Early Bird DNA
#
# - DexScreener = main discovery + /check engine
# - BirdEye + Helius = extra safety when available (optional)
# - Early Bird DNA:
#     * Coins with life = PASS / BUILDER / NEAR / WARMUP (no hard reject)
#     * Only true trash / old-dead / bloated / unsafe = REJECT
# - Journaling:
#     * Candidates   (pass / dings)
#     * NearMisses   (near / builder / warmup)
#     * Dings        (strong/provisional dings)
#     * ManualChecks (/check)
# ================================================================================

import os
import re
import asyncio
import time
import random
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import httpx
from aiohttp import web

# ===================== Keep-alive (Render) =====================
async def _health_handle(request):
    return web.Response(text="ok")

async def _doorbell_handle(request):
    # placeholder route; can be used later for external "ping" if needed
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
# ===============================================================

# ===================== ENV & defaults ==========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GOOGLE_SHEET_URL   = os.getenv("GOOGLE_SHEET_URL", "").strip()
SHEET_WEBHOOK_URL  = os.getenv("SHEET_WEBHOOK_URL", "").strip()

BIRDEYE_API_KEY    = os.getenv("BIRDEYE_API_KEY", "").strip()
HELIUS_API_KEY     = os.getenv("HELIUS_API_KEY", "").strip()

# Loop pacing / de-dup
DEX_CYCLE_SECONDS      = int(os.getenv("DEX_CYCLE_SECONDS", "60"))   # 45–60 is safe
DEX_JITTER_MAX_SECONDS = float(os.getenv("DEX_JITTER_MAX_SECONDS", "5"))
COOLDOWN_MINUTES       = int(os.getenv("COOLDOWN_MINUTES", "15"))   # per pair; suppress duplicate alerts
NOTIFY_COOLDOWN_MIN    = int(os.getenv("NOTIFY_COOLDOWN_MIN", "15"))

# Discovery feeds
USE_SEARCH_FEED        = os.getenv("USE_SEARCH_FEED", "false").lower() == "true"
REJECT_MEMORY_MINUTES  = int(os.getenv("REJECT_MEMORY_MINUTES", "180"))

# DNA thresholds
MIN_LIQ_USD            = float(os.getenv("MIN_LIQ_USD", "20000"))    # main LP floor for full pass
MAX_FDV_USD            = float(os.getenv("MAX_FDV_USD", "600000"))   # FDV cap for main pass
AGE_MAX_MINUTES        = float(os.getenv("AGE_MAX_MINUTES", "4320")) # 72h

# Bands
BUILDER_MIN_LP         = float(os.getenv("BUILDER_MIN_LP", "9000"))
BUILDER_MAX_LP         = float(os.getenv("BUILDER_MAX_LP", "15000"))
NEAR_MIN_LP            = float(os.getenv("NEAR_MIN_LP", "15000"))
NEAR_MAX_LP            = float(os.getenv("NEAR_MAX_LP", "24900"))
PROMOTION_LP           = float(os.getenv("PROMOTION_LP", "25000"))   # promotion gate

# Early Bird extra DNA guards
DUST_LP_USD            = float(os.getenv("DUST_LP_USD", "3000"))     # below this + no tx = dust
FDV_HARD_CAP_USD       = float(os.getenv("FDV_HARD_CAP_USD", str(MAX_FDV_USD * 2)))
OLD_MAX_AGE_MINUTES    = float(os.getenv("OLD_MAX_AGE_MINUTES", str(AGE_MAX_MINUTES * 2)))

# TX leniency
YOUNG_AGE_MINUTES      = float(os.getenv("YOUNG_AGE_MINUTES", "30"))
MIN_TXNS_5M_YOUNG      = int(os.getenv("MIN_TXNS_5M_YOUNG", "1"))
MIN_TXNS_5M_OLD        = int(os.getenv("MIN_TXNS_5M_OLD", "3"))

# Watchlist queues
WATCHLIST_RECHECK_SECONDS = float(os.getenv("WATCHLIST_RECHECK_SECONDS", "60"))
WATCHLIST_WINDOW_SECONDS  = float(os.getenv("WATCHLIST_WINDOW_SECONDS", "900"))

# Confirmers pacing
BIRDEYE_CYCLE_SECONDS  = int(os.getenv("BIRDEYE_CYCLE_SECONDS", "30"))
HELIUS_CYCLE_SECONDS   = int(os.getenv("HELIUS_CYCLE_SECONDS", "90"))

# Holder safety
TOP1_HOLDER_MAX_PCT    = float(os.getenv("TOP1_HOLDER_MAX_PCT", "25.0"))
TOP5_HOLDER_MAX_PCT    = float(os.getenv("TOP5_HOLDER_MAX_PCT", "45.0"))
SAFETY_REQUIRE_HELIUS_OK = os.getenv("SAFETY_REQUIRE_HELIUS_OK", "false").lower() == "true"

# Journaling behavior
JOURNAL_ONLY_ON_CHANGE = os.getenv("JOURNAL_ONLY_ON_CHANGE", "true").lower() == "true"

# Manual /check helpers
MANUAL_RETRY_SECONDS   = int(os.getenv("MANUAL_RETRY_SECONDS", "30"))
MANUAL_RETRY_WINDOW    = int(os.getenv("MANUAL_RETRY_WINDOW", "300"))
PROVISIONAL_DING       = os.getenv("PROVISIONAL_DING", "false").lower() == "true"

BACKOFF_BASE           = int(os.getenv("BACKOFF_BASE", "20"))

# ===================== Globals ================================
seen_pairs: Dict[str, float] = {}            # pair_address -> last seen ts (cooldown)
notify_last: Dict[str, float] = {}           # pair_address -> last telegram ding ts
reject_memory: Dict[str, float] = {}         # pair_address -> ignore until ts
builder_watch: Dict[str, float] = {}         # pair_address -> expiry ts
near_watch: Dict[str, float] = {}            # pair_address -> expiry ts

# ===================== Utils =================================
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")

def _addr_of(p: Dict[str, Any]) -> Optional[str]:
    return (p.get("baseToken") or {}).get("address") or p.get("tokenAddress") or p.get("pairAddress") or p.get("address")

def _fdv_usd(p: Dict[str, Any]) -> float:
    return float(p.get("fdv") or p.get("marketCap") or (p.get("info") or {}).get("fdv") or 0)

def _lp_usd(p: Dict[str, Any]) -> float:
    liq = (p.get("liquidity") or {})
    v = liq.get("usd") if isinstance(liq, dict) else (p.get("liquidityUsd") or p.get("lp"))
    try:
        return float(v or 0)
    except Exception:
        return 0.0

def _txns_m5(p: Dict[str, Any]) -> int:
    m5 = (p.get("txns") or {}).get("m5") or {}
    return int((m5.get("buys") or 0)) + int((m5.get("sells") or 0))

def _pair_age_minutes(p: Dict[str, Any]) -> float:
    ts = p.get("creationTime") or p.get("pairCreatedAt") or p.get("createdAt")
    try:
        ts = int(ts) if ts else 0
        if ts <= 0:
            return 9e9
        if ts > 10**12:
            ts //= 1000  # ms -> s
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return 9e9

def best_by_token(pairs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by = {}
    for p in pairs:
        t = _addr_of(p)
        if not t:
            continue
        lp = _lp_usd(p)
        if t not in by or lp > by[t][0]:
            by[t] = (lp, p)
    return [v[1] for v in by.values()]

# ===================== Telegram & Sheets ======================
async def telegram_send(session: httpx.AsyncClient, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[tg] skip send: {text[:80]}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        await session.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print(f"[tg] error: {e}")

async def sheet_append(session: httpx.AsyncClient, tab: str, row: Dict[str, Any]):
    payload = {"tab": tab, "row": row, "sheet_url": GOOGLE_SHEET_URL}
    if not SHEET_WEBHOOK_URL:
        print(f"[sheet:{tab}] {row}")
        return
    try:
        r = await session.post(SHEET_WEBHOOK_URL, json=payload, timeout=20)
        if r.status_code >= 300:
            print(f"[sheet] webhook non-200: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[sheet] webhook error: {e}")

_last_row_cache: Dict[Tuple[str,str], Dict[str,Any]] = {}  # (tab, addr) -> last payload
def _changed(tab: str, addr: str, row: Dict[str, Any]) -> bool:
    if not JOURNAL_ONLY_ON_CHANGE:
        return True
    key = (tab, addr or "?")
    prev = _last_row_cache.get(key)
    if prev is None or prev != row:
        _last_row_cache[key] = dict(row)
        return True
    return False

# ===================== Dex (ENGINE) ===========================
async def dexscreener_latest(session: httpx.AsyncClient) -> List[Dict[str, Any]]:
    endpoints = ["https://api.dexscreener.com/latest/dex/pairs/solana"]
    if USE_SEARCH_FEED:
        endpoints.append("https://api.dexscreener.com/latest/dex/search?q=solana")

    out: List[Dict[str, Any]] = []
    for url in endpoints:
        for attempt in range(3):
            try:
                r = await session.get(url, timeout=20)
                if r.status_code == 429:
                    wait = BACKOFF_BASE * (attempt + 1)
                    print(f"[dex] 429 {url}; backoff {wait}s")
                    await asyncio.sleep(wait)
                    continue
                body = (r.text or "").strip()
                if not body:
                    await asyncio.sleep(1.5); continue
                try:
                    data = r.json()
                except Exception as je:
                    print(f"[dex] json fail {url}: {je}")
                    await asyncio.sleep(1.5); continue
                pairs = data.get("pairs") or data.get("tokens") or []
                if not isinstance(pairs, list):
                    pairs = []
                pairs = [p for p in pairs if (p.get("chainId") or p.get("chain")) == "solana"]
                out.extend(pairs)
                break
            except Exception as e:
                print(f"[dex] error {url}: {e}")
                await asyncio.sleep(1.5)

    seen: set = set()
    dedup: List[Dict[str, Any]] = []
    for p in out:
        k = _addr_of(p) or p.get("url") or p.get("pairAddress") or ""
        if not k or k in seen:
            continue
        seen.add(k)
        dedup.append(p)
    return dedup

async def dexscreener_search_by_mint(session: httpx.AsyncClient, mint: str) -> Optional[Dict[str, Any]]:
    try:
        r = await session.get("https://api.dexscreener.com/latest/dex/search", params={"q": mint}, timeout=20)
        data = r.json() if r.status_code < 300 else {}
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        p = max(pairs, key=lambda x: (_lp_usd(x) or 0))
        return p
    except Exception as e:
        print(f"[dex] search error: {e}")
        return None

# ===================== BirdEye helpers (optional) =============
def _be_headers():
    return {"X-API-KEY": BIRDEYE_API_KEY, "accept": "application/json"}

async def birdeye_price_liq(session: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    if not BIRDEYE_API_KEY:
        return None
    url = "https://public-api.birdeye.so/defi/price"
    headers = _be_headers()
    params = {"chain": "solana", "address": token_address, "include_liquidity": "true"}
    try:
        r = await session.get(url, headers=headers, params=params, timeout=20)
        if r.status_code in (429, 400):
            print("[birdeye] throttle/limit; backoff 30s")
            await asyncio.sleep(BIRDEYE_CYCLE_SECONDS)
            return None
        if r.status_code >= 300:
            print(f"[birdeye] {r.status_code} {r.text[:160]}")
            return None
        return r.json().get("data")
    except Exception as e:
        print(f"[birdeye] error: {e}")
        return None

# ===================== Helius holder safety ===================
async def helius_holder_safety(session: httpx.AsyncClient, token_mint: str) -> Optional[Dict[str, Any]]:
    if not HELIUS_API_KEY:
        return None
    url = f"https://api.helius.xyz/v0/tokens/holders?tokenMint={token_mint}&api-key={HELIUS_API_KEY}"
    try:
        r = await session.get(url, timeout=25)
        if r.status_code in (429, 400):
            print("[helius] throttle/limit; backoff 90s")
            await asyncio.sleep(HELIUS_CYCLE_SECONDS)
            return None
        if r.status_code >= 300:
            print(f"[helius] {r.status_code} {r.text[:160]}")
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

# ===================== Early Bird DNA & verdicts ==============
def dna_verdict(p: Dict[str, Any]) -> Dict[str, Any]:
    """
    Early Bird DNA:
    - status = "reject"  : true trash / old-dead / bloated / no data
    - status = "pass"    : full floor met, main ding zone
    - status = "builder" : builder band (early entries)
    - status = "near"    : near band (bigger LP but under cap)
    - status = "warmup"  : has life but still building; DO NOT hard reject
    """
    fdv = _fdv_usd(p)
    lp  = _lp_usd(p)
    age = _pair_age_minutes(p)
    m5  = _txns_m5(p)

    def _payload(status: str, why: str) -> Dict[str, Any]:
        return {"status": status, "why": why, "fdv": fdv, "lp": lp, "age": age, "m5": m5}

    # -------- HARD REJECTS (trash / no data) --------

    # missing core numbers
    if fdv <= 0 or lp <= 0:
        return _payload("reject", "missing fdv/liq")

    # FDV way too high (outside playbook)
    if fdv > FDV_HARD_CAP_USD:
        return _payload("reject", f"fdv too high {fdv:.0f}>{FDV_HARD_CAP_USD:.0f}")

    # very old + no life = dead
    if age > OLD_MAX_AGE_MINUTES and m5 == 0 and lp < BUILDER_MIN_LP:
        return _payload("reject", f"old & inactive age={age:.1f}m>{OLD_MAX_AGE_MINUTES}m")

    # no life (dust)
    has_life = (lp >= DUST_LP_USD or m5 > 0)
    if not has_life:
        return _payload("reject", "no life (dust lp/tx)")

    # -------- Early Bird logic (pass / builder / near / warmup) --------

    # txn requirement (for main pass/near/builder)
    req = MIN_TXNS_5M_YOUNG if age <= YOUNG_AGE_MINUTES else MIN_TXNS_5M_OLD
    tx_ok = (m5 >= req)

    # 1) Full floor pass (normal Ding tier)
    if age <= AGE_MAX_MINUTES and tx_ok and fdv <= MAX_FDV_USD and lp >= MIN_LIQ_USD:
        return _payload("pass", "meets floor")

    # 2) Builder early-pass: in builder band with life, can ding earlier
    if (
        age <= AGE_MAX_MINUTES
        and m5 > 0
        and fdv <= MAX_FDV_USD
        and BUILDER_MIN_LP <= lp <= BUILDER_MAX_LP
    ):
        return _payload("builder", "builder band (early)")

    # 3) Near band: bigger LP but still under cap
    if age <= AGE_MAX_MINUTES and fdv <= MAX_FDV_USD and NEAR_MIN_LP <= lp <= NEAR_MAX_LP:
        if tx_ok:
            return _payload("near", "near band")
        else:
            return _payload("warmup", "near band but low activity")

    # 4) Warmup catch-all: alive but not yet at floors
    pieces = []
    if lp < MIN_LIQ_USD:
        pieces.append(f"lp {lp:.0f}<{MIN_LIQ_USD:.0f}")
    if not tx_ok:
        pieces.append(f"tx5 {m5}<{req}")
    if age > AGE_MAX_MINUTES:
        pieces.append(f"age {age:.1f}m>{AGE_MAX_MINUTES}m")
    why = "warmup: " + (", ".join(pieces) if pieces else "building")
    return _payload("warmup", why)

# ===================== Journal helpers ========================
async def journal_candidate(session: httpx.AsyncClient, p: Dict[str, Any], verdict: Dict[str, Any]):
    addr = _addr_of(p) or "?"
    row = {
        "ts_detected": utcnow_iso(),
        "pair_address": addr,
        "symbol": (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?",
        "chain": p.get("chainId") or p.get("chain") or "solana",
        "dex_url": p.get("url") or p.get("pairLink") or "",
        "mc_at_detect": verdict.get("fdv"),
        "liq_at_detect": verdict.get("lp"),
        "dna_pass_type": "TTYL",
        "why_candidate": verdict.get("why"),
        "status": "candidate",
    }
    if _changed("Candidates", addr, row):
        await sheet_append(session, "Candidates", row)

async def journal_near(session: httpx.AsyncClient, p: Dict[str, Any], verdict: Dict[str, Any], status: str):
    addr = _addr_of(p) or "?"
    row = {
        "ts": utcnow_iso(),
        "pair_address": addr,
        "symbol": (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?",
        "fdv": verdict.get("fdv"),
        "lp": verdict.get("lp"),
        "status": status,
        "url": p.get("url") or p.get("pairLink") or "",
    }
    if _changed("NearMisses", addr, row):
        await sheet_append(session, "NearMisses", row)

async def journal_ding(session: httpx.AsyncClient, p: Dict[str, Any], tier: str, liq_at_ding: Any, safety: str):
    addr = _addr_of(p) or "?"
    row = {
        "ts_ding": utcnow_iso(),
        "pair_address": addr,
        "symbol": (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?",
        "ding_tier": tier,
        "mc_at_ding": p.get("fdv"),
        "liq_at_ding": liq_at_ding,
        "dna_flavor": "TTYL",
        "safety_snapshot": safety,
        "dex_url": p.get("url") or p.get("pairLink") or "",
    }
    if _changed("Dings", addr, row):
        await sheet_append(session, "Dings", row)

# ===================== Confirmers & notify-once ================
async def confirm_and_notify(session: httpx.AsyncClient, p: Dict[str, Any], provisional_allowed: bool = False):
    """
    BirdEye is OPTIONAL here:
    - If BirdEye returns data -> use BirdEye LP.
    - If BirdEye returns None -> FALL BACK to Dex LP and still ding + journal.
    """
    addr = _addr_of(p) or "?"

    now = time.time()
    last = notify_last.get(addr, 0)
    if now - last < NOTIFY_COOLDOWN_MIN * 60:
        return

    lp_now = _lp_usd(p)
    if lp_now < PROMOTION_LP and not provisional_allowed:
        return

    bi = await birdeye_price_liq(session, addr)
    hel = await helius_holder_safety(session, addr)

    safety_ok = True
    top1 = top5 = None
    if hel:
        top1 = hel.get("top1_pct")
        top5 = hel.get("top5_pct")
        safety_ok = (hel.get("risk") == "ok")

    safety_flag = "OK" if safety_ok else "WARN"

    if bi is None and provisional_allowed:
        await telegram_send(
            session,
            f"🧬 PROVISIONAL DING (Dex LP only): "
            f"{(p.get('baseToken') or {}).get('symbol') or p.get('symbol') or '?'}\n"
            f"LP≈${int(lp_now):,}\n{p.get('url') or ''}"
        )
        await journal_ding(session, p, tier="Provisional", liq_at_ding=lp_now, safety=safety_flag)
        notify_last[addr] = now
        return

    if bi is None:
        liq_usd = lp_now
    else:
        liq_field = bi.get("liquidity", {})
        liq_usd = (liq_field.get("usd") if isinstance(liq_field, dict) else liq_field) or lp_now

    if SAFETY_REQUIRE_HELIUS_OK and not safety_ok:
        await telegram_send(
            session,
            f"👀 Watchlist (holder risk): "
            f"{(p.get('baseToken') or {}).get('symbol') or p.get('symbol') or '?'}\n"
            f"Top1:{top1}% Top5:{top5}%\n{p.get('url') or ''}"
        )
        return

    sym = (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?"
    parts = [f"🧬 STRONG DING: {sym}", f"LP≈${int(float(liq_usd)):,}"]
    if top1 is not None and top5 is not None:
        parts.append(f"Top1:{top1}% Top5:{top5}% {safety_flag}")
    parts.append(p.get("url") or "")
    await telegram_send(session, "\n".join(parts))
    await journal_ding(session, p, tier="Strong", liq_at_ding=liq_usd, safety=safety_flag)
    notify_last[addr] = now

# ===================== Manual /check auto-retry ================
async def manual_confirm_with_retry(session: httpx.AsyncClient, p: Dict[str, Any]):
    start = time.time()
    if PROVISIONAL_DING:
        await confirm_and_notify(session, p, provisional_allowed=True)
    while time.time() - start < MANUAL_RETRY_WINDOW:
        await confirm_and_notify(session, p, provisional_allowed=False)
        await asyncio.sleep(MANUAL_RETRY_SECONDS)

# ===================== Core processing (Early Bird) ===========
async def process_pair(session: httpx.AsyncClient, p: Dict[str, Any]):
    addr = _addr_of(p)
    if not addr:
        return

    now_ts = time.time()

    last_seen = seen_pairs.get(addr, 0)
    if now_ts - last_seen < COOLDOWN_MINUTES * 60:
        return

    rej_until = reject_memory.get(addr, 0)
    if now_ts < rej_until:
        return

    verdict = dna_verdict(p)
    status = verdict.get("status")

    # HARD REJECT only for true trash / dead / bloated
    if status == "reject":
        reject_memory[addr] = now_ts + REJECT_MEMORY_MINUTES * 60
        builder_watch.pop(addr, None)
        near_watch.pop(addr, None)
        return

    sym = (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?"
    url = p.get("url") or p.get("pairLink") or ""

    # WARMUP: alive but still cooking; log but DO NOT reject or cooldown
    if status == "warmup":
        await journal_near(session, p, verdict, status="warmup")
        return

    # PASS: full candidate
    if status == "pass":
        seen_pairs[addr] = now_ts
        await journal_candidate(session, p, verdict)
        await confirm_and_notify(session, p, provisional_allowed=False)
        builder_watch.pop(addr, None)
        near_watch.pop(addr, None)
        return

    exp = now_ts + WATCHLIST_WINDOW_SECONDS

    # NEAR: bigger LP
    if status == "near":
        await journal_near(session, p, verdict, status="near")
        near_watch[addr] = exp
        if now_ts - notify_last.get(addr, 0) > NOTIFY_COOLDOWN_MIN * 60:
            await telegram_send(
                session,
                f"🟨 Near-miss: {sym} LP≈${int(verdict['lp']):,} needs ≥ ${int(MIN_LIQ_USD):,}\n{url}"
            )
            notify_last[addr] = now_ts
        return

    # BUILDER: early entries band
    if status == "builder":
        await journal_near(session, p, verdict, status="builder")
        builder_watch[addr] = exp
        if now_ts - notify_last.get(addr, 0) > NOTIFY_COOLDOWN_MIN * 60:
            await telegram_send(
                session,
                f"🟧 Builder: {sym} LP≈${int(verdict['lp']):,}\n{url}"
            )
            notify_last[addr] = now_ts
        return

# ===================== Telegram loop ==========================
async def telegram_get_updates(session: httpx.AsyncClient, offset: int) -> list:
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = await session.get(url, params={"timeout": 20, "offset": offset}, timeout=25)
        data = r.json() if r.status_code < 300 else {}
        return data.get("result", [])
    except Exception as e:
        print(f"[tg] getUpdates error: {e}")
        return []

LAST_UPDATE_ID = 0

def _extract_mint(text: str) -> Optional[str]:
    text = (text or "").strip()
    m = re.search(r"dexscreener\.com/solana/([A-Za-z0-9]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", text)
    return m.group(0) if m else None

async def handle_telegram_updates(session: httpx.AsyncClient):
    global LAST_UPDATE_ID
    while True:
        updates = await telegram_get_updates(session, LAST_UPDATE_ID)
        for u in updates:
            try:
                LAST_UPDATE_ID = max(LAST_UPDATE_ID, int(u.get("update_id", 0)) + 1)
                msg = u.get("message") or u.get("edited_message") or {}
                chat = msg.get("chat", {}) or {}
                chat_id = chat.get("id")
                text = (msg.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
                    continue

                low = text.lower()
                if low.startswith("/start"):
                    await telegram_send(
                        session,
                        "👋 Early Bird / Bird Drop online (Dex engine).\n"
                        f"Sheet: {GOOGLE_SHEET_URL or 'not set'}\n"
                        "Commands: /ping /dna /check <mint-or-url> /help"
                    )
                elif low.startswith("/ping"):
                    await telegram_send(session, f"🏓 pong — {utcnow_iso()}")
                elif low.startswith("/help"):
                    await telegram_send(session, "Commands: /ping /dna /check <mint-or-url> /help")
                elif low.startswith("/dna"):
                    await telegram_send(
                        session,
                        "🧬 Early Bird DNA:\n"
                        f"- LP floor (full pass): ${int(MIN_LIQ_USD):,} (promotion @{int(PROMOTION_LP):,})\n"
                        f"- Builder band:  ${int(BUILDER_MIN_LP):,}–${int(BUILDER_MAX_LP):,}\n"
                        f"- Near band:     ${int(NEAR_MIN_LP):,}–${int(NEAR_MAX_LP):,}\n"
                        f"- FDV cap:       ${int(MAX_FDV_USD):,} (hard cap≈{int(FDV_HARD_CAP_USD):,})\n"
                        f"- Dust LP:       < ${int(DUST_LP_USD):,} & no tx\n"
                        f"- Age ≤ {int(AGE_MAX_MINUTES/60)}h (old-dead>{int(OLD_MAX_AGE_MINUTES/60)}h)\n"
                        f"- 5m txns: young≥{MIN_TXNS_5M_YOUNG}, old≥{MIN_TXNS_5M_OLD}\n"
                        f"- Cycle: {DEX_CYCLE_SECONDS}s (Dex engine; BirdEye optional confirm)"
                    )
                elif low.startswith("/check"):
                    parts = text.split(maxsplit=1)
                    if len(parts) == 1:
                        await telegram_send(session, "Usage: /check <mint-or-dexscreener-url>")
                        continue
                    mint = _extract_mint(parts[1])
                    if not mint:
                        await telegram_send(session, "⚠️ /check needs a Solana mint or DexScreener URL")
                        continue

                    p = await dexscreener_search_by_mint(session, mint)
                    if not p:
                        await telegram_send(session, "❔ No pairs found for that mint yet.")
                        continue

                    sym = (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?"
                    lp  = _lp_usd(p)
                    fdv = _fdv_usd(p)
                    age = _pair_age_minutes(p)
                    m5  = _txns_m5(p)
                    url = p.get("url") or p.get("pairLink") or f"https://dexscreener.com/solana/{mint}"

                    await telegram_send(
                        session,
                        f"🔎 Scan {sym}: LP=${int(lp):,} | FDV=${int(fdv):,} | age={age:.1f}m | 5m txns={m5}"
                    )

                    v = dna_verdict(p)
                    await telegram_send(session, f"🧪 DNA verdict: {v['status']} (why: {v['why']})")

                    addr = _addr_of(p) or mint

                    row = {
                        "ts_check": utcnow_iso(),
                        "pair_address": addr,
                        "symbol": sym,
                        "fdv": fdv,
                        "lp": lp,
                        "age_min": round(age, 2),
                        "tx5": m5,
                        "verdict": v["status"],
                        "why": v["why"],
                        "url": url,
                    }
                    if _changed("ManualChecks", addr, row):
                        await sheet_append(session, "ManualChecks", row)

                    if v["status"] == "pass":
                        await journal_candidate(session, p, v)
                        await manual_confirm_with_retry(session, p)
                    elif v["status"] in ("near", "builder", "warmup"):
                        await journal_near(session, p, v, status=v["status"])
                        await confirm_and_notify(session, p, provisional_allowed=False)
                    else:
                        reject_memory[addr] = time.time() + REJECT_MEMORY_MINUTES * 60

            except Exception as e:
                print(f"[tg] handle error: {e}")
                continue
        await asyncio.sleep(2)

# ===================== Discovery loop =========================
async def discovery_cycle():
    async with httpx.AsyncClient(timeout=30) as session:
        await start_health_server()
        asyncio.create_task(handle_telegram_updates(session))
        await telegram_send(
            session,
            f"✅ Early Bird / Bird Drop live @ {utcnow_iso()}\nSheet: {GOOGLE_SHEET_URL or 'not set'}"
        )
        backoff = 0
        loop_count = 0
        while True:
            cycle_start = time.time()
            try:
                loop_count += 1

                if DEX_JITTER_MAX_SECONDS > 0:
                    await asyncio.sleep(random.uniform(0, DEX_JITTER_MAX_SECONDS))

                pairs = best_by_token(await dexscreener_latest(session))
                print(f"[loop] {utcnow_iso()} tokens={len(pairs)} builders={len(builder_watch)} near={len(near_watch)}")

                for p in pairs:
                    await process_pair(session, p)

                if builder_watch or near_watch:
                    await asyncio.sleep(WATCHLIST_RECHECK_SECONDS)
                    still_builder, still_near = {}, {}

                    # Builders
                    for addr, exp in list(builder_watch.items()):
                        if time.time() > exp:
                            continue
                        bi = await birdeye_price_liq(session, addr)
                        lp_now = 0.0
                        if bi:
                            liq = bi.get("liquidity", {})
                            lp_now = (liq.get("usd") if isinstance(liq, dict) else liq) or 0
                            try:
                                lp_now = float(lp_now)
                            except Exception:
                                lp_now = 0.0
                        if lp_now >= MIN_LIQ_USD:
                            now_pairs = best_by_token(await dexscreener_latest(session))
                            for p2 in now_pairs:
                                if _addr_of(p2) == addr:
                                    await journal_candidate(session, p2, dna_verdict(p2))
                                    await confirm_and_notify(session, p2, provisional_allowed=False)
                                    break
                        else:
                            still_builder[addr] = exp

                    # Near
                    for addr, exp in list(near_watch.items()):
                        if time.time() > exp:
                            continue
                        bi = await birdeye_price_liq(session, addr)
                        lp_now = 0.0
                        if bi:
                            liq = bi.get("liquidity", {})
                            lp_now = (liq.get("usd") if isinstance(liq, dict) else liq) or 0
                            try:
                                lp_now = float(lp_now)
                            except Exception:
                                lp_now = 0.0
                        if lp_now >= MIN_LIQ_USD:
                            now_pairs = best_by_token(await dexscreener_latest(session))
                            for p2 in now_pairs:
                                if _addr_of(p2) == addr:
                                    await journal_candidate(session, p2, dna_verdict(p2))
                                    await confirm_and_notify(session, p2, provisional_allowed=False)
                                    break
                        else:
                            still_near[addr] = exp

                    builder_watch.clear(); builder_watch.update(still_builder)
                    near_watch.clear(); near_watch.update(still_near)

                backoff = 0
            except Exception as e:
                print(f"[loop] error: {e}")
                backoff = min(backoff + 1, 3)
                await asyncio.sleep(BACKOFF_BASE * backoff)

            elapsed = time.time() - cycle_start
            await asyncio.sleep(max(DEX_CYCLE_SECONDS - elapsed, 5))

# ===================== Entrypoint ==============================
def main():
    print("Starting Early Bird / Bird Drop — Dex engine with BirdEye fallback + Early Bird DNA.")
    print(f"Keys: BirdEye={'yes' if BIRDEYE_API_KEY else 'no'} | Helius={'yes' if HELIUS_API_KEY else 'no'}")
    print(f"Sheet: {GOOGLE_SHEET_URL or 'not set'}  (webhook: {'yes' if SHEET_WEBHOOK_URL else 'no'})")
    print(f"Cycle: Dex={DEX_CYCLE_SECONDS}s | Jitter≤{DEX_JITTER_MAX_SECONDS}s | WatchRecheck={WATCHLIST_RECHECK_SECONDS}s | WatchWindow={WATCHLIST_WINDOW_SECONDS}s")
    print(f"DNA: LPfloor={MIN_LIQ_USD} | Builder={BUILDER_MIN_LP}-{BUILDER_MAX_LP} | Near={NEAR_MIN_LP}-{NEAR_MAX_LP} | FDVcap={MAX_FDV_USD} | Age≤{AGE_MAX_MINUTES}m")
    asyncio.run(discovery_cycle())

if __name__ == "__main__":
    main()
