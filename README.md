# Telegram WebApp магазин (Flask + python-telegram-bot)

Готовый шаблон магазина для Telegram WebApp:
- кнопка WebApp в боте, для админа дополнительно ссылка на админку;
- каталог с категориями и товарами, корзина, оформление;
- админка (добавление товаров/категорий, просмотр логов);
- webhook для Telegram.

> Данные хранятся в памяти процесса (демо). Для продакшена подключайте БД (Postgres/SQLite) и сохранение на диск.

## Переменные окружения
- `BOT_TOKEN` — токен бота.
- `WEBAPP_URL` — URL WebApp (пример: `https://<app>.onrender.com/webapp`).
- `WEBHOOK_SECRET` — секрет для вебхука (`setWebhook secret_token`).
- `ADMIN_CHAT_ID` — Telegram ID админа; при `/start` ему высылаются 2 кнопки (WebApp + ссылка на админку).
- `ADMIN_PANEL_URL` — опционально, если админка на другом URL (по умолчанию `<WEBAPP_URL>/../admin`).
- `ADMIN_TOKEN` — токен для защищённых админ-эндпоинтов (`X-Admin-Token` или `?token=...`).
- `PORT` — Render задаёт автоматически (локально можно 8000).

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

python app.py  # запустит 0.0.0.0:8000
```
Проверка:
- WebApp: http://localhost:8000/webapp
- Админка: http://localhost:8000/admin (укажите `ADMIN_TOKEN` в поле «X-Admin-Token»).

## Развёртывание на Render
1. Создать Web Service, указать репозиторий.
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn app:app`
4. Environment: заполнить переменные из списка выше (минимум `BOT_TOKEN`, `WEBAPP_URL`, `WEBHOOK_SECRET`, `ADMIN_CHAT_ID`, `ADMIN_TOKEN`).
5. После первого старта поставить вебхук:
   ```bash
   export BOT_TOKEN=...
   export WEBHOOK_SECRET=...
   curl -X POST "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" \
     -d "url=https://<your-render>.onrender.com/telegram/webhook" \
     -d "secret_token=$WEBHOOK_SECRET"
   ```
6. Проверить:
   ```bash
   curl -s "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo"
   ```

### Обновление/редеплой
- Меняйте код → пуш в репозиторий → Render сам пересоберёт.
- После замены домена или `WEBHOOK_SECRET` переустановите вебхук командой выше.
- Если меняете `BOT_TOKEN` — обновите переменную на Render и снова `setWebhook`.

## Поведение бота
- `/start` обычному пользователю: кнопка «Открыть магазин» (WebApp).
- `/start` для `ADMIN_CHAT_ID`: две кнопки — WebApp и ссылка на админку.
- При отправке данных из WebApp (`tg.sendData`) бот отвечает «Спасибо, получил: ...».

## API (укорочено)
- `GET /api/categories` — список категорий.
- `POST /api/categories` — создать категорию (требует `ADMIN_TOKEN`).
- `GET /api/products[?category_id=]` — товары.
- `POST /api/products` — создать товар (требует `ADMIN_TOKEN`).
- `POST /api/cart/add` — `{user_id, product_id, qty}`.
- `GET /api/cart/<user_id>` — корзина.
- `POST /api/cart/clear` — `{user_id}`.
- `POST /api/cart/checkout` — `{user_id, contact, note}` (очищает корзину, пишет лог).
- `GET /api/admin/logs` — логи (требует `ADMIN_TOKEN`, параметр `limit`).

## Админка
- Доступна на `/admin`. Все запросы используют токен из поля `X-Admin-Token`.
- Что умеет: добавить категорию, добавить товар, посмотреть последние логи (cart, checkout, web_app_data, CRUD).
- Кнопка в боте выдаётся только `ADMIN_CHAT_ID`.

## Логи
- Хранятся в памяти (до 200 записей). Формат: `ts`, `kind`, `user_id`, `payload`.
- Для продакшена: сохраняйте в БД/файл, добавьте вывод в тех-чат Telegram при ошибках 5xx или при новых заказах.

## Что доработать для продакшена
- Перенести данные в Postgres/SQLite + SQLAlchemy/peewee.
- Авторизация в админке (Telegram Login Widget или JWT).
- Очереди/уведомления: отправка заказа в чат менеджеру, webhooks в CRM.
- Больше статуса у заказов (draft/paid/shipped) и почтовые/Push уведомления.
