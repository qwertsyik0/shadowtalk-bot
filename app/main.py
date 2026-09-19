from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from sqlalchemy import text
from telegram import Update
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut

from app.bot import build_application
from app.config import get_settings
from app.database import Database


settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

db = Database(settings)
telegram_app, handlers = build_application(settings=settings, db=db)

T = TypeVar("T")


async def _telegram_retry(
    operation_name: str,
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 4,
) -> T:
    """Retry transient Telegram transport failures with bounded exponential backoff."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except RetryAfter as exc:
            if attempt == attempts:
                raise
            delay = max(float(exc.retry_after), 1.0)
            logger.warning(
                "Telegram rate limit during %s attempt=%s/%s retry_in=%.1fs",
                operation_name,
                attempt,
                attempts,
                delay,
            )
            await asyncio.sleep(delay)
        except (TimedOut, NetworkError) as exc:
            if attempt == attempts:
                raise
            delay = min(2.0 ** (attempt - 1), 8.0)
            logger.warning(
                "Transient Telegram error during %s attempt=%s/%s error=%s retry_in=%.1fs",
                operation_name,
                attempt,
                attempts,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"Unreachable retry state for {operation_name}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialized = False
    started = False
    try:
        await _telegram_retry("application.initialize", telegram_app.initialize)
        initialized = True

        await _telegram_retry(
            "telegram preflight",
            lambda: handlers.preflight(telegram_app),
        )

        webhook_secret = settings.webhook_secret.get_secret_value()
        webhook_set = await _telegram_retry(
            "setWebhook",
            lambda: telegram_app.bot.set_webhook(
                url=settings.webhook_url,
                secret_token=webhook_secret,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=False,
                max_connections=40,
            ),
        )
        if not webhook_set:
            raise RuntimeError("Telegram rejected setWebhook")

        await telegram_app.start()
        started = True
        logger.info("ShadowTalk bot started webhook=%s", settings.webhook_url)

        yield
    finally:
        # Do not delete the webhook here. Render free services can be suspended or
        # restarted at any time, and Telegram must retain the webhook so the next
        # update can wake the service back up.
        if started:
            await telegram_app.stop()
        if initialized:
            await telegram_app.shutdown()
        await db.dispose()
        logger.info("ShadowTalk bot stopped")


app = FastAPI(
    title="ShadowTalk Autotaker",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    try:
        async with db.session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.exception("Database health check failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_unavailable",
        ) from exc

    return {"status": "ok"}


@app.post("/telegram/webhook", status_code=status.HTTP_200_OK)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    expected = settings.webhook_secret.get_secret_value()
    supplied = x_telegram_bot_api_secret_token or ""
    if not secrets.compare_digest(supplied, expected):
        logger.warning("Rejected webhook request with invalid secret")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid_webhook_secret",
        )

    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_content_length",
            ) from exc
        if content_length < 0 or content_length > settings.max_webhook_body_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="webhook_body_too_large",
            )

    body = await request.body()
    if len(body) > settings.max_webhook_body_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="webhook_body_too_large",
        )

    try:
        payload: Any = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Webhook JSON root must be an object")
        update = Update.de_json(payload, telegram_app.bot)
    except (ValueError, TypeError) as exc:
        logger.warning("Rejected malformed Telegram update: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="malformed_update",
        ) from exc

    if update is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty_update",
        )

    await telegram_app.process_update(update)
    return Response(status_code=status.HTTP_200_OK)
