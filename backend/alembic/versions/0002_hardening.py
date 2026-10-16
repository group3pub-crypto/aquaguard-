"""Auth lifecycle, onboarding support, auto shut-off safety columns and scan indexes.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)
NOW = sa.func.now()


def upgrade() -> None:
    # --- users: lifecycle + lockout + service accounts; usernames are stored lowercase
    op.execute("UPDATE users SET username = lower(username)")  # fails loudly on a case collision
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(
            sa.Column("is_service", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("last_failed_login_at", TS))
        batch.add_column(sa.Column("locked_until", TS))
        batch.create_check_constraint("username_lower", "username = lower(username)")

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
        sa.Column("replaced_by", sa.Uuid()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("user_agent", sa.String(300)),
        sa.Column("ip", sa.String(64)),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "ws_tickets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("scope", JSON, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("used_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_ws_tickets_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_ws_tickets_token_hash"),
    )
    op.create_index("ix_ws_tickets_user_id", "ws_tickets", ["user_id"])
    op.create_index("ix_ws_tickets_expires_at", "ws_tickets", ["expires_at"])

    # --- alerts: shut-off warning window / give-up bookkeeping + scan indexes
    with op.batch_alter_table("alerts") as batch:
        batch.add_column(sa.Column("shutoff_warned_at", TS))
        batch.add_column(sa.Column("shutoff_gave_up_at", TS))
    op.create_index(
        "ix_alerts_escalation",
        "alerts",
        ["tier", "created_at"],
        postgresql_where=sa.text("state IN ('nudge', 'alert', 'ticket')"),
        sqlite_where=sa.text("state IN ('nudge', 'alert', 'ticket')"),
    )
    op.create_index(
        "ix_alerts_snoozed",
        "alerts",
        ["snoozed_until"],
        postgresql_where=sa.text("state = 'snoozed'"),
        sqlite_where=sa.text("state = 'snoozed'"),
    )
    op.create_index(
        "ix_valve_commands_alert_issued", "valve_commands", ["alert_id", "issued_at"]
    )
    op.create_index("ix_flow_readings_timestamp", "flow_readings", ["timestamp"])
    op.create_index("ix_sensor_health_last_seen", "sensor_health", ["last_seen"])


def downgrade() -> None:
    op.drop_index("ix_sensor_health_last_seen", table_name="sensor_health")
    op.drop_index("ix_flow_readings_timestamp", table_name="flow_readings")
    op.drop_index("ix_valve_commands_alert_issued", table_name="valve_commands")
    op.drop_index("ix_alerts_snoozed", table_name="alerts")
    op.drop_index("ix_alerts_escalation", table_name="alerts")
    with op.batch_alter_table("alerts") as batch:
        batch.drop_column("shutoff_gave_up_at")
        batch.drop_column("shutoff_warned_at")
    op.drop_index("ix_ws_tickets_expires_at", table_name="ws_tickets")
    op.drop_index("ix_ws_tickets_user_id", table_name="ws_tickets")
    op.drop_table("ws_tickets")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("username_lower", type_="check")
        batch.drop_column("locked_until")
        batch.drop_column("last_failed_login_at")
        batch.drop_column("failed_logins")
        batch.drop_column("token_version")
        batch.drop_column("is_service")
        batch.drop_column("is_active")
