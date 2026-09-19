from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import Settings
from app.content import GENRES, extract_draft, send_rendered_content, validate_genre
from app.database import Database
from app.models import ModeratorSession, Submission, UserSession

logger = logging.getLogger(__name__)

USER_STATE_IDLE = "idle"
USER_STATE_CHOOSING_GENRE = "choosing_genre"
USER_STATE_AWAITING_CONTENT = "awaiting_content"
USER_STATE_PREVIEW = "preview"

MOD_STATE_IDLE = "idle"
MOD_STATE_AWAITING_REJECTION = "awaiting_rejection_reason"

STATUS_PENDING = "pending"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"
STATUS_NEEDS_REVIEW = "needs_review"


class BotHandlers:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("queue", self.queue))
        application.add_handler(CallbackQueryHandler(self.callback))
        application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self.message))
        application.add_error_handler(self.error_handler)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_chat is None:
            return
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "анонимный тейкер ShadowTalk.\n\n"
                "здесь можно отправить историю, мнение, вопрос или опыт из тг сферы."
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("ОТПРАВИТЬ ТЕЙК", callback_data="take:new")]]
            ),
        )

    async def queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return
        if user.id not in self.settings.moderator_ids:
            await context.bot.send_message(chat.id, "Команда доступна только модерации.")
            return

        async with self.db.session() as session:
            result = await session.execute(
                select(Submission)
                .where(Submission.status.in_([STATUS_PENDING, STATUS_NEEDS_REVIEW]))
                .order_by(Submission.created_at.asc())
                .limit(10)
            )
            submissions = list(result.scalars())

        if not submissions:
            await context.bot.send_message(chat.id, "Очередь пуста.")
            return

        await context.bot.send_message(
            chat.id,
            f"В очереди показаны первые {len(submissions)} тейк(а/ов).",
        )
        for submission in submissions:
            await self._send_submission_to_moderator(
                context=context,
                moderator_id=user.id,
                submission=submission,
                queue_view=True,
            )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return

        data = query.data or ""
        try:
            if data == "take:new":
                await query.answer()
                await self._begin_take(user.id, context)
                return
            if data.startswith("genre:"):
                await query.answer()
                await self._choose_genre(user.id, data.removeprefix("genre:"), context)
                return
            if data == "take:confirm":
                await query.answer()
                await self._confirm_take(user.id, context)
                return
            if data == "take:edit":
                await query.answer()
                await self._edit_take(user.id, context)
                return
            if data == "take:cancel":
                await query.answer()
                await self._cancel_take(user.id, context)
                return
            if data.startswith("mod:approve:"):
                await query.answer()
                await self._approve(
                    moderator_id=user.id,
                    submission_id=self._parse_submission_id(data, "mod:approve:"),
                    context=context,
                )
                return
            if data.startswith("mod:reject:"):
                await query.answer()
                await self._begin_rejection(
                    moderator_id=user.id,
                    submission_id=self._parse_submission_id(data, "mod:reject:"),
                    context=context,
                )
                return
            if data == "mod:rejectcancel":
                await query.answer()
                await self._cancel_rejection(user.id, context)
                return

            await query.answer("Кнопка устарела.", show_alert=False)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
        except TelegramError:
            logger.exception("Telegram error while processing callback user_id=%s data=%s", user.id, data)
            try:
                await query.answer("Ошибка Telegram. Попробуй ещё раз.", show_alert=True)
            except TelegramError:
                pass

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.effective_message
        if user is None or message is None:
            return

        if user.id in self.settings.moderator_ids:
            if await self._maybe_handle_rejection_reason(user.id, message.text, context):
                return

        async with self.db.session() as session:
            user_session = await self._get_or_create_user_session(session, user.id)
            state = user_session.state

        if state != USER_STATE_AWAITING_CONTENT:
            await message.reply_text("Чтобы отправить тейк, нажми «ОТПРАВИТЬ ТЕЙК».")
            return

        try:
            draft = extract_draft(message)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return

        async with self.db.session() as session:
            user_session = await self._get_or_create_user_session(
                session, user.id, for_update=True
            )
            if user_session.state != USER_STATE_AWAITING_CONTENT or not user_session.genre:
                await session.rollback()
                await message.reply_text("Сессия изменилась. Начни отправку тейка заново.")
                return

            user_session.draft_content_type = draft.content_type
            user_session.draft_text = draft.text
            user_session.draft_entities = draft.entities
            user_session.draft_file_id = draft.file_id
            user_session.state = USER_STATE_PREVIEW
            genre = user_session.genre
            await session.commit()

        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Подтвердить", callback_data="take:confirm"),
                    InlineKeyboardButton("Изменить", callback_data="take:edit"),
                ],
                [InlineKeyboardButton("Отмена", callback_data="take:cancel")],
            ]
        )

        await message.reply_text("Предпросмотр:")
        try:
            await send_rendered_content(
                bot=context.bot,
                chat_id=user.id,
                genre=genre,
                content_type=draft.content_type,
                text=draft.text,
                raw_entities=draft.entities,
                file_id=draft.file_id,
                reply_markup=markup,
            )
        except ValueError as exc:
            async with self.db.session() as session:
                user_session = await self._get_or_create_user_session(
                    session, user.id, for_update=True
                )
                user_session.state = USER_STATE_AWAITING_CONTENT
                await session.commit()
            await message.reply_text(str(exc))

    async def _begin_take(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with self.db.session() as session:
            user_session = await self._get_or_create_user_session(
                session, user_id, for_update=True
            )
            self._clear_user_draft(user_session)
            user_session.state = USER_STATE_CHOOSING_GENRE
            await session.commit()

        rows: list[list[InlineKeyboardButton]] = []
        current: list[InlineKeyboardButton] = []
        for slug, (label, hashtag) in GENRES.items():
            current.append(
                InlineKeyboardButton(f"{hashtag} · {label}", callback_data=f"genre:{slug}")
            )
            if len(current) == 2:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
        rows.append([InlineKeyboardButton("Отмена", callback_data="take:cancel")])

        await context.bot.send_message(
            user_id,
            "Выбери жанр. Он станет хештегом при публикации:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _choose_genre(
        self, user_id: int, genre: str, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        validate_genre(genre)
        async with self.db.session() as session:
            user_session = await self._get_or_create_user_session(
                session, user_id, for_update=True
            )
            if user_session.state not in {
                USER_STATE_CHOOSING_GENRE,
                USER_STATE_AWAITING_CONTENT,
            }:
                raise ValueError("Сессия устарела. Начни отправку тейка заново.")
            self._clear_user_draft(user_session)
            user_session.genre = genre
            user_session.state = USER_STATE_AWAITING_CONTENT
            await session.commit()

        await context.bot.send_message(
            user_id,
            (
                f"Выбран {GENRES[genre][1]}.\n\n"
                "Теперь пришли тейк одним сообщением. Форматирование текста сохранится."
            ),
        )

    async def _edit_take(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with self.db.session() as session:
            user_session = await self._get_or_create_user_session(
                session, user_id, for_update=True
            )
            if user_session.state != USER_STATE_PREVIEW:
                raise ValueError("Этот предпросмотр уже неактуален.")
            user_session.draft_content_type = None
            user_session.draft_text = None
            user_session.draft_entities = None
            user_session.draft_file_id = None
            user_session.state = USER_STATE_AWAITING_CONTENT
            await session.commit()
        await context.bot.send_message(user_id, "Пришли новую версию тейка.")

    async def _cancel_take(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with self.db.session() as session:
            user_session = await self._get_or_create_user_session(
                session, user_id, for_update=True
            )
            self._clear_user_draft(user_session)
            user_session.genre = None
            user_session.state = USER_STATE_IDLE
            await session.commit()
        await context.bot.send_message(user_id, "Отправка тейка отменена.")

    async def _confirm_take(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with self.db.session() as session:
            user_session = await self._get_or_create_user_session(
                session, user_id, for_update=True
            )
            if (
                user_session.state != USER_STATE_PREVIEW
                or not user_session.genre
                or not user_session.draft_content_type
            ):
                raise ValueError("Тейк уже отправлен или предпросмотр устарел.")

            submission = Submission(
                author_id=user_id,
                genre=user_session.genre,
                content_type=user_session.draft_content_type,
                text=user_session.draft_text,
                entities=user_session.draft_entities,
                file_id=user_session.draft_file_id,
                status=STATUS_PENDING,
            )
            session.add(submission)
            await session.flush()
            submission_id = submission.id

            self._clear_user_draft(user_session)
            user_session.genre = None
            user_session.state = USER_STATE_IDLE
            await session.commit()

        await context.bot.send_message(
            user_id,
            f"Тейк #{submission_id} отправлен на модерацию.",
        )

        async with self.db.session() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                logger.error("Submission disappeared after commit id=%s", submission_id)
                return

        delivered = 0
        for moderator_id in self.settings.moderator_ids:
            try:
                await self._send_submission_to_moderator(
                    context=context,
                    moderator_id=moderator_id,
                    submission=submission,
                    queue_view=False,
                )
                delivered += 1
            except (Forbidden, NetworkError, TimedOut, TelegramError):
                logger.exception(
                    "Failed to notify moderator moderator_id=%s submission_id=%s",
                    moderator_id,
                    submission_id,
                )

        if delivered == 0:
            logger.error(
                "Submission %s is pending but no moderator notification was delivered",
                submission_id,
            )

    async def _send_submission_to_moderator(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        moderator_id: int,
        submission: Submission,
        queue_view: bool,
    ) -> None:
        if moderator_id not in self.settings.moderator_ids:
            raise PermissionError("Unknown moderator")

        genre = GENRES.get(submission.genre, (submission.genre, f"#{submission.genre}"))[1]
        prefix = "Очередь" if queue_view else "Новый тейк"
        await context.bot.send_message(
            moderator_id,
            (
                f"{prefix} #{submission.id}\n"
                f"жанр: {genre}\n"
                f"статус: {submission.status}"
            ),
        )

        markup: InlineKeyboardMarkup | None = None
        if submission.status == STATUS_PENDING:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Опубликовать",
                            callback_data=f"mod:approve:{submission.id}",
                        ),
                        InlineKeyboardButton(
                            "Отклонить",
                            callback_data=f"mod:reject:{submission.id}",
                        ),
                    ]
                ]
            )

        await send_rendered_content(
            bot=context.bot,
            chat_id=moderator_id,
            genre=submission.genre,
            content_type=submission.content_type,
            text=submission.text,
            raw_entities=submission.entities,
            file_id=submission.file_id,
            reply_markup=markup,
        )

    async def _approve(
        self,
        *,
        moderator_id: int,
        submission_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        self._require_moderator(moderator_id)

        async with self.db.session() as session:
            submission = await self._get_submission_for_update(session, submission_id)
            if submission.status == STATUS_PUBLISHED:
                await context.bot.send_message(moderator_id, "Этот тейк уже опубликован.")
                return
            if submission.status != STATUS_PENDING:
                await context.bot.send_message(
                    moderator_id,
                    f"Нельзя опубликовать тейк со статусом {submission.status}.",
                )
                return

            submission.status = STATUS_PUBLISHING
            submission.moderated_by = moderator_id
            submission.internal_error = None
            await session.commit()

        try:
            published = await send_rendered_content(
                bot=context.bot,
                chat_id=self.settings.channel_username,
                genre=submission.genre,
                content_type=submission.content_type,
                text=submission.text,
                raw_entities=submission.entities,
                file_id=submission.file_id,
            )
        except (RetryAfter, NetworkError, TimedOut, TelegramError) as exc:
            logger.exception(
                "Ambiguous publish failure submission_id=%s; refusing automatic retry",
                submission_id,
            )
            async with self.db.session() as session:
                current = await self._get_submission_for_update(session, submission_id)
                if current.status == STATUS_PUBLISHING:
                    current.status = STATUS_NEEDS_REVIEW
                    current.internal_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
                    await session.commit()
            await context.bot.send_message(
                moderator_id,
                (
                    "Telegram не подтвердил публикацию. Автоповтор отключён, чтобы не "
                    "создать дубль. Тейк переведён в needs_review и требует ручной проверки."
                ),
            )
            return

        async with self.db.session() as session:
            current = await self._get_submission_for_update(session, submission_id)
            if current.status != STATUS_PUBLISHING:
                logger.critical(
                    "Submission state changed during publish id=%s status=%s",
                    submission_id,
                    current.status,
                )
                raise RuntimeError("Submission state changed during publication")
            current.status = STATUS_PUBLISHED
            current.channel_message_id = published.message_id
            current.internal_error = None
            await session.commit()

        await context.bot.send_message(moderator_id, f"Тейк #{submission_id} опубликован.")
        try:
            await context.bot.send_message(
                submission.author_id,
                f"Твой тейк #{submission_id} опубликован.",
            )
        except Forbidden:
            logger.info(
                "Author blocked bot; publication notice not delivered author_id=%s",
                submission.author_id,
            )

    async def _begin_rejection(
        self,
        *,
        moderator_id: int,
        submission_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        self._require_moderator(moderator_id)

        async with self.db.session() as session:
            submission = await self._get_submission_for_update(session, submission_id)
            if submission.status != STATUS_PENDING:
                await context.bot.send_message(
                    moderator_id,
                    f"Тейк уже обработан. Статус: {submission.status}.",
                )
                return

            mod_session = await self._get_or_create_moderator_session(
                session, moderator_id, for_update=True
            )
            mod_session.state = MOD_STATE_AWAITING_REJECTION
            mod_session.submission_id = submission_id
            await session.commit()

        await context.bot.send_message(
            moderator_id,
            (
                f"Пришли причину отклонения тейка #{submission_id}. "
                "Причина обязательна и будет отправлена автору."
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data="mod:rejectcancel")]]
            ),
        )

    async def _cancel_rejection(
        self, moderator_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._require_moderator(moderator_id)
        async with self.db.session() as session:
            mod_session = await self._get_or_create_moderator_session(
                session, moderator_id, for_update=True
            )
            mod_session.state = MOD_STATE_IDLE
            mod_session.submission_id = None
            await session.commit()
        await context.bot.send_message(moderator_id, "Отклонение отменено.")

    async def _maybe_handle_rejection_reason(
        self,
        moderator_id: int,
        text: str | None,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        async with self.db.session() as session:
            mod_session = await self._get_or_create_moderator_session(
                session, moderator_id
            )
            if mod_session.state != MOD_STATE_AWAITING_REJECTION:
                return False
            submission_id = mod_session.submission_id

        if submission_id is None:
            async with self.db.session() as session:
                mod_session = await self._get_or_create_moderator_session(
                    session, moderator_id, for_update=True
                )
                mod_session.state = MOD_STATE_IDLE
                await session.commit()
            return True

        reason = (text or "").strip()
        if len(reason) < 3:
            await context.bot.send_message(
                moderator_id,
                "Причина должна быть текстом минимум из 3 символов.",
            )
            return True
        if len(reason) > 1000:
            await context.bot.send_message(
                moderator_id,
                "Причина слишком длинная. Максимум 1000 символов.",
            )
            return True

        async with self.db.session() as session:
            submission = await self._get_submission_for_update(session, submission_id)
            mod_session = await self._get_or_create_moderator_session(
                session, moderator_id, for_update=True
            )

            if submission.status != STATUS_PENDING:
                mod_session.state = MOD_STATE_IDLE
                mod_session.submission_id = None
                await session.commit()
                await context.bot.send_message(
                    moderator_id,
                    f"Тейк уже обработан. Статус: {submission.status}.",
                )
                return True

            submission.status = STATUS_REJECTED
            submission.moderated_by = moderator_id
            submission.rejection_reason = reason
            mod_session.state = MOD_STATE_IDLE
            mod_session.submission_id = None
            author_id = submission.author_id
            await session.commit()

        await context.bot.send_message(moderator_id, f"Тейк #{submission_id} отклонён.")
        try:
            await context.bot.send_message(
                author_id,
                f"Тейк #{submission_id} отклонён.\n\nПричина: {reason}",
            )
        except Forbidden:
            logger.info(
                "Author blocked bot; rejection reason not delivered author_id=%s",
                author_id,
            )
        return True

    async def preflight(self, application: Application) -> None:
        me = await application.bot.get_me()
        member = await application.bot.get_chat_member(self.settings.channel_username, me.id)

        if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
            raise RuntimeError(
                f"Bot @{me.username} must be an administrator of "
                f"{self.settings.channel_username}"
            )

        if member.status == ChatMemberStatus.ADMINISTRATOR:
            can_post = getattr(member, "can_post_messages", False)
            if can_post is not True:
                raise RuntimeError(
                    f"Bot @{me.username} has no permission to post messages in "
                    f"{self.settings.channel_username}"
                )

        logger.info(
            "Telegram preflight OK bot=@%s channel=%s moderators=%s",
            me.username,
            self.settings.channel_username,
            sorted(self.settings.moderator_ids),
        )

    async def error_handler(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.error(
            "Unhandled Telegram handler error update=%r",
            update,
            exc_info=(
                type(context.error),
                context.error,
                context.error.__traceback__,
            )
            if context.error
            else None,
        )

    async def _get_or_create_user_session(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        for_update: bool = False,
    ) -> UserSession:
        stmt = select(UserSession).where(UserSession.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        created = UserSession(user_id=user_id, state=USER_STATE_IDLE)
        session.add(created)
        try:
            await session.flush()
        except Exception:
            await session.rollback()
            result = await session.execute(
                select(UserSession).where(UserSession.user_id == user_id)
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                raise
            return existing
        return created

    async def _get_or_create_moderator_session(
        self,
        session: AsyncSession,
        moderator_id: int,
        *,
        for_update: bool = False,
    ) -> ModeratorSession:
        stmt = select(ModeratorSession).where(
            ModeratorSession.moderator_id == moderator_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        created = ModeratorSession(
            moderator_id=moderator_id,
            state=MOD_STATE_IDLE,
        )
        session.add(created)
        try:
            await session.flush()
        except Exception:
            await session.rollback()
            result = await session.execute(
                select(ModeratorSession).where(
                    ModeratorSession.moderator_id == moderator_id
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                raise
            return existing
        return created

    async def _get_submission_for_update(
        self, session: AsyncSession, submission_id: int
    ) -> Submission:
        result = await session.execute(
            select(Submission)
            .where(Submission.id == submission_id)
            .with_for_update()
        )
        submission = result.scalar_one_or_none()
        if submission is None:
            raise ValueError("Тейк не найден.")
        return submission

    def _require_moderator(self, user_id: int) -> None:
        if user_id not in self.settings.moderator_ids:
            raise ValueError("Нет доступа к модерации.")

    @staticmethod
    def _parse_submission_id(data: str, prefix: str) -> int:
        raw = data.removeprefix(prefix)
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("Некорректный ID тейка.") from exc
        if value <= 0:
            raise ValueError("Некорректный ID тейка.")
        return value

    @staticmethod
    def _clear_user_draft(user_session: UserSession) -> None:
        user_session.draft_content_type = None
        user_session.draft_text = None
        user_session.draft_entities = None
        user_session.draft_file_id = None
