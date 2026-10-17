"""Persist firmware extras on flow_readings instead of dropping them: tot, vbat, valve, fw, err.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("flow_readings") as batch:
        batch.add_column(sa.Column("total_volume", sa.Float()))
        batch.add_column(sa.Column("battery_volts", sa.Float()))
        batch.add_column(sa.Column("device_valve_state", sa.String(10)))
        batch.add_column(sa.Column("firmware_version", sa.String(32)))
        batch.add_column(sa.Column("fault_codes", JSON))


def downgrade() -> None:
    with op.batch_alter_table("flow_readings") as batch:
        batch.drop_column("fault_codes")
        batch.drop_column("firmware_version")
        batch.drop_column("device_valve_state")
        batch.drop_column("battery_volts")
        batch.drop_column("total_volume")
