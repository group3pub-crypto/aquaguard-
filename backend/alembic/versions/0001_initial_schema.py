"""Initial schema.

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

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BIGPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
NOW = sa.func.now()
TS = sa.DateTime(timezone=True)


def _pg() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id", sa.Uuid(), primary_key=True,
        server_default=sa.text("gen_random_uuid()") if _pg() else None,
    )


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    ]


def upgrade() -> None:
    op.create_table(
        "societies",
        _uuid_pk(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("address", sa.String(500)),
        sa.Column("total_flats", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.UniqueConstraint("slug", name="uq_societies_slug"),
    )
    op.create_table(
        "flats",
        _uuid_pk(),
        sa.Column("society_id", sa.Uuid(), nullable=False),
        sa.Column("unit_number", sa.String(50), nullable=False),
        sa.Column("floor", sa.Integer()),
        sa.Column("residents", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sensor_id", sa.String(100), nullable=False),
        sa.Column("valve_state", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("baseline_status", sa.String(20), nullable=False, server_default="learning"),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["society_id"], ["societies.id"], name="fk_flats_society_id_societies", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("sensor_id", name="uq_flats_sensor_id"),
        sa.UniqueConstraint("society_id", "unit_number", name="uq_flats_society_unit"),
        sa.CheckConstraint(_in("valve_state", ("open", "closed", "unknown")), name="ck_flats_valve_state"),
        sa.CheckConstraint(
            _in("baseline_status", ("learning", "ready", "stale")), name="ck_flats_baseline_status"
        ),
    )
    op.create_index("ix_flats_society_id", "flats", ["society_id"])

    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="resident"),
        sa.Column("telegram_id", sa.BigInteger()),
        sa.Column("flat_id", sa.Uuid()),
        sa.Column("society_id", sa.Uuid()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(
            ["flat_id"], ["flats.id"], name="fk_users_flat_id_flats", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["society_id"], ["societies.id"], name="fk_users_society_id_societies", ondelete="SET NULL"
        ),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("telegram_id", name="uq_users_telegram_id"),
        sa.CheckConstraint(_in("role", ("resident", "admin", "maintenance")), name="ck_users_role"),
    )

    op.create_table(
        "flow_readings",
        sa.Column("id", BIGPK, primary_key=True, autoincrement=True),
        sa.Column("flat_id", sa.Uuid(), nullable=False),
        sa.Column("timestamp", TS, nullable=False),
        sa.Column("flow_rate", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("pressure", sa.Float()),
        sa.Column("temperature", sa.Float()),
        sa.Column("battery_pct", sa.Integer()),
        sa.Column("rssi", sa.Integer()),
        sa.Column("message_id", sa.String(16), nullable=False),
        sa.Column("received_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(
            ["flat_id"], ["flats.id"], name="fk_flow_readings_flat_id_flats", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("flat_id", "message_id", name="uq_flow_readings_flat_message"),
    )
    op.create_index("ix_flow_readings_flat_ts", "flow_readings", ["flat_id", "timestamp"])

    op.create_table(
        "baselines",
        _uuid_pk(),
        sa.Column("flat_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("slots", JSON, nullable=False),
        sa.Column("trained_from", TS),
        sa.Column("trained_to", TS),
        sa.Column("sample_days", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(
            ["flat_id"], ["flats.id"], name="fk_baselines_flat_id_flats", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("flat_id", "version", name="uq_baselines_flat_version"),
    )
    op.create_index(
        "uq_baselines_one_active", "baselines", ["flat_id"], unique=True,
        postgresql_where=sa.text("is_active"), sqlite_where=sa.text("is_active"),
    )

    op.create_table(
        "anomalies",
        _uuid_pk(),
        sa.Column("flat_id", sa.Uuid(), nullable=False),
        sa.Column("detected_at", TS, nullable=False),
        sa.Column("anomaly_type", sa.String(30), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("severity", sa.Float(), nullable=False),
        sa.Column("features", JSON),
        sa.Column("shap_values", JSON),
        sa.Column("explanation", JSON),
        sa.Column("baseline_version", sa.Integer()),
        sa.Column("window_start", TS),
        sa.Column("window_end", TS),
        sa.Column("est_loss_lpm", sa.Float()),
        sa.Column("resolved_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(
            ["flat_id"], ["flats.id"], name="fk_anomalies_flat_id_flats", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            _in("anomaly_type", ("leak", "burst", "overuse", "running_toilet", "dripping_tap", "unknown")),
            name="ck_anomalies_anomaly_type",
        ),
    )
    op.create_index("ix_anomalies_flat_detected", "anomalies", ["flat_id", "detected_at"])
    op.create_index(
        "ix_anomalies_unresolved", "anomalies", ["flat_id"],
        postgresql_where=sa.text("resolved_at IS NULL"), sqlite_where=sa.text("resolved_at IS NULL"),
    )

    states = (
        "nudge", "alert", "ticket", "auto_shutoff", "acknowledged", "snoozed", "overridden", "resolved",
    )
    op.create_table(
        "alerts",
        _uuid_pk(),
        sa.Column("flat_id", sa.Uuid(), nullable=False),
        sa.Column("anomaly_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("severity_score", sa.Float(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("message_source", sa.String(30)),
        sa.Column("escalated_at", JSON),
        sa.Column("snoozed_until", TS),
        sa.Column("acknowledged_by", sa.String(100)),
        sa.Column("acknowledged_at", TS),
        sa.Column("resolved_at", TS),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["flat_id"], ["flats.id"], name="fk_alerts_flat_id_flats", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["anomaly_id"], ["anomalies.id"], name="fk_alerts_anomaly_id_anomalies", ondelete="CASCADE"
        ),
        sa.CheckConstraint(_in("state", states), name="ck_alerts_state"),
        sa.CheckConstraint("tier BETWEEN 1 AND 4", name="ck_alerts_tier"),
    )
    op.create_index("ix_alerts_flat_id", "alerts", ["flat_id"])
    op.create_index("ix_alerts_anomaly_id", "alerts", ["anomaly_id"])
    op.create_index(
        "ix_alerts_open", "alerts", ["flat_id", "state"],
        postgresql_where=sa.text("state NOT IN ('resolved', 'overridden')"),
        sqlite_where=sa.text("state NOT IN ('resolved', 'overridden')"),
    )

    op.create_table(
        "valve_commands",
        _uuid_pk(),
        sa.Column("flat_id", sa.Uuid(), nullable=False),
        sa.Column("alert_id", sa.Uuid()),
        sa.Column("command", sa.String(10), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("issued_by", sa.String(100)),
        sa.Column("issued_at", TS, nullable=False),
        sa.Column("confirmed_at", TS),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.ForeignKeyConstraint(
            ["flat_id"], ["flats.id"], name="fk_valve_commands_flat_id_flats", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"], ["alerts.id"], name="fk_valve_commands_alert_id_alerts", ondelete="SET NULL"
        ),
        sa.CheckConstraint("command IN ('open', 'close')", name="ck_valve_commands_command"),
        sa.CheckConstraint(
            _in("source", ("auto", "manual_dashboard", "manual_bot", "manual_override")),
            name="ck_valve_commands_source",
        ),
        sa.CheckConstraint(
            _in("status", ("pending", "confirmed", "failed", "timeout")), name="ck_valve_commands_status"
        ),
    )
    op.create_index("ix_valve_commands_flat_id", "valve_commands", ["flat_id"])
    op.create_index(
        "ix_valve_commands_pending", "valve_commands", ["flat_id"],
        postgresql_where=sa.text("status = 'pending'"), sqlite_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", BIGPK, primary_key=True, autoincrement=True),
        sa.Column("timestamp", TS, nullable=False, server_default=NOW),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(64)),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("details", JSON),
        sa.Column("flat_id", sa.Uuid()),
        sa.Column("society_id", sa.Uuid()),
        sa.ForeignKeyConstraint(
            ["flat_id"], ["flats.id"], name="fk_audit_log_flat_id_flats", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["society_id"], ["societies.id"], name="fk_audit_log_society_id_societies", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"])
    op.create_index("ix_audit_log_timestamp", "audit_log", ["timestamp"])

    op.create_table(
        "forecasts",
        _uuid_pk(),
        sa.Column("flat_id", sa.Uuid(), nullable=False),
        sa.Column("generated_at", TS, nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("predictions", JSON, nullable=False),
        sa.Column("model_version", sa.String(100), nullable=False),
        sa.Column("mape", sa.Float()),
        sa.ForeignKeyConstraint(
            ["flat_id"], ["flats.id"], name="fk_forecasts_flat_id_flats", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_forecasts_flat_generated", "forecasts", ["flat_id", "generated_at"])

    op.create_table(
        "sensor_health",
        sa.Column("flat_id", sa.Uuid(), primary_key=True),
        sa.Column("last_seen", TS),
        sa.Column("status", sa.String(10), nullable=False, server_default="offline"),
        sa.Column("missed_readings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(
            ["flat_id"], ["flats.id"], name="fk_sensor_health_flat_id_flats", ondelete="CASCADE"
        ),
        sa.CheckConstraint("status IN ('online', 'late', 'offline')", name="ck_sensor_health_status"),
    )

    if _pg():
        op.execute(
            """
            CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
            BEGIN
                NEW.updated_at = now();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in ("societies", "flats", "alerts"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
            )


def downgrade() -> None:
    if _pg():
        for table in ("societies", "flats", "alerts"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
        op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    for table in (
        "sensor_health", "forecasts", "audit_log", "valve_commands", "alerts", "anomalies",
        "baselines", "flow_readings", "users", "flats", "societies",
    ):
        op.drop_table(table)
