import asyncio
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
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0")) or None
ADMIN_PANEL_URL = os.environ.get("ADMIN_PANEL_URL")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")
SAMOPIS_NICK = os.environ.get("SAMOPIS_NICK", "").lstrip("@")

if not BOT_TOKEN or not WEBAPP_URL:
    raise RuntimeError("BOT_TOKEN and WEBAPP_URL are required")

request_client = HTTPXRequest(
    connection_pool_size=int(os.environ.get("TG_POOL_SIZE", "20")),
    connect_timeout=float(os.environ.get("TG_CONNECT_TIMEOUT", "10")),
    read_timeout=float(os.environ.get("TG_READ_TIMEOUT", "20")),
    write_timeout=float(os.environ.get("TG_WRITE_TIMEOUT", "20")),
    pool_timeout=float(os.environ.get("TG_POOL_TIMEOUT", "10")),
)

app = Flask(__name__, static_folder="static", static_url_path="/static")
bot = Bot(token=BOT_TOKEN, request=request_client)

DATA_DIR = Path("data")
IMAGES_DIR = DATA_DIR / "images"
THUMBS_DIR = DATA_DIR / "thumbs"
for d in (DATA_DIR, IMAGES_DIR, THUMBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shop")


def clone(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False))


def save_json(name: str, data: Any) -> None:
    path = DATA_DIR / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_json(name: str, default: Any) -> Any:
    path = DATA_DIR / name
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as exc:
            log.warning("Failed to read %s: %s", name, exc)
    data = clone(default)
    save_json(name, data)
    return data


def normalize_carts(raw: Dict[str, Any]) -> Dict[int, Dict[int, int]]:
    carts: Dict[int, Dict[int, int]] = {}
    for uid, cart in raw.items():
        try:
            uid_int = int(uid)
        except Exception:
            continue
        if not isinstance(cart, dict):
            continue
        carts[uid_int] = {}
        for pid, qty in cart.items():
            try:
                carts[uid_int][int(pid)] = int(qty)
            except Exception:
                continue
    return carts


def load_state() -> None:
    global categories, products, carts, logs, bans, settings  # noqa: PLW0603
    categories = load_json("categories.json", [])
    products = load_json("products.json", [])
    carts = normalize_carts(load_json("carts.json", {}))
    logs = load_json("logs.json", [])
    bans = load_json("bans.json", [])
    settings = load_json("settings.json", {"mode": "samootsos"})


load_state()


def log_event(kind: str, user_id: int | None = None, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    entry = {"ts": datetime.utcnow().isoformat() + "Z", "kind": kind, "user_id": user_id, "payload": payload or {}}
    logs.append(entry)
    if len(logs) > 200:
        logs.pop(0)
    save_json("logs.json", logs)
    return entry


def next_id(items: List[Dict[str, Any]]) -> int:
    return max((item["id"] for item in items), default=0) + 1


def is_banned(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return any(b.get("user_id") == int(user_id) for b in bans)


def safe_media_path(rel_path: str) -> Path:
    candidate = (DATA_DIR / rel_path).resolve()
    if not str(candidate).startswith(str(DATA_DIR.resolve())):
        abort(400, description="invalid media path")
    return candidate


def verify_secret() -> None:
    if not WEBHOOK_SECRET:
        return
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(401)


def require_admin() -> None:
    if not ADMIN_TOKEN:
        return
    provided = request.headers.get("X-Admin-Token") or request.args.get("token")
    if provided != ADMIN_TOKEN:
        abort(401)


def resolve_admin_url() -> str:
    if ADMIN_PANEL_URL:
        return ADMIN_PANEL_URL
    base = WEBAPP_URL.rsplit("/webapp", 1)[0] if WEBAPP_URL.endswith("/webapp") else WEBAPP_URL
    return base.rstrip("/") + "/admin"


def send_sync(chat_id: int, text: str, reply_markup: Any | None = None) -> bool:
    async def run() -> None:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    try:
        asyncio.run(run())
        return True
    except (TimedOut, NetworkError) as exc:
        log.warning("Send timeout/network: %s", exc)
    except Exception as exc:
        log.exception("Send failed: %s", exc)
    return False


def notify_admin(text: str) -> bool:
    if not ADMIN_CHAT_ID:
        return False
    if send_sync(ADMIN_CHAT_ID, text):
        return True
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text},
            timeout=10,
        )
        if resp.ok:
            return True
        log.warning("Fallback sendMessage failed: %s %s", resp.status_code, resp.text)
    except Exception as exc:
        log.warning("Fallback sendMessage exception: %s", exc)
    return False


