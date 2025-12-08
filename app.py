import asyncio
import copy
import io
import json
import logging
import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import quote_plus

import requests
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
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
DATA_DIR = Path("data")
IMAGES_DIR = DATA_DIR / "images"
THUMBS_DIR = DATA_DIR / "thumbs"
for d in (DATA_DIR, IMAGES_DIR, THUMBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

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


def _is_banned(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return any(ban.get("user_id") == int(user_id) for ban in bans)


def _safe_media_path(rel_path: str) -> Path:
    candidate = (DATA_DIR / rel_path).resolve()
    if not str(candidate).startswith(str(DATA_DIR.resolve())):
        abort(400, description="invalid media path")
    return candidate


LOG_LIMIT = 200


def _load_all_data() -> None:
    global categories, products, carts, logs, bans, settings  # noqa: PLW0603
    categories = _load_json("categories.json", [])
    products = _load_json("products.json", [])
    carts_raw = _load_json("carts.json", {})
    carts = _normalize_carts(carts_raw)
    logs = _load_json("logs.json", [])
    bans = _load_json("bans.json", [])
    settings = _load_json("settings.json", {"mode": "samootsos"})


def _get_samopis_nick() -> str:
    return os.environ.get("SAMOPIS_NICK", "").lstrip("@")


_load_all_data()


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


def notify_admin(text: str) -> bool:
    """Send message to admin; fallback to direct HTTP if Telegram client fails."""
    if not ADMIN_CHAT_ID:
        return False

    if send_message_sync(chat_id=ADMIN_CHAT_ID, text=text):
        return True

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text},
            timeout=10,
        )
        if resp.ok:
            return True
        logger.warning("Fallback sendMessage failed: %s %s", resp.status_code, resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fallback sendMessage exception: %s", exc)
    return False


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

    def save_variant(img_obj: Image.Image, max_size: int, folder: Path, suffix: str, url_prefix: str) -> str | None:
        try:
            clone = img_obj.copy()
            clone.thumbnail((max_size, max_size))
            filename = f"product_{product_id}{suffix}.jpg"
            out_path = folder / filename
            clone.save(out_path, format="JPEG", optimize=True, quality=85)
            return f"{url_prefix}/{filename}"
        except Exception as inner_exc:  # noqa: BLE001
            logger.warning("Save image variant failed: %s", inner_exc)
            return None

    main_path = save_variant(img, 1600, IMAGES_DIR, "", "/media/images")
    thumb_path = save_variant(img, 600, THUMBS_DIR, "", "/media/thumbs")
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


@app.route("/media/<path:filename>")
def media(filename: str) -> Any:
    path = _safe_media_path(filename)
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(path)


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

    if _is_banned(int(user_id)):
        return jsonify({"ok": False, "error": "banned"}), 403

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
    if _is_banned(int(user_id)):
        return jsonify({"ok": False, "error": "banned"}), 403
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
    if _is_banned(int(user_id)):
        return jsonify({"ok": False, "error": "banned"}), 403

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
    if not items:
        return jsonify({"ok": False, "error": "cart_empty"}), 400

    _log_event(
        "checkout",
        user_id=int(user_id),
        payload={"contact": contact, "note": note, "items": cart},
    )

    mode = settings.get("mode", "samootsos")
    response: Dict[str, Any] = {"ok": True}

    lines: List[str] = []
    if mode == "samopis":
        nick = _get_samopis_nick()
        if nick:
            lines.append(f"Доброго времени суток, {nick}!")
        else:
            lines.append("Доброго времени суток!")
        lines.append("Хотел бы приобрести следующие товары:")
    else:
        lines.extend([
            "🛒 Новый заказ",
            f"user_id: {user_id}",
            f"tg: @{tg_username}" if tg_username else "tg: не указал",
        ])
        if tg_name:
            lines.append(f"имя: {tg_name}")
        lines.append(f"контакт: {contact or '—'}")
        if note:
            lines.append(f"Комментарий: {note}")

    lines.append("Позиций:")
    for row in items:
        lines.append(f"- {row['product']['title']} x{row['qty']} = {int(row['subtotal'])}₽")
    lines.append(f"Итого: {int(total)}₽")

    if mode == "samopis":
        nick = _get_samopis_nick()
        if nick:
            text = "\n".join(lines)
            redirect_url = f"https://t.me/{nick}?text={quote_plus(text)}"
            response["redirect"] = redirect_url
            response["message"] = "Откройте диалог для оформления."
    else:
        if ADMIN_CHAT_ID:
            if not notify_admin("\n".join(lines)):
                logger.warning("Failed to notify admin about checkout for user %s", user_id)
        response["message"] = "Заказ принят. Менеджер свяжется в Telegram."

    carts[int(user_id)] = {}
    _save_json("carts.json", carts)
    return jsonify(response)


@app.route("/api/admin/logs", methods=["GET"])
def api_admin_logs() -> Any:
    _require_admin()
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"items": logs[-limit:]})


