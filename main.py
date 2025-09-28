# main.py — PTB v20.7 + DexScreener (Solana) discovery+tracking + backoff + keep-alive web
import os
import time
import asyncio
import random
import logging
from typing import Dict, Any, List, Tuple
from datetime import datetime, timezone

import httpx
from httpx import HTTPStatusError
from aiohttp import web

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("strict-dna-bot")

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# scanning cadence
DISCOVERY_INTERVAL = int(os.getenv("DISCOVERY_INTERVAL_SEC", "60"))  # broad search
TRACK_INTERVAL     = int(os.getenv("TRACK_INTERVAL_SEC", "15"))      # re-check watchlist
HTTP_TIMEOUT_SEC   = float(os.getenv("HTTP_TIMEOUT_SEC", "8.0"))

# rate-limit knobs (local client-side)
MIN_GAP_SEARCH = int(os.getenv("MIN_GAP_SEARCH_SEC", "45"))     # min seconds between search calls
MIN_GAP_TRACK  = int(os.getenv("MIN_GAP_TRACK_SEC", "10"))      # min seconds between each track call batch
BACKOFF_BASE   = int(os.getenv("BACKOFF_BASE_SEC", "60"))       # base backoff on 429
BACKOFF_MAX    = int(os.getenv("BACKOFF_MAX_SEC", "240"))       # cap backoff

# DNA thresholds (tweak anytime)
DNA: Dict[str, Any] = {
    "min_liq_usd": 35_000,   # Liquidity ≥ $35k
    "max_fdv_usd": 600_000,  # FDV ≤ $600k
    "max_age_min": 360,      # Age ≤ 6 hours
    "min_vol1h_usd": 50_000, # 1h volume ≥ $50k
    "min_m5_activity": 10,   # (buys+sells) last 5m ≥ 10
    "m5_change_tol": -2.0,   # allow m5 change down to -2%
}

if not BOT_TOKEN or not CHAT_ID:
    log.error("Missing BOT_TOKEN or TELEGRAM_CHAT_ID.")
    raise SystemExit(1)

# ---------- STATE ----------
last_alert_ts: Dict[str, datetime] = {}    # pairAddr -> last alert time (UTC)
alert_cooldown_sec = int(os.getenv("ALERT_COOLDOWN_SEC", "300"))

# telemetry
last_scan_info: Dict[str, Any] = {
    "ts": None, "duration_ms": 0, "pairs": 0, "hits": 0, "last_error": None
}

# discovery cache + watchlist
seen_pairs: set[str] = set()               # pairAddress you've seen (avoid reprocessing as "new")
watchlist: Dict[str, float] = {}           # pairAddress -> last_seen_ts (seconds); prunes if cold
watchlist_ttl = int(os.getenv("WATCHLIST_TTL_SEC", "1800"))  # drop if no updates for 30m

# per-endpoint throttling + adaptive backoff
_last_search_ts = 0.0
_next_search_ok = 0.0
_backoff_search = BACKOFF_BASE

_last_track_ts = 0.0
_next_track_ok = 0.0
_backoff_track = BACKOFF_BASE

# ---------- HELPERS ----------
def now_utc_ms() -> int:
    return int(time.time() * 1000)

def minutes_since_ms(ms: int) -> float:
    if not ms:
        return 1e9
    return (now_utc_ms() - ms) / 60000.0

