# main.py — PTB v20.7 + Render friendly (no Updater, no APScheduler)
import os, time, logging
from datetime import datetime, timezone
from typing import Dict, Any, List

import httpx
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("strict-dna-bot")

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SEC", "15"))        # seconds
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "300"))  # seconds
HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "8.0"))
NEAR_MISS_WINDOW_SEC = int(os.getenv("NEAR_MISS_WINDOW_SEC", "60"))

if not BOT_TOKEN or not CHAT_ID:
    log.error("Missing BOT_TOKEN or TELEGRAM_CHAT_ID.")
    raise SystemExit(1)

# ---------- STRICT / ORIGINAL DNA (adjust as needed) ----------
DNA: Dict[str, Any] = {
    "min_liq_usd": 35_000,     # Liquidity ≥ $35k
    "max_fdv_usd": 600_000,    # FDV ≤ $600k
    "max_age_min": 360,        # Age ≤ 6 hours
    "min_vol1h_usd": 50_000,   # 1h volume ≥ $50k
    "min_m5_activity": 10,     # (buys + sells) over last 5m ≥ 10
    "m5_change_tol": -2.0,     # allow m5 change down to -2%
}

# ---------- RUNTIME STATE ----------
last_alert_ts: Dict[str, datetime] = {}    # ca -> last alert time (UTC)
near_miss: Dict[str, Any] = {}             # reserved for future
last_scan_info: Dict[str, Any] = {         # telemetry
    "ts": None,
    "duration_ms": 0,
    "pairs": 0,
    "hits": 0,
    "last_error": None,
}

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

# ---------- DATA FETCH (stub) ----------
async def fetch_pairs() -> List[Dict[str, Any]]:
    """
    TODO: Replace this stub with your real DexScreener call.
    Return a list of pair dicts with keys used by the DNA check.
    """
    # Example shape for later:
    # return [{
    #   "ca": "So1anaContractAddr",
    #   "liquidity": {"usd": 40000},
    #   "fdv": 580000,
    #   "volume": {"h1": 52000},
    #   "priceChange": {"m5": -1.3},
    #   "txns": {"m5": {"buys": 6, "sells": 6}},
    #   "pairCreatedAt": now_utc_ms() - 45*60*1000,
    # }]
    return []

# ---------- DNA CHECK ----------
def strict_dna_pass(p: Dict[str, Any]) -> (bool, str):
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

# ---------- SCAN LOOP ----------
async def scan_once(app: Application):
    """One scanner cycle: fetch pairs, run DNA, send alerts."""
    t0 = time.time()
    hits = 0
    try:
        pairs = await fetch_pairs()
        for p in pairs:
            ok, why = strict_dna_pass(p)
            if ok:
                hits += 1
                ca = p.get("ca") or "unknown"
                last = last_alert_ts.get(ca)
                if last and (datetime.now(timezone.utc) - last).total_seconds() < ALERT_COOLDOWN_SEC:
                    continue
                last_alert_ts[ca] = datetime.now(timezone.utc)

                await app.bot.send_message(chat_id=CHAT_ID, text=fmt_verdict(p, ok, why))

        last_scan_info.update({
            "ts": datetime.now(timezone.utc),
            "duration_ms": int((time.time() - t0) * 1000),
            "pairs": len(pairs),
            "hits": hits,
            "last_error": None,
        })
    except Exception as e:
        log.exception("scan_once error")
        last_scan_info.update({
            "ts": datetime.now(timezone.utc),
            "duration_ms": int((time.time() - t0) * 1000),
            "last_error": repr(e),
        })

# ---------- COMMANDS ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("DNA bot is online 🔬  Try /status or /scan")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = last_scan_info.copy()
    ts = info["ts"]
    when = ts.strftime("%Y-%m-%d %H:%M:%S UTC") if ts else "never"
    msg = (
        f"Last scan: {when}\n"
        f"Duration: {info['duration_ms']} ms\n"
        f"Pairs: {info.get('pairs', 0)} | Hits: {info.get('hits', 0)}\n"
        f"Last error: {info.get('last_error')}"
    )
    await update.message.reply_text(msg)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await scan_once(context.application)
    await update.message.reply_text("Scan complete.")

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs = await fetch_pairs()
    if not pairs:
        await update.message.reply_text("No data.")
        return
    p = pairs[0]
    ok, why = strict_dna_pass(p)
    await update.message.reply_text(fmt_verdict(p, ok, why))

# job_queue callback (runs on the bot's event loop)
async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    await scan_once(context.application)

# ---------- MAIN ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("check", cmd_check))

    # periodic scan using PTB job_queue (no external scheduler needed)
    app.job_queue.run_repeating(
        scanner_job,
        interval=SCAN_INTERVAL,
        first=0,
        name="scanner",
    )

    log.info("Bot started (STRICT DNA + delivery hardened).")
    app.run_polling()

if __name__ == "__main__":
    main()

