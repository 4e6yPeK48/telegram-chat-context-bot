# Telegram chat context bot

Бот собирает сообщения из чата, сохраняет их в SQLite и по команде делает краткую сводку контекста.

Примеры команд:

- `!сводка 300` — сводка последних 300 сообщений
- `/summary 100` — то же самое
- `/start` — справка по боту

## Возможности

- хранение контекста чата в `SQLite`
- сводка через OpenAI-compatible API, включая `OpenRouter`
- обработка обычных сообщений, медиа и простых служебных событий
- очистка старых сообщений по сроку хранения

## Требования

- Python 3.10+
- Telegram Bot Token от `@BotFather`
- API key для OpenAI или OpenRouter

## Быстрый запуск локально

1. Склонируйте проект или загрузите его на машину.
2. Создайте виртуальное окружение:

   ```bash
   python -m venv .venv
   ```

3. Активируйте окружение:

   ```bash
   .\.venv\Scripts\Activate.ps1
   ```

4. Установите зависимости:

   ```bash
   pip install -r requirements.txt
   ```

5. Создайте файл `.env` рядом с `main.py` и заполните его по примеру из `.env.example`.
6. Запустите бота:

   ```bash
   python main.py
   ```

## Переменные окружения

Основные параметры:

- `TELEGRAM_BOT_TOKEN` — токен бота из BotFather
- `OPENAI_API_KEY` — ключ API
- `OPENAI_BASE_URL` — базовый URL API
  - для OpenRouter: `https://openrouter.ai/api/v1`
  - для OpenAI: `https://api.openai.com/v1`
- `OPENAI_MODEL` — имя модели, например `openrouter/free` или другая доступная модель
- `DATABASE_PATH` — путь к SQLite-файлу, по умолчанию `data/chat_context.sqlite3`
- `RETENTION_DAYS` — сколько дней хранить сообщения, по умолчанию `30`
- `DEFAULT_SUMMARY_MESSAGES` — сколько сообщений брать по умолчанию, по умолчанию `80`
- `MAX_SUMMARY_MESSAGES` — максимальный лимит для одной сводки, по умолчанию `500`
- `SUMMARY_CHUNK_SIZE_CHARS` — размер одного чанка для обработки, по умолчанию `10000`
- `LOG_LEVEL` — `INFO`, `DEBUG`, `WARNING` и т. д.

Если используете OpenRouter, можно дополнительно задать:

- `OPENROUTER_HTTP_REFERER`
- `OPENROUTER_APP_NAME`

## Настройка Telegram

1. Создайте бота через `@BotFather`.
2. Отключите `Privacy Mode`, чтобы бот видел обычные сообщения в группе.
3. Добавьте бота в нужный чат.
4. После этого в чате можно писать `!сводка 500` или `/summary 500`.

## Запуск на Ubuntu VPS

### 1. Установите системные пакеты

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

### 2. Разместите проект

Например, в `/opt/telegram-chat-context-bot`:

```bash
sudo mkdir -p /opt/telegram-chat-context-bot
sudo chown -R $USER:$USER /opt/telegram-chat-context-bot
cd /opt/telegram-chat-context-bot
```

Скопируйте туда файлы проекта или выполните `git clone`.

### 3. Создайте виртуальное окружение и установите зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Создайте `.env`

```bash
cp .env.example .env
nano .env
```

Пример для OpenRouter:

```env
TELEGRAM_BOT_TOKEN=123456:ABCDEF
OPENAI_API_KEY=or-xxxxxxxxxxxxxxxx
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_MODEL=openrouter/free
DATABASE_PATH=data/chat_context.sqlite3
RETENTION_DAYS=30
DEFAULT_SUMMARY_MESSAGES=80
MAX_SUMMARY_MESSAGES=500
SUMMARY_CHUNK_SIZE_CHARS=10000
LOG_LEVEL=INFO
OPENROUTER_HTTP_REFERER=https://your-domain.example
OPENROUTER_APP_NAME=telegram-chat-context-bot
```

### 5. Проверочный запуск

```bash
source .venv/bin/activate
python main.py
```

Если бот стартовал без ошибок, можно переводить его в фон через `systemd`.

## systemd service

Создайте файл `/etc/systemd/system/telegram-chat-context-bot.service`:

```ini
[Unit]
Description=Telegram Chat Context Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/telegram-chat-context-bot
EnvironmentFile=/opt/telegram-chat-context-bot/.env
ExecStart=/opt/telegram-chat-context-bot/.venv/bin/python /opt/telegram-chat-context-bot/main.py
Restart=always
RestartSec=5
User=YOUR_USER

[Install]
WantedBy=multi-user.target
```

Дальше:

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-chat-context-bot
sudo systemctl start telegram-chat-context-bot
sudo systemctl status telegram-chat-context-bot
```

Логи:

```bash
journalctl -u telegram-chat-context-bot -f
```

## Полезные замечания

- Бот не увидит обычные сообщения в группе, если не выключить Privacy Mode.
- Контекст хранится в SQLite с очисткой старых сообщений по `RETENTION_DAYS` (по умолчанию 30 дней); дополнительно бот запускает фоновую чистку раз в 3 дня.
- Если чат очень активный, увеличивать `MAX_SUMMARY_MESSAGES` выше 500 обычно не нужно.
- История хранится в SQLite-файле и переживает перезапуск VPS.