def fmt_usd(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except Exception:
        return str(x)

def dna_pass(p: Dict[str, Any]) -> Tuple[bool, str]:
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    fdv = p.get("fdv", 0) or 0
    vol1h = (p.get("volume") or {}).get("h1", 0) or 0
    m5 = (p.get("priceChange") or {}).get("m5", 0.0) or 0.0
    tx5 = (p.get("txns") or {}).get("m5", {}) or {}
    buys = tx5.get("buys", 0) or 0
    sells = tx5.get("sells", 0) or 0
    activity5 = buys + sells
    age_min = minutes_since_ms(p.get("pairCreatedAt", 0) or 0)

    if liq < DNA["min_liq_usd"]:
        return False, f"liq {liq}<{DNA['min_liq_usd']}"
    if fdv > DNA["max_fdv_usd"]:
        return False, f"fdv {fdv}>{DNA['max_fdv_usd']}"
    if age_min > DNA["max_age_min"]:
        return False, f"age {age_min:.1f}m>{DNA['max_age_min']}m"
    if vol1h < DNA["min_vol1h_usd"]:
        return False, f"1h vol {vol1h}<{DNA['min_vol1h_usd']}"
    if activity5 < DNA["min_m5_activity"]:
        return False, f"m5 activity {activity5}<{DNA['min_m5_activity']}"
    if m5 < DNA["m5_change_tol"]:
        return False, f"m5 Δ {m5}%<{DNA['m5_change_tol']}%"
    return True, "OK"

def fmt_verdict(p: Dict[str, Any], ok: bool, why: str) -> str:
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    fdv = p.get("fdv", 0) or 0
    vol1h = (p.get("volume") or {}).get("h1", 0) or 0
    m5 = (p.get("priceChange") or {}).get("m5", 0.0) or 0.0
    tx5 = (p.get("txns") or {}).get("m5", {}) or {}
    buys = tx5.get("buys", 0) or 0
    sells = tx5.get("sells", 0) or 0
    activity5 = buys + sells
    age_min = minutes_since_ms(p.get("pairCreatedAt", 0) or 0)
    verdict = "PASS ✅" if ok else "FAIL ❌"
    return (
        f"{verdict} — {why}\n"
        f"FDV {fmt_usd(fdv)} | Liq {fmt_usd(liq)} | 1h Vol {fmt_usd(vol1h)}\n"
        f"m5 Δ {m5:.1f}% | m5 activity {activity5} | age {age_min:.0f}m"
    )

# ---------- DEXSCREENER CALLS ----------
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q=solana"
PAIR_URL   = "https://api.dexscreener.com/latest/dex/pairs/solana/{pair}"

async def _guard_gap(now: float, last_ts: float, min_gap: int):
    gap = now - last_ts
    jitter = random.uniform(0, 3)
    need = min_gap + jitter
    if gap < need:
        await asyncio.sleep(need - gap)

async def fetch_search() -> List[Dict[str, Any]]:
    """Discovery: pull recent Solana pairs via search."""
    global _last_search_ts, _next_search_ok, _backoff_search
    now = time.time()
    if now < _next_search_ok:
        log.info("Discovery backoff %.0fs left; skipping search", _next_search_ok - now)
        return []
    await _guard_gap(now, _last_search_ts, MIN_GAP_SEARCH)
    try:
        headers = {"User-Agent": "dna-bot/1.0 (contact: telegram)"}
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC, headers=headers) as client:
            r = await client.get(SEARCH_URL)
            r.raise_for_status()
            data = r.json() or {}
            raw = data.get("pairs", []) or []
        _last_search_ts = time.time()
        _backoff_search = BACKOFF_BASE  # success: reset backoff
        out = []
        for it in raw:
            if (it.get("chainId") or "").lower() != "solana":
                continue
            base = (it.get("baseToken") or {})
            ca = base.get("address") or it.get("pairAddress") or ""
            out.append({
                "pairAddress": it.get("pairAddress") or "",
                "ca": ca,
                "liquidity": {"usd": (it.get("liquidity") or {}).get("usd") or 0},
                "fdv": it.get("fdv") or 0,
                "volume": {"h1": (it.get("volume") or {}).get("h1") or 0},
                "priceChange": {"m5": (it.get("priceChange") or {}).get("m5") or 0},
                "txns": {"m5": (it.get("txns") or {}).get("m5") or {"buys": 0, "sells": 0}},
                "pairCreatedAt": it.get("pairCreatedAt") or now_utc_ms(),
            })
        return out
    except HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 429:
            _backoff_search = min(_backoff_search * 2, BACKOFF_MAX)
            _next_search_ok = time.time() + _backoff_search
            log.warning("DexScreener 429 on search. Backing off %.0fs", _backoff_search)
            return []
        log.exception("search HTTP error")
        return []
    except Exception:
        log.exception("search error")
        return []

