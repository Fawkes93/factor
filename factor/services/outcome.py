"""
Tag closed Factor orders as won / lost / breakeven from cTrader deal PnL.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Labels stored on factor_orders.result and detail.outcome
RESULT_WON = "won"
RESULT_LOST = "lost"
RESULT_BREAKEVEN = "breakeven"
RESULT_UNKNOWN = "unknown"

# Net PnL at or below this (account currency units) counts as break-even
BREAKEVEN_EPS = 0.01


def money_to_float(raw: Any, money_digits: Optional[int] = None) -> float:
    """Convert cTrader money int + moneyDigits to float account currency."""
    if raw is None:
        return 0.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if money_digits is None:
        return v
    try:
        d = int(money_digits)
    except (TypeError, ValueError):
        return v
    if d <= 0:
        return v
    return v / (10**d)


def classify_net_pnl(net: float, *, eps: float = BREAKEVEN_EPS) -> str:
    if net > eps:
        return RESULT_WON
    if net < -eps:
        return RESULT_LOST
    return RESULT_BREAKEVEN


def summarize_close_deals(deals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    From cTrader deals for one position, compute net PnL and won/lost tag.

    Closing deals include closePositionDetail with grossProfit/swap/commission.
    """
    total_gross = 0.0
    total_swap = 0.0
    total_commission = 0.0
    total_fee = 0.0
    close_prices: List[float] = []
    close_deals = 0

    for d in deals or []:
        if not isinstance(d, dict):
            continue
        cpd = d.get("closePositionDetail") or d.get("close_position_detail")
        if not isinstance(cpd, dict):
            continue
        close_deals += 1
        digits = (
            cpd.get("moneyDigits")
            if cpd.get("moneyDigits") is not None
            else d.get("moneyDigits")
        )
        total_gross += money_to_float(cpd.get("grossProfit"), digits)
        total_swap += money_to_float(cpd.get("swap"), digits)
        total_commission += money_to_float(cpd.get("commission"), digits)
        total_fee += money_to_float(cpd.get("pnlConversionFee"), digits)
        # execution price on the deal is the close fill
        px = d.get("executionPrice") or d.get("execution_price") or cpd.get("entryPrice")
        try:
            if px is not None:
                close_prices.append(float(px))
        except (TypeError, ValueError):
            pass

    if close_deals == 0:
        return {
            "result": RESULT_UNKNOWN,
            "net_pnl": None,
            "gross_profit": None,
            "swap": None,
            "commission": None,
            "close_price": None,
            "close_deals": 0,
        }

    net = total_gross + total_swap + total_commission + total_fee
    result = classify_net_pnl(net)
    close_price = close_prices[-1] if close_prices else None
    return {
        "result": result,
        "net_pnl": round(net, 6),
        "gross_profit": round(total_gross, 6),
        "swap": round(total_swap, 6),
        "commission": round(total_commission, 6),
        "close_price": close_price,
        "close_deals": close_deals,
    }


def result_label(result: Optional[str]) -> str:
    """UI-friendly label."""
    r = (result or "").lower()
    if r == RESULT_WON:
        return "Won"
    if r == RESULT_LOST:
        return "Lost"
    if r == RESULT_BREAKEVEN:
        return "Break-even"
    if r == RESULT_UNKNOWN:
        return "Unknown"
    return "—"
