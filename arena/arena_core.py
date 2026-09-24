"""The arena's rules, shared by both players (Eikos and Jev get the identical request template; only the portfolio
lines differ, each sees its own). Every cycle, for each market: one "choice" question (long / flat / short for the
next 5 minutes) and one "boolean" question (higher in 5 minutes?). Paper accounting: fills at the best bid/ask,
the market's taker fee, Hyperliquid's hourly funding. Forecasts are scored with the Brier score."""
import datetime as dt
import math
from zoneinfo import ZoneInfo

from hl import MARKETS

START_EQUITY = 10_000.0
NOTIONAL = 2_000.0
CYCLE_MIN = 5
NY = ZoneInfo("America/New_York")
SIDE = {"long": 1, "flat": 0, "short": -1}
SIDE_NAME = {1: "long", 0: "flat", -1: "short"}


def fpx(x):
    if x <= 0:
        return "0"
    d = min(4, max(0, 5 - int(math.floor(math.log10(x)))))
    return f"{x:,.{d}f}"


def fusd(x, sign=False):
    s = f"${abs(x):,.2f}"
    return (("+" if x >= 0 else "-") + s) if sign else (("-" if x < 0 else "") + s)


def fbig(x):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= div:
            return f"${x / div:,.2f}{suf}"
    return f"${x:,.0f}"


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def fch(x):
    s = f"{x:+.2f}%"
    return "+0.00%" if s == "-0.00%" else s


def us_market_open(t):
    ny = dt.datetime.fromtimestamp(t, NY)
    return ny.weekday() < 5 and dt.time(9, 30) <= ny.time() < dt.time(16, 0)


class Portfolio:
    def __init__(self, d=None):
        d = d or {}
        self.balance = d.get("balance", START_EQUITY)
        self.pos = d.get("pos", {})  # coin -> {"size": signed units, "entry": px, "t": opened}
        self.fees, self.funding = d.get("fees", 0.0), d.get("funding", 0.0)
        self.realized, self.trades = d.get("realized", 0.0), d.get("trades", 0)

    def to_dict(self):
        return {"balance": self.balance, "pos": self.pos, "fees": self.fees, "funding": self.funding,
                "realized": self.realized, "trades": self.trades}

    def side(self, coin):
        p = self.pos.get(coin)
        return 0 if not p else (1 if p["size"] > 0 else -1)

    def upnl(self, coin, mid):
        p = self.pos.get(coin)
        return p["size"] * (mid - p["entry"]) if p else 0.0

    def equity(self, mids):
        return self.balance + sum(self.upnl(c, mids[c]) for c in self.pos if c in mids)

    def set_side(self, coin, side, bid, ask, fee, t):
        cur = self.side(coin)
        if side == cur:
            return []
        fills = []
        if cur:
            p = self.pos.pop(coin)
            px = bid if p["size"] > 0 else ask
            pnl, f = p["size"] * (px - p["entry"]), abs(p["size"]) * px * fee
            self.balance += pnl - f
            self.realized += pnl
            self.fees += f
            self.trades += 1
            fills.append({"coin": coin, "action": "close", "side": cur, "px": px, "pnl": pnl, "fee": f})
        if side:
            px = ask if side > 0 else bid
            f = NOTIONAL * fee
            self.balance -= f
            self.fees += f
            self.trades += 1
            self.pos[coin] = {"size": side * NOTIONAL / px, "entry": px, "t": t}
            fills.append({"coin": coin, "action": "open", "side": side, "px": px, "fee": f})
        return fills

    def pay_funding(self, ctx):
        """Hourly: a long pays size * oracle * rate when the rate is positive (a short receives it)."""
        paid = 0.0
        for c, p in self.pos.items():
            amt = p["size"] * ctx[c]["oracle"] * ctx[c]["funding"]
            self.balance -= amt
            self.funding += amt
            paid += amt
        return paid