async def fetch_pair(pair_addr: str) -> Dict[str, Any] | None:
    """Tracking: re-check a specific pair by address."""
    global _last_track_ts, _next_track_ok, _backoff_track
    now = time.time()
    if now < _next_track_ok:
        log.info("Track backoff %.0fs left; skipping track", _next_track_ok - now)
        return None
    # throttle by batch—not per pair—to avoid bursts
    await _guard_gap(now, _last_track_ts, MIN_GAP_TRACK)
    try:
        url = PAIR_URL.format(pair=pair_addr)
        headers = {"User-Agent": "dna-bot/1.0 (contact: telegram)"}
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC, headers=headers) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json() or {}
            pairs = data.get("pairs", []) or []
        _last_track_ts = time.time()
        _backoff_track = BACKOFF_BASE  # success: reset backoff
        if not pairs:
            return None
        it = pairs[0]
        if (it.get("chainId") or "").lower() != "solana":
            return None
        base = (it.get("baseToken") or {})
        ca = base.get("address") or it.get("pairAddress") or ""
        return {
            "pairAddress": it.get("pairAddress") or "",
            "ca": ca,
            "liquidity": {"usd": (it.get("liquidity") or {}).get("usd") or 0},
            "fdv": it.get("fdv") or 0,
            "volume": {"h1": (it.get("volume") or {}).get("h1") or 0},
            "priceChange": {"m5": (it.get("priceChange") or {}).get("m5") or 0},
            "txns": {"m5": (it.get("txns") or {}).get("m5") or {"buys": 0, "sells": 0}},
            "pairCreatedAt": it.get("pairCreatedAt") or now_utc_ms(),
        }
    except HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 429:
            _backoff_track = min(_backoff_track * 2, BACKOFF_MAX)
            _next_track_ok = time.time() + _backoff_track
            log.warning("DexScreener 429 on track. Backing off %.0fs", _backoff_track)
            return None
        log.exception("track HTTP error")
        return None
    except Exception:
        log.exception("track error")
        return None

# ---------- SCANNERS ----------
async def discovery_cycle(app: Application):
    """Find new pairs, push to watchlist, send alerts if they pass DNA."""
    t0 = time.time()
    hits = 0
    pairs_checked = 0
    try:
        fresh = await fetch_search()
        pairs_checked = len(fresh)
        # newest first
        fresh.sort(key=lambda x: x.get("pairCreatedAt", 0), reverse=True)

        now_s = time.time()
        for p in fresh:
            addr = p.get("pairAddress") or ""
            if not addr:
                continue
            # mark seen & add to watchlist
            seen_pairs.add(addr)
            watchlist[addr] = now_s

            ok, why = dna_pass(p)
            if ok:
                hits += 1
                last = last_alert_ts.get(addr)
                if last and (datetime.now(timezone.utc) - last).total_seconds() < alert_cooldown_sec:
                    continue
                last_alert_ts[addr] = datetime.now(timezone.utc)
                await app.bot.send_message(chat_id=CHAT_ID, text=fmt_verdict(p, ok, why))

        # prune cold watchlist
        cutoff = now_s - watchlist_ttl
        for addr in list(watchlist.keys()):
            if watchlist[addr] < cutoff:
                watchlist.pop(addr, None)

        last_scan_info.update({
            "ts": datetime.now(timezone.utc),
            "duration_ms": int((time.time() - t0) * 1000),
            "pairs": pairs_checked,
            "hits": hits,
            "last_error": None,
            "mode": "discovery",
            "watchlist_size": len(watchlist),
        })
    except Exception as e:
        log.exception("discovery error")
        last_scan_info.update({
            "ts": datetime.now(timezone.utc),
            "duration_ms": int((time.time() - t0) * 1000),
            "last_error": repr(e),
            "mode": "discovery",
        })

