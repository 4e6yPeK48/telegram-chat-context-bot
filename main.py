import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import aiohttp
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram-context-bot")

SUMMARY_REQUEST_RE = re.compile(r"^(?:!сводка|!summary)(?:\s+(\d+))?\s*$", re.IGNORECASE)
MAX_TELEGRAM_MESSAGE_LEN = 4096


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    database_path: Path
    retention_days: int
    default_summary_messages: int
    max_summary_messages: int
    chunk_size_chars: int
    openrouter_http_referer: str | None
    openrouter_app_name: str | None


@dataclass(frozen=True)
class ChatMessage:
    chat_id: int
    message_id: int
    author: str
    content_type: str
    text: str
    created_at: datetime


def _read_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def load_settings() -> Settings:
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if not telegram_token:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")
    if not openai_api_key:
        raise RuntimeError("Не задан OPENAI_API_KEY")

    return Settings(
        telegram_token=telegram_token,
        openai_api_key=openai_api_key,
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "openrouter/free").strip(),
        database_path=Path(os.getenv("DATABASE_PATH", "data/chat_context.sqlite3")).expanduser(),
        retention_days=_read_int("RETENTION_DAYS", 30),
        default_summary_messages=_read_int("DEFAULT_SUMMARY_MESSAGES", 50),
        max_summary_messages=_read_int("MAX_SUMMARY_MESSAGES", 300),
        chunk_size_chars=_read_int("SUMMARY_CHUNK_SIZE_CHARS", 12000),
        openrouter_http_referer=os.getenv("OPENROUTER_HTTP_REFERER", "").strip() or None,
        openrouter_app_name=os.getenv("OPENROUTER_APP_NAME", "").strip() or None,
    )


class MessageStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    CREATE TABLE messages
                    (
                        id           INTEGER PRIMARY KEY,
                        chat_id      INTEGER NOT NULL,
                        message_id   INTEGER NOT NULL,
                        author       TEXT    NOT NULL,
                        content_type TEXT    NOT NULL,
                        text         TEXT    NOT NULL,
                        created_at   TEXT    NOT NULL,
                        UNIQUE (chat_id, message_id)
                    )
                    """
                )
            except sqlite3.OperationalError as exc:
                if "already exists" not in str(exc).lower():
                    raise

            try:
                connection.execute("CREATE INDEX idx_messages_chat_created ON messages(chat_id, created_at DESC)")
            except sqlite3.OperationalError as exc:
                if "already exists" not in str(exc).lower():
                    raise

    def save_message(self, message: ChatMessage) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (chat_id, message_id, author, content_type, text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message.chat_id,
                    message.message_id,
                    message.author,
                    message.content_type,
                    message.text,
                    message.created_at.isoformat(),
                ),
            )

    def fetch_recent_messages(self, chat_id: int, limit: int) -> list[ChatMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, message_id, author, content_type, text, created_at
                FROM messages
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()

        result = [
            ChatMessage(
                chat_id=row["chat_id"],
                message_id=row["message_id"],
                author=row["author"],
                content_type=row["content_type"],
                text=row["text"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]
        result.reverse()
        return result

    def cleanup_old_messages(self, retention_days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM messages WHERE created_at < ?", (cutoff.isoformat(),))
            return cursor.rowcount


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def _display_name(message: Message) -> str:
    user = message.from_user
    if user is None:
        if message.sender_chat and message.sender_chat.title:
            return message.sender_chat.title
        return "Неизвестный автор"

    full_name = " ".join(part for part in [user.first_name, user.last_name] if part)
    if user.username:
        return f"{full_name} (@{user.username})" if full_name else f"@{user.username}"
    return full_name or str(user.id)


def _message_datetime(message: Message) -> datetime:
    message_dt = message.date
    if message_dt.tzinfo is None:
        return message_dt.replace(tzinfo=timezone.utc)
    return message_dt.astimezone(timezone.utc)


def _extract_content(message: Message) -> tuple[str | None, str]:
    if message.text:
        return message.text, "text"
    if message.caption:
        return message.caption, "caption"
    if message.sticker:
        emoji = f" {message.sticker.emoji}" if message.sticker.emoji else ""
        return f"[стикер{emoji}]", "sticker"
    if message.photo:
        return "[фото]", "photo"
    if message.video:
        return "[видео]", "video"
    if message.voice:
        return "[голосовое сообщение]", "voice"
    if message.document:
        name = message.document.file_name or "документ"
        return f"[документ: {name}]", "document"
    if message.audio:
        name = message.audio.file_name or "аудио"
        return f"[аудио: {name}]", "audio"
    if message.animation:
        return "[анимация]", "animation"
    if message.video_note:
        return "[видео-заметка]", "video_note"
    if message.contact:
        return "[контакт]", "contact"
    if message.location:
        return "[локация]", "location"
    if message.new_chat_members:
        names = ", ".join(member.full_name for member in message.new_chat_members)
        return f"[событие: в чат вошли {names}]", "service"
    if message.left_chat_member:
        return f"[событие: {message.left_chat_member.full_name} покинул(а) чат]", "service"
    if message.pinned_message:
        return "[событие: закреплённое сообщение]", "service"
    if message.group_chat_created:
        return "[событие: группа создана]", "service"
    if message.supergroup_chat_created:
        return "[событие: супергруппа создана]", "service"
    if message.channel_chat_created:
        return "[событие: канал создан]", "service"
    return None, "unknown"


def _format_record(message: ChatMessage) -> str:
    timestamp = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = _truncate(_normalize_text(message.text), 350)
    return f"[{timestamp}] {message.author}: {body}"


def _chunk_records(records: Sequence[ChatMessage], chunk_size_chars: int) -> list[list[ChatMessage]]:
    chunks: list[list[ChatMessage]] = []
    current: list[ChatMessage] = []
    current_size = 0

    for record in records:
        formatted = _format_record(record)
        size = len(formatted) + 1
        if current and current_size + size > chunk_size_chars:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(record)
        current_size += size

    if current:
        chunks.append(current)
    return chunks


class OpenRouterClient:
    def __init__(self, session: aiohttp.ClientSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def chat(self, messages: list[dict[str, str]], max_tokens: int) -> str:
        url = self.settings.openai_base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        if self.settings.openrouter_http_referer:
            headers["HTTP-Referer"] = self.settings.openrouter_http_referer
        if self.settings.openrouter_app_name:
            headers["X-Title"] = self.settings.openrouter_app_name

        payload = {
            "model": self.settings.openai_model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }

        timeout = aiohttp.ClientTimeout(total=90)
        async with self.session.post(url, headers=headers, json=payload, timeout=timeout) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"OpenAI-compatible API вернул {response.status}: {text[:500]}")

            try:
                data = await response.json(content_type=None)
            except aiohttp.ContentTypeError as exc:
                raise RuntimeError(f"Некорректный JSON от API: {text[:500]}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("API не вернул choices")

        content = choices[0].get("message", {}).get("content", "")
        return str(content).strip()


class ContextSummarizerBot:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self.store = MessageStore(settings.database_path)
        self.api = OpenRouterClient(session, settings)

    def initialize(self) -> None:
        self.store.initialize()
        removed = self.store.cleanup_old_messages(self.settings.retention_days)
        if removed:
            logger.info("Удалено устаревших сообщений: %s", removed)

    def is_summary_request(self, text: str) -> int | None:
        match = SUMMARY_REQUEST_RE.match(text.strip())
        if not match:
            return None
        requested = int(match.group(1)) if match.group(1) else self.settings.default_summary_messages
        return max(1, min(requested, self.settings.max_summary_messages))

    def make_message_record(self, message: Message) -> ChatMessage | None:
        if message.from_user and message.from_user.is_bot:
            return None

        content, content_type = _extract_content(message)
        if content is None or message.chat is None:
            return None

        return ChatMessage(
            chat_id=message.chat.id,
            message_id=message.message_id,
            author=_display_name(message),
            content_type=content_type,
            text=content,
            created_at=_message_datetime(message),
        )

    async def reply_long(self, message: Message, text: str) -> None:
        if len(text) <= MAX_TELEGRAM_MESSAGE_LEN:
            await message.answer(text)
            return

        for start in range(0, len(text), MAX_TELEGRAM_MESSAGE_LEN):
            await message.answer(text[start: start + MAX_TELEGRAM_MESSAGE_LEN])

    def build_chunk_prompt(self, chat_title: str, chunk_index: int, total_chunks: int, chunk_text: str) -> list[
        dict[str, str]]:
        system_prompt = (
            "Ты анализируешь Telegram-чат и делаешь очень короткую, полезную сводку по-русски. "
            "Не выдумывай факты. Если в данных мало смысла, честно скажи об этом. "
            "Старайся выделить темы, решения, вопросы и действия."
        )
        user_prompt = (
            f"Чат: {chat_title}\n"
            f"Фрагмент: {chunk_index}/{total_chunks}\n\n"
            f"Сообщения:\n{chunk_text}\n\n"
            "Сделай промежуточную сводку этого фрагмента в 4-8 пунктов. "
            "Пиши кратко, без длинных вступлений и без лишних повторов."
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def build_final_prompt(self, chat_title: str, requested_count: int, chunk_summaries: list[str]) -> list[
        dict[str, str]]:
        system_prompt = (
            "Ты сводишь промежуточные результаты анализа Telegram-чата в одну короткую итоговую сводку по-русски. "
            "Не добавляй фактов, которых нет в исходных данных. "
            "Пиши структурированно и очень кратко."
        )
        user_prompt = (
                f"Чат: {chat_title}\n"
                f"Нужно учесть последние примерно {requested_count} сообщений.\n\n"
                "Промежуточные сводки:\n"
                + "\n\n".join(f"--- Фрагмент {i + 1} ---\n{summary}" for i, summary in enumerate(chunk_summaries))
                + "\n\n"
                  "Сделай итоговую сводку в формате:\n"
                  "1) Кратко: 2-4 предложения\n"
                  "2) Основные темы: 3-6 буллетов\n"
                  "3) Решения/договорённости\n"
                  "4) Открытые вопросы или следующие шаги\n"
                  "Если данных недостаточно — так и скажи."
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    async def build_summary(self, chat_id: int, chat_title: str, requested_count: int) -> str:
        records = await asyncio.to_thread(self.store.fetch_recent_messages, chat_id, requested_count)
        if not records:
            return "Пока нет сохранённого контекста для этого чата."

        chunks = _chunk_records(records, self.settings.chunk_size_chars)
        if not chunks:
            return "Пока нет сохранённого контекста для этого чата."

        chunk_summaries: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_text = "\n".join(_format_record(record) for record in chunk)
            summary = await self.api.chat(self.build_chunk_prompt(chat_title, index, len(chunks), chunk_text),
                                          max_tokens=500)
            chunk_summaries.append(summary or "Краткая сводка этого фрагмента не получилась.")

        if len(chunk_summaries) == 1:
            return chunk_summaries[0]

        final_summary = await self.api.chat(self.build_final_prompt(chat_title, requested_count, chunk_summaries),
                                            max_tokens=700)
        return final_summary or "\n\n".join(chunk_summaries)

    async def handle_start(self, message: Message) -> None:
        await message.answer(
            "Я собираю сообщения чата и умею делать короткую сводку контекста.\n\n"
            "Примеры:\n"
            "• !сводка 300 — сводка последних 300 сообщений\n"
            "• /summary 100 — то же самое\n\n"
            "Важно: бот должен видеть сообщения в группе. Для этого в BotFather обычно нужно отключить privacy mode."
        )

    async def handle_summary_command(self, message: Message, command: CommandObject) -> None:
        if message.chat is None:
            return

        requested_count = self.settings.default_summary_messages
        if command.args:
            try:
                requested_count = int(command.args.split()[0])
            except (ValueError, IndexError):
                requested_count = self.settings.default_summary_messages

        requested_count = max(1, min(requested_count, self.settings.max_summary_messages))
        chat_title = message.chat.title or "частный чат"

        await message.answer(f"Собираю сводку по последним {requested_count} сообщениям...")
        try:
            summary = await self.build_summary(message.chat.id, chat_title, requested_count)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось сгенерировать сводку")
            await message.answer(f"Не удалось сгенерировать сводку: {exc}")
            return

        await self.reply_long(message, summary)

    async def handle_text_message(self, message: Message) -> None:
        if message.chat is None:
            return

        text = message.text or message.caption or ""
        requested_count = self.is_summary_request(text)
        if requested_count is not None:
            await message.answer(f"Собираю сводку по последним {requested_count} сообщениям...")
            try:
                summary = await self.build_summary(message.chat.id, message.chat.title or "чат", requested_count)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Не удалось сгенерировать сводку")
                await message.answer(f"Не удалось сгенерировать сводку: {exc}")
                return

            await self.reply_long(message, summary)
            return

        if text.startswith("/"):
            return

        record = self.make_message_record(message)
        if record is None:
            return

        try:
            await asyncio.to_thread(self.store.save_message, record)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось сохранить сообщение")
            await message.answer(f"Не удалось сохранить сообщение для контекста: {exc}")


async def main() -> None:
    settings = load_settings()
    bot = Bot(token=settings.telegram_token)
    dp = Dispatcher()
    router = Router()

    async with aiohttp.ClientSession() as session:
        app = ContextSummarizerBot(settings, session)
        app.initialize()

        router.message.register(app.handle_start, Command("start"))
        router.message.register(app.handle_summary_command, Command("summary"))
        router.message.register(app.handle_text_message)

        dp.include_router(router)

        logger.info("Бот запущен")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
