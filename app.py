import asyncio
import logging
import os
from typing import Any, Dict

from flask import Flask, abort, jsonify, request, send_from_directory
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)


BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

if not BOT_TOKEN:
    raise RuntimeError("Environment variable BOT_TOKEN is required")

if not WEBAPP_URL:
    raise RuntimeError("Environment variable WEBAPP_URL is required")


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
app = Flask(__name__, static_folder="static", static_url_path="/static")


def send_message_sync(chat_id: int, text: str, reply_markup: Any | None = None) -> None:
    """Send Telegram message using async bot inside sync Flask handler."""
    asyncio.run(
        bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
    )


def _verify_secret_token() -> None:
    """Abort if Telegram secret token header is missing or incorrect."""
    if not WEBHOOK_SECRET:
        return

    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if provided != WEBHOOK_SECRET:
        logger.warning("Invalid secret token on webhook call")
        abort(401)


def _handle_update(update: Update) -> None:
    """Process Telegram update and respond with WebApp button."""
    if update.message:
        chat_id = update.message.chat.id

        if update.message.web_app_data:
            payload = update.message.web_app_data.data
            logger.info("Received web_app_data: %s", payload)
            send_message_sync(chat_id=chat_id, text=f"Спасибо, получил: {payload}")
            return

        text = update.message.text or ""
        if text.startswith("/start"):
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="Открыть мини-приложение",
                            web_app=WebAppInfo(url=WEBAPP_URL),
                        )
                    ]
                ]
            )
            send_message_sync(chat_id=chat_id, text="Нажми кнопку, чтобы открыть мини-приложение.", reply_markup=keyboard)
        else:
            send_message_sync(chat_id=chat_id, text="Отправь /start, чтобы открыть мини-приложение.")


@app.route("/")
def health() -> Dict[str, Any]:
    return {"ok": True}


@app.route("/webapp")
def webapp() -> Any:
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/ping", methods=["POST"])
def api_ping() -> Any:
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "друг").strip()
    greeting = f"Привет, {name}!"
    return jsonify({"ok": True, "greeting": greeting})


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook() -> Any:
    _verify_secret_token()
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"ok": False, "description": "empty body"})

    update = Update.de_json(data, bot)
    try:
        _handle_update(update)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to handle update: %s", exc)
        return jsonify({"ok": False})

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