@app.route("/api/admin/bans", methods=["GET", "POST"])
def api_admin_bans() -> Any:
    _require_admin()
    if request.method == "GET":
        return jsonify({"items": bans})
    body = request.get_json(force=True, silent=True) or {}
    user_id = body.get("user_id")
    reason = (body.get("reason") or "").strip()
    try:
        user_id_int = int(user_id)
    except Exception:
        return jsonify({"ok": False, "error": "user_id required"}), 400
    if any(b["user_id"] == user_id_int for b in bans):
        return jsonify({"ok": False, "error": "already banned"}), 400
    ban = {"user_id": user_id_int, "reason": reason}
    bans.append(ban)
    _log_event("user_banned", user_id=user_id_int, payload={"reason": reason})
    _save_json("bans.json", bans)
    return jsonify(ban), 201


@app.route("/api/admin/bans/<int:user_id>", methods=["DELETE"])
def api_admin_bans_delete(user_id: int) -> Any:
    _require_admin()
    initial = len(bans)
    bans[:] = [b for b in bans if b["user_id"] != user_id]
    if len(bans) == initial:
        return jsonify({"ok": False, "error": "not found"}), 404
    _log_event("user_unbanned", user_id=user_id)
    _save_json("bans.json", bans)
    return jsonify({"ok": True})


@app.route("/api/admin/mode", methods=["GET", "POST"])
def api_admin_mode() -> Any:
    _require_admin()
    if request.method == "GET":
        return jsonify({"mode": settings.get("mode", "samootsos")})
    body = request.get_json(force=True, silent=True) or {}
    mode = (body.get("mode") or "").strip()
    if mode not in {"samootsos", "samopis"}:
        return jsonify({"ok": False, "error": "invalid_mode"}), 400
    settings["mode"] = mode
    _save_json("settings.json", settings)
    return jsonify({"ok": True, "mode": mode})


def _create_data_zip(tmpdir: Path, name: str = "data.zip") -> Path:
    zip_path = tmpdir / name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in DATA_DIR.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(DATA_DIR))
    return zip_path


@app.route("/api/admin/data/download", methods=["GET"])
def api_admin_data_download() -> Any:
    _require_admin()
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = _create_data_zip(Path(tmpdir))
        return send_file(zip_path, as_attachment=True, download_name="data.zip")


def _safe_extract(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            name = member.filename
            if not name or name.endswith("/") and len(name.strip("/")) == 0:
                continue
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("unsafe path in archive")
            dest_path = (DATA_DIR / name).resolve()
            if not str(dest_path).startswith(str(DATA_DIR.resolve())):
                raise ValueError("unsafe path in archive")
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if member.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)
                continue
            with zf.open(member) as src, open(dest_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


@app.route("/api/admin/data/upload", methods=["POST"])
def api_admin_data_upload() -> Any:
    _require_admin()
    file = request.files.get("file")
    if not file:
        return jsonify({"ok": False, "error": "file_required"}), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "upload.zip"
        file.save(tmp_path)
        try:
            for child in DATA_DIR.iterdir():
                if child.is_file():
                    child.unlink()
                else:
                    shutil.rmtree(child)
            _safe_extract(tmp_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to extract data upload: %s", exc)
            return jsonify({"ok": False, "error": "extract_failed"}), 400

    for d in (IMAGES_DIR, THUMBS_DIR):
        d.mkdir(parents=True, exist_ok=True)

    _load_all_data()
    return jsonify({"ok": True})


@app.route("/api/ping", methods=["POST"])
def api_ping() -> Any:
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "друг").strip()
    greeting = f"Привет, {name}!"
    return jsonify({"ok": True, "greeting": greeting})


@app.route("/api/status", methods=["POST"])
def api_status() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    user_id = body.get("user_id")
    try:
        user_id_int = int(user_id)
    except Exception:
        return jsonify({"ok": True, "banned": False, "mode": settings.get("mode", "samootsos")})
    banned = _is_banned(user_id_int)
    reason = ""
    if banned:
        match = next((b for b in bans if b.get("user_id") == user_id_int), None)
        reason = (match or {}).get("reason") or ""
    return jsonify({"ok": True, "banned": banned, "reason": reason, "mode": settings.get("mode", "samootsos")})


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
