"""initial schema

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "vacancies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dedup_key", sa.Text, nullable=False),
        sa.Column("company", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("cities", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("work_formats", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("employment", sa.Text, nullable=True),
        sa.Column("experience_years_min", sa.Integer, nullable=True),
        sa.Column("accepted", sa.Boolean, nullable=False),
        sa.Column("reject_reason", sa.Text, nullable=True),
        sa.Column("flags", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
        sa.Column("is_open", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("reopened", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("notified_at", TS, nullable=True),
    )
    op.create_index(
        "uq_vacancies_dedup_key_open",
        "vacancies",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("is_open"),
    )
    op.create_table(
        "postings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("vacancy_id", sa.Integer, sa.ForeignKey("vacancies.id"), nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("raw", postgresql.JSONB, nullable=False),
        sa.Column("is_open", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.UniqueConstraint("source", "external_id", name="uq_postings_source_ext"),
    )
    op.create_index("ix_postings_vacancy_id", "postings", ["vacancy_id"])
    op.create_table(
        "source_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("fetched", sa.Integer, nullable=False),
    )
    op.create_table(
        "source_state",
        sa.Column("source", sa.Text, primary_key=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("initialized", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("last_nonempty_at", TS, nullable=True),
        sa.Column("failing", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("empty_alerted", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_table("source_state")
    op.drop_table("source_runs")
    op.drop_index("ix_postings_vacancy_id", table_name="postings")
    op.drop_table("postings")
    op.drop_index("uq_vacancies_dedup_key_open", table_name="vacancies")
    op.drop_table("vacancies")
