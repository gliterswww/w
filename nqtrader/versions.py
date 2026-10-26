"""Strategy versions: controlled promotion and instant rollback.

Nothing promotes itself. A human runs `python -m nqtrader promote ...`, which only succeeds if
the named experiment shows that exact strategy and parameter set passing every validation gate.
`--paper-only` allows an unvalidated version for paper trading; such versions can never be
authorized for live trading.
"""
from __future__ import annotations

import json

from .research.strategies import ALL
from .store import Store, utcnow

DEFAULT = ("orb", {"minutes": 30, "target_r": 2.0})


def production(store: Store) -> dict:
    rows = store.query("SELECT * FROM strategy_versions WHERE status IN ('production','paper-only') "
                       "ORDER BY version DESC LIMIT 1")
    if rows:
        r = rows[0]
        return {**r, "params": json.loads(r["params"])}
    store.execute("INSERT INTO strategy_versions(created,strategy,params,status,experiment_id,notes) "
                  "VALUES(?,?,?,?,?,?)", (utcnow(), DEFAULT[0], json.dumps(DEFAULT[1]), "paper-only", None,
                                          "Default starting strategy (10 AM range breakout). Not validated."))
    return production(store)


def history(store: Store) -> list[dict]:
    return [{**r, "params": json.loads(r["params"])}
            for r in store.query("SELECT * FROM strategy_versions ORDER BY version DESC")]


def promote(store: Store, strategy: str, params: dict, experiment_id: str | None, paper_only: bool = False,
            notes: str = "") -> dict:
    if strategy not in ALL or strategy.startswith("baseline_"):
        raise ValueError(f"unknown or non-tradeable strategy {strategy!r}")
    key = ALL[strategy](**params).key
    status = "paper-only"
    if not paper_only:
        if not experiment_id:
            raise ValueError("promotion to production needs --experiment (or use --paper-only)")
        rows = store.query("SELECT summary FROM experiments WHERE id=?", (experiment_id,))
        if not rows:
            raise ValueError(f"experiment {experiment_id} not found")
        summary = json.loads(rows[0]["summary"]).get(strategy)
        if not summary:
            raise ValueError(f"experiment {experiment_id} did not test {strategy}")
        if summary["verdict"] != "candidate":
            raise ValueError(f"{strategy} did not pass validation in {experiment_id} (verdict: {summary['verdict']})")
        if summary["holdout_params"] != key:
            raise ValueError(f"validated parameters were {summary['holdout_params']}, not {key}")
        status = "production"
    store.execute("UPDATE strategy_versions SET status='retired' WHERE status IN ('production','paper-only')")
    store.execute("INSERT INTO strategy_versions(created,strategy,params,status,experiment_id,notes) VALUES(?,?,?,?,?,?)",
                  (utcnow(), strategy, json.dumps(params), status, experiment_id, notes))
    store.log("warning", "versions", f"Promoted {key} as {status}", experiment=experiment_id)
    return production(store)


def rollback(store: Store) -> dict:
    """Retire the current version and reactivate the one before it."""
    rows = store.query("SELECT * FROM strategy_versions ORDER BY version DESC")
    current = next((r for r in rows if r["status"] in ("production", "paper-only")), None)
    previous = [r for r in rows if current and r["version"] < current["version"]]
    if not current or not previous:
        raise ValueError("nothing to roll back to")
    prev = previous[0]
    store.execute("UPDATE strategy_versions SET status='rolled-back' WHERE version=?", (current["version"],))
    store.execute("UPDATE strategy_versions SET status=? WHERE version=?",
                  ("production" if prev["experiment_id"] else "paper-only", prev["version"]))
    store.log("warning", "versions", f"Rolled back v{current['version']} -> v{prev['version']}")
    return production(store)
