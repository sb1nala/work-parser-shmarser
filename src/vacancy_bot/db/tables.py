from datetime import UTC, datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class VacancyRow(Base):
    __tablename__ = "vacancies"
    __table_args__ = (
        Index(
            "uq_vacancies_dedup_key_open",
            "dedup_key",
            unique=True,
            postgresql_where=text("is_open"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dedup_key: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    cities: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    work_formats: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    employment: Mapped[str | None] = mapped_column(Text, nullable=True)
    experience_years_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    status: Mapped[str] = mapped_column(Text, default="new", server_default="new")
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    reopened: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PostingRow(Base):
    __tablename__ = "postings"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_postings_source_ext"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"), index=True)
    source: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SourceRunRow(Base):
    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched: Mapped[int] = mapped_column(Integer, default=0)


class SourceStateRow(Base):
    __tablename__ = "source_state"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    initialized: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    last_nonempty_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failing: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    empty_alerted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
