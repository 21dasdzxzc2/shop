import asyncio
import copy
import io
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
from flask import Flask, abort, jsonify, request, send_from_directory
from PIL import Image
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest


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

request_client = HTTPXRequest(
    connection_pool_size=int(os.environ.get("TG_POOL_SIZE", "20")),
    connect_timeout=float(os.environ.get("TG_CONNECT_TIMEOUT", "10")),
    read_timeout=float(os.environ.get("TG_READ_TIMEOUT", "20")),
    write_timeout=float(os.environ.get("TG_WRITE_TIMEOUT", "20")),
    pool_timeout=float(os.environ.get("TG_POOL_TIMEOUT", "10")),
)
bot = Bot(token=BOT_TOKEN, request=request_client)
app = Flask(__name__, static_folder="static", static_url_path="/static")
THUMBS_DIR = Path(app.static_folder) / "thumbs"
UPLOADS_DIR = Path(app.static_folder) / "uploads"
DATA_DIR = Path("data")
THUMBS_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

def _clone(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False))


def _save_json(name: str, data: Any) -> None:
    path = DATA_DIR / name
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to save %s: %s", name, exc)


def _load_json(name: str, default: Any) -> Any:
    path = DATA_DIR / name
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read %s: %s", name, exc)
    data = _clone(default)
    _save_json(name, data)
    return data


def _normalize_carts(data: Dict[str, Any]) -> Dict[int, Dict[int, int]]:
    normalized: Dict[int, Dict[int, int]] = {}
    for user_id_str, cart in data.items():
        try:
            uid = int(user_id_str)
        except Exception:
            continue
        if not isinstance(cart, dict):
            continue
        normalized[uid] = {}
        for pid_str, qty in cart.items():
            try:
                normalized[uid][int(pid_str)] = int(qty)
            except Exception:
                continue
    return normalized


# ===== In-memory data store (demo) =====
_default_categories: List[Dict[str, Any]] = [
    {"id": 1, "name": "Футболки", "icon": "👕"},
    {"id": 2, "name": "Толстовки", "icon": "🧥"},
    {"id": 3, "name": "Кроссовки", "icon": "👟"},
    {"id": 4, "name": "Аксессуары", "icon": "🧢"},
]

_default_products: List[Dict[str, Any]] = [
    {
        "id": 1,
        "title": "Oversize худи графит",
        "price": 5990,
        "category_id": 2,
        "image_url": "/static/img/placeholder.svg",
        "thumb_url": "/static/img/placeholder.svg",
        "description": "Плотный футер, удлинённый крой.",
    },
    {
        "id": 2,
        "title": "Базовая белая футболка",
        "price": 1990,
        "category_id": 1,
        "image_url": "/static/img/placeholder.svg",
        "thumb_url": "/static/img/placeholder.svg",
        "description": "100% хлопок, оверсайз.",
    },
    {
        "id": 3,
        "title": "Кроссовки street black",
        "price": 8990,
        "category_id": 3,
        "image_url": "/static/img/placeholder.svg",
        "thumb_url": "/static/img/placeholder.svg",
        "description": "Лёгкие, нескользящая подошва.",
    },
    {
        "id": 4,
        "title": "Рюкзак urban",
        "price": 4990,
        "category_id": 4,
        "image_url": "/static/img/placeholder.svg",
        "thumb_url": "/static/img/placeholder.svg",
        "description": "14\", защита от влаги, скрытый карман.",
    },
]

categories: List[Dict[str, Any]] = _load_json("categories.json", _default_categories)
products: List[Dict[str, Any]] = _load_json("products.json", _default_products)
carts_raw = _load_json("carts.json", {})
carts: Dict[int, Dict[int, int]] = _normalize_carts(carts_raw)
logs: List[Dict[str, Any]] = _load_json("logs.json", [])
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
    _save_json("logs.json", logs)
    return entry


def _next_id(items: List[Dict[str, Any]]) -> int:
    return max((item["id"] for item in items), default=0) + 1


def send_message_sync(chat_id: int, text: str, reply_markup: Any | None = None) -> bool:
    """Send Telegram message using async bot inside sync Flask handler."""
    async def _send() -> None:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
            )
        except (TimedOut, NetworkError) as exc:
            logger.warning("Send message timeout/network error: %s", exc)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Send message failed: %s", exc)
            raise

    try:
        asyncio.run(_send())
        return True
    except Exception:
        return False


def _make_thumbnail(image_url: str, product_id: int, max_size: int = 600) -> str | None:
    """Fetch image_url and create a resized thumbnail; return static path or None."""
    try:
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        img = img.convert("RGB")
        img.thumbnail((max_size, max_size))
        filename = f"product_{product_id}.jpg"
        out_path = THUMBS_DIR / filename
        img.save(out_path, format="JPEG", optimize=True, quality=82)
        return f"/static/thumbs/{filename}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Thumbnail generation failed for %s: %s", image_url, exc)
        return None


