import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List

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
ADMIN_CHAT_ID_RAW = os.environ.get("ADMIN_CHAT_ID")
ADMIN_PANEL_URL = os.environ.get("ADMIN_PANEL_URL")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")

ADMIN_CHAT_ID = None
if ADMIN_CHAT_ID_RAW:
    try:
        ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
    except ValueError:
        raise RuntimeError("ADMIN_CHAT_ID must be an integer") from None

if not BOT_TOKEN:
    raise RuntimeError("Environment variable BOT_TOKEN is required")

if not WEBAPP_URL:
    raise RuntimeError("Environment variable WEBAPP_URL is required")


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
app = Flask(__name__, static_folder="static", static_url_path="/static")

# ===== In-memory data store (demo) =====
categories: List[Dict[str, Any]] = [
    {"id": 1, "name": "Футболки", "icon": "👕"},
    {"id": 2, "name": "Толстовки", "icon": "🧥"},
    {"id": 3, "name": "Кроссовки", "icon": "👟"},
    {"id": 4, "name": "Аксессуары", "icon": "🧢"},
]

products: List[Dict[str, Any]] = [
    {
        "id": 1,
        "title": "Oversize худи графит",
        "price": 5990,
        "category_id": 2,
        "image_url": "/static/img/hoodie.svg",
        "description": "Плотный футер, удлинённый крой.",
    },
    {
        "id": 2,
        "title": "Базовая белая футболка",
        "price": 1990,
        "category_id": 1,
        "image_url": "/static/img/tshirt.svg",
        "description": "100% хлопок, оверсайз.",
    },
    {
        "id": 3,
        "title": "Кроссовки street black",
        "price": 8990,
        "category_id": 3,
        "image_url": "/static/img/sneakers.svg",
        "description": "Лёгкие, нескользящая подошва.",
    },
    {
        "id": 4,
        "title": "Рюкзак urban",
        "price": 4990,
        "category_id": 4,
        "image_url": "/static/img/bag.svg",
        "description": "14\", защита от влаги, скрытый карман.",
    },
]

carts: Dict[int, Dict[int, int]] = {}
logs: List[Dict[str, Any]] = []
LOG_LIMIT = 200


def _log_event(kind: str, user_id: int | None = None, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "kind": kind,
        "user_id": user_id,
        "payload": payload or {},
    }
    logs.append(entry)
    if len(logs) > LOG_LIMIT:
        logs.pop(0)
    return entry


def _next_id(items: List[Dict[str, Any]]) -> int:
    return max((item["id"] for item in items), default=0) + 1


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


def _require_admin() -> None:
    """Check admin token header or query param if ADMIN_TOKEN is set."""
    if not ADMIN_TOKEN:
        return
    provided = request.headers.get("X-Admin-Token") or request.args.get("token")
    if provided != ADMIN_TOKEN:
        abort(401)


def _resolve_admin_url() -> str:
    if ADMIN_PANEL_URL:
        return ADMIN_PANEL_URL
    if WEBAPP_URL.endswith("/webapp"):
        return WEBAPP_URL.rsplit("/webapp", 1)[0] + "/admin"
    return WEBAPP_URL.rstrip("/") + "/admin"


