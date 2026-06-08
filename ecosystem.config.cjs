/** PM2 apps for hf-workbench: HTTP API, story pipeline, and daily DB backup. */
module.exports = {
  apps: [
    {
      name: "hf-workbench",
      cwd: __dirname,
      script: ".venv/bin/python",
      args: "-m uvicorn app:app --host 0.0.0.0 --port 8088",
      interpreter: "none",
      env: {
        HF_INTERNAL_METRICS_ENABLED: "1",
      },
      autorestart: true,
      kill_timeout: 10_000,
    },
    {
      name: "hf-pipeline",
      cwd: __dirname,
      script: ".venv/bin/python",
      // --no-social: Grok x_search sweeps are manual-only
      // (`uv run python -m agents.social_topics`).
      args: "-m agents.pipeline_scheduler --no-social",
      interpreter: "none",
      autorestart: true,
      // Scheduler terminates in-flight step children on SIGTERM; PM2 SIGKILL is fallback.
      kill_timeout: 20_000,
    },
    {
      name: "hf-db-backup",
      cwd: __dirname,
      script: ".venv/bin/python",
      args: "-m ops.db_backup_scheduler",
      interpreter: "none",
      autorestart: true,
      kill_timeout: 60_000,
    },
  ],
};
