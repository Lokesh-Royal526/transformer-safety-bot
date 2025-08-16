# app.py
import os, json, asyncio, logging
from typing import Dict, Any, List

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import uvicorn

# --- Telegram bot (v20) ---
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Firebase Admin ---
import firebase_admin
from firebase_admin import credentials, db

# ---------- CONFIG (from env) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]            # e.g. 1234:AA...
AUTHORIZED_CHAT_IDS = os.environ.get("AUTHORIZED_CHAT_IDS", "").split(",")
FIREBASE_DB_URL = os.environ["FIREBASE_DB_URL"]          # https://...firebaseio.com
FB_ROOT = os.environ.get("FB_ROOT", "transformer_safety")

# thresholds (override via env if you want)
CURRENT_THRESHOLD = float(os.environ.get("CURRENT_THRESHOLD", "2.0"))
TEMP_THRESHOLD = float(os.environ.get("TEMP_THRESHOLD", "50.0"))
WARNING_ZONE_CM = float(os.environ.get("WARNING_ZONE_CM", "1000"))
DANGER_ZONE_CM = float(os.environ.get("DANGER_ZONE_CM", "500"))

# service account (Render env var holds the *JSON contents*)
GOOGLE_CRED_JSON = os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("transformer-bot")

# ---------- Firebase init ----------
cred = credentials.Certificate(json.loads(GOOGLE_CRED_JSON))
firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})

def ref_root():
    return db.reference(FB_ROOT)

def get_data() -> Dict[str, Any]:
    try:
        obj = ref_root().get()
        return obj or {}
    except Exception as e:
        log.exception("Firebase read error: %s", e)
        return {}

def update_data(pairs: Dict[str, Any]):
    try:
        ref_root().update(pairs)
    except Exception as e:
        log.exception("Firebase write error: %s", e)

# ---------- Telegram ----------
application: Application = Application.builder().token(TELEGRAM_TOKEN).build()

def is_authorized(update: Update) -> bool:
    chat_id = str(update.effective_chat.id) if update and update.effective_chat else ""
    return chat_id in AUTHORIZED_CHAT_IDS

async def safe_send(chat_id: str, text: str):
    try:
        await application.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        log.exception("Telegram send error: %s", e)

# Commands
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text(
        "Transformer Safety Bot ready.\n"
        "/status – live snapshot\n"
        "/maintenance_on /maintenance_off – toggle maintenance\n"
        "/relay_open /relay_close – control relay\n"
        "/earthrod_on /earthrod_off – control earth rod\n"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    d = get_data()
    dist_cm = d.get("distance_cm")
    temp = d.get("temperature_c")
    curr = d.get("current_a")
    human = bool(d.get("human_detected", 0))
    current_fault = bool(d.get("current_fault", 0))
    relay_closed = bool(d.get("relay_status", 0))
    rod_engaged = bool(d.get("earth_rod_status", 0))
    maintenance = bool(d.get("maintenance_mode", 0))

    parts: List[str] = ["📡 Transformer Status"]
    if isinstance(dist_cm, (int, float)) and dist_cm > 0:
        parts.append(f"Distance: {dist_cm/100.0:.2f} m")
    else:
        parts.append("Distance: N/A")

    parts.append(f"Human detected: {'YES' if human else 'NO'}")
    parts.append(f"Current: {curr:.2f} A" if isinstance(curr, (int, float)) else "Current: N/A")
    if isinstance(temp, (int, float)):
        flag = " (HIGH!)" if temp >= TEMP_THRESHOLD else ""
        parts.append(f"Temperature: {temp:.1f} °C{flag}")
    else:
        parts.append("Temperature: N/A")
    parts.append(f"Overcurrent: {'YES' if current_fault else 'NO'}")
    parts.append(f"Relay (closed): {'YES' if relay_closed else 'NO'}")
    parts.append(f"Earth rod engaged: {'YES' if rod_engaged else 'NO'}")
    parts.append(f"Maintenance mode: {'ON' if maintenance else 'OFF'}")
    await update.message.reply_text("\n".join(parts))

async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    update_data({"maintenance_mode": True})
    await update.message.reply_text("🛠 Maintenance mode: ON")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    update_data({"maintenance_mode": False})
    await update.message.reply_text("🛠 Maintenance mode: OFF")

async def relay_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    # relay_status: 1 = closed, 0 = open
    update_data({"relay_status": 0, "relay_on": False})
    await update.message.reply_text("🔌 Relay: OPEN requested")

async def relay_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    update_data({"relay_status": 1, "relay_on": True})
    await update.message.reply_text("🔌 Relay: CLOSED requested")

async def earthrod_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    update_data({"earth_rod_status": 1})
    await update.message.reply_text("🌎 Earth rod: ENGAGE requested")

async def earthrod_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    update_data({"earth_rod_status": 0})
    await update.message.reply_text("🌎 Earth rod: RETRACT requested")

# Register handlers
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("status", status_cmd))
application.add_handler(CommandHandler("maintenance_on", maintenance_on))
application.add_handler(CommandHandler("maintenance_off", maintenance_off))
application.add_handler(CommandHandler("relay_open", relay_open))
application.add_handler(CommandHandler("relay_close", relay_close))
application.add_handler(CommandHandler("earthrod_on", earthrod_on))
application.add_handler(CommandHandler("earthrod_off", earthrod_off))

