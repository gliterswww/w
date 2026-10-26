# Operations: deploy, monitor, recover, go live

## Processes

| Process | Command | Runs |
|---|---|---|
| Engine (paper) | `python -m nqtrader paper` | Market hours. Polls data, trades, writes the DB. |
| Dashboard | `python -m nqtrader serve` | Always. Read-only views plus kill switch and alerts. |
| News/earnings refresh | `python -m nqtrader refresh` | Cron, every 15 min on weekdays |
| Research | `python -m nqtrader research --data …` | On demand, never on the trading host during RTH |

They share one SQLite database (`var/nqtrader.db`, or set `NQT_DB`).

## Deploy on a Linux VPS

```bash
sudo useradd -m -s /bin/bash nqt && sudo -iu nqt
git clone <your repo> nqtrader && cd nqtrader
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p var reports
cp deploy/env.example .env && chmod 600 .env    # fill in secrets; never commit .env
.venv/bin/python -m pytest -q                    # must pass before anything runs
exit
sudo cp ~nqt/nqtrader/deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nqtrader-dashboard nqtrader-engine
```

* The dashboard binds to 127.0.0.1. Reach it through an SSH tunnel
  (`ssh -L 8000:127.0.0.1:8000 vps`) or a reverse proxy with TLS. Exposing it requires
  `NQT_DASHBOARD_TOKEN`; the server refuses to bind publicly without one.
* The TradingView webhook needs a public HTTPS URL on port 443 (TradingView only posts to
  80/443). Put Caddy or nginx in front and allowlist TradingView's IPs (see their help page).
* Set the VPS clock to UTC with NTP. All trading logic uses `America/New_York` explicitly.

## Secrets

* Only in environment variables, loaded from `.env` (mode 600) by systemd `EnvironmentFile=`.
* `NQT_DASHBOARD_TOKEN` (dashboard controls), `NQT_TV_WEBHOOK_SECRET` (notes only),
  `TRADOVATE_*` (Phase 4). None of them ever goes into config files, TradingView alerts, the
  browser, or git (`.env` is gitignored).
* Rotate them if a machine or account is compromised. The TradingView secret can't trade, so
  leaking it is low impact.

## Monitoring

* `GET /api/health`: liveness, plus engine heartbeat and last state time. Point an uptime
  monitor (UptimeRobot, Healthchecks) at it through your proxy.
* The dashboard shows red banners for: kill switch engaged, broker or reconciliation problems,
  stale engine heartbeat during RTH, daily lockout, and news blackout.
* Critical events (`level=critical`) mean reconciliation or recovery happened. Read each one.

## Backups

```bash
deploy/backup.sh            # consistent online copy via sqlite3 .backup, keeps 30 days
```

Run it from cron after the close (e.g. `15 17 * * 1-5`) and copy `var/backups/` off the host.
Restore: stop the engine, copy a backup over `var/nqtrader.db`, start the engine. On start it
reloads its risk history from the `trades` table, then reconciles against the broker.

## Runbooks

| Situation | What the system does | What you do |
|---|---|---|
| Broker disconnect | No new entries; resting stops stay at the exchange | Check broker status; the engine resumes when connected and reconciled |
| Stop not confirmed | Re-places it; flattens if that fails; locks out for the day | Inspect the event log and the broker's order book |
| Unknown position at broker | Protects it with a stop at risk-budget distance; locks out | Decide manually; never let the bot "trade around" it |
| Data stale | No new entries | Check the feed (`heartbeat.feed_error`) |
| Anything odd | n/a | **Emergency stop** (dashboard button or `python -m nqtrader kill`); it flattens and blocks entries |
| Bad strategy release | n/a | `python -m nqtrader rollback` |
| Contract roll | Switches automatically on the roll date (8 days pre-expiry) | Make sure no position is held into the roll day (the bot is flat nightly) |

## Live-launch checklist

`python -m nqtrader authorize-live` runs these checks; all must pass:

1. A live broker adapter exists and was tested on the broker's **demo** environment.
2. The production strategy version was promoted from an experiment that **passed every gate on
   real data**.
3. A paper track record on live data: at least 30 trades over at least 20 sessions, PF ≥ 1.0,
   consistent with the backtest.
4. Risk limits are valid, with first-live risk per trade ≤ 1% of capital.
5. The economic calendar covers the next 30 days.
6. The dashboard token is set, and broker credentials are in the environment only.
7. The kill switch is released, with no critical events in 24 hours.

Then go live with **one MNQ contract**, for at least 20 sessions, before you consider scaling.
Compare live fills to paper and backtest every week; a slippage gap is a reason to stop.
