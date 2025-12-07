# Telegram WebApp (Python + Flask)

Минимальный Telegram WebApp бот: отдаёт страницу с приветствием и кнопками, принимает вебхук и умеет получать `web_app_data`.

## Что нужно
- `BOT_TOKEN` — токен бота.
- `WEBAPP_URL` — полный URL страницы мини-приложения, например `https://<app>.onrender.com/webapp`.
- `WEBHOOK_SECRET` — любая строка, должна совпадать в вебхуке и настройке `setWebhook` (рекомендуется).

## Локальный запуск
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=...
export WEBAPP_URL=http://localhost:8000/webapp
export WEBHOOK_SECRET=local-secret
python app.py  # слушает на 0.0.0.0:8000
```

## Настройка вебхука
После деплоя (или локально через туннель) выполните:
```bash
curl -X POST "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" \
  -d "url=https://<domain>/telegram/webhook" \
  -d "secret_token=$WEBHOOK_SECRET"
```

## Render
- Build: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Environment: `BOT_TOKEN`, `WEBAPP_URL` (например `https://<app>.onrender.com/webapp`), `WEBHOOK_SECRET`, `PORT` (Render ставит сам).
- После первого старта выставьте вебхук командой выше (с реальным доменом Render).

## Проверка
- Напишите боту `/start` — придёт кнопка «Открыть мини-приложение».
- В WebApp:
  - «Поприветствовать» — запрос к бекенду `/api/ping`.
  - «Отправить данные в бот» — уходит `web_app_data`, ответ приходит в чат.
  - «Закрыть» — закрывает WebApp.
