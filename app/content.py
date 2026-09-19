from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from telegram import Bot, InlineKeyboardMarkup, Message, MessageEntity


ContentType = Literal["text", "photo", "video", "animation", "document", "audio", "voice"]

TEXT_LIMIT_UTF16 = 4096
CAPTION_LIMIT_UTF16 = 1024

TAKE_SEPARATOR = "➤"
TAKE_HASHTAG = "#тейк"
TAKE_CTA_TEXT = "отправить тейк"

GENRES: dict[str, tuple[str, str]] = {
    "spam": ("Спам", "#спам"),
    "invite": ("Инвайт", "#инвайт"),
    "arbitrage": ("Арбитраж", "#арбитраж"),
    "networks": ("Сетки", "#сетки"),
    "shops": ("Шопы", "#шопы"),
    "traffic": ("Трафик", "#трафик"),
    "earnings": ("Заработок", "#заработок"),
    "story": ("История", "#история"),
    "question": ("Вопрос", "#вопрос"),
    "opinion": ("Мнение", "#мнение"),
    "other": ("Другое", "#другое"),
}


@dataclass(frozen=True, slots=True)
class DraftContent:
    content_type: ContentType
    text: str | None
    entities: list[dict[str, Any]] | None
    file_id: str | None


def utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def serialize_entities(entities: tuple[MessageEntity, ...] | None) -> list[dict[str, Any]] | None:
    if not entities:
        return None
    return [entity.to_dict() for entity in entities]


def deserialize_entities(
    raw: list[dict[str, Any]] | None, bot: Bot
) -> list[MessageEntity] | None:
    if not raw:
        return None
    return [MessageEntity.de_json(item, bot) for item in raw]


def _shift_entities(
    entities: list[MessageEntity] | None, utf16_shift: int
) -> list[MessageEntity] | None:
    if not entities:
        return None
    return [
        MessageEntity(
            type=entity.type,
            offset=entity.offset + utf16_shift,
            length=entity.length,
            url=entity.url,
            user=entity.user,
            language=entity.language,
            custom_emoji_id=entity.custom_emoji_id,
        )
        for entity in entities
    ]


def extract_draft(message: Message) -> DraftContent:
    if message.media_group_id:
        raise ValueError(
            "Альбомы из нескольких медиа пока не принимаются. Пришли одно фото/видео за раз."
        )

    if message.text is not None:
        if not message.text.strip():
            raise ValueError("Пустой тейк отправить нельзя.")
        return DraftContent(
            content_type="text",
            text=message.text,
            entities=serialize_entities(message.entities),
            file_id=None,
        )

    content_type: ContentType | None = None
    file_id: str | None = None

    if message.photo:
        content_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        content_type = "video"
        file_id = message.video.file_id
    elif message.animation:
        content_type = "animation"
        file_id = message.animation.file_id
    elif message.document:
        content_type = "document"
        file_id = message.document.file_id
    elif message.audio:
        content_type = "audio"
        file_id = message.audio.file_id
    elif message.voice:
        content_type = "voice"
        file_id = message.voice.file_id

    if content_type is None or file_id is None:
        raise ValueError(
            "Такой тип сообщения не поддерживается. Отправь текст, фото, видео, GIF, "
            "документ, аудио или голосовое."
        )

    caption = message.caption
    if caption is not None and not caption.strip():
        caption = None

    return DraftContent(
        content_type=content_type,
        text=caption,
        entities=serialize_entities(message.caption_entities),
        file_id=file_id,
    )


def validate_genre(genre: str) -> str:
    if genre not in GENRES:
        raise ValueError("Неизвестный жанр тейка.")
    return genre


def _bot_public_username(bot: Bot) -> str:
    username = (bot.username or "shadowtalkCF_bot").lstrip("@").strip()
    if not username:
        raise RuntimeError("Telegram bot username is unavailable")
    return username


