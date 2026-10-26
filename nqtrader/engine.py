"""Trading engine: turns completed bars into risk-checked orders, and keeps the books honest.

Per completed 1-minute bar, in this order:
  1. Record the bar; let the broker report fills (the paper broker simulates them here).
  2. Book fills. Entry fills are checked for slippage; exits become trades.
  3. Reconcile with the broker: positions must match, and any open position must have a
     working protective stop. Any mismatch -> lockout, then the recovery procedure.
  4. Safety actions: emergency stop, end-of-day flatten, data-staleness check.
  5. Price alerts.
  6. Strategy -> signal -> risk manager -> order (idempotent client id).
  7. Publish a state snapshot for the dashboard.

Recovery procedure (no new entries in any of these cases):
  * Position without a confirmed stop -> re-place the stop; if that fails, flatten.
  * Broker reports a position the engine didn't open -> place a protective stop at the risk
    budget's distance; if that fails, flatten.
  * Engine thinks it's in a position but the broker is flat -> adopt the broker's state.
  * Broker disconnected or data stale -> no new entries; resting exchange-side stops stay working.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta

import numpy as np

from . import events as ev
from .brokers.base import Broker, Fill
from .data import ET, Session, session_vwap
from .fills import CostModel
from .instruments import Instrument, front_contract
from .research.engine import BacktestConfig, Strategy, View, _valid, simulate
from .research.strategies import ALL, CANDIDATES
from .risk import RiskLimits, RiskManager, SystemHealth
from .store import Store


@dataclass
class Bar:
    ts: datetime     # bar open, ET (tz-aware)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class OpenPosition:
    client_id: str
    side: int
    qty: int
    entry: float
    stop: float
    target: float | None
    opened_at: str
    strategy: str
    tag: str = ""


class Engine:
    def __init__(self, *, store: Store, broker: Broker, instrument: Instrument, limits: RiskLimits,
                 strategy_name: str, strategy_params: dict, strategy_version: int | None,
                 history: list[Session], calendar: list[ev.EconEvent], earnings: list[dict],
                 mode: str = "paper", cost: CostModel | None = None, data_label: str = ""):
        if mode == "live" and not broker.is_live:
            raise ValueError("live mode needs a live broker adapter")
        self.store, self.broker, self.inst, self.mode = store, broker, instrument, mode
        self.cost = cost or CostModel(instrument)
        self.risk = RiskManager(limits, instrument)
        self.limits = limits
        self.strategy_cls = ALL[strategy_name]
        self.strategy_params = strategy_params
        self.strategy: Strategy = self.strategy_cls(**strategy_params)
        self.strategy_version = strategy_version
        self.history = list(history)
        self.calendar, self.earnings = calendar, earnings
        self.data_label = data_label
        self.position: OpenPosition | None = None
        self.pending: dict | None = None     # order sent, entry not yet filled
        self.flatten_requested: str | None = None  # reason; set until the closing fill arrives
        self.day: date | None = None
        self.bars: list[Bar] = []
        self.session: Session | None = None
        self.trades_today = 0
        self.last_signal: dict | None = None
        self.health = SystemHealth()
        self.scanner: list[dict] = []
        self.alerts_fired: list[dict] = []
        self._restore_risk_history()
        store.log("info", "engine", f"Engine started in {mode.upper()} mode", strategy=self.strategy.key,
                  limits_digest=limits.digest(), broker=broker.name)

    # ------------------------------------------------------------------ session handling

    def _restore_risk_history(self) -> None:
        for t in self.store.query("SELECT day, pnl_usd FROM trades WHERE mode=? ORDER BY id", (self.mode,)):
            self.risk.record_trade(date.fromisoformat(t["day"]), t["pnl_usd"])

    def _start_session(self, day: date) -> None:
        if self.session is not None and len(self.session) >= 200:
            self.history.append(self.session)
        self.day, self.bars, self.trades_today = day, [], 0
        if self.position is None:
            self.flatten_requested = None
        self.strategy = self.strategy_cls(**self.strategy_params)
        self.session = None
        self.contract = front_contract(self.inst.root, day)
        self.store.set("contract", {"symbol": self.contract.symbol, "expiration": str(self.contract.expiration),
                                    "roll_date": str(self.contract.roll_date)})
        self.store.log("info", "session", f"New session {day} trading {self.contract.symbol} "
                       f"(rolls {self.contract.roll_date})", ts=_iso(datetime.combine(day, time(9, 30), ET)))

    def _build_session(self) -> Session:
        b = self.bars
        s = Session(
            day=self.day,
            ts=np.array([x.ts.replace(tzinfo=None) for x in b], dtype="datetime64[ns]"),
            minute=np.array([x.ts.hour * 60 + x.ts.minute for x in b]),
            open=np.array([x.open for x in b]), high=np.array([x.high for x in b]),
            low=np.array([x.low for x in b]), close=np.array([x.close for x in b]),
            volume=np.array([x.volume for x in b]),
        )
        if self.history:
            p = self.history[-1]
            s.prev_high, s.prev_low, s.prev_close = float(p.high.max()), float(p.low.min()), float(p.close[-1])
            ranges = [h.range for h in self.history[-14:]]
            s.atr = float(np.mean(ranges)) if len(ranges) >= 5 else np.nan
        return s

    # ------------------------------------------------------------------ main step

    def on_bar(self, bar: Bar, now: datetime | None = None) -> None:
        now = now or (bar.ts + timedelta(minutes=1))
        if bar.ts.date() != self.day:
            self._start_session(bar.ts.date())
        self.bars.append(bar)
        self.session = self._build_session()
        if len(self.bars) == 1:
            self.strategy.reset(self.session)  # same per-session lifecycle as the backtester
        vwap, _ = session_vwap(self.session)
        self.store.execute("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?)",
                           (_iso(bar.ts), bar.open, bar.high, bar.low, bar.close, bar.volume, float(vwap[-1])))

        if hasattr(self.broker, "on_bar"):
            self.broker.on_bar(_iso(bar.ts), bar.open, bar.high, bar.low, bar.close)
        for f in self.broker.poll_fills():
            self._on_fill(f, now)

        self._reconcile(now, bar.close)
        self.health.data_fresh = (now - (bar.ts + timedelta(minutes=1))).total_seconds() <= self.limits.max_data_staleness_seconds
        self.health.contract_ok = self.contract.symbol == front_contract(self.inst.root, bar.ts.date()).symbol

        kill = bool(self.store.get("kill_switch", False))
        minute_after = bar.ts.hour * 60 + bar.ts.minute + 1
        flatten_min = self.limits.flatten_at.hour * 60 + self.limits.flatten_at.minute
        if self.position and kill:
            self._flatten(now, "emergency stop")
        elif self.position and minute_after >= flatten_min:
            self._flatten(now, "end-of-day flatten")

        self._check_alerts(bar, now)
        self._maybe_enter(bar, now, kill)
        self._snapshot(bar, now, kill)

    # ------------------------------------------------------------------ fills

    def _on_fill(self, f: Fill, now: datetime) -> None:
        if f.kind == "entry":
            p = self.pending or {}
            self.position = OpenPosition(
                client_id=f.client_id, side=f.side, qty=f.qty, entry=f.price, stop=p.get("stop"),
                target=p.get("target"), opened_at=f.ts, strategy=p.get("strategy", self.strategy.key),
                tag=p.get("tag", ""))
            self.pending = None
            self.store.upsert_order(client_id=f.client_id, ts=f.ts, status="filled", filled_price=f.price)
            self.store.log("info", "executor", f"FILLED {'BUY' if f.side > 0 else 'SELL'} {f.qty} "
                           f"{f.symbol} @ {f.price:.2f}", ts=f.ts)
            if msg := self.risk.check_fill_slippage(f.expected, f.price, f.side):
                self.risk.force_lockout(now, msg)
                self.store.log("warning", "risk", f"{msg}: no new entries today (stop stays in place)", ts=f.ts)
            return

        pos = self.position
        if pos is None:
            self.store.log("critical", "executor", f"Exit fill with no known position: {f}", ts=f.ts)
            return
        pnl = self.cost.pnl_usd(pos.side, pos.entry, f.price, pos.qty)
        risk_pts = abs(pos.entry - pos.stop) if pos.stop is not None else 0
        r = ((f.price - pos.entry) * pos.side / risk_pts) if risk_pts else 0.0
        self.store.execute(
            "INSERT INTO trades(day,strategy,symbol,side,qty,entry_time,exit_time,entry,exit,stop,target,reason,"
            "pnl_usd,r_multiple,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(self.day), pos.strategy, self.contract.symbol, pos.side, pos.qty, pos.opened_at, f.ts,
             round(pos.entry, 2), round(f.price, 2), pos.stop, pos.target, f.kind, round(pnl, 2), round(r, 3), self.mode))
        self.risk.record_trade(self.day, pnl)
        self.position = None
        self.flatten_requested = None
        level = "info" if pnl > 0 else "warning"
        self.store.log(level, "executor", f"EXIT {f.kind} @ {f.price:.2f}  P&L ${pnl:+.2f} ({r:+.2f}R)", ts=f.ts)

    # ------------------------------------------------------------------ reconciliation & recovery

    def _reconcile(self, now: datetime, last_price: float) -> None:
        h = self.health
        h.broker_connected = self.broker.connected()
        if not h.broker_connected:
            h.positions_reconciled = False
            self.store.log("error", "broker", "Broker disconnected: no new entries", ts=_iso(now))
            return
        broker_pos = [p for p in self.broker.positions() if p.symbol == self.contract.symbol]
        bqty = sum(p.qty for p in broker_pos)
        eqty = (self.position.side * self.position.qty) if self.position else 0
        orders = self.broker.working_orders()

        if bqty != eqty:
            if bqty == 0:
                self.store.log("critical", "reconcile", f"Engine expected {eqty} contracts but broker is flat; "
                               "adopting broker state", ts=_iso(now))
                self.position = None
                self.flatten_requested = None
            else:
                self.store.log("critical", "reconcile", f"Broker shows {bqty} contracts, engine expected {eqty}",
                               ts=_iso(now))
                bp = broker_pos[0]
                side = 1 if bqty > 0 else -1
                dist = max(self.inst.tick_size * 8, self.limits.risk_per_trade_usd / (abs(bqty) * self.inst.point_value))
                self.position = OpenPosition("recovered-" + _iso(now), side, abs(bqty), bp.avg_price,
                                             self.inst.round_to_tick(bp.avg_price - side * dist), None,
                                             _iso(now), "unknown")
            self.risk.force_lockout(now, "position mismatch with broker")

        h.positions_reconciled = True
        if self.position is None:
            h.protective_orders_confirmed = True
            return
        if self.flatten_requested:
            h.protective_orders_confirmed = False  # closing at market; no new entries until flat
            return
        side = self.position.side
        has_stop = any(o.kind == "stop" and o.side == -side and o.qty >= self.position.qty for o in orders)
        if has_stop:
            h.protective_orders_confirmed = True
            return
        h.protective_orders_confirmed = False
        self.risk.force_lockout(now, "protective stop missing")
        self.store.log("critical", "recovery", "Open position has no working stop; re-placing it", ts=_iso(now))
        stop = self.position.stop
        if stop is None or (last_price - stop) * side <= 0:
            stop = self.inst.round_to_tick(last_price - side * self.inst.tick_size * 8)
        ack = self.broker.place_stop(client_id=self.position.client_id + ":stop-r", symbol=self.contract.symbol,
                                     side=-side, qty=self.position.qty, stop=stop, parent=self.position.client_id)
        if ack.accepted:
            self.position.stop = stop
            h.protective_orders_confirmed = True
            self.store.log("warning", "recovery", f"Protective stop restored at {stop:.2f}", ts=_iso(now))
        else:
            self.store.log("critical", "recovery", f"Could not place stop ({ack.reason}); flattening", ts=_iso(now))
            self._flatten(now, "recovery: stop could not be confirmed")

    def _flatten(self, now: datetime, reason: str) -> None:
        if self.flatten_requested:
            return  # already working; never fire a second market order
        self.flatten_requested = reason
        self.store.log("warning", "executor", f"FLATTEN: {reason}", ts=_iso(now))
        for f in self.broker.flatten(self.contract.symbol, reason):
            self._on_fill(f, now)
        self.pending = None

    # ------------------------------------------------------------------ alerts

    def _check_alerts(self, bar: Bar, now: datetime) -> None:
        for a in self.store.query("SELECT * FROM alerts WHERE status='active'"):
            hit = bar.high >= a["price"] if a["direction"] == "above" else bar.low <= a["price"]
            if hit:
                self.store.execute("UPDATE alerts SET status='triggered', triggered_at=? WHERE id=?", (_iso(now), a["id"]))
                msg = f"Price alert: NQ {'above' if a['direction'] == 'above' else 'below'} {a['price']:.2f}"
                if a.get("note"):
                    msg += f" ({a['note']})"
                self.store.log("alert", "alerts", msg, ts=_iso(now), alert_id=a["id"])

    # ------------------------------------------------------------------ entries

    def news_blackout(self, now: datetime) -> str | None:
        return ev.blackout_reason(self.calendar, now, self.limits.news_blackout_minutes_before,
                                  self.limits.news_blackout_minutes_after)

    def _maybe_enter(self, bar: Bar, now: datetime, kill: bool) -> None:
        s, strat = self.session, self.strategy
        m = bar.ts.hour * 60 + bar.ts.minute
        if self.position or self.pending or np.isnan(s.atr):
            return
        flatten_min = self.limits.flatten_at.hour * 60 + self.limits.flatten_at.minute
        if self.trades_today >= strat.max_trades_per_day or not (strat.entry_start <= m < min(strat.entry_end, flatten_min)):
            return
        vwap, vsd = session_vwap(s)
        sig = strat.on_bar(View(s, len(s) - 1, vwap, vsd))
        if sig is None or not _valid(sig, bar.close):
            return
        stop = self.inst.round_to_tick(sig.stop)
        target = self.inst.round_to_tick(sig.target) if sig.target is not None else None
        decision = self.risk.check_entry(now=now, side=sig.side, price=bar.close, stop=stop, open_positions=0,
                                         health=self.health, kill_switch=kill, news_blackout=self.news_blackout(now))
        self.last_signal = {"time": _iso(bar.ts), "strategy": strat.key, "side": sig.side, "price": bar.close,
                            "stop": stop, "target": target, "allowed": decision.allowed, "qty": decision.qty,
                            "reasons": decision.reasons, "tag": sig.tag}
        self.trades_today += 1  # the strategy's setup is consumed whether or not risk allowed it
        if not decision.allowed:
            self.store.log("warning", "risk", f"Signal BLOCKED ({strat.key} {'long' if sig.side > 0 else 'short'}): "
                           + "; ".join(decision.reasons), ts=_iso(now))
            return

        client_id = hashlib.sha256(f"{strat.key}|{self.day}|{_iso(bar.ts)}|{sig.side}".encode()).hexdigest()[:16]
        if self.store.order_exists(client_id):
            self.store.log("warning", "executor", f"Duplicate signal ignored ({client_id})", ts=_iso(now))
            return
        self.store.upsert_order(client_id=client_id, ts=_iso(now), symbol=self.contract.symbol, side=sig.side,
                                qty=decision.qty, kind="bracket", price=bar.close, status="sent", note=strat.key)
        ack = self.broker.place_bracket(client_id=client_id, symbol=self.contract.symbol, side=sig.side,
                                        qty=decision.qty, ref_price=bar.close, stop=stop, target=target)
        if not ack.accepted:
            self.store.upsert_order(client_id=client_id, ts=_iso(now), status="rejected", note=ack.reason)
            self.store.log("error", "executor", f"Order REJECTED: {ack.reason}", ts=_iso(now))
            return
        self.store.upsert_order(client_id=client_id, ts=_iso(now), status="working", broker_id=ack.broker_id)
        self.pending = {"client_id": client_id, "stop": stop, "target": target, "strategy": strat.key, "tag": sig.tag}
        self.store.log("info", "executor", f"ORDER {'BUY' if sig.side > 0 else 'SELL'} {decision.qty} "
                       f"{self.contract.symbol} mkt | stop {stop:.2f} | target "
                       f"{'-' if target is None else f'{target:.2f}'} ({strat.key})", ts=_iso(now))

    # ------------------------------------------------------------------ scanner

    def scan(self) -> list[dict]:
        """Run every candidate strategy over today's bars to show what each one sees right now."""
        s = self.session
        if s is None or np.isnan(s.atr):
            return []
        cfg = BacktestConfig(cost=self.cost)
        vwap, vsd = session_vwap(s)
        m = int(s.minute[-1])
        out = []
        for cls in CANDIDATES:
            strat = cls(**cls.param_grid()[0]) if cls.name != self.strategy_cls.name else \
                self.strategy_cls(**self.strategy_params)
            trades, signals, open_pos, pending = simulate(strat, s, cfg, live=True)
            if m < strat.entry_start:
                window = "waiting"
            elif m < strat.entry_end:
                window = "active"
            else:
                window = "closed"
            state = "signal" if pending else "in trade" if open_pos else (
                "done" if len(trades) >= strat.max_trades_per_day else "watching" if window == "active" else window)
            out.append({
                "strategy": strat.key, "name": cls.name, "description": (cls.__doc__ or "").strip().split("\n")[0],
                "production": cls.name == self.strategy_cls.name, "state": state, "window": window,
                "window_start": _hm(strat.entry_start), "window_end": _hm(strat.entry_end),
                "signals_today": signals[-3:], "sim_pnl_today": round(sum(t.pnl_usd for t in trades), 2),
            })
        k30 = int(np.searchsorted(s.minute, 600))
        levels = {"vwap": round(float(vwap[-1]), 2), "vwap_upper": round(float(vwap[-1] + 2 * vsd[-1]), 2),
                  "vwap_lower": round(float(vwap[-1] - 2 * vsd[-1]), 2),
                  "prior_high": s.prev_high, "prior_low": s.prev_low, "prior_close": s.prev_close,
                  "atr": round(s.atr, 2)}
        if m >= 600:
            levels.update(orb30_high=float(s.high[:k30].max()), orb30_low=float(s.low[:k30].min()))
        self.scanner = out
        self.store.set("scanner", {"strategies": out, "levels": levels, "as_of": str(s.ts[-1])[:16]})
        return out

    # ------------------------------------------------------------------ dashboard state

    def exit_plan(self, last: float) -> dict | None:
        p = self.position
        if p is None:
            return None
        risk_pts = abs(p.entry - p.stop) if p.stop is not None else None
        pv = self.inst.point_value * p.qty
        levels = []
        if risk_pts:
            for r in (-1, 1, 2, 3):
                levels.append({"label": "Stop (-1R)" if r == -1 else f"+{r}R",
                               "price": self.inst.round_to_tick(p.entry + p.side * r * risk_pts),
                               "pnl": round(r * risk_pts * pv - self.cost.commission * p.qty, 2)})
        upnl = (last - p.entry) * p.side * pv
        return {
            **asdict(p), "last": last, "unrealized": round(upnl, 2),
            "r_now": round((last - p.entry) * p.side / risk_pts, 2) if risk_pts else None,
            "risk_usd": round(risk_pts * pv + self.cost.commission * p.qty, 2) if risk_pts else None,
            "reward_usd": round(abs(p.target - p.entry) * pv - self.cost.commission * p.qty, 2) if p.target else None,
            "time_exit": self.limits.flatten_at.strftime("%H:%M"), "levels": levels,
            "rules": ["Stop is a resting exchange order and is never widened or removed",
                      f"Target {'%.2f' % p.target if p.target else 'none (time exit)'}",
                      f"Flat by {self.limits.flatten_at:%H:%M} ET regardless of P&L"],
        }

    def timers(self, now: datetime) -> list[dict]:
        day = now.date()
        mk = lambda hh, mm: datetime.combine(day, time(hh, mm), ET)  # noqa: E731
        st = self.strategy
        rows = [
            {"label": "RTH open", "at": mk(9, 30)},
            {"label": "10:00 opening range complete", "at": mk(10, 0)},
            {"label": f"{st.name} entry window opens", "at": mk(st.entry_start // 60, st.entry_start % 60)},
            {"label": f"{st.name} entry window closes", "at": mk(st.entry_end // 60, st.entry_end % 60)},
            {"label": "No new entries (risk)", "at": datetime.combine(day, self.limits.no_entries_after, ET)},
            {"label": "Flatten all positions", "at": datetime.combine(day, self.limits.flatten_at, ET)},
            {"label": "RTH close", "at": mk(16, 0)},
        ]
        for e in self.calendar:
            if e.at.date() == day:
                rows.append({"label": f"{e.event} ({e.impact})", "at": e.at, "news": True})
        rows.sort(key=lambda r: r["at"])
        return [{**r, "at": r["at"].isoformat()} for r in rows]

    def _snapshot(self, bar: Bar, now: datetime, kill: bool) -> None:
        last = bar.close
        plan = self.exit_plan(last)
        unreal = plan["unrealized"] if plan else 0.0
        budgets = self.risk.budgets(now.date(), unreal)
        balance = self.broker.balance()
        day_pnl = budgets["rows"][0]["pnl"]
        self.store.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?,?,?,?)",
                           (_iso(bar.ts), balance, round(balance + unreal, 2), unreal, day_pnl,
                            budgets["rows"][3]["used"]))
        state = {
            "as_of": _iso(now), "bar_time": _iso(bar.ts), "mode": self.mode, "broker": self.broker.name,
            "data_label": self.data_label, "last_price": last,
            "instrument": {"root": self.inst.root, "tick_size": self.inst.tick_size,
                           "point_value": self.inst.point_value, "commission_rt": self.cost.commission},
            "contract": {"symbol": self.contract.symbol, "expiration": str(self.contract.expiration),
                         "roll_date": str(self.contract.roll_date),
                         "days_to_roll": (self.contract.roll_date - now.date()).days},
            "health": asdict(self.health), "kill_switch": kill,
            "lockout": self.risk.lockout, "news_blackout": self.news_blackout(now),
            "position": plan, "pending_order": self.pending,
            "working_orders": [asdict(o) for o in self.broker.working_orders()], "last_signal": self.last_signal,
            "budgets": budgets, "balance": balance, "unrealized": unreal,
            "strategy": {"key": self.strategy.key, "version": self.strategy_version,
                         "trades_today": self.trades_today, "max_per_day": self.strategy.max_trades_per_day},
            "limits": json.loads(self.limits.model_dump_json()), "limits_digest": self.limits.digest(),
            "consecutive_losses": self.risk.consecutive_losses(now.date()),
            "timers": self.timers(now),
            "econ_upcoming": ev.upcoming(self.calendar, now),
            "earnings": ev.earnings_outlook(self.earnings, now.date()),
        }
        self.store.set("engine_state", state)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
