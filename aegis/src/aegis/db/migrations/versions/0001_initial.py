"""Initial AegisOps schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def now() -> sa.sql.elements.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", TS, nullable=False, server_default=now()),
    )
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, index=True),
        sa.Column("detected_at", TS, nullable=False),
        sa.Column("onset_at", TS),
        sa.Column("resolved_at", TS),
        sa.Column("affected_services", JSONB, nullable=False, server_default="[]"),
        sa.Column("symptoms", JSONB, nullable=False, server_default="[]"),
        sa.Column("root_service", sa.String(64)),
        sa.Column("category", sa.String(40)),
        sa.Column("confidence", sa.Float),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("outcome", sa.String(40)),
        sa.Column("iteration", sa.Integer, nullable=False, server_default="0"),
        sa.Column("human_interventions", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=False, server_default=now()),
        sa.Column("updated_at", TS, nullable=False, server_default=now()),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(40), sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True),
        sa.Column("ts", TS, nullable=False, server_default=now(), index=True),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("actor", sa.String(80), nullable=False, server_default="aegis-engine"),
        sa.Column("data", JSONB, nullable=False, server_default="{}"),
    )
    op.create_table(
        "evidence",
        sa.Column("incident_id", sa.String(40), sa.ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.String(24), primary_key=True),
        sa.Column("iteration", sa.Integer, nullable=False, server_default="0"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("service", sa.String(64)),
        sa.Column("signal", sa.String(64)),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("data", JSONB, nullable=False, server_default="{}"),
        sa.Column("source", JSONB, nullable=False, server_default="{}"),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("score", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("created_at", TS, nullable=False, server_default=now()),
    )
    for table in ("diagnoses", "remediation_plans"):
        cols = [
            sa.Column("id", sa.String(40), primary_key=True),
            sa.Column("incident_id", sa.String(40), sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True),
            sa.Column("content", JSONB, nullable=False),
            sa.Column("created_at", TS, nullable=False, server_default=now()),
        ]
        if table == "diagnoses":
            cols += [sa.Column("iteration", sa.Integer, nullable=False), sa.Column("confidence", sa.Float, nullable=False),
                     sa.Column("method", sa.String(16), nullable=False)]
        else:
            cols += [sa.Column("diagnosis_id", sa.String(40), nullable=False)]
        op.create_table(table, *cols)
    op.create_table(
        "actions",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("incident_id", sa.String(40), sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True),
        sa.Column("plan_id", sa.String(40)),
        sa.Column("candidate_id", sa.String(40)),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("target", JSONB, nullable=False),
        sa.Column("params", JSONB, nullable=False, server_default="{}"),
        sa.Column("category", sa.String(40)),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("risk_level", sa.String(12)),
        sa.Column("risk_score", sa.Integer),
        sa.Column("requires_approval", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("decision", JSONB, nullable=False, server_default="{}"),
        sa.Column("message", sa.Text, nullable=False, server_default=""),
        sa.Column("revert_of", sa.String(80)),
        sa.Column("simulation_id", sa.String(80)),
        sa.Column("rationale", sa.Text, nullable=False, server_default=""),
        sa.Column("outcome", sa.String(24)),
        sa.Column("created_at", TS, nullable=False, server_default=now()),
        sa.Column("updated_at", TS, nullable=False, server_default=now()),
        sa.Column("started_at", TS),
        sa.Column("completed_at", TS),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("incident_id", sa.String(40), sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("requested_at", TS, nullable=False, server_default=now()),
        sa.Column("decided_at", TS),
        sa.Column("decided_by", sa.String(64)),
        sa.Column("reason", sa.Text),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("context", JSONB, nullable=False, server_default="{}"),
    )
    op.create_table(
        "simulations",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("incident_id", sa.String(40), sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True),
        sa.Column("candidate_id", sa.String(40)),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("target", JSONB, nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("verdict", sa.String(24)),
        sa.Column("baseline", JSONB),
        sa.Column("candidate", JSONB),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", TS, nullable=False, server_default=now()),
        sa.Column("completed_at", TS),
    )
    op.create_table(
        "verifications",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("incident_id", sa.String(40), sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True),
        sa.Column("action_id", sa.String(80), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=now()),
    )
    op.create_table(
        "telemetry_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(40), sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("captured_at", TS, nullable=False, server_default=now()),
        sa.Column("data", JSONB, nullable=False),
    )
    op.create_table(
        "postmortems",
        sa.Column("incident_id", sa.String(40), sa.ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("generated_at", TS, nullable=False, server_default=now()),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("markdown", sa.Text, nullable=False),
        sa.Column("method", sa.String(24), nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column("resource", sa.String(200), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("details", JSONB, nullable=False, server_default="{}"),
        sa.Column("source", sa.String(24), nullable=False, server_default="aegis"),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False, unique=True),
    )
    op.create_table(
        "model_calls",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(40), index=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("latency_ms", sa.Float, nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("fallback", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=now()),
    )
    op.create_table(
        "runbooks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("categories", ARRAY(sa.String(40)), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("source_path", sa.String(200), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("tsv", TSVECTOR),
    )
    op.create_index("ix_runbooks_tsv", "runbooks", ["tsv"], postgresql_using="gin")
    op.create_table(
        "service_status",
        sa.Column("service", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("signals", JSONB, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "component_heartbeats",
        sa.Column("name", sa.String(48), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("details", JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "commands",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("incident_id", sa.String(40)),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=now()),
        sa.Column("processed_at", TS),
        sa.Column("result", sa.Text),
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("summary", JSONB, nullable=False, server_default="{}"),
    )
    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(40), sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True),
        sa.Column("scenario_id", sa.String(64), nullable=False),
        sa.Column("repetition", sa.Integer, nullable=False),
        sa.Column("incident_id", sa.String(40)),
        sa.Column("passed", sa.Boolean, nullable=False),
        sa.Column("metrics", JSONB, nullable=False),
        sa.Column("details", JSONB, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=False),
    )

    # Event streaming: every timeline/system event is announced on a channel.
    op.execute("""        CREATE OR REPLACE FUNCTION aegis_notify_event() RETURNS trigger AS $$
        BEGIN
          PERFORM pg_notify('aegis_events', json_build_object('id', NEW.id, 'incident_id', NEW.incident_id,
                                                              'type', NEW.type)::text);
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
    op.execute("""        CREATE TRIGGER events_notify AFTER INSERT ON events FOR EACH ROW EXECUTE FUNCTION aegis_notify_event()""")
    op.execute("""        CREATE OR REPLACE FUNCTION aegis_notify_command() RETURNS trigger AS $$
        BEGIN
          PERFORM pg_notify('aegis_commands', NEW.id::text);
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
    op.execute("""        CREATE TRIGGER commands_notify AFTER INSERT ON commands FOR EACH ROW EXECUTE FUNCTION aegis_notify_command()""")
    # Audit log is append-only (tampering is additionally detectable via the hash chain).
    op.execute("""        CREATE OR REPLACE FUNCTION aegis_audit_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit_log is append-only';
        END; $$ LANGUAGE plpgsql""")
    op.execute("""        CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
          FOR EACH ROW EXECUTE FUNCTION aegis_audit_append_only()""")
    op.execute("""        CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
          FOR EACH STATEMENT EXECUTE FUNCTION aegis_audit_append_only()""")
    op.execute("""        CREATE OR REPLACE FUNCTION aegis_runbook_tsv() RETURNS trigger AS $$
        BEGIN
          NEW.tsv := setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
                     setweight(to_tsvector('english', array_to_string(NEW.categories, ' ')), 'A') ||
                     setweight(to_tsvector('english', coalesce(NEW.body, '')), 'B');
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
    op.execute("""        CREATE TRIGGER runbooks_tsv BEFORE INSERT OR UPDATE ON runbooks FOR EACH ROW EXECUTE FUNCTION aegis_runbook_tsv()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    for table in ("evaluation_results", "evaluation_runs", "commands", "component_heartbeats", "service_status",
                  "runbooks", "model_calls", "audit_log", "postmortems", "telemetry_snapshots", "verifications",
                  "simulations", "approvals", "actions", "remediation_plans", "diagnoses", "evidence", "events",
                  "incidents", "users"):
        op.drop_table(table)
    for fn in ("aegis_notify_event", "aegis_notify_command", "aegis_audit_append_only", "aegis_runbook_tsv"):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}() CASCADE")
