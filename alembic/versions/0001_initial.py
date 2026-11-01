"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tables are created by SQLAlchemy metadata + init_db FTS setup.
    # This revision marks the baseline for future migrations.
    pass


def downgrade() -> None:
    pass
