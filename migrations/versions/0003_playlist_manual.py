# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""playlists.manual (entered by hand, not discovered)

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existenz-Guard: 0001 (metadata.create_all) already created the column on a
    # fresh database; only databases stuck at 0002 need the ALTER.
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("playlists")}
    if "manual" not in cols:
        op.add_column(
            "playlists",
            sa.Column("manual", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("playlists")}
    if "manual" in cols:
        op.drop_column("playlists", "manual")