def _handle_update(update: Update) -> None:
    """Process Telegram update and respond with WebApp button."""
    if update.message:
        chat_id = update.message.chat.id

        if update.message.web_app_data:
            payload = update.message.web_app_data.data
            logger.info("Received web_app_data: %s", payload)
            _log_event("web_app_data", user_id=chat_id, payload={"raw": payload})
            send_message_sync(chat_id=chat_id, text=f"Спасибо, получил: {payload}")
            return

        text = update.message.text or ""
        if text.startswith("/start"):
            buttons = [
                InlineKeyboardButton(
                    text="Открыть магазин",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
            if ADMIN_CHAT_ID and chat_id == ADMIN_CHAT_ID:
                buttons.append(
                    InlineKeyboardButton(
                        text="Админ-панель",
                        url=_resolve_admin_url(),
                    )
                )
            keyboard = InlineKeyboardMarkup([buttons])
            send_message_sync(chat_id=chat_id, text="Нажми кнопку, чтобы открыть мини-приложение.", reply_markup=keyboard)
        else:
            send_message_sync(chat_id=chat_id, text="Отправь /start, чтобы открыть мини-приложение.")


def _get_product(product_id: int) -> Dict[str, Any] | None:
    for product in products:
        if product["id"] == product_id:
            return product
    return None


@app.route("/")
def health() -> Dict[str, Any]:
    return {"ok": True}


@app.route("/webapp")
def webapp() -> Any:
    return send_from_directory(app.static_folder, "index.html")


@app.route("/admin")
def admin_page() -> Any:
    return send_from_directory(app.static_folder, "admin.html")


@app.route("/api/categories", methods=["GET", "POST"])
def api_categories() -> Any:
    if request.method == "GET":
        return jsonify({"items": categories})

    _require_admin()
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    icon = (body.get("icon") or "").strip() or None
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    new_cat = {"id": _next_id(categories), "name": name, "icon": icon}
    categories.append(new_cat)
    _log_event("category_created", payload=new_cat)
    return jsonify(new_cat), 201


@app.route("/api/products", methods=["GET", "POST"])
def api_products() -> Any:
    if request.method == "GET":
        category_id = request.args.get("category_id", type=int)
        data = products
        if category_id:
            data = [p for p in products if p["category_id"] == category_id]
        return jsonify({"items": data})

    _require_admin()
    body = request.get_json(force=True, silent=True) or {}
    title = (body.get("title") or "").strip()
    price = body.get("price")
    category_id = body.get("category_id")
    image_url = (body.get("image_url") or "").strip() or "/static/img/placeholder.svg"
    description = (body.get("description") or "").strip()

    if not title or price is None or category_id is None:
        return jsonify({"ok": False, "error": "title, price, category_id required"}), 400

    new_product = {
        "id": _next_id(products),
        "title": title,
        "price": float(price),
        "category_id": int(category_id),
        "image_url": image_url,
        "description": description,
    }
    products.append(new_product)
    _log_event("product_created", payload=new_product)
    return jsonify(new_product), 201


@app.route("/api/cart/add", methods=["POST"])
def api_cart_add() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    user_id = body.get("user_id")
    product_id = body.get("product_id")
    qty = int(body.get("qty") or 1)

    if not user_id or not product_id:
        return jsonify({"ok": False, "error": "user_id and product_id required"}), 400

    product = _get_product(int(product_id))
    if not product:
        return jsonify({"ok": False, "error": "product not found"}), 404

    cart = carts.setdefault(int(user_id), {})
    cart[int(product_id)] = cart.get(int(product_id), 0) + max(1, qty)
    _log_event("cart_add", user_id=int(user_id), payload={"product_id": int(product_id), "qty": qty})
    return jsonify({"ok": True})


@app.route("/api/cart/<int:user_id>", methods=["GET"])
def api_cart_get(user_id: int) -> Any:
    cart = carts.get(user_id, {})
    items: List[Dict[str, Any]] = []
    total = 0.0
    for product_id, qty in cart.items():
        product = _get_product(product_id)
        if not product:
            continue
        subtotal = product["price"] * qty
        total += subtotal
        items.append({"product": product, "qty": qty, "subtotal": subtotal})
    return jsonify({"items": items, "total": total})


@app.route("/api/cart/clear", methods=["POST"])
def api_cart_clear() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    user_id = body.get("user_id")
    if not user_id:
        return jsonify({"ok": False, "error": "user_id required"}), 400
    carts[int(user_id)] = {}
    _log_event("cart_clear", user_id=int(user_id))
    return jsonify({"ok": True})


@app.route("/api/cart/checkout", methods=["POST"])
def api_cart_checkout() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    user_id = body.get("user_id")
    contact = (body.get("contact") or "").strip()
    note = (body.get("note") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "error": "user_id required"}), 400

    cart = carts.get(int(user_id), {})
    _log_event(
        "checkout",
        user_id=int(user_id),
        payload={"contact": contact, "note": note, "items": cart},
    )
    carts[int(user_id)] = {}
    return jsonify({"ok": True, "message": "Заказ принят. Менеджер свяжется в Telegram."})


@app.route("/api/admin/logs", methods=["GET"])
def api_admin_logs() -> Any:
    _require_admin()
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"items": logs[-limit:]})


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
