import os, asyncio, httpx, logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
MODE = os.getenv("MODE", "moderate")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SEC", "10"))
COOLDOWN = int(os.getenv("COOLDOWN_SEC", "60"))
BACKFILL = int(os.getenv("BACKFILL_SEC", "60"))

last_alert = {}
near_miss_watch = {}

MODES = {
"moderate": {"liq":20000,"fdv":2000000,"vol1h":20000,"buy_ratio":0.55,"m5":0.15,"h1":0.35,"age":5},
"winner": {"liq":25000,"fdv":1500000,"vol1h":25000,"buy_ratio":0.60,"m5":0.25,"h1":0.45,"age":8},
}

async def fetch_pairs():
url = "https://api.dexscreener.com/latest/dex/search?q=solana"
async with httpx.AsyncClient() as client:
r = await client.get(url, timeout=10)
r.raise_for_status()
return r.json().get("pairs", [])

def dna_pass(p, mode="moderate"):
f = MODES[mode]
liq = p.get("liquidity",{}).get("usd",0)
fdv = p.get("fdv",0)
vol1h = p.get("volume",{}).get("h1",0)
buys = p.get("txns",{}).get("h1",{}).get("buys",0)
sells= p.get("txns",{}).get("h1",{}).get("sells",0)
ratio= buys/(buys+sells) if (buys+sells)>0 else 0
age = (datetime.utcnow()-datetime.fromtimestamp(p.get("pairCreatedAt",0)/1000)).seconds/60
m5 = p.get("priceChange",{}).get("m5",0)/100
h1 = p.get("priceChange",{}).get("h1",0)/100

if liq < f["liq"]: return False,"Low liq"
if fdv > f["fdv"]: return False,"High FDV"
if vol1h < f["vol1h"]: return False,"Low vol"
if ratio < f["buy_ratio"]: return False,"Low buys"
if m5 < f["m5"]: return False,"Weak m5"
if h1 < f["h1"]: return False,"Weak h1"
if age < f["age"]: return False,"Too new"
return True,"DNA pass"

async def send_alert(app, p, why):
url = p.get("url","")
base = p.get("baseToken",{}).get("symbol","?")
mc = p.get("fdv",0)
liq = p.get("liquidity",{}).get("usd",0)
msg = f"🚨 DING [{base}] {why}\nMC ${mc:,} | Liq ${liq:,}\n{url}"
await app.bot.send_message(chat_id=CHAT_ID, text=msg)

async def scan_once(app):
global last_alert, near_miss_watch
try:
pairs = await fetch_pairs()
except Exception as e:
logging.warning(f"fetch error: {e}")
return

now = datetime.utcnow()
for p in pairs:
ca = p.get("baseToken",{}).get("address")
if not ca: continue
passed, why = dna_pass(p, MODE)
if passed:
if ca not in last_alert or (now-last_alert[ca]).seconds > COOLDOWN:
await send_alert(app, p, why)
last_alert[ca]=now
else:
near_miss_watch[ca]=(p,now)

# backfill near-misses
for ca,(p,t0) in list(near_miss_watch.items()):
if (now-t0).seconds>BACKFILL: near_miss_watch.pop(ca,None)
else:
passed,why=dna_pass(p,MODE)
if passed and (ca not in last_alert or (now-last_alert[ca]).seconds>COOLDOWN):
await send_alert(app,p,"Backfill:"+why)
last_alert[ca]=now
near_miss_watch.pop(ca,None)

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(f"Bot on | Mode:{MODE} | Int:{SCAN_INTERVAL}s | Cool:{COOLDOWN}s | Back:{BACKFILL}s")

async def metrics(update:Update,context:ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(f"Alerts:{len(last_alert)} | NearMiss:{len(near_miss_watch)}")

async def check(update:Update,context:ContextTypes.DEFAULT_TYPE):
if not context.args:
await update.message.reply_text("Give dexscreener link")
return
url=context.args[0]
if "solana/" not in url:
await update.message.reply_text("Only solana links")
return
ca=url.split("solana/")[-1]
try:
api=f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
async with httpx.AsyncClient() as client:
r=await client.get(api,timeout=10)
r.raise_for_status()
pairs=r.json().get("pairs",[])
if not pairs:
await update.message.reply_text("No data")
return
p=pairs[0]
passed,why=dna_pass(p,MODE)
await update.message.reply_text(f"{'PASS' if passed else 'FAIL'}: {why}")
except Exception as e:
await update.message.reply_text(f"Check err:{e}")

def main():
app=Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start",start))
app.add_handler(CommandHandler("metrics",metrics))
app.add_handler(CommandHandler("check",check))
sched=AsyncIOScheduler()
sched.add_job(lambda:scan_once(app),"interval",seconds=SCAN_INTERVAL)
sched.start()
app.run_polling()

if __name__=="__main__":
main()
