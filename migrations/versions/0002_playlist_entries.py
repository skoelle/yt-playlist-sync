"""playlist entries (remote listing snapshot)

Revision ID: 0002
Revises: 0001
"""
from alembic import op

from app.models import PlaylistEntry

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # checkfirst: a fresh database already got every table from 0001 (metadata.create_all)
    PlaylistEntry.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    PlaylistEntry.__table__.drop(bind=op.get_bind(), checkfirst=True)