def download_image(url: str) -> Image.Image | None:
    candidates = [url]
    if "postimg.cc" in url:
        parts = [p for p in url.split("/") if p]
        if len(parts) >= 2:
            album, name = parts[-2], parts[-1]
            candidates.insert(0, f"https://i.postimg.cc/{album}/{name}.jpg")
            candidates.insert(1, f"https://i.postimg.cc/{album}/{name}.png")
    img = None
    for candidate in candidates:
        try:
            resp = requests.get(candidate, timeout=20)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            break
        except Exception as exc:
            log.warning("Download failed %s: %s", candidate, exc)
    return img


def save_variants(img: Image.Image, product_id: int) -> Tuple[str | None, str | None]:
    def save_copy(max_size: int, folder: Path, prefix: str) -> str | None:
        try:
            clone = img.copy()
            clone.thumbnail((max_size, max_size))
            name = f"product_{product_id}.jpg"
            path = folder / name
            clone.save(path, format="JPEG", optimize=True, quality=85)
            return f"/media/{prefix}/{name}"
        except Exception as exc:
            log.warning("Save variant failed: %s", exc)
            return None

    return save_copy(1600, IMAGES_DIR, "images"), save_copy(600, THUMBS_DIR, "thumbs")


def get_product(product_id: int) -> Dict[str, Any] | None:
    return next((p for p in products if p["id"] == product_id), None)


def admin_mode() -> str:
    return settings.get("mode", "samootsos")


@app.route("/")
def health() -> Any:
    return {"ok": True}


@app.route("/webapp")
def webapp_page() -> Any:
    return send_from_directory(app.static_folder, "index.html")


@app.route("/admin")
def admin_page() -> Any:
    return send_from_directory(app.static_folder, "admin.html")


@app.route("/media/<path:filename>")
def media(filename: str) -> Any:
    path = safe_media_path(filename)
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(path)


@app.route("/api/categories", methods=["GET", "POST"])
def api_categories() -> Any:
    if request.method == "GET":
        return jsonify({"items": categories})
    require_admin()
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    icon = (body.get("icon") or "").strip() or None
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    cat = {"id": next_id(categories), "name": name, "icon": icon}
    categories.append(cat)
    log_event("category_created", payload=cat)
    save_json("categories.json", categories)
    return jsonify(cat), 201


@app.route("/api/categories/<int:cat_id>", methods=["PATCH", "DELETE"])
def api_category_update(cat_id: int) -> Any:
    require_admin()
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
        log_event("category_updated", payload=category)
        save_json("categories.json", categories)
        return jsonify(category)
    categories[:] = [c for c in categories if c["id"] != cat_id]
    for p in products:
        if p["category_id"] == cat_id:
            p["category_id"] = None
    log_event("category_deleted", payload={"id": cat_id})
    save_json("categories.json", categories)
    save_json("products.json", products)
    return jsonify({"ok": True})


@app.route("/api/products", methods=["GET", "POST"])
def api_products() -> Any:
    if request.method == "GET":
        cid = request.args.get("category_id", type=int)
        items = [p for p in products if p["category_id"] == cid] if cid else products
        return jsonify({"items": items})
    require_admin()
    body = request.get_json(force=True, silent=True) or {}
    title = (body.get("title") or "").strip()
    price = body.get("price")
    category_id = body.get("category_id")
    image_url = (body.get("image_url") or "").strip() or "/static/img/placeholder.svg"
    thumb_url = (body.get("thumb_url") or "").strip()
    description = (body.get("description") or "").strip()
    if not title or price is None or category_id is None:
        return jsonify({"ok": False, "error": "title, price, category_id required"}), 400
    product = {
        "id": next_id(products),
        "title": title,
        "price": float(price),
        "category_id": int(category_id),
        "image_url": image_url,
        "thumb_url": thumb_url,
        "description": description,
    }
    img = download_image(image_url)
    if img:
        main_url, thumb = save_variants(img, product["id"])
        if main_url:
            product["image_url"] = main_url
        if thumb:
            product["thumb_url"] = thumb
    if not product["thumb_url"]:
        product["thumb_url"] = product["image_url"]
    products.append(product)
    log_event("product_created", payload=product)
    save_json("products.json", products)
    return jsonify(product), 201


