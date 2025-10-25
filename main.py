# main.py
# MemeBot (Tytty + Follow-up) — FULL + Commands + Render Keep-Alive
# - Builders / Near-miss bands with 15s rechecks for 5m
# - Promotion to >=$20k LP -> BirdEye + Helius -> Strong Ding
# - Holder safety: Top1<=25%, Top5<=45% (warn -> Watchlist)
# - Google Sheets journaling: NearMisses, Candidates, Dings
# - Telegram commands: /start /ping /dna /help /scan <mint-or-url>
# - Render keep-alive web server (PORT)

import os
import re
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import httpx
from aiohttp import web  # keep-alive web server

# ========= Render Keep Alive =========
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

# Follow-up bands
BUILDER_MIN_LP = float(os.getenv("BUILDER_MIN_LP", "5000"))
BUILDER_MAX_LP = float(os.getenv("BUILDER_MAX_LP", "9000"))
NEAR_MIN_LP = float(os.getenv("NEAR_MIN_LP", "15000"))
NEAR_MAX_LP = float(os.getenv("NEAR_MAX_LP", "19900"))

AGE_MAX_MINUTES = float(os.getenv("AGE_MAX_MINUTES", "4320"))  # 72h
MIN_TXNS_5M = int(os.getenv("MIN_TXNS_5M", "3"))

NEAR_WINDOW_SECONDS = float(os.getenv("NEAR_WINDOW_SECONDS", "300"))   # 5m
NEAR_RECHECK_SECONDS = float(os.getenv("NEAR_RECHECK_SECONDS", "15"))  # 15s

# Holder safety (your latest)
TOP1_HOLDER_MAX_PCT = float(os.getenv("TOP1_HOLDER_MAX_PCT", "25.0"))
TOP5_HOLDER_MAX_PCT = float(os.getenv("TOP5_HOLDER_MAX_PCT", "45.0"))
REQUIRE_HELIUS_OK = os.getenv("SAFETY_REQUIRE_HELIUS_OK", "false").lower() == "true"

# ========= GLOBALS =========
seen_pairs: Dict[str, float] = {}
candidates: Dict[str, float] = {}
near_watch: Dict[str, float] = {}       # token_addr -> expire_ts
builder_watch: Dict[str, float] = {}    # token_addr -> expire_ts
BACKOFF_BASE = 20

# Telegram state
LAST_UPDATE_ID = 0

# ========= UTILS =========
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")

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

# --- FIXED: Unknown age returns None (don’t hard-reject) ---
def _pair_age_minutes(pair: Dict[str, Any]) -> Optional[float]:
    """
    Return age in minutes if we can compute it, otherwise None (unknown).
    """
    ts = pair.get("creationTime") or pair.get("pairCreatedAt")
    try:
        ts = int(ts) if ts else 0
        if ts > 10**12:  # ms -> s
            ts //= 1000
        if ts <= 0:
            return None  # unknown age
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return None  # unknown age

# --- FIXED: Unknown txns returns None (don’t hard-reject) ---
def _txns_m5(pair: Dict[str, Any]) -> Optional[int]:
    """
    Return 5-minute txn count if present; otherwise None (unknown).
    """
    m5 = (pair.get("txns") or {}).get("m5") or {}
    has_keys = ("buys" in m5) or ("sells" in m5)
    if not has_keys:
        return None  # unknown activity
    return int(m5.get("buys") or 0) + int(m5.get("sells") or 0)

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

# ========= DNA (with explanations) =========
def dna_pass_explain(pair: Dict[str, Any]) -> Dict[str, Any]:
    fdv = _fdv_usd(pair)
    liq = _liq_usd(pair)
    age_min = _pair_age_minutes(pair)   # may be None (unknown)
    tx5 = _txns_m5(pair)                # may be None (unknown)

    if fdv <= 0 or liq <= 0:
        return {"status": "reject", "why": "missing fdv/liq", "fdv": fdv, "liq": liq}

    # Only reject for age if we actually know the age
    if age_min is not None and age_min > AGE_MAX_MINUTES:
        return {"status": "reject", "why": f"too old {age_min:.1f}m>{AGE_MAX_MINUTES}m", "fdv": fdv, "liq": liq}

    # Only reject for low activity if we actually know the 5m tx count
    if tx5 is not None and tx5 < MIN_TXNS_5M:
        return {"status": "reject", "why": f"low activity tx5={tx5}<{MIN_TXNS_5M}", "fdv": fdv, "liq": liq}

    if fdv <= MAX_FDV_USD and liq >= MIN_LIQ_USD:
        return {"status": "pass", "why": "meets floor", "fdv": fdv, "liq": liq}
    if fdv <= MAX_FDV_USD and NEAR_MIN_LP <= liq <= NEAR_MAX_LP:
        return {"status": "near", "why": "near-miss band", "fdv": fdv, "liq": liq}
    if BUILDER_MIN_LP <= liq <= BUILDER_MAX_LP:
        return {"status": "builder", "why": "early-builder band", "fdv": fdv, "liq": liq}

    return {"status": "reject", "why": "outside all bands", "fdv": fdv, "liq": liq}

