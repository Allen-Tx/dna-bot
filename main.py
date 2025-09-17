import os, asyncio, logging, math, time
from datetime import datetime, timezone
import httpx

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s: %(message)s")
log = logging.getLogger("strict-dna-bot")

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SEC", "15")) # faster scans
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "300"))
HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "8.0"))
NEAR_MISS_WINDOW_SEC = int(os.getenv("NEAR_MISS_WINDOW_SEC", "60")) # recheck near misses for 60s

if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing BOT_TOKEN or TELEGRAM_CHAT_ID.")
raise SystemExit(1)

# ---------- STRICT / ORIGINAL DNA (UNCHANGED) ----------
DNA = {
"min_liq_usd": 35_000, # Liquidity ≥ $35k
"max_fdv_usd": 600_000, # FDV ≤ $600k
"max_age_min": 360, # Age ≤ 6 hours
"min_vol1h_usd": 50_000, # 1h volume ≥ $50k
"min_m5_activity": 10, # (buys + sells) over last 5m ≥ 10
"m5_change_tol": -2.0, # allow m5 change down to -2%
}

# ---------- RUNTIME STATE ----------
last_alert_ts = {} # ca -> datetime
near_miss = {} # ca -> (pair, first_seen_utc)
last_scan_info = { # telemetry
"ts": None,
"duration_ms": 0,
"pairs": 0,
"hits": 0,
"last_error": None,
}

# ---------- HTTP CLIENT (pool + retries) ----------
def _client():
        limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
        transport = httpx.AsyncHTTPTransport(retries=3)
return httpx.AsyncClient(
timeout=HTTP_TIMEOUT_SEC,
headers={"Accept": "application/json"},
limits=limits,
transport=transport,
)

DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=solana"
TOKEN_API = "https://api.dexscreener.com/latest/dex/tokens/{}"

async def fetch_pairs():
        async with _client() as client:
                r = await client.get(DEX_SEARCH)
        r.raise_for_status()
return r.json().get("pairs", [])

def minutes_since_ms(ms_epoch: int) -> float:
        if not ms_epoch:
                return math.inf
created = datetime.fromtimestamp(ms_epoch/1000, tz=timezone.utc)
now = datetime.now(tz=timezone.utc)
return (now - created).total_seconds() / 60.0

def strict_dna_pass(p: dict) -> tuple[bool, str, bool]:
        """
Returns (passed, reason, near) where near=True marks a near-miss worth backfilling.
DNA checks (unchanged):
- Liquidity ≥ 35k
- FDV ≤ 600k
- Age ≤ 360 min
- 1h Volume ≥ 50k
- m5 activity (buys+sells) ≥ 10
- m5 Δ ≥ -2% tolerance
"""
liq = (p.get("liquidity") or {}).get("usd", 0) or 0
fdv = p.get("fdv", 0) or 0
vol1h = (p.get("volume") or {}).get("h1", 0) or 0
tx5 = (p.get("txns") or {}).get("m5") or {}
buys5 = tx5.get("buys", 0) or 0
sells5 = tx5.get("sells", 0) or 0
activity5 = buys5 + sells5
m5_change = (p.get("priceChange") or {}).get("m5", 0.0) or 0.0
age_min = minutes_since_ms(p.get("pairCreatedAt", 0) or 0)

# primary fails
if liq < DNA["min_liq_usd"]:
        return False, f"Low liq ${liq:,.0f} < ${DNA['min_liq_usd']:,.0f}", False
if fdv > DNA["max_fdv_usd"]:
        return False, f"FDV ${fdv:,.0f} > ${DNA['max_fdv_usd']:,.0f}", False
if age_min > DNA["max_age_min"]:
        return False, f"Too old {age_min:.0f}m > {DNA['max_age_min']}m", False
if vol1h < DNA["min_vol1h_usd"]:
        near = vol1h >= 0.8 * DNA["min_vol1h_usd"]
        return False, f"Low vol1h ${vol1h:,.0f}", near
if activity5 < DNA["min_m5_activity"]:
        near = activity5 >= max(1, int(0.8 * DNA["min_m5_activity"]))
        return False, f"Low m5 activity {activity5}", near
if m5_change < DNA["m5_change_tol"]:
        near = m5_change >= (DNA["m5_change_tol"] - 1.0)
        return False, f"Weak m5 {m5_change:.1f}%", near

return True, "DNA PASS", False

async def send_alert(app: Application, p: dict, why: str):
        base = (p.get("baseToken") or {}).get("symbol", "?")
        url = p.get("url", "")
        liq = (p.get("liquidity") or {}).get("usd", 0) or 0
        fdv = p.get("fdv", 0) or 0
        vol1h = (p.get("volume") or {}).get("h1", 0) or 0
        tx5 = (p.get("txns") or {}).get("m5") or {}
        activity5 = (tx5.get("buys", 0) or 0) + (tx5.get("sells", 0) or 0)
        m5_change = (p.get("priceChange") or {}).get("m5", 0.0) or 0.0
        age_min = minutes_since_ms(p.get("pairCreatedAt", 0) or 0)

text = (
f"🚨 DING (STRICT DNA) [{base}] — {why}\n"
f"FDV ${fdv:,.0f} | Liq ${liq:,.0f} | 1h Vol ${vol1h:,.0f}\n"
f"m5 Δ {m5_change:.1f}% | m5 activity {activity5} | age {age_min:.0f}m\n"
f"{url}"
)
await app.bot.send_message(chat_id=CHAT_ID, text=text)

