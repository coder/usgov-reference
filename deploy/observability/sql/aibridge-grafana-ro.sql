-- Read-only Coder database role for the Grafana "AI Gateway DB" datasource.
--
-- The AI Governance dashboard's Usage & Cost and Intercepts & Sessions panels
-- query the Coder database directly, because per-interception, per-session,
-- token, and cost data is not exposed to Prometheus or Loki. This role is the
-- least-privilege identity Grafana uses for those queries: LOGIN only, no
-- superuser, no CREATEROLE, and SELECT on just the AI Gateway / Agent Firewall
-- tables (plus users and workspace_agents for joins).
--
-- The Coder application role (`coder`) lacks CREATEROLE, so apply this as the
-- RDS master user (AWS Secrets Manager: <CLUSTER_NAME>/rds/master), connected
-- to the `coder` database. The password is supplied as a psql variable so it is
-- never written into this file:
--
--   psql "<master-url>/coder?sslmode=require" \
--     -v ON_ERROR_STOP=1 -v pw="<generated-password>" \
--     -f deploy/observability/sql/aibridge-grafana-ro.sql
--
-- deploy/observability/scripts/setup-aibridge-db-datasource.sh wraps this and
-- also publishes the password to the Kubernetes Secret aigov-grafana-db.
--
-- To revoke: REVOKE the grants below and DROP ROLE grafana_ro.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
    CREATE ROLE grafana_ro LOGIN;
  END IF;
END
$$;

ALTER ROLE grafana_ro LOGIN PASSWORD :'pw';

GRANT CONNECT ON DATABASE coder TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;

GRANT SELECT ON
  aibridge_interceptions,
  aibridge_token_usages,
  aibridge_user_prompts,
  aibridge_tool_usages,
  aibridge_model_thoughts,
  boundary_sessions,
  ai_model_prices,
  users,
  workspace_agents
TO grafana_ro;