# ========= Telegram: /scan helper =========
def _extract_mint(text: str) -> Optional[str]:
    text = text.strip()
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", text):
        return text
    m = re.search(r"dexscreener\.com/solana/([A-Za-z0-9]+)", text)
    if m:
        return m.group(1)
    return None

async def scan_one(session: httpx.AsyncClient, mint_or_url: str):
    mint = _extract_mint(mint_or_url)
    if not mint:
        await telegram_send(session, "⚠️ /scan needs a Solana mint or DexScreener URL.")
        return
    try:
        r = await session.get("https://api.dexscreener.com/latest/dex/search", params={"q": mint}, timeout=20)
        data = r.json() if r.status_code < 300 else {}
        pairs = data.get("pairs") or []
    except Exception as e:
        await telegram_send(session, f"⚠️ Dex error: {e}")
        return
    if not pairs:
        await telegram_send(session, "❔ No pairs found for that mint yet.")
        return
    # choose highest-LP pool
    p = max(pairs, key=lambda x: (_liq_usd(x) or 0))
    sym = (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?"
    liq = _liq_usd(p); fdv = _fdv_usd(p); age_m = _pair_age_minutes(p)
    age_s = f"{round(age_m,1)}m" if age_m is not None else "unknown"
    tx5 = _txns_m5(p); tx_s = tx5 if tx5 is not None else "unknown"
    await telegram_send(session, f"🔎 Scan {sym}: LP=${int(liq):,} | FDV=${int(fdv):,} | age={age_s} | 5m txns={tx_s}")
    verdict = dna_pass_explain(p)
    await telegram_send(session, f"🧪 DNA verdict: {verdict['status']} (why: {verdict['why']})")
    if verdict["status"] == "pass":
        await process_candidate(session, p)
    else:
        # optional: still show safety snapshot
        addr = _addr_of(p)
        bi = await birdeye_price_liq(session, addr) if addr else None
        hel = await helius_holder_safety(session, addr) if addr else None
        if bi:
            l = bi.get("liquidity", {})
            l = (l.get("usd") if isinstance(l, dict) else l) or 0
            await telegram_send(session, f"ℹ️ BirdEye LP≈${int(float(l)):,}")
        if hel:
            await telegram_send(session, f"ℹ️ Holders: Top1={hel.get('top1_pct')}% Top5={hel.get('top5_pct')}% risk={hel.get('risk')}")

# ========= CORE LOGIC =========
async def process_candidate(session: httpx.AsyncClient, pair: Dict[str, Any]):
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

    await sheet_append(session, "Candidates", {**row, "status": "candidate"})

# ========= Telegram command loop =========
async def telegram_command_loop(session: httpx.AsyncClient):
    global LAST_UPDATE_ID
    print("[tg] command loop started")
    while True:
        updates = await telegram_get_updates(session, LAST_UPDATE_ID)
        for u in updates:
            try:
                LAST_UPDATE_ID = max(LAST_UPDATE_ID, int(u.get("update_id", 0)) + 1)
                msg = u.get("message") or u.get("edited_message") or {}
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
                        "Commands: /ping /dna /scan <mint-or-url> /help")
                elif low.startswith("/ping"):
                    await telegram_send(session, f"🏓 pong — {utcnow_iso()}")
                elif low.startswith("/dna"):
                    await telegram_send(session,
                        "🧬 DNA settings:\n"
                        f"- LP floor: ${int(MIN_LIQ_USD):,}\n"
                        f"- FDV cap:  ${int(MAX_FDV_USD):,}\n"
                        f"- Near band: ${int(NEAR_MIN_LP):,}–${int(NEAR_MAX_LP):,}\n"
                        f"- Builder band: ${int(BUILDER_MIN_LP):,}–${int(BUILDER_MAX_LP):,}\n"
                        f"- Age ≤ {int(AGE_MAX_MINUTES/60)}h, 5m txns ≥ {MIN_TXNS_5M}\n"
                        f"- Holders: Top1 ≤ {TOP1_HOLDER_MAX_PCT}%, Top5 ≤ {TOP5_HOLDER_MAX_PCT}%")
                elif low.startswith("/help"):
                    await telegram_send(session, "Commands: /ping /dna /scan <mint-or-url> /help")
                elif low.startswith("/scan"):
                    parts = text.split(maxsplit=1)
                    if len(parts) == 2:
                        await scan_one(session, parts[1])
                    else:
                        await telegram_send(session, "Usage: /scan <mint-or-dexscreener-url>")
            except Exception as e:
                print(f"[tg] handle error: {e}")
                continue
        await asyncio.sleep(2)

