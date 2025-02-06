# File: crypto/trading_engine/paper_trading.py

import math
import json
import os

# 读取 config.json
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config.json")
with open(CONFIG_PATH, "r") as f:
    config_data = json.load(f)

INIT_BALANCE = config_data["trading"]["init_balance"]
FEE_RATE = config_data["trading"]["fee_rate"]
SLIP_RATE = config_data["trading"]["slip_rate"]

# 简单全局flag, 表示模拟盘是否启用
_paper_trading_active = True  # 默认打开, 也可False

def paper_trading_enabled():
    return _paper_trading_active

def set_paper_trading_enabled(enabled: bool):
    global _paper_trading_active
    _paper_trading_active = enabled

# 维护账户(只做单向多头)
paper_account = {
    "balance": INIT_BALANCE,
    "position": 0.0,
    "avg_price": 0.0,
    "unrealized_pnl": 0.0,
    "realized_pnl": 0.0,
}

def reset_paper_account():
    paper_account["balance"] = INIT_BALANCE
    paper_account["position"] = 0.0
    paper_account["avg_price"] = 0.0
    paper_account["unrealized_pnl"] = 0.0
    paper_account["realized_pnl"] = 0.0

def update_unrealized_pnl(latest_price):
    pos = paper_account["position"]
    avgp = paper_account["avg_price"]
    if pos > 0:
        paper_account["unrealized_pnl"] = (latest_price - avgp)*pos
    else:
        paper_account["unrealized_pnl"] = 0.0

def execute_market_order(side, qty, price):
    """
    side='buy'/'sell', 价格+滑点 => 成交价
    返回 (trade_price, fee)
    """
    slip = price * SLIP_RATE
    if side=='buy':
        trade_price = price + slip
    else:
        trade_price = price - slip

    amount = trade_price * qty
    fee = amount * FEE_RATE
    return trade_price, fee


def on_new_bar_boll_strategy(bar):
    """
    每次K线收盘时, 若close<下轨 => 全仓买, 若close>上轨 => 全部卖
    返回: None or 交易信息dict, 用于前端markers
    """
    close_p = bar["close"]
    bb_up = bar.get("bb_upper")
    bb_low= bar.get("bb_lower")

    if bb_up is None or bb_low is None:
        return None  # 尚未形成boll

    bal  = paper_account["balance"]
    pos  = paper_account["position"]
    avgp = paper_account["avg_price"]

    # buy
    if close_p < bb_low:
        if pos<=0:  # 空仓时买
            qty = bal/close_p  # 全仓
            trade_price, fee = execute_market_order('buy', qty, close_p)
            cost = trade_price*qty + fee
            if cost<=bal:
                paper_account["balance"] -= cost
                paper_account["position"] = pos+qty
                paper_account["avg_price"] = trade_price
                # realized_pnl不变
                print(f"[PaperTrade] BUY {qty:.4f} @ {trade_price:.2f}, fee={fee:.4f}")
                return {
                    "barTime": bar["time"],
                    "side": "buy",
                    "qty": qty,
                    "price": trade_price,
                    "fee": fee,
                    "pnl": 0.0
                }

    # sell
    elif close_p > bb_up:
        if pos>0:
            qty = pos
            trade_price, fee = execute_market_order('sell', qty, close_p)
            income = trade_price*qty
            open_cost = avgp*pos
            pnl = income - open_cost
            paper_account["balance"] += income - fee
            paper_account["realized_pnl"] += pnl
            paper_account["position"] = 0.0
            paper_account["avg_price"] = 0.0
            print(f"[PaperTrade] SELL {qty:.4f} @ {trade_price:.2f}, fee={fee:.4f}, pnl={pnl:.2f}")
            return {
                "barTime": bar["time"],
                "side": "sell",
                "qty": qty,
                "price": trade_price,
                "fee": fee,
                "pnl": pnl
            }

    return None