def _render_text_and_entities(
    *,
    genre: str,
    text: str | None,
    raw_entities: list[dict[str, Any]] | None,
    bot: Bot,
    is_caption: bool,
) -> tuple[str, list[MessageEntity] | None]:
    validate_genre(genre)

    genre_hashtag = GENRES[genre][1]
    header = f"{genre_hashtag} {TAKE_SEPARATOR} {TAKE_HASHTAG}"
    prefix = f"{header}\n\n"

    body = text or ""

    bot_username = _bot_public_username(bot)
    footer_line = f"{TAKE_CTA_TEXT} {TAKE_SEPARATOR} @{bot_username}"
    footer = f"\n\n{footer_line}"

    rendered = prefix + body + footer

    limit = CAPTION_LIMIT_UTF16 if is_caption else TEXT_LIMIT_UTF16
    current_len = utf16_len(rendered)
    if current_len > limit:
        overflow = current_len - limit
        raise ValueError(
            "Тейк слишком длинный с учётом оформления. "
            f"Сократи его минимум на {overflow} UTF-16 символ(а/ов)."
        )

    entities: list[MessageEntity] = []

    # Header hashtags remain native Telegram hashtags and are independently clickable.
    entities.append(
        MessageEntity(
            type=MessageEntity.HASHTAG,
            offset=0,
            length=utf16_len(genre_hashtag),
        )
    )
    take_hashtag_offset = utf16_len(f"{genre_hashtag} {TAKE_SEPARATOR} ")
    entities.append(
        MessageEntity(
            type=MessageEntity.HASHTAG,
            offset=take_hashtag_offset,
            length=utf16_len(TAKE_HASHTAG),
        )
    )

    # Preserve every entity from the user's original message with UTF-16-safe offsets.
    original_entities = deserialize_entities(raw_entities, bot)
    shifted_original = _shift_entities(original_entities, utf16_len(prefix))
    if shifted_original:
        entities.extend(shifted_original)

    footer_start = utf16_len(prefix + body + "\n\n")
    footer_length = utf16_len(footer_line)

    # Quote the CTA as a separate visual footer.
    entities.append(
        MessageEntity(
            type=MessageEntity.BLOCKQUOTE,
            offset=footer_start,
            length=footer_length,
        )
    )

    username_label = f"@{bot_username}"
    username_offset = footer_start + utf16_len(
        f"{TAKE_CTA_TEXT} {TAKE_SEPARATOR} "
    )
    entities.append(
        MessageEntity(
            type=MessageEntity.TEXT_LINK,
            offset=username_offset,
            length=utf16_len(username_label),
            url=f"https://t.me/{bot_username}",
        )
    )

    return rendered, entities


async def send_rendered_content(
    *,
    bot: Bot,
    chat_id: int | str,
    genre: str,
    content_type: str,
    text: str | None,
    raw_entities: list[dict[str, Any]] | None,
    file_id: str | None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    is_caption = content_type != "text"
    rendered, entities = _render_text_and_entities(
        genre=genre,
        text=text,
        raw_entities=raw_entities,
        bot=bot,
        is_caption=is_caption,
    )

    if content_type == "text":
        return await bot.send_message(
            chat_id=chat_id,
            text=rendered,
            entities=entities,
            reply_markup=reply_markup,
            disable_web_page_preview=False,
        )

    if not file_id:
        raise ValueError(f"Missing file_id for media content type {content_type!r}")

    common = {
        "chat_id": chat_id,
        "caption": rendered,
        "caption_entities": entities,
        "reply_markup": reply_markup,
    }

    if content_type == "photo":
        return await bot.send_photo(photo=file_id, **common)
    if content_type == "video":
        return await bot.send_video(video=file_id, **common)
    if content_type == "animation":
        return await bot.send_animation(animation=file_id, **common)
    if content_type == "document":
        return await bot.send_document(document=file_id, **common)
    if content_type == "audio":
        return await bot.send_audio(audio=file_id, **common)
    if content_type == "voice":
        return await bot.send_voice(voice=file_id, **common)

    raise ValueError(f"Unsupported persisted content type: {content_type!r}")
