import asyncio
import logging
import os
import threading

from flask import Flask, Request, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from db import get_products, get_subscriptions, save_subscriptions

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

# Load supported pincodes
with open("pincodes.txt", "r", encoding="utf-8") as _pf:
    SUPPORTED_PINCODES = set(
        ln.strip() for ln in _pf if ln.strip().isdigit() and len(ln.strip()) == 6
    )

pending_pincode: dict = {}

# ── Bot handlers ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Amul Product Notifier!\n"
        "Use /products to see available products."
    )

async def products_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "pincode" not in context.user_data:
        context.user_data["awaiting_pincode_for_products"] = True
        await update.message.reply_text("Please enter your 6-digit delivery pincode:")
        return
    await _show_products(update, context)

async def _show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prods = get_products()
    if not prods:
        await update.message.reply_text("No products found. Try again later.")
        return
    buttons = [
        [InlineKeyboardButton(
            f"{p['name']} (Rs.{p['price']})",
            callback_data=p["alias"][:60]
        )]
        for p in prods
    ]
    await update.message.reply_text(
        f"Products for pincode {context.user_data['pincode']}:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prods = get_products()
    product = next((p for p in prods if p["alias"][:60] == query.data), None)
    if not product:
        await query.edit_message_text("Product not found.")
        return
    pending_pincode[query.from_user.id] = product
    await query.edit_message_text(
        f"You selected: {product['name']}\nEnter your delivery pincode:"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    if not text.isdigit() or len(text) != 6:
        await update.message.reply_text("Please enter a valid 6-digit pincode.")
        return
    if text not in SUPPORTED_PINCODES:
        await update.message.reply_text(
            "Sorry, that pincode is not supported yet."
        )
        return
    if context.user_data.get("awaiting_pincode_for_products"):
        context.user_data["pincode"] = text
        context.user_data.pop("awaiting_pincode_for_products", None)
        await _show_products(update, context)
        return
    if user_id not in pending_pincode:
        await update.message.reply_text(
            "Use /products to browse and subscribe to Amul products."
        )
        return
    product = pending_pincode.pop(user_id)
    subs = [s for s in get_subscriptions() if s["user_id"] != user_id]
    status = "in_stock" if product.get("inventory_quantity", 0) > 0 else "out_of_stock"
    subs.append({
        "user_id": user_id,
        "alias": product["alias"],
        "name": product["name"],
        "pincode": text,
        "last_notified_status": status,
    })
    save_subscriptions(subs)
    msg = (
        f"Subscribed to {product['name']} for pincode {text}!\n"
        + ("Currently IN STOCK." if status == "in_stock" else "Currently OUT OF STOCK. We will notify you!")
    )
    await update.message.reply_text(msg)

# ── Notifier ──────────────────────────────────────────────────────────────────

async def notifier_loop(app: Application):
    while True:
        await asyncio.sleep(420)
        subs = get_subscriptions()
        changed = False
        for sub in subs:
            prods = get_products()
            product = next((p for p in prods if p.get("alias") == sub["alias"]), None)
            if not product:
                continue
            new_status = "in_stock" if product.get("inventory_quantity", 0) > 0 else "out_of_stock"
            if sub.get("last_notified_status") != new_status:
                try:
                    msg = (
                        f"{product['name']} is BACK IN STOCK! (Rs.{product['price']})\n{product.get('url', '')}"
                        if new_status == "in_stock"
                        else f"{product['name']} is now OUT OF STOCK. We will notify you when it is back!"
                    )
                    await app.bot.send_message(chat_id=sub["user_id"], text=msg)
                    sub["last_notified_status"] = new_status
                    changed = True
                except Exception as e:
                    log.error("Notify failed for %s: %s", sub["user_id"], e)
        if changed:
            save_subscriptions(subs)
        log.info("Notifier check done.")

# ── Build PTB application ─────────────────────────────────────────────────────

ptb_app: Application = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .updater(None)          # disable built-in updater; we handle updates via Flask
    .build()
)
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("products", products_cmd))
ptb_app.add_handler(CallbackQueryHandler(button))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ── Flask app ─────────────────────────────────────────────────────────────────

flask_app = Flask(__name__)
event_loop: asyncio.AbstractEventLoop

@flask_app.route("/", methods=["GET"])
def home():
    return "Amul bot is running!"

@flask_app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@flask_app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, ptb_app.bot)
    asyncio.run_coroutine_threadsafe(
        ptb_app.process_update(update), event_loop
    ).result(timeout=30)
    return Response("ok", status=200)

# ── Async startup ─────────────────────────────────────────────────────────────

async def main():
    global event_loop
    event_loop = asyncio.get_running_loop()

    # Initialise PTB
    await ptb_app.initialize()

    # Set webhook so Telegram pushes updates to our Render URL
    webhook_url = f"{RENDER_URL}/{TELEGRAM_TOKEN}"
    await ptb_app.bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
    )
    log.info("Webhook set to %s", webhook_url)

    await ptb_app.start()

    # Start notifier in background
    asyncio.create_task(notifier_loop(ptb_app))

    # Run Flask in a daemon thread
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False),
        daemon=True,
    ).start()

    log.info("Amul bot is live on port %s", PORT)

    # Keep the async loop alive forever
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
