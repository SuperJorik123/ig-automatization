"""shared/monitoring — alert email, error-to-email logging, heartbeats and the
machine/balance checks. Everything here is a silent no-op until the ALERT_*
vars are set in .env, so dev runs and un-monitored deployments behave exactly
as before the package existed."""
