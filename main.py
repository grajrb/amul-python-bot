import asyncio
import threading
import os
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from db import get_products, get_subscriptions, save_subscriptions

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# ── Flask keep-alive ──────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Amul bot is running!"

@flask_app.route("/health")
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

# ── Load supported pincodes ───────────────────────────────────────────────────
with open("pincodes.txt", "r", encoding="utf-8") as _f:
    SUPPORTED_PINCODES = set(
        line.strip() for line in _f if line.strip().isdigit() and len(line.strip()) == 6
    )

# ── Bot handlers ─────────────────────────────────────────────────────────────
pending_pincode = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Amul Product Notifier!\nUse /products to see available products."
    )

async def products_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "pincode" not in context.user_data:
        await update.message.reply_text("Please enter your 6-digit delivery pincode:")
        context.user_data["awaiting_pincode_for_products"] = True
        return
    await _show_products(update, context)

async def _show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prods = get_products()
    if not prods:
        await update.message.reply_text("No products found. Please try again later.")
        return
    buttons = [
        [InlineKeyboardButton(f"{p['name']} (\u20b9{p['price']})", callback_data=p["alias"][:60])]
        for p in prods
    ]
    await update.message.reply_text(
        f"Select a product to subscribe for pincode {context.user_data['pincode']}:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    alias = query.data
    prods = get_products()
    product = next((p for p in prods if p["alias"][:60] == alias), None)
    if not product:
        await query.edit_message_text("Product not found.")
        return
    user_id = query.from_user.id
    pending_pincode[user_id] = product
    await query.edit_message_text(
        f"You selected: {product['name']}\nPlease enter your delivery pincode:"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    if not text.isdigit() or len(text) != 6:
        await update.message.reply_text("Please enter a valid 6-digit pincode:")
        return
    pincode = text
    if pincode not in SUPPORTED_PINCODES:
        await update.message.reply_text(
            "Sorry, your pincode is not supported for tracking yet."
        )
        return
    if context.user_data.get("awaiting_pincode_for_products"):
        context.user_data["pincode"] = pincode
        context.user_data.pop("awaiting_pincode_for_products", None)
        await _show_products(update, context)
        return
    if user_id not in pending_pincode:
        await update.message.reply_text(
            "Use /products to browse and subscribe to Amul products."
        )
        return
    product = pending_pincode.pop(user_id)
    subs = get_subscriptions()
    subs = [s for s in subs if s["user_id"] != user_id]
    last_status = "in_stock" if product.get("inventory_quantity", 0) > 0 else "out_of_stock"
    subs.append({
        "user_id": user_id,
        "alias": product["alias"],
        "name": product["name"],
        "pincode": pincode,
        "last_notified_status": last_status,
    })
    save_subscriptions(subs)
    if product.get("inventory_quantity", 0) > 0:
        msg = f"\u2705 Subscribed to {product['name']} for pincode {pincode}!\n\nThis product is currently IN STOCK."
    else:
        msg = f"\u2705 Subscribed to {product['name']} for pincode {pincode}!\n\nThis product is currently OUT OF STOCK. You'll be notified when it's back!"
    await update.message.reply_text(msg)

# ── Notifier loop ─────────────────────────────────────────────────────────────
async def notifier_loop(app: Application):
    while True:
        await asyncio.sleep(420)  # wait 7 minutes between checks
        subs = get_subscriptions()
        changed = False
        for sub in subs:
            user_id = sub["user_id"]
            alias = sub["alias"]
            pincode = sub.get("pincode")
            if not pincode:
                continue
            prods = get_products()
            product = next((p for p in prods if p.get("alias") == alias), None)
            if not product:
                continue
            now_qty = product.get("inventory_quantity", 0)
            new_status = "in_stock" if now_qty > 0 else "out_of_stock"
            last_status = sub.get("last_notified_status")
            if last_status != new_status:
                try:
                    if new_status == "in_stock":
                        msg = f"\u2705 {product['name']} is BACK IN STOCK! (\u20b9{product['price']})\n{product.get('url', '')}"
                    else:
                        msg = f"\u26a0\ufe0f {product['name']} is now OUT OF STOCK. We'll notify you when it's back!"
                    await app.bot.send_message(chat_id=user_id, text=msg)
                except Exception as e:
                    print(f"Failed to notify {user_id}: {e}")
                sub["last_notified_status"] = new_status
                changed = True
        if changed:
            save_subscriptions(subs)
        print("Notifier check complete.")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start Flask in background thread
    threading.Thread(target=run_flask, daemon=True).start()

    # Build the PTB application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("products", products_cmd))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Schedule the notifier as a post-init task
    async def post_init(app: Application):
        asyncio.create_task(notifier_loop(app))

    application.post_init = post_init

    print("Starting Amul bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
