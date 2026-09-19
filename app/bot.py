from __future__ import annotations

from telegram.ext import AIORateLimiter, Application, ApplicationBuilder

from app.config import Settings
from app.database import Database
from app.handlers import BotHandlers


def build_application(
    *,
    settings: Settings,
    db: Database,
) -> tuple[Application, BotHandlers]:
    application = (
        ApplicationBuilder()
        .token(settings.bot_token.get_secret_value())
        .rate_limiter(AIORateLimiter(max_retries=2))
        .concurrent_updates(False)
        .build()
    )
    handlers = BotHandlers(db=db, settings=settings)
    handlers.register(application)
    return application, handlers