async def tracking_cycle(app: Application):
    """Re-check watchlisted pairs more frequently, alert on passes."""
    if not watchlist:
        return
    t0 = time.time()
    hits = 0
    checked = 0
    now_s = time.time()
    try:
        # check a small batch each cycle (avoid large bursts)
        batch = list(watchlist.keys())[:10]  # tune as needed
        for addr in batch:
            p = await fetch_pair(addr)
            if not p:
                continue
            checked += 1
            watchlist[addr] = now_s  # refresh “hotness”
            ok, why = dna_pass(p)
            if ok:
                hits += 1
                last = last_alert_ts.get(addr)
                if last and (datetime.now(timezone.utc) - last).total_seconds() < alert_cooldown_sec:
                    continue
                last_alert_ts[addr] = datetime.now(timezone.utc)
                await app.bot.send_message(chat_id=CHAT_ID, text=fmt_verdict(p, ok, why))

        last_scan_info.update({
            "ts": datetime.now(timezone.utc),
            "duration_ms": int((time.time() - t0) * 1000),
            "pairs": checked,
            "hits": hits,
            "last_error": None,
            "mode": "tracking",
            "watchlist_size": len(watchlist),
        })
    except Exception as e:
        log.exception("tracking error")
        last_scan_info.update({
            "ts": datetime.now(timezone.utc),
            "duration_ms": int((time.time() - t0) * 1000),
            "last_error": repr(e),
            "mode": "tracking",
        })

# ---------- TELEGRAM COMMANDS ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "DNA bot is online 🔬\n"
        "Commands: /status /scan\n"
        "Running discovery + tracking on Solana."
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = last_scan_info.copy()
    ts = info.get("ts")
    when = ts.strftime("%Y-%m-%d %H:%M:%S UTC") if ts else "never"
    msg = (
        f"Last scan: {when}\n"
        f"Mode: {info.get('mode')}\n"
        f"Duration: {info.get('duration_ms', 0)} ms\n"
        f"Pairs: {info.get('pairs', 0)} | Hits: {info.get('hits', 0)}\n"
        f"Watchlist: {info.get('watchlist_size', 0)}\n"
        f"Last error: {info.get('last_error')}"
    )
    await update.message.reply_text(msg)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # run one discovery + one tracking pass on-demand
    await discovery_cycle(context.application)
    await tracking_cycle(context.application)
    await update.message.reply_text("Scan complete.")

# ---------- KEEP-ALIVE WEB (for Render) ----------
# Expose a tiny HTTP server so Render sees a port and UptimeRobot can ping.
async def healthz(_):
    return web.Response(text="ok")

async def start_web_app():
    app = web.Application()
    app.add_routes([web.get("/", healthz), web.get("/healthz", healthz)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))  # Render sets PORT
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Keep-alive web server listening on :%s", port)

# ---------- MAIN ----------
def main():
    tg = Application.builder().token(BOT_TOKEN).build()

    tg.add_handler(CommandHandler("start", cmd_start))
    tg.add_handler(CommandHandler("status", cmd_status))
    tg.add_handler(CommandHandler("scan", cmd_scan))

    # schedule discovery + tracking
    tg.job_queue.run_repeating(lambda c: discovery_cycle(c.application),
                               interval=DISCOVERY_INTERVAL, first=0, name="discovery")
    tg.job_queue.run_repeating(lambda c: tracking_cycle(c.application),
                               interval=TRACK_INTERVAL, first=10, name="tracking")

    # start keep-alive web server inside the same asyncio loop
    tg.create_task(start_web_app())

    log.info("Bot started (Solana discovery+tracking, backoff, keep-alive web).")
    tg.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