# ---------- Background Firebase poller ----------
last_state = {
    "human_zone": None,
    "fault": False,
    "temp_high": False
}

async def poll_firebase_task():
    while True:
        d = get_data()
        dist_cm = d.get("distance_cm")
        temp = float(d.get("temperature_c") or 0.0)
        current = float(d.get("current_a") or 0.0)

        zone = "none"
        dist_m = None
        if isinstance(dist_cm, (int, float)) and dist_cm > 0:
            dist_m = dist_cm / 100.0
            if dist_cm <= DANGER_ZONE_CM:
                zone = "danger"
            elif dist_cm <= WARNING_ZONE_CM:
                zone = "warning"

        # zone transitions
        if zone == "warning" and last_state["human_zone"] != "warning":
            for chat in AUTHORIZED_CHAT_IDS:
                if chat:
                    await safe_send(chat, f"⚠️ Warning: human at {dist_m:.2f} m — Buzzer ON")
            last_state["human_zone"] = "warning"
        elif zone == "danger" and last_state["human_zone"] != "danger":
            for chat in AUTHORIZED_CHAT_IDS:
                if chat:
                    await safe_send(chat, f"🚨 DANGER: human at {dist_m:.2f} m — Relay OPEN & Earth rod ENGAGED")
            last_state["human_zone"] = "danger"
        elif zone == "none":
            last_state["human_zone"] = None

        # overcurrent
        if current > CURRENT_THRESHOLD and not last_state["fault"]:
            for chat in AUTHORIZED_CHAT_IDS:
                if chat:
                    await safe_send(chat, f"⚡ Overcurrent: {current:.2f} A — Relay opened")
            last_state["fault"] = True
        elif current <= CURRENT_THRESHOLD:
            last_state["fault"] = False

        # temperature
        if temp >= TEMP_THRESHOLD and not last_state["temp_high"]:
            for chat in AUTHORIZED_CHAT_IDS:
                if chat:
                    await safe_send(chat, f"🔥 HIGH TEMP: {temp:.1f} °C")
            last_state["temp_high"] = True
        elif temp < TEMP_THRESHOLD:
            last_state["temp_high"] = False

        await asyncio.sleep(2)

# ---------- FastAPI app & webhook ----------
app = FastAPI()
bg_task: asyncio.Task | None = None

@app.on_event("startup")
async def on_startup():
    global bg_task
    await application.initialize()
    await application.start()   # starts the PTB event processing loop
    if bg_task is None:
        bg_task = asyncio.create_task(poll_firebase_task())
    log.info("Startup complete.")

@app.on_event("shutdown")
async def on_shutdown():
    global bg_task
    if bg_task:
        bg_task.cancel()
        try:
            await bg_task
        except asyncio.CancelledError:
            pass
    await application.stop()
    await application.shutdown()
    log.info("Shutdown complete.")

@app.get("/health")
async def health():
    return PlainTextResponse("ok", status_code=200)

# Telegram will POST updates here
@app.post("/webhook/{secret_token}")
async def webhook(secret_token: str, request: Request):
    # Use your bot token as the secret path OR a separate secret string
    if secret_token != os.environ.get("WEBHOOK_SECRET", "") and secret_token != TELEGRAM_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return PlainTextResponse("ok")

# Convenience endpoint to register webhook with Telegram
@app.get("/set_webhook")
async def set_webhook():
    # Render provides the port; use your Render URL
    base_url = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    secret = os.environ.get("WEBHOOK_SECRET", TELEGRAM_TOKEN)
    url = f"{base_url}/webhook/{secret}"
    ok = await application.bot.set_webhook(url=url)
    return {"webhook_set": ok, "url": url}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