async def scan_once(app: Application):
        to = time.perf_counter()
        last_scan_info["last_error"] = None
        hits = 0
try:
        pairs = await fetch_pairs()
        last_scan_info["pairs"] = len(pairs)
        now = datetime.now(tz=timezone.utc)

        # main sweep
        for p in pairs:
                ca = (p.get("baseToken") or {}).get("address", "")
                if not ca:
                        continue

                passed, why, near = strict_dna_pass(p)
                if passed:
                        last = last_alert_ts.get(ca)
                        if not last or (now - last).total_seconds() >= ALERT_COOLDOWN_SEC:
                                await send_alert(app, p, why)
                                last_alert_ts[ca] = now
                                hits += 1
                        continue

                        # track near-misses briefly
                        if near and ca not in near_miss:
                                near_miss[ca] = (p, now)

                        # backfill near-misses
                        to_delete = []
                        for ca, (p0, t_first) in near_miss.items():
                                if (now - t_first).total_seconds() > NEAR_MISS_WINDOW_SEC:
                                        to_delete.append(ca)
                        continue
                        passed, why, _ = strict_dna_pass(p0)
                        if passed:
                                last = last_alert_ts.get(ca)
                        if not last or (now - last).total_seconds() >= ALERT_COOLDOWN_SEC:
                                await send_alert(app, p0, f"Backfill: {why}")
                                last_alert_ts[ca] = now
                                hits += 1
                                to_delete.append(ca)
                                for ca in to_delete:
                                        near_miss.pop(ca, None)

except Exception as e:
last_scan_info["last_error"] = str(e)
logging.warning(f"scan error: {e}")

finally:
last_scan_info["hits"] = hits
last_scan_info["ts"] = datetime.now(tz=timezone.utc)
last_scan_info["duration_ms"] = int((time.perf_counter() - t0) * 1000)
if hits:
log.info(f"scan: pairs={last_scan_info['pairs']} hits={hits} duration={last_scan_info['duration_ms']}ms")

# ---------- TELEGRAM ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
"Bot up ✅ (STRICT DNA)\n"
f"Scan: {SCAN_INTERVAL}s | Cooldown: {ALERT_COOLDOWN_SEC}s | Near-miss: {NEAR_MISS_WINDOW_SEC}s"
)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
info = last_scan_info
ts = info["ts"].strftime("%Y-%m-%d %H:%M:%S UTC") if info["ts"] else "—"
err = info["last_error"] or "none"
await update.message.reply_text(
"STRICT DNA (unchanged):\n"
f"- Liq ≥ ${DNA['min_liq_usd']:,}\n"
f"- FDV ≤ ${DNA['max_fdv_usd']:,}\n"
f"- Age ≤ {DNA['max_age_min']}m\n"
f"- 1h Vol ≥ ${DNA['min_vol1h_usd']:,}\n"
f"- m5 activity ≥ {DNA['min_m5_activity']}\n"
f"- m5 Δ ≥ {DNA['m5_change_tol']}% (tolerance)\n\n"
f"Last scan: {ts} | pairs={info['pairs']} | hits={info['hits']} | {info['duration_ms']}ms\n"
f"Last error: {err}"
)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text("Scanning once…")
await scan_once(context.application)
await update.message.reply_text("Scan done.")

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not context.args:
await update.message.reply_text("Use: /check https://dexscreener.com/solana/<CA>")
return
url = context.args[0].strip()
if "dexscreener.com/solana/" not in url:
await update.message.reply_text("Only Solana links supported.")
return
ca = url.split("dexscreener.com/solana/")[-1].split("?")[0].strip()
try:
async with _client() as client:
r = await client.get(TOKEN_API.format(ca))
r.raise_for_status()
pairs = r.json().get("pairs", [])
except Exception as e:
await update.message.reply_text(f"check error: {e}")
return
if not pairs:
await update.message.reply_text("No data.")
return
p = pairs[0]
ok, why, _ = strict_dna_pass(p)
verdict = "PASS ✅" if ok else "FAIL ❌"
liq = (p.get("liquidity") or {}).get("usd", 0) or 0
fdv = p.get("fdv", 0) or 0
vol1h = (p.get("volume") or {}).get("h1", 0) or 0
m5 = (p.get("priceChange") or {}).get("m5", 0.0) or 0.0
tx5 = (p.get("txns") or {}).get("m5") or {}
activity5 = (tx5.get("buys", 0) or 0) + (tx5.get("sells", 0) or 0)
age_min = minutes_since_ms(p.get("pairCreatedAt", 0) or 0)

await update.message.reply_text(
f"{verdict} — {why}\n"
f"FDV ${fdv:,.0f} | Liq ${liq:,.0f} | 1h Vol ${vol1h:,.0f}\n"
f"m5 Δ {m5:.1f}% | m5 activity {activity5} | age {age_min:.0f}m"
)

def main():
app = Application.builder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CommandHandler("status", cmd_status))
app.add_handler(CommandHandler("scan", cmd_scan))
app.add_handler(CommandHandler("check", cmd_check))

# coalesce + max_instances=1 prevents overlap (no lag spikes)
sched = AsyncIOScheduler(job_defaults={"coalesce": True, "max_instances": 1})
sched.add_job(lambda: asyncio.create_task(scan_once(app)),
"interval", seconds=SCAN_INTERVAL, id="scanner")
sched.start()

log.info("Bot started (STRICT DNA + delivery hardened).")
app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
main()
