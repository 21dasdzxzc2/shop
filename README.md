# Telegram WebApp магазин (Flask + Telegram Bot)

Функционал:
- Магазин (WebApp): категории, товары, поиск, карточки с миниатюрами, просмотр товара, корзина, оформление заказа.
- Админка: категории/товары, баны пользователей, просмотр логов, экспорт/импорт `data`, файловый список.
- Webhook для бота: выдача кнопок WebApp, при чекауте — уведомление админу.

## Переменные окружения
- `BOT_TOKEN` — токен бота.
- `WEBAPP_URL` — URL WebApp (например, `https://<app>.onrender.com/webapp`).
- `WEBHOOK_SECRET` — секрет для вебхука (`setWebhook secret_token`).
- `ADMIN_CHAT_ID` — Telegram ID админа (получает кнопку на админку и уведомления о заказах).
- `ADMIN_TOKEN` — токен для защищённых админ-эндпоинтов/админки (`X-Admin-Token` или `?token=`).
- `PORT` — Render ставит сам (локально можно 8000).

## Структура данных/медиа
- Все данные: `data/` (JSON: categories, products, carts, logs, bans).
- Изображения товаров: `data/images/…` (JPEG, сжатие до 1600px).
- Миниатюры: `data/thumbs/…` (JPEG, сжатие до 600px).
- Статика: `static/` (HTML/CSS).
- Доступ к медиа: `/media/images/<file>.jpg`, `/media/thumbs/<file>.jpg`.

> Важно: на Render файловая система по умолчанию эфемерная. Нужен Render Disk или регулярный экспорт/импорт `data.zip`, чтобы не терять данные/картинки при redeploy.

## Локальный запуск
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export BOT_TOKEN=...
export WEBAPP_URL=http://localhost:8000/webapp
export WEBHOOK_SECRET=local-secret
export ADMIN_CHAT_ID=<ваш_id>
export ADMIN_TOKEN=dev-token

python app.py  # 0.0.0.0:8000
```
Проверка:
- WebApp: http://localhost:8000/webapp
- Медиа: http://localhost:8000/media/<...>
- Админка: http://localhost:8000/admin (по умолчанию путь `/admin`).

## Развёртывание (Render)
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app`
- Env: `BOT_TOKEN`, `WEBAPP_URL`, `WEBHOOK_SECRET`, `ADMIN_CHAT_ID`, `ADMIN_TOKEN` (опц. `ADMIN_PANEL_URL`).
- После старта: поставить вебхук
```bash
curl -X POST "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" \
  -d "url=https://<app>.onrender.com/telegram/webhook" \
  -d "secret_token=$WEBHOOK_SECRET"
```
Проверка: `getWebhookInfo`.

> Для стойкости медиа/данных подключите Render Disk и/или регулярно экспортируйте `data.zip` через админку.

## Поведение бота
- `/start` обычному пользователю: кнопка «Открыть магазин».
- `/start` для `ADMIN_CHAT_ID`: кнопка магазина + ссылка на админку.
- При `checkout` WebApp: админу прилетает уведомление с корзиной, суммой, контактами.
- Бан: если user_id в банах, WebApp показывает экран «ты забанен», API возвращает 403.

## API (ключевые)
- `GET /api/categories`, `POST /api/categories` (admin), `PATCH/DELETE /api/categories/<id>` (admin).
- `GET /api/products[?category_id=]`, `POST /api/products` (admin; скачивание изображения, генерация превью).
- `POST /api/cart/add`, `GET /api/cart/<user_id>`, `POST /api/cart/clear`, `POST /api/cart/checkout`.
- `GET /api/admin/logs` (admin).
- `GET/POST /api/admin/bans`, `DELETE /api/admin/bans/<user_id>` (admin).
- `POST /api/status` — проверка бана (WebApp).
- Медиа: `GET /media/<path>` из `data`.
- Админ резерв: `GET /api/admin/data/download`, `POST /api/admin/data/upload` (zip).

## Админка (web UI)
Доступна на `/admin` (если не переопределено `ADMIN_PANEL_URL`). Все действия требуют `ADMIN_TOKEN`:
- Категории: добавить/редактировать/удалить.
- Товары: добавить (указать URL картинки, превью генерится автоматически, хранится в `data/images` и `data/thumbs`).
- Логи: просмотр последних логов.
- Баны: добавить/снять бан по user_id.
- Архив data: скачать `data.zip` или загрузить новый (заменяет текущую `data/`, перезагружаются JSON и пересоздаются папки медиа).

## WebApp (покупатель)
- Категории, поиск, карточки, просмотр товара (большое изображение), миниатюры.
- Корзина: добавить, очистить, оформить заказ. Пустую корзину оформить нельзя.
- Чекаут отправляет контакт/комментарий; уведомление уходит админу.

## Ограничения/заметки
- По умолчанию плейсхолдер картинок: `/static/img/placeholder.svg`.
- Медиа в `data/` пропадают при redeploy, если нет диска/бэкапа.
- Админ API и админка защищены токеном `ADMIN_TOKEN`. P.S. Путь админки можно сменить через `ADMIN_PANEL_URL`.

## Типовые команды
- Проверка вебхука: `curl -s "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo"`
- Сброс вебхука: `setWebhook` как выше.
- Скачать архив data (нужен токен): `curl -H "X-Admin-Token: $ADMIN_TOKEN" -O http://localhost:8000/api/admin/data/download`
- Загрузить архив data: `curl -H "X-Admin-Token: $ADMIN_TOKEN" -F "file=@data.zip" http://localhost:8000/api/admin/data/upload`

## Что можно доработать
- Перевести данные/медиа на персистентный диск или внешнее хранилище (S3/R2/B2).
- Авторизация в админке (JWT/Telegram Login).
- БД (Postgres/SQLite) вместо JSON.
- Нотификации о заказах в отдельный тех-чат, платежи, статусы заказов.


ПАРОЛЬ ЛОГИН ОТ ГИТХАБА И HOTMAIL
chhommdingafzny@hotmail.com:CSQguywFsCUbf7

 codex resume 019af864-8edb-77f1-8703-3c439c2dbd4c

