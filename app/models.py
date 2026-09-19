from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, JSON, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserSession(Base):
    __tablename__ = "user_sessions"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    genre: Mapped[str | None] = mapped_column(String(32))

    draft_content_type: Mapped[str | None] = mapped_column(String(24))
    draft_text: Mapped[str | None] = mapped_column(Text)
    draft_entities: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    draft_file_id: Mapped[str | None] = mapped_column(Text)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ModeratorSession(Base):
    __tablename__ = "moderator_sessions"

    moderator_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    submission_id: Mapped[int | None] = mapped_column(BigInteger)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        Index("ix_submissions_status_created_at", "status", "created_at"),
        Index("ix_submissions_author_id_created_at", "author_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    genre: Mapped[str] = mapped_column(String(32), nullable=False)

    content_type: Mapped[str] = mapped_column(String(24), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    file_id: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    moderated_by: Mapped[int | None] = mapped_column(BigInteger)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    internal_error: Mapped[str | None] = mapped_column(Text)
    channel_message_id: Mapped[int | None] = mapped_column(BigInteger)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
