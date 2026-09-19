from __future__ import annotations

from telegram.request import HTTPXRequest
from telegram.ext import AIORateLimiter, Application, ApplicationBuilder

from app.config import Settings
from app.database import Database
from app.handlers import BotHandlers


def build_application(
    *,
    settings: Settings,
    db: Database,
) -> tuple[Application, BotHandlers]:
    # Render cold starts and transient Telegram network stalls can exceed PTB's
    # small defaults. Keep finite timeouts, but make them production-friendly.
    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=10.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=10.0,
        media_write_timeout=30.0,
    )

    application = (
        ApplicationBuilder()
        .token(settings.bot_token.get_secret_value())
        .request(request)
        .rate_limiter(AIORateLimiter(max_retries=2))
        .concurrent_updates(False)
        .build()
    )
    handlers = BotHandlers(db=db, settings=settings)
    handlers.register(application)
    return application, handlers
