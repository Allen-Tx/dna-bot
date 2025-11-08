# main.py — Bird Call (MemeBot vNext, BirdEye-first engine)
# Discovery via BirdEye • Telegram /check with journaling • Notify-once
# Watchlists • Holder safety via Helius • Render keep-alive

import os
import re
import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import httpx
from aiohttp import web

# ===================== Keep-alive (Render) =====================
async def _health_handle(_):
    return web.Response(text="ok")

async def start_health_server():
    port = int(os.getenv("PORT", 10000))
    app = web.Application()
    app.add_routes([web.get("/", _health_handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[health] running on :{port}")
# ===============================================================

# ===================== ENV & defaults ==========================
# Telegram
TELEGRAM_BOT_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID         = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Google Sheets webhook (Apps Script) + sheet url
GOOGLE_SHEET_URL         = os.getenv("GOOGLE_SHEET_URL", "").strip()
SHEET_WEBHOOK_URL        = os.getenv("SHEET_WEBHOOK_URL", "").strip()

# BirdEye / Helius
BIRDEYE_API_KEY          = os.getenv("BIRDEYE_API_KEY", "").strip()
HELIUS_API_KEY           = os.getenv("HELIUS_API_KEY", "").strip()

# Discovery pacing (BirdEye-first)
BIRDCALL_CYCLE_SECONDS   = int(os.getenv("BIRDCALL_CYCLE_SECONDS", "45"))   # main loop pace
BIRDEYE_CYCLE_SECONDS    = int(os.getenv("BIRDEYE_CYCLE_SECONDS", "30"))    # confirmers backoff
HELIUS_CYCLE_SECONDS     = int(os.getenv("HELIUS_CYCLE_SECONDS", "90"))

# Optional Dex backup (only if you want a secondary radar)
USE_DEX_BACKUP           = os.getenv("USE_DEX_BACKUP", "false").lower() == "true"
DEX_BACKUP_CYCLE_SECONDS = int(os.getenv("DEX_BACKUP_CYCLE_SECONDS", "120"))
BACKOFF_BASE             = int(os.getenv("BACKOFF_BASE", "20"))

# DNA thresholds (Tytty default; switch via env to Winner/Strict)
MIN_LIQ_USD              = float(os.getenv("MIN_LIQ_USD", "20000"))
MAX_FDV_USD              = float(os.getenv("MAX_FDV_USD", "600000"))
AGE_MAX_MINUTES          = float(os.getenv("AGE_MAX_MINUTES", "4320"))  # 72h

# Bands (builder / near ranges)
BUILDER_MIN_LP           = float(os.getenv("BUILDER_MIN_LP", "9000"))
BUILDER_MAX_LP           = float(os.getenv("BUILDER_MAX_LP", "15000"))
NEAR_MIN_LP              = float(os.getenv("NEAR_MIN_LP", "15000"))
NEAR_MAX_LP              = float(os.getenv("NEAR_MAX_LP", "24900"))
PROMOTION_LP             = float(os.getenv("PROMOTION_LP", "25000"))    # BirdEye confirm gate

# TX leniency
YOUNG_AGE_MINUTES        = float(os.getenv("YOUNG_AGE_MINUTES", "30"))
MIN_TXNS_5M_YOUNG        = int(os.getenv("MIN_TXNS_5M_YOUNG", "1"))
MIN_TXNS_5M_OLD          = int(os.getenv("MIN_TXNS_5M_OLD", "3"))

# De-dupe & notify pacing
COOLDOWN_MINUTES         = int(os.getenv("COOLDOWN_MINUTES", "15"))
NOTIFY_COOLDOWN_MIN      = int(os.getenv("NOTIFY_COOLDOWN_MIN", "15"))
REJECT_MEMORY_MINUTES    = int(os.getenv("REJECT_MEMORY_MINUTES", "180"))

# Watchlists recheck/window
WATCHLIST_RECHECK_SECONDS= float(os.getenv("WATCHLIST_RECHECK_SECONDS", "60"))
WATCHLIST_WINDOW_SECONDS = float(os.getenv("WATCHLIST_WINDOW_SECONDS", "900"))

# Holder safety (Helius)
TOP1_HOLDER_MAX_PCT      = float(os.getenv("TOP1_HOLDER_MAX_PCT", "25.0"))
TOP5_HOLDER_MAX_PCT      = float(os.getenv("TOP5_HOLDER_MAX_PCT", "45.0"))
SAFETY_REQUIRE_HELIUS_OK = os.getenv("SAFETY_REQUIRE_HELIUS_OK", "false").lower() == "true"

# Journaling behavior
JOURNAL_ONLY_ON_CHANGE   = os.getenv("JOURNAL_ONLY_ON_CHANGE", "true").lower() == "true"

# Manual /check helpers
MANUAL_RETRY_SECONDS     = int(os.getenv("MANUAL_RETRY_SECONDS", "30"))
MANUAL_RETRY_WINDOW      = int(os.getenv("MANUAL_RETRY_WINDOW", "300"))
PROVISIONAL_DING         = os.getenv("PROVISIONAL_DING", "false").lower() == "true"

# ===================== Globals ================================
seen_pairs: Dict[str, float]   = {}
notify_last: Dict[str, float]  = {}
reject_memory: Dict[str, float]= {}
builder_watch: Dict[str, float]= {}
near_watch: Dict[str, float]   = {}

# ===================== Utils =================================
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")

def _addr_of(p: Dict[str, Any]) -> Optional[str]:
    return p.get("address") or (p.get("baseToken") or {}).get("address") or p.get("tokenAddress") or p.get("pairAddress")

def _fdv_usd(p: Dict[str, Any]) -> float:
    try:
        return float(p.get("fdv") or (p.get("info") or {}).get("fdv") or p.get("marketCap") or 0)
    except Exception:
        return 0.0

def _lp_usd(p: Dict[str, Any]) -> float:
    liq = p.get("liquidity")
    if isinstance(liq, dict):
        v = liq.get("usd")
    else:
        v = liq
    if v is None:
        liq2 = p.get("liquidityUsd") or (p.get("liquidity") or {}).get("usd")
        v = liq2
    try:
        return float(v or 0)
    except Exception:
        return 0.0

def _txns_m5(p: Dict[str, Any]) -> int:
    tx5 = p.get("tx5") or (p.get("txns") or {}).get("m5", {}).get("buys", 0) + (p.get("txns") or {}).get("m5", {}).get("sells", 0)
    try:
        return int(tx5 or 0)
    except Exception:
        return 0

def _pair_age_minutes(p: Dict[str, Any]) -> float:
    ts = p.get("createdAt") or p.get("creationTime") or p.get("pairCreatedAt")
    try:
        ts = int(ts) if ts else 0
        if ts <= 0:
            return 9e9
        if ts > 10**12:  # ms to s
            ts //= 1000
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

# ===================== IO helpers =============================
async def telegram_send(session: httpx.AsyncClient, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[tg] skip send: {text[:120]}")
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

_last_row_cache: Dict[Tuple[str,str], Dict[str,Any]] = {}

def _changed(tab: str, addr: str, row: Dict[str, Any]) -> bool:
    if not JOURNAL_ONLY_ON_CHANGE:
        return True
    key = (tab, addr or "?")
    prev = _last_row_cache.get(key)
    if prev is None or prev != row:
        _last_row_cache[key] = dict(row)
        return True
    return False

# ===================== BirdEye API lanes =======================
def _be_headers():
    return {"X-API-KEY": BIRDEYE_API_KEY, "accept": "application/json"}

async def birdeye_price_liq(session: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    if not BIRDEYE_API_KEY:
        return None
    url = "https://public-api.birdeye.so/defi/price"
    params = {"chain": "solana", "address": token_address, "include_liquidity": "true"}
    try:
        r = await session.get(url, headers=_be_headers(), params=params, timeout=20)
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

async def birdeye_overview(session: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    if not BIRDEYE_API_KEY:
        return None
    url = "https://public-api.birdeye.so/defi/token_overview"
    params = {"chain": "solana", "address": token_address}
    try:
        r = await session.get(url, headers=_be_headers(), params=params, timeout=20)
        if r.status_code in (429, 400):
            print("[birdeye] overview throttle; backoff 30s")
            await asyncio.sleep(BIRDEYE_CYCLE_SECONDS)
            return None
        if r.status_code >= 300:
            return None
        return r.json().get("data")
    except Exception:
        return None

# ======= BirdEye discovery (replacement for old new-tokens) ===
async def birdeye_new_tokens(session: httpx.AsyncClient, limit: int = 120) -> List[Dict[str, Any]]:
    """
    Discovery via token_list (newest first). Normalizes to:
    address, symbol, createdAt, url. Liquidity/FDV confirmed later via price API.
    """
    if not BIRDEYE_API_KEY:
        return []
    url = "https://public-api.birdeye.so/defi/token_list"
    params = {"chain": "solana", "sort_by": "createdAt", "sort_type": "desc", "limit": str(limit)}
    out: List[Dict[str, Any]] = []
    try:
        r = await session.get(url, headers=_be_headers(), params=params, timeout=25)
        if r.status_code in (429, 400):
            print("[birdeye] token_list throttle; backoff 30s")
            await asyncio.sleep(BIRDEYE_CYCLE_SECONDS)
            return []
        if r.status_code >= 300:
            print(f"[birdeye] token_list {r.status_code}")
            return []
        data = r.json().get("data") or []
        for t in data:
            mint = t.get("address") or t.get("mint") or t.get("tokenAddress") or t.get("mintAddress")
            sym  = t.get("symbol") or t.get("ticker") or "?"
            created = t.get("createdAt") or t.get("mintedAt") or t.get("creationTime") or 0
            if not mint:
                continue
            out.append({
                "address": mint,
                "symbol": sym,
                "createdAt": created,
                "url": f"https://birdeye.so/token/{mint}?chain=solana"
            })
        seen=set(); dedup=[]
        for p in out:
            a=p["address"]
            if a in seen: continue
            seen.add(a); dedup.append(p)
        return dedup
    except Exception as e:
        print(f"[birdeye] token_list error: {e}")
        return []

# ===================== Optional Dex backup =====================
async def dexscreener_backup(session: httpx.AsyncClient) -> List[Dict[str, Any]]:
    url = "https://api.dexscreener.com/latest/dex/pairs/solana"
    out: List[Dict[str, Any]] = []
    for attempt in range(3):
        try:
            r = await session.get(url, timeout=20)
            if r.status_code == 429:
                wait = BACKOFF_BASE * (attempt + 1)
                print(f"[dex] 429 backoff {wait}s")
                await asyncio.sleep(wait)
                continue
            data = r.json()
            pairs = data.get("pairs") or []
            for p in pairs:
                addr = (p.get("baseToken") or {}).get("address")
                mapped = {
                    "address": addr,
                    "symbol": (p.get("baseToken") or {}).get("symbol") or p.get("symbol"),
                    "liquidity": (p.get("liquidity") or {}),
                    "fdv": p.get("fdv") or p.get("marketCap"),
                    "createdAt": p.get("pairCreatedAt") or p.get("creationTime"),
                    "url": p.get("url")
                }
                out.append(mapped)
            break
        except Exception as e:
            print(f"[dex] error: {e}")
            await asyncio.sleep(1.5)
    seen=set(); dedup=[]
    for p in out:
        a=p.get("address")
        if not a or a in seen: continue
        seen.add(a); dedup.append(p)
    return dedup

# ===================== Helius holder safety ====================
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

# ===================== DNA & verdicts =========================
def _meets_tx_gate(age_min: float, m5: Optional[int]) -> bool:
    if m5 is None:
        return True if age_min <= YOUNG_AGE_MINUTES else True
    if age_min <= YOUNG_AGE_MINUTES:
        return m5 >= MIN_TXNS_5M_YOUNG
    return m5 >= MIN_TXNS_5M_OLD

def dna_verdict(p: Dict[str, Any]) -> Dict[str, Any]:
    fdv = _fdv_usd(p)
    lp  = _lp_usd(p)
    age = _pair_age_minutes(p)
    m5  = p.get("tx5") if "tx5" in p or "txns" in p else None

    if fdv <= 0 or lp <= 0:
        return {"status": "reject", "why": "missing fdv/liq", "fdv": fdv, "lp": lp, "age": age, "m5": m5 or 0}
    if age > AGE_MAX_MINUTES:
        return {"status": "reject", "why": f"too old {age:.1f}m>{AGE_MAX_MINUTES}m", "fdv": fdv, "lp": lp, "age": age, "m5": m5 or 0}
    if not _meets_tx_gate(age, m5):
        req = MIN_TXNS_5M_YOUNG if age <= YOUNG_AGE_MINUTES else MIN_TXNS_5M_OLD
        return {"status": "reject", "why": f"low activity tx5={m5}<{req}", "fdv": fdv, "lp": lp, "age": age, "m5": m5 or 0}

    if fdv <= MAX_FDV_USD and lp >= MIN_LIQ_USD:
        return {"status": "pass", "why": "meets floor", "fdv": fdv, "lp": lp, "age": age, "m5": m5 or 0}
    if fdv <= MAX_FDV_USD and NEAR_MIN_LP <= lp <= NEAR_MAX_LP:
        return {"status": "near", "why": "near band", "fdv": fdv, "lp": lp, "age": age, "m5": m5 or 0}
    if BUILDER_MIN_LP <= lp <= BUILDER_MAX_LP:
        return {"status": "builder", "why": "builder band", "fdv": fdv, "lp": lp, "age": age, "m5": m5 or 0}

    return {"status": "reject", "why": "outside bands", "fdv": fdv, "lp": lp, "age": age, "m5": m5 or 0}

# ===================== Journal helpers ========================
async def journal_candidate(session: httpx.AsyncClient, p: Dict[str, Any], verdict: Dict[str, Any]):
    addr = _addr_of(p) or "?"
    row = {
        "ts_detected": utcnow_iso(),
        "pair_address": addr,
        "symbol": p.get("symbol") or (p.get("baseToken") or {}).get("symbol") or "?",
        "chain": "solana",
        "dex_url": p.get("url") or f"https://birdeye.so/token/{addr}?chain=solana",
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
        "symbol": p.get("symbol") or "?",
        "fdv": verdict.get("fdv"),
        "lp": verdict.get("lp"),
        "status": status,
        "url": p.get("url") or f"https://birdeye.so/token/{addr}?chain=solana",
    }
    if _changed("NearMisses", addr, row):
        await sheet_append(session, "NearMisses", row)

async def journal_ding(session: httpx.AsyncClient, p: Dict[str, Any], tier: str, liq_at_ding: Any, safety: str):
    addr = _addr_of(p) or "?"
    row = {
        "ts_ding": utcnow_iso(),
        "pair_address": addr,
        "symbol": p.get("symbol") or "?",
        "ding_tier": tier,
        "mc_at_ding": p.get("fdv") or _fdv_usd(p),
        "liq_at_ding": liq_at_ding,
        "dna_flavor": "TTYL",
        "safety_snapshot": safety,
        "dex_url": p.get("url") or f"https://birdeye.so/token/{addr}?chain=solana",
    }
    if _changed("Dings", addr, row):
        await sheet_append(session, "Dings", row)

# ===================== Confirmers & notify-once ================
async def confirm_and_notify(session: httpx.AsyncClient, p: Dict[str, Any], provisional_allowed: bool = False):
    addr = _addr_of(p) or "?"
    now = time.time()

    if now - notify_last.get(addr, 0) < NOTIFY_COOLDOWN_MIN * 60:
        return

    lp_now = _lp_usd(p)
    if lp_now < PROMOTION_LP and not provisional_allowed:
        return

    bi = await birdeye_price_liq(session, addr)
    hel = await helius_holder_safety(session, addr)

    safety_ok = True
    top1 = top5 = None
    if hel:
        top1 = hel.get("top1_pct"); top5 = hel.get("top5_pct")
        safety_ok = (hel.get("risk") == "ok")
    safety_flag = "OK" if safety_ok else "WARN"

    if bi is None and provisional_allowed:
        await telegram_send(session, f"🧬 PROVISIONAL DING: {p.get('symbol') or '?'}\nLP≈${int(lp_now):,}\nhttps://birdeye.so/token/{addr}?chain=solana")
        await journal_ding(session, p, tier="Provisional", liq_at_ding=lp_now, safety=safety_flag)
        notify_last[addr] = now
        return

    if bi is None:
        return

    liq_field = bi.get("liquidity", {})
    liq_usd = (liq_field.get("usd") if isinstance(liq_field, dict) else liq_field) or lp_now

    if SAFETY_REQUIRE_HELIUS_OK and not safety_ok:
        await telegram_send(session, f"👀 Watchlist (holder risk): {p.get('symbol') or '?'}\nTop1:{top1}% Top5:{top5}%\nhttps://birdeye.so/token/{addr}?chain=solana")
        return

    sym = p.get("symbol") or "?"
    parts = [f"🧬 STRONG DING: {sym}", f"LP≈${int(float(liq_usd)):,}"]
    if top1 is not None and top5 is not None:
        parts.append(f"Top1:{top1}% Top5:{top5}% {safety_flag}")
    parts.append(f"https://birdeye.so/token/{addr}?chain=solana")
    await telegram_send(session, "\n".join(parts))
    await journal_ding(session, p, tier="Strong", liq_at_ding=liq_usd, safety=safety_flag)
    notify_last[addr] = now

# ===================== Telegram ================================
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
    text = text.strip()
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", text):
        return text
    m = re.search(r"(?:birdeye\.so/token|dexscreener\.com/solana)/([A-Za-z0-9]+)", text)
    return m.group(1) if m else None

async def journal_manual_check(session: httpx.AsyncClient, mint: str, snap: Dict[str, Any], verdict: Dict[str, Any]):
    addr = mint
    row = {
        "ts_check": utcnow_iso(),
        "pair_address": addr,
        "symbol": snap.get("symbol") or "?",
        "fdv": verdict.get("fdv"),
        "lp": verdict.get("lp"),
        "age_min": verdict.get("age"),
        "tx5": verdict.get("m5"),
        "verdict": verdict.get("status"),
        "why": verdict.get("why"),
        "url": f"https://birdeye.so/token/{addr}?chain=solana"
    }
    if _changed("ManualChecks", addr, row):
        await sheet_append(session, "ManualChecks", row)

async def manual_confirm_with_retry(session: httpx.AsyncClient, p: Dict[str, Any]):
    start = time.time()
    if PROVISIONAL_DING:
        await confirm_and_notify(session, p, provisional_allowed=True)
    while time.time() - start < MANUAL_RETRY_WINDOW:
        await confirm_and_notify(session, p, provisional_allowed=False)
        await asyncio.sleep(MANUAL_RETRY_SECONDS)

# ===================== Core processing ========================
async def process_token(session: httpx.AsyncClient, p: Dict[str, Any]):
    addr = _addr_of(p)
    if not addr:
        return
    now_ts = time.time()

    if now_ts - seen_pairs.get(addr, 0) < COOLDOWN_MINUTES * 60:
        return
    if now_ts < reject_memory.get(addr, 0):
        return

    bi = await birdeye_price_liq(session, addr)
    if bi:
        p = {
            **p,
            "liquidity": bi.get("liquidity"),
            "fdv": bi.get("fdv") or _fdv_usd(p),
            "price": bi.get("value") or bi.get("price")
        }

    verdict = dna_verdict(p)
    status = verdict.get("status")

    if status == "reject":
        reject_memory[addr] = now_ts + REJECT_MEMORY_MINUTES * 60
        builder_watch.pop(addr, None); near_watch.pop(addr, None)
        return

    sym = p.get("symbol") or "?"
    url = p.get("url") or f"https://birdeye.so/token/{addr}?chain=solana"

    if status == "pass":
        seen_pairs[addr] = now_ts
        await journal_candidate(session, p, verdict)
        await confirm_and_notify(session, p, provisional_allowed=False)
        builder_watch.pop(addr, None); near_watch.pop(addr, None)
        return

    exp = now_ts + WATCHLIST_WINDOW_SECONDS
    if status == "near":
        await journal_near(session, p, verdict, status="near")
        near_watch[addr] = exp
        if now_ts - notify_last.get(addr, 0) > NOTIFY_COOLDOWN_MIN * 60:
            await telegram_send(session, f"🟨 Near-miss: {sym} LP≈${int(verdict['lp']):,} needs ≥ ${int(MIN_LIQ_USD):,}\n{url}")
            notify_last[addr] = now_ts
        return

    if status == "builder":
        await journal_near(session, p, verdict, status="builder")
        builder_watch[addr] = exp
        if now_ts - notify_last.get(addr, 0) > NOTIFY_COOLDOWN_MIN * 60:
            await telegram_send(session, f"🟧 Builder: {sym} LP≈${int(verdict['lp']):,}\n{url}")
            notify_last[addr] = now_ts
        return

# ===================== Telegram loop ==========================
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
                    await telegram_send(session,
                        "👋 Bird Call online.\n"
                        f"Sheet: {GOOGLE_SHEET_URL or 'not set'}\n"
                        "Commands: /ping /dna /check <mint-or-url> /help")
                elif low.startswith("/ping"):
                    await telegram_send(session, f"🏓 pong — {utcnow_iso()}")
                elif low.startswith("/help"):
                    await telegram_send(session, "Commands: /ping /dna /check <mint-or-url> /help")
                elif low.startswith("/dna"):
                    await telegram_send(session,
                        "🦅 Bird Call DNA:\n"
                        f"- LP floor: ${int(MIN_LIQ_USD):,} (promotion @{int(PROMOTION_LP):,})\n"
                        f"- FDV cap:  ${int(MAX_FDV_USD):,}\n"
                        f"- Builder:  ${int(BUILDER_MIN_LP):,}–${int(BUILDER_MAX_LP):,}\n"
                        f"- Near:     ${int(NEAR_MIN_LP):,}–${int(NEAR_MAX_LP):,}\n"
                        f"- Age ≤ {int(AGE_MAX_MINUTES/60)}h (young≤{int(YOUNG_AGE_MINUTES)}m lenient)\n"
                        f"- 5m txns (if avail): young≥{MIN_TXNS_5M_YOUNG}, old≥{MIN_TXNS_5M_OLD}\n"
                        f"- Cycle: {BIRDCALL_CYCLE_SECONDS}s (BE confirm pace {BIRDEYE_CYCLE_SECONDS}s)")
                elif low.startswith("/check"):
                    parts = text.split(maxsplit=1)
                    if len(parts) == 1:
                        await telegram_send(session, "Usage: /check <mint-or-birdeye/dex url>")
                        continue
                    mint = _extract_mint(parts[1])
                    if not mint:
                        await telegram_send(session, "⚠️ /check needs a Solana mint or Birdeye/Dex URL")
                        continue
                    bi = await birdeye_price_liq(session, mint)
                    ov = await birdeye_overview(session, mint)
                    snap = {
                        "address": mint,
                        "symbol": (ov or {}).get("symbol") or (bi or {}).get("symbol") or "?",
                        "liquidity": (bi or {}).get("liquidity"),
                        "fdv": (bi or {}).get("fdv") or (ov or {}).get("fdv"),
                        "createdAt": (ov or {}).get("createdAt")
                    }
                    sym = snap.get("symbol") or "?"
                    fdv = _fdv_usd(snap); lp = _lp_usd(snap); age = _pair_age_minutes(snap)
                    await telegram_send(session, f"🔎 Scan {sym}: LP=${int(lp):,} | FDV=${int(fdv):,} | age={age:.1f}m")
                    v = dna_verdict(snap)
                    await telegram_send(session, f"🧪 DNA verdict: {v['status']} (why: {v['why']})")
                    await journal_manual_check(session, mint, snap, v)
                    if v["status"] == "pass":
                        await journal_candidate(session, snap, v)
                        await manual_confirm_with_retry(session, snap)
                    elif v["status"] in ("near", "builder"):
                        await journal_near(session, snap, v, status=v["status"])
                        await confirm_and_notify(session, snap, provisional_allowed=False)
                    else:
                        reject_memory[mint] = time.time() + REJECT_MEMORY_MINUTES * 60
            except Exception as e:
                print(f"[tg] handle error: {e}")
                continue
        await asyncio.sleep(2)

# ===================== Discovery loop =========================
async def discovery_cycle():
    async with httpx.AsyncClient(timeout=30) as session:
        await start_health_server()
        asyncio.create_task(handle_telegram_updates(session))
        await telegram_send(session, f"✅ Bird Call live @ {utcnow_iso()}\nSheet: {GOOGLE_SHEET_URL or 'not set'}")
        backoff = 0
        while True:
            cycle_start = time.time()
            try:
                tokens = await birdeye_new_tokens(session, limit=120)

                if USE_DEX_BACKUP and (int(cycle_start) % DEX_BACKUP_CYCLE_SECONDS < BIRDCALL_CYCLE_SECONDS):
                    tokens.extend(await dexscreener_backup(session))

                tokens = best_by_token(tokens)
                print(f"[loop] {utcnow_iso()} tokens={len(tokens)} builders={len(builder_watch)} near={len(near_watch)}")

                for t in tokens:
                    await process_token(session, t)

                if builder_watch or near_watch:
                    await asyncio.sleep(WATCHLIST_RECHECK_SECONDS)
                    still_builder, still_near = {}, {}

                    for addr, exp in list(builder_watch.items()):
                        if time.time() > exp:
                            continue
                        bi = await birdeye_price_liq(session, addr)
                        lp_now = 0.0
                        if bi:
                            liq = bi.get("liquidity", {})
                            lp_now = (liq.get("usd") if isinstance(liq, dict) else liq) or 0
                            try: lp_now = float(lp_now)
                            except: lp_now = 0.0
                        if lp_now >= MIN_LIQ_USD:
                            snap = {"address": addr, "symbol": (bi or {}).get("symbol") or "?", "liquidity": bi.get("liquidity") if bi else {"usd": lp_now}, "fdv": (bi or {}).get("fdv")}
                            await journal_candidate(session, snap, dna_verdict(snap))
                            await confirm_and_notify(session, snap, provisional_allowed=False)
                        else:
                            still_builder[addr] = exp

                    for addr, exp in list(near_watch.items()):
                        if time.time() > exp:
                            continue
                        bi = await birdeye_price_liq(session, addr)
                        lp_now = 0.0
                        if bi:
                            liq = bi.get("liquidity", {})
                            lp_now = (liq.get("usd") if isinstance(liq, dict) else liq) or 0
                            try: lp_now = float(lp_now)
                            except: lp_now = 0.0
                        if lp_now >= MIN_LIQ_USD:
                            snap = {"address": addr, "symbol": (bi or {}).get("symbol") or "?", "liquidity": bi.get("liquidity") if bi else {"usd": lp_now}, "fdv": (bi or {}).get("fdv")}
                            await journal_candidate(session, snap, dna_verdict(snap))
                            await confirm_and_notify(session, snap, provisional_allowed=False)
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
            await asyncio.sleep(max(BIRDCALL_CYCLE_SECONDS - elapsed, 5))

# ===================== Entrypoint ==============================
def main():
    print("Starting Bird Call (BirdEye-first)…")
    print(f"Keys: BirdEye={'yes' if BIRDEYE_API_KEY else 'no'} | Helius={'yes' if HELIUS_API_KEY else 'no'}")
    print(f"Sheet: {GOOGLE_SHEET_URL or 'not set'}  (webhook: {'yes' if SHEET_WEBHOOK_URL else 'no'})")
    print(f"Cycle: BirdCall={BIRDCALL_CYCLE_SECONDS}s | WatchRecheck={WATCHLIST_RECHECK_SECONDS}s | WatchWindow={WATCHLIST_WINDOW_SECONDS}s")
    print(f"DNA: LPfloor={MIN_LIQ_USD} | PromoLP={PROMOTION_LP} | FDVcap={MAX_FDV_USD} | Age≤{AGE_MAX_MINUTES}m | TX young/old={MIN_TXNS_5M_YOUNG}/{MIN_TXNS_5M_OLD}")
    asyncio.run(discovery_cycle())

if __name__ == "__main__":
    main()