def _resolve_postimg(url: str) -> str:
    """If URL is postimg.cc page, convert to direct i.postimg.cc link (jpg)."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if "postimg.cc" in host:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2:
                album, name = parts[-2], parts[-1]
                # try jpg by default
                return f"https://i.postimg.cc/{album}/{name}.jpg"
        return url
    except Exception:
        return url


def _download_and_resize(image_url: str, product_id: int) -> Tuple[str | None, str | None]:
    """Download source image, save main (scaled to 1600) and thumb (scaled to 600)."""
    source_url = _resolve_postimg(image_url)
    candidates = [source_url]
    if "i.postimg.cc" in source_url and source_url.endswith(".jpg"):
        candidates.append(source_url[:-4] + ".png")
    img = None
    for candidate in candidates:
        try:
            resp = requests.get(candidate, timeout=20)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Download image failed for %s: %s", candidate, exc)
            img = None
    if img is None:
        return None, None

    def save_variant(img_obj: Image.Image, max_size: int, folder: Path, suffix: str) -> str | None:
        try:
            clone = img_obj.copy()
            clone.thumbnail((max_size, max_size))
            filename = f"product_{product_id}{suffix}.jpg"
            out_path = folder / filename
            clone.save(out_path, format="JPEG", optimize=True, quality=85)
            return f"/static/{folder.name}/{filename}"
        except Exception as inner_exc:  # noqa: BLE001
            logger.warning("Save image variant failed: %s", inner_exc)
            return None

    main_path = save_variant(img, 1600, UPLOADS_DIR, "")
    thumb_path = save_variant(img, 600, THUMBS_DIR, "")
    return main_path, thumb_path


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
    _save_json("categories.json", categories)
    return jsonify(new_cat), 201


@app.route("/api/categories/<int:cat_id>", methods=["PATCH", "DELETE"])
def api_category_update(cat_id: int) -> Any:
    _require_admin()
    category = next((c for c in categories if c["id"] == cat_id), None)
    if not category:
        return jsonify({"ok": False, "error": "category not found"}), 404

    if request.method == "PATCH":
        body = request.get_json(force=True, silent=True) or {}
        name = (body.get("name") or "").strip()
        icon = (body.get("icon") or "").strip()
        if name:
            category["name"] = name
        category["icon"] = icon or None
        _log_event("category_updated", payload=category)
        _save_json("categories.json", categories)
        return jsonify(category)

    # DELETE
    categories[:] = [c for c in categories if c["id"] != cat_id]
    for product in products:
        if product["category_id"] == cat_id:
            product["category_id"] = None
    _log_event("category_deleted", payload={"id": cat_id})
    _save_json("categories.json", categories)
    _save_json("products.json", products)
    return jsonify({"ok": True})


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
    thumb_url = (body.get("thumb_url") or "").strip()
    description = (body.get("description") or "").strip()

    if not title or price is None or category_id is None:
        return jsonify({"ok": False, "error": "title, price, category_id required"}), 400

    new_product = {
        "id": _next_id(products),
        "title": title,
        "price": float(price),
        "category_id": int(category_id),
        "image_url": image_url,
        "thumb_url": thumb_url,
        "description": description,
    }
    main_path, thumb_path = _download_and_resize(image_url=image_url, product_id=new_product["id"])
    if main_path:
        new_product["image_url"] = main_path
    if thumb_path:
        new_product["thumb_url"] = thumb_path
    if not new_product["thumb_url"]:
        new_product["thumb_url"] = new_product["image_url"]
    products.append(new_product)
    _log_event("product_created", payload=new_product)
    _save_json("products.json", products)
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
    _save_json("carts.json", carts)
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
    _save_json("carts.json", carts)
    return jsonify({"ok": True})


@app.route("/api/cart/checkout", methods=["POST"])
def api_cart_checkout() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    user_id = body.get("user_id")
    contact = (body.get("contact") or "").strip()
    note = (body.get("note") or "").strip()
    tg_username = (body.get("tg_username") or "").strip()
    tg_name = (body.get("tg_name") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "error": "user_id required"}), 400

    cart = carts.get(int(user_id), {})
    items: List[Dict[str, Any]] = []
    total = 0.0
    for product_id, qty in cart.items():
        product = _get_product(product_id)
        if not product:
            continue
        subtotal = product["price"] * qty
        total += subtotal
        items.append({"product": product, "qty": qty, "subtotal": subtotal})

    _log_event(
        "checkout",
        user_id=int(user_id),
        payload={"contact": contact, "note": note, "items": cart},
    )

    if ADMIN_CHAT_ID:
        lines = [
            "🛒 Новый заказ",
            f"user_id: {user_id}",
            f"tg: @{tg_username}" if tg_username else "tg: не указал",
            f"контакт: {contact or '—'}",
        ]
        if tg_name:
            lines.append(f"имя: {tg_name}")
        lines.append("Позиций:")
        for row in items:
            lines.append(f"- {row['product']['title']} x{row['qty']} = {int(row['subtotal'])}₽")
        lines.append(f"Итого: {int(total)}₽")
        if note:
            lines.append(f"Комментарий: {note}")
        if not send_message_sync(chat_id=ADMIN_CHAT_ID, text="\n".join(lines)):
            logger.warning("Failed to notify admin about checkout for user %s", user_id)

    carts[int(user_id)] = {}
    _save_json("carts.json", carts)
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