@app.route("/api/cart/add", methods=["POST"])
def api_cart_add() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    user_id = body.get("user_id")
    product_id = body.get("product_id")
    qty = int(body.get("qty") or 1)
    if not user_id or not product_id:
        return jsonify({"ok": False, "error": "user_id and product_id required"}), 400
    if is_banned(int(user_id)):
        return jsonify({"ok": False, "error": "banned"}), 403
    product = get_product(int(product_id))
    if not product:
        return jsonify({"ok": False, "error": "product not found"}), 404
    cart = carts.setdefault(int(user_id), {})
    cart[int(product_id)] = cart.get(int(product_id), 0) + max(1, qty)
    log_event("cart_add", user_id=int(user_id), payload={"product_id": int(product_id), "qty": qty})
    save_json("carts.json", carts)
    return jsonify({"ok": True})


@app.route("/api/cart/<int:user_id>", methods=["GET"])
def api_cart_get(user_id: int) -> Any:
    cart = carts.get(user_id, {})
    items: List[Dict[str, Any]] = []
    total = 0.0
    for pid, qty in cart.items():
        product = get_product(pid)
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
    if is_banned(int(user_id)):
        return jsonify({"ok": False, "error": "banned"}), 403
    carts[int(user_id)] = {}
    log_event("cart_clear", user_id=int(user_id))
    save_json("carts.json", carts)
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
    if is_banned(int(user_id)):
        return jsonify({"ok": False, "error": "banned"}), 403
    cart = carts.get(int(user_id), {})
    items: List[Dict[str, Any]] = []
    total = 0.0
    for pid, qty in cart.items():
        product = get_product(pid)
        if not product:
            continue
        subtotal = product["price"] * qty
        total += subtotal
        items.append({"product": product, "qty": qty, "subtotal": subtotal})
    if not items:
        return jsonify({"ok": False, "error": "cart_empty"}), 400

    log_event("checkout", user_id=int(user_id), payload={"contact": contact, "note": note, "items": cart})

    mode = admin_mode()
    response: Dict[str, Any] = {"ok": True}
    lines: List[str] = []
    if mode == "samopis":
        greeting = f"Доброго времени суток, {SAMOPIS_NICK}!" if SAMOPIS_NICK else "Доброго времени суток!"
        lines.append(greeting)
        lines.append("Хотел бы приобрести следующие товары:")
    else:
        lines.extend(
            [
                "🛒 Новый заказ",
                f"user_id: {user_id}",
                f"tg: @{tg_username}" if tg_username else "tg: не указал",
            ]
        )
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
        if SAMOPIS_NICK:
            text = "\n".join(lines)
            response["redirect"] = f"https://t.me/{SAMOPIS_NICK}?text={quote_plus(text)}"
            response["message"] = "Откройте диалог для оформления."
    else:
        if ADMIN_CHAT_ID:
            if not notify_admin("\n".join(lines)):
                log.warning("Failed to notify admin about checkout for user %s", user_id)
        response["message"] = "Заказ принят. Менеджер свяжется в Telegram."

    carts[int(user_id)] = {}
    save_json("carts.json", carts)
    return jsonify(response)


@app.route("/api/admin/logs", methods=["GET"])
def api_admin_logs() -> Any:
    require_admin()
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"items": logs[-limit:]})


@app.route("/api/admin/bans", methods=["GET", "POST"])
def api_admin_bans() -> Any:
    require_admin()
    if request.method == "GET":
        return jsonify({"items": bans})
    body = request.get_json(force=True, silent=True) or {}
    user_id = body.get("user_id")
    reason = (body.get("reason") or "").strip()
    try:
        uid = int(user_id)
    except Exception:
        return jsonify({"ok": False, "error": "user_id required"}), 400
    if any(b["user_id"] == uid for b in bans):
        return jsonify({"ok": False, "error": "already banned"}), 400
    ban = {"user_id": uid, "reason": reason}
    bans.append(ban)
    log_event("user_banned", user_id=uid, payload={"reason": reason})
    save_json("bans.json", bans)
    return jsonify(ban), 201