def state_text(snap, pf: Portfolio):
    t = snap["t"]
    utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    mids = snap["mid"]
    eq = pf.equity(mids)
    n_long = sum(1 for c in pf.pos if pf.side(c) > 0)
    lines = [
        "LIVE PAPER-TRADING ARENA on Hyperliquid (simulated money, real market data).",
        f"Time: {utc:%Y-%m-%d %H:%M} UTC ({utc:%A}). US stock market: {'open' if us_market_open(t) else 'closed'} "
        "(regular session 13:30-20:00 UTC on weekdays); tokenized stocks, indices and commodities trade 24/7 on Hyperliquid.",
        f"Rules: every {CYCLE_MIN} minutes you choose, for each of the {len(MARKETS)} markets below, to be long, flat or short, "
        f"with ${NOTIONAL:,.0f} notional per position, held until the next decision. Trades fill at the best bid/ask and "
        "pay the market's taker fee; open positions pay or receive Hyperliquid's hourly funding.",
        f"Your portfolio: equity {fusd(eq)} (started at {fusd(START_EQUITY)}; {pct(eq, START_EQUITY):+.2f}%). "
        f"Realized P&L {fusd(pf.realized, True)}, fees paid {fusd(pf.fees)}, funding paid {fusd(pf.funding, True)}. "
        f"Open positions: {len(pf.pos)} ({n_long} long, {len(pf.pos) - n_long} short).",
        "",
        "MARKETS (price = middle of best bid and ask; closes run oldest to newest, the last one is the current price)",
    ]
    for coin, label, cat, desc in MARKETS:
        c5, c1, cx = snap["c5"][coin], snap["c1"][coin], snap["ctx"][coin]
        bid, ask = snap["book"][coin]
        mid = mids[coin]
        ch5 = pct(mid, c5[-2]) if len(c5) >= 2 else 0.0
        ch1h = pct(mid, c5[0]) if len(c5) >= 13 else pct(mid, c1[-2]) if len(c1) >= 2 else 0.0
        ch24 = pct(mid, cx["prev_day"]) if cx["prev_day"] else 0.0
        p = pf.pos.get(coin)
        if p:
            since = dt.datetime.fromtimestamp(p["t"], dt.timezone.utc)
            yp = (f"{SIDE_NAME[pf.side(coin)]} ${NOTIONAL:,.0f} since {since:%H:%M} UTC, entry {fpx(p['entry'])}, "
                  f"unrealized {fusd(pf.upnl(coin, mid), True)}")
        else:
            yp = "none"
        lines += [
            f"{label} | {cat}: {desc}",
            f"  price {fpx(mid)}; change 5m {fch(ch5)}, 1h {fch(ch1h)}, 24h {fch(ch24)}",
            "  5-minute closes: " + " ".join(fpx(x) for x in c5[-12:]),
            "  1-hour closes: " + " ".join(fpx(x) for x in c1[-24:]),
            f"  funding {cx['funding'] * 100:+.4f}%/h; open interest {fbig(cx['oi_usd'])}; 24h volume {fbig(cx['vol24'])}; "
            f"spread {(ask - bid) / mid * 100:.3f}%; taker fee {cx['fee'] * 100:.3f}%",
            f"  your position: {yp}",
        ]
    return "\n".join(lines)


def questions(snap, pf: Portfolio):
    qs = {}
    for coin, label, cat, desc in MARKETS:
        fee = snap["ctx"][coin]["fee"] * 100
        cur = pf.side(coin)
        cost = f"costs the spread and a {fee:.3f}% fee"
        if cur == 0:
            crit = {"long": f"open a ${NOTIONAL:,.0f} long (gains if {label} rises; {cost})",
                    "flat": "stay out of this market (no trade, no cost)",
                    "short": f"open a ${NOTIONAL:,.0f} short (gains if {label} falls; {cost})"}
        else:
            mine, other = SIDE_NAME[cur], SIDE_NAME[-cur]
            crit = {mine: f"keep your {mine} (no trade, no cost)",
                    "flat": f"close your {mine} ({cost})",
                    other: f"close your {mine} and open a ${NOTIONAL:,.0f} {other} (two trades, two fees)"}
            crit = {k: crit[k] for k in ("long", "flat", "short")}
        qs[f"pos_{label}"] = {"type": "choice", "criteria": crit,
                              "instructions": f"{label} ({desc}): which position do you want for the next "
                                              f"{CYCLE_MIN} minutes? Aim to make money after costs."}
        qs[f"up_{label}"] = {"type": "boolean",
                             "instructions": f"Will {label}'s price be higher at the next decision, in {CYCLE_MIN} minutes, "
                                             f"than it is now ({fpx(snap['mid'][coin])})?",
                             "criteria": {"true": f"{label} will be higher in {CYCLE_MIN} minutes",
                                          "false": f"{label} will be the same or lower in {CYCLE_MIN} minutes"}}
    return qs


def parse_answers(ans):
    """-> {label: {"pos": side or None, "p_pos": {long, flat, short}, "p_up": float or None}}"""
    out = {}
    for coin, label, cat, desc in MARKETS:
        a, u = ans.get(f"pos_{label}") or {}, ans.get(f"up_{label}") or {}
        probs = {k: float(v) for k, v in (a.get("probabilities") or {}).items()}
        choice = a.get("choice") or (max(probs, key=probs.get) if probs else None)
        p_up = u.get("probability", u.get("noul"))
        out[label] = {"pos": SIDE.get(choice), "p_pos": probs, "p_up": float(p_up) if p_up is not None else None}
    return out


def brier(p, y):
    return (p - y) ** 2
