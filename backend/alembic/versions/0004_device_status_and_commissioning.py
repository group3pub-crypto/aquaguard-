"""device_status (latest firmware-reported values) + per-flat auto shut-off commissioning gate.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "device_status",
        sa.Column("flat_id", sa.Uuid(), primary_key=True),
        sa.Column("firmware_version", sa.String(50)),
        sa.Column("battery_pct", sa.Float()),
        sa.Column("battery_volts", sa.Float()),
        sa.Column("total_volume", sa.Float()),
        sa.Column("valve_reported", sa.String(10)),
        sa.Column("err_codes", JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("reported_at", TS),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["flat_id"], ["flats.id"], name="fk_device_status_flat_id_flats", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "valve_reported IN ('open', 'closed')", name="valve_reported"
        ),
    )
    with op.batch_alter_table("flats") as batch:
        batch.add_column(
            sa.Column(
                "auto_shutoff_validated", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("auto_shutoff_validated_at", TS))
        batch.add_column(sa.Column("auto_shutoff_validated_by", sa.String(100)))
        batch.add_column(sa.Column("auto_shutoff_notes", sa.String(1000)))
    with op.batch_alter_table("alerts") as batch:
        batch.add_column(sa.Column("shutoff_uncommissioned_at", TS))


def downgrade() -> None:
    with op.batch_alter_table("alerts") as batch:
        batch.drop_column("shutoff_uncommissioned_at")
    with op.batch_alter_table("flats") as batch:
        batch.drop_column("auto_shutoff_notes")
        batch.drop_column("auto_shutoff_validated_by")
        batch.drop_column("auto_shutoff_validated_at")
        batch.drop_column("auto_shutoff_validated")
    op.drop_table("device_status")