@app.route("/api/admin/bans/<int:user_id>", methods=["DELETE"])
def api_admin_bans_delete(user_id: int) -> Any:
    require_admin()
    before = len(bans)
    bans[:] = [b for b in bans if b["user_id"] != user_id]
    if len(bans) == before:
        return jsonify({"ok": False, "error": "not found"}), 404
    log_event("user_unbanned", user_id=user_id)
    save_json("bans.json", bans)
    return jsonify({"ok": True})


@app.route("/api/admin/mode", methods=["GET", "POST"])
def api_admin_mode() -> Any:
    require_admin()
    if request.method == "GET":
        return jsonify({"mode": admin_mode()})
    body = request.get_json(force=True, silent=True) or {}
    mode = (body.get("mode") or "").strip()
    if mode not in {"samootsos", "samopis"}:
        return jsonify({"ok": False, "error": "invalid_mode"}), 400
    settings["mode"] = mode
    save_json("settings.json", settings)
    return jsonify({"ok": True, "mode": mode})


def create_data_zip(tmpdir: Path) -> Path:
    zip_path = tmpdir / "data.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in DATA_DIR.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(DATA_DIR))
    return zip_path


def safe_extract(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            name = member.filename
            if not name or name.endswith("/") and len(name.strip("/")) == 0:
                continue
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("unsafe path")
            dest = (DATA_DIR / name).resolve()
            if not str(dest).startswith(str(DATA_DIR.resolve())):
                raise ValueError("unsafe path")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if member.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            with zf.open(member) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)


@app.route("/api/admin/data/download", methods=["GET"])
def api_admin_data_download() -> Any:
    require_admin()
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = create_data_zip(Path(tmpdir))
        return send_file(zip_path, as_attachment=True, download_name="data.zip")


@app.route("/api/admin/data/upload", methods=["POST"])
def api_admin_data_upload() -> Any:
    require_admin()
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
            safe_extract(tmp_path)
        except Exception as exc:
            log.exception("Extract failed: %s", exc)
            return jsonify({"ok": False, "error": "extract_failed"}), 400
    for d in (IMAGES_DIR, THUMBS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    load_state()
    return jsonify({"ok": True})


@app.route("/api/ping", methods=["POST"])
def api_ping() -> Any:
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "друг").strip()
    return jsonify({"ok": True, "greeting": f"Привет, {name}!"})


@app.route("/api/status", methods=["POST"])
def api_status() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    user_id = body.get("user_id")
    try:
        uid = int(user_id)
    except Exception:
        return jsonify({"ok": True, "banned": False, "mode": admin_mode()})
    banned = is_banned(uid)
    reason = ""
    if banned:
        match = next((b for b in bans if b.get("user_id") == uid), None)
        reason = (match or {}).get("reason") or ""
    return jsonify({"ok": True, "banned": banned, "reason": reason, "mode": admin_mode()})


def handle_update(update: Update) -> None:
    if not update.message:
        return
    chat_id = update.message.chat.id
    if update.message.web_app_data:
        payload = update.message.web_app_data.data
        log_event("web_app_data", user_id=chat_id, payload={"raw": payload})
        send_sync(chat_id, f"Спасибо, получил: {payload}")
        return
    text = update.message.text or ""
    if text.startswith("/start"):
        buttons = [InlineKeyboardButton(text="Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))]
        if ADMIN_CHAT_ID and chat_id == ADMIN_CHAT_ID:
            buttons.append(InlineKeyboardButton(text="Админ-панель", url=resolve_admin_url()))
        kb = InlineKeyboardMarkup([buttons])
        send_sync(chat_id, "Нажми кнопку, чтобы открыть мини-приложение.", reply_markup=kb)
    else:
        send_sync(chat_id, "Отправь /start, чтобы открыть мини-приложение.")


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook() -> Any:
    verify_secret()
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"ok": False, "description": "empty body"})
    update = Update.de_json(data, bot)
    try:
        handle_update(update)
    except Exception as exc:
        log.exception("Handle update failed: %s", exc)
        return jsonify({"ok": False})
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