# ========= DISCOVERY LOOP =========
async def discovery_cycle():
    # declare globals once, at the very top of the function
    global builder_watch, near_watch

    async with httpx.AsyncClient(timeout=30) as session:
        # start keep-alive web server once
        await start_health_server()

        # start command listener
        asyncio.create_task(telegram_command_loop(session))

        # Startup ping
        await telegram_send(session, f"✅ Bot live @ {utcnow_iso()}\nSheet: {GOOGLE_SHEET_URL or 'not set'}")
        backoff = 0

        while True:
            started = time.time()
            try:
                pairs = await dexscreener_latest(session)
                pairs = best_by_token(pairs)
                print(f"[dex] fetched {len(pairs)} items")
                now_ts = time.time()

                for p in pairs:
                    addr = _addr_of(p)
                    if not addr:
                        continue

                    last = seen_pairs.get(addr, 0)
                    if now_ts - last < COOLDOWN_MINUTES * 60:
                        continue

                    verdict = dna_pass_explain(p)
                    status = verdict["status"]

                    if status == "reject":
                        builder_watch.pop(addr, None)
                        near_watch.pop(addr, None)
                        continue

                    sym = (p.get("baseToken") or {}).get("symbol") or p.get("symbol") or "?"
                    url = p.get("url") or p.get("pairLink") or ""

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
                        await asyncio.sleep(2)
                        builder_watch.pop(addr, None)
                        near_watch.pop(addr, None)
                        continue

                    if status == "near":
                        exp = now_ts + NEAR_WINDOW_SECONDS
                        if addr not in near_watch:
                            near_watch[addr] = exp
                            await telegram_send(session, f"🟨 Near-miss: {sym} LP≈${int(verdict['liq']):,} (needs ≥ ${int(MIN_LIQ_USD):,})\nWatching ~{int(exp-now_ts)}s\n{url}")
                            await sheet_append(session, "NearMisses", {
                                "ts": utcnow_iso(), "pair_address": addr, "symbol": sym,
                                "fdv": verdict["fdv"], "lp": verdict["liq"], "status": "near", "url": url
                            })
                        else:
                            near_watch[addr] = exp
                        continue

                    if status == "builder":
                        exp = now_ts + NEAR_WINDOW_SECONDS
                        if addr not in builder_watch:
                            builder_watch[addr] = exp
                            await telegram_send(session, f"🟧 Early Builder: {sym} LP≈${int(verdict['liq']):,} | Active + New\nWatching ~{int(exp-now_ts)}s\n{url}")
                            await sheet_append(session, "NearMisses", {
                                "ts": utcnow_iso(), "pair_address": addr, "symbol": sym,
                                "fdv": verdict["fdv"], "lp": verdict["liq"], "status": "builder", "url": url
                            })
                        else:
                            builder_watch[addr] = exp
                        continue

                # follow-up rechecks
                if builder_watch or near_watch:
                    still_builder, still_near = {}, {}

                    # builders
                    for addr, exp in list(builder_watch.items()):
                        if time.time() > exp:
                            continue
                        bi = await birdeye_price_liq(session, addr)
                        liq_now = 0.0
                        if bi:
                            liq_obj = bi.get("liquidity", {})
                            liq_now = (liq_obj.get("usd") if isinstance(liq_obj, dict) else liq_obj) or 0
                            try:
                                liq_now = float(liq_now)
                            except:
                                liq_now = 0.0
                        if liq_now >= MIN_LIQ_USD:
                            await telegram_send(session, f"🟩 Promoted: LP now ≈ ${int(liq_now):,} (≥ ${int(MIN_LIQ_USD):,}) — validating…")
                            pairs_now = await dexscreener_latest(session)
                            pairs_now = best_by_token(pairs_now)
                            for p2 in pairs_now:
                                if _addr_of(p2) == addr:
                                    ts_now = time.time()
                                    seen_pairs[addr] = ts_now
                                    candidates[addr] = ts_now
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
                            liq_obj = bi.get("liquidity", {})
                            liq_now = (liq_obj.get("usd") if isinstance(liq_obj, dict) else liq_obj) or 0
                            try:
                                liq_now = float(liq_now)
                            except:
                                liq_now = 0.0
                        if liq_now >= MIN_LIQ_USD:
                            await telegram_send(session, f"🟩 Promoted: LP now ≈ ${int(liq_now):,} (≥ ${int(MIN_LIQ_USD):,}) — validating…")
                            pairs_now = await dexscreener_latest(session)
                            pairs_now = best_by_token(pairs_now)
                            for p2 in pairs_now:
                                if _addr_of(p2) == addr:
                                    ts_now = time.time()
                                    seen_pairs[addr] = ts_now
                                    candidates[addr] = ts_now
                                    await process_candidate(session, p2)
                                    await asyncio.sleep(2)
                                    break
                            continue
                        still_near[addr] = exp

                    builder_watch = still_builder
                    near_watch = still_near
                    await asyncio.sleep(NEAR_RECHECK_SECONDS)
                    continue

                # success path
                backoff = 0

            except Exception as e:
                print(f"[loop] error: {e}")
                backoff = min(backoff + 1, 3)
                sleep_for = BACKOFF_BASE * backoff
                print(f"[loop] backing off {sleep_for}s")
                await asyncio.sleep(sleep_for)

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
