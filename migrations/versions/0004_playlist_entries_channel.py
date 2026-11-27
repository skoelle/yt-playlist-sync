# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""playlist_entries.channel (video channel for full-text search)

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existenz-Guard: 0001 (metadata.create_all) already created the column on a
    # fresh database; only databases stuck at 0003 need the ALTER.
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("playlist_entries")}
    if "channel" not in cols:
        op.add_column("playlist_entries", sa.Column("channel", sa.String(300), nullable=True))


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("playlist_entries")}
    if "channel" in cols:
        op.drop_column("playlist_entries", "channel")
