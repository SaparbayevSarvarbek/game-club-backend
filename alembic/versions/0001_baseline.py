"""baseline

Revision ID: 0001_baseline
Revises: 
Create Date: 2026-06-22 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0001_baseline'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # NOTE: This baseline migration is intentionally empty.
    # It is provided to initialize Alembic's versions history.
    # Run `alembic revision --autogenerate -m "baseline"` locally
    # against your DATABASE_URL to produce a full schema migration
    # if you want Alembic to manage DDL going forward.
    pass


def downgrade():
    pass
