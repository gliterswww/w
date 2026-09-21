"""NQ Session Dashboard: news + up to 4 suggested entry points for the trading session."""
from __future__ import annotations

from datetime import datetime

from flask import Flask, jsonify, render_template

from entries import build_entries
from market_data import ET, get_current_price, get_opening_range, get_pivots
from news import get_news

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/session")
def api_session():
    price = get_current_price()
    pivots = get_pivots()
    orb = get_opening_range()
    entries = build_entries(pivots, orb, price)

    return jsonify({
        "price": price,
        "pivots": {
            "pivot": round(pivots.pivot, 2),
            "r1": round(pivots.r1, 2),
            "r2": round(pivots.r2, 2),
            "s1": round(pivots.s1, 2),
            "s2": round(pivots.s2, 2),
        } if pivots else None,
        "opening_range": {
            "high": round(orb.high, 2),
            "low": round(orb.low, 2),
            "formed": orb.formed,
        } if orb else None,
        "entries": entries,
        "updated_at": datetime.now(ET).strftime("%I:%M:%S %p ET"),
    })


@app.route("/api/news")
def api_news():
    return jsonify({"items": get_news()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
