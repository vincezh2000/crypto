# File: crypto/app.py

import os
import logging
import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit
from binance.client import Client
from binance import ThreadedWebsocketManager

from read_data import load_initial_klines
from trading_engine.orders import KLINE_DATA_MAP, boll_params_map, DEFAULT_BOLL_PARAMS
from trading_engine.strategies.boll_strategy import recalc_boll_indicators
from trading_engine.paper_trading import (
    paper_account, reset_paper_account, update_unrealized_pnl,
    on_new_bar_boll_strategy, paper_trading_enabled, set_paper_trading_enabled
)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'secret!')
socketio = SocketIO(app, cors_allowed_origins="*")

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', 8000))

rest_client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

DEFAULT_SYMBOL = 'BTCUSDT'
DEFAULT_INTERVAL = '1m'
SUBSCRIPTION_MAP = {}  # (symbol, interval) -> socket handle

########################
#     Flask 路由
########################

@app.route('/')
def index():
    """主页面(实时K线)"""
    return render_template('index.html')

@app.route('/paper')
def paper_trading_page():
    """模拟盘页面"""
    return render_template('paper_trading.html')

@app.route('/backtest')
def backtest_page():
    """回测页面(占位)"""
    return render_template('backtest.html')

# ---- 模拟盘相关接口 ----
@app.route('/paper_trading_info')
def paper_trading_info():
    """查看当前模拟账户信息"""
    return jsonify(paper_account)

@app.route('/paper_trading_reset', methods=['POST'])
def paper_trading_reset():
    """重置模拟账户"""
    reset_paper_account()
    return jsonify({"msg": "paper trading reset done"})

@app.route('/paper_start', methods=['POST'])
def paper_start():
    """开始模拟盘"""
    set_paper_trading_enabled(True)
    return jsonify({"msg": "paper trading started"})

@app.route('/paper_stop', methods=['POST'])
def paper_stop():
    """结束模拟盘"""
    set_paper_trading_enabled(False)
    return jsonify({"msg": "paper trading stopped"})

# ---- Boll 参数设置接口 ----
@app.route('/get_klines')
def get_klines():
    """返回后端存储的 (symbol, interval) 全部K线 + 指标"""
    symbol = request.args.get('symbol', DEFAULT_SYMBOL)
    interval = request.args.get('interval', DEFAULT_INTERVAL)
    key = (symbol, interval)

    if key not in KLINE_DATA_MAP:
        load_initial_klines(rest_client, symbol, interval, KLINE_DATA_MAP, limit=500)

    data_list = KLINE_DATA_MAP.get(key, [])
    return jsonify({"symbol": symbol, "interval": interval, "data": data_list})

@app.route('/set_boll_params', methods=['POST'])
def set_boll_params():
    """POST Body: { "symbol":"BTCUSDT", "interval":"1m", "period":20, "k":2.0 }"""
    body = request.json
    symbol = body.get("symbol")
    interval = body.get("interval")
    period = body.get("period")
    k_val = body.get("k")

    if not symbol or not interval:
        return jsonify({"error": "symbol, interval 必填"}), 400
    if not isinstance(period, int) or period <= 0:
        return jsonify({"error": "period must be positive int"}), 400
    if not isinstance(k_val, (int, float)) or k_val <= 0:
        return jsonify({"error": "k must be positive float"}), 400

    key = (symbol, interval)
    boll_params_map[key] = { "period": period, "k": float(k_val) }

    # 若已有数据则重算
    if key in KLINE_DATA_MAP:
        data_list = KLINE_DATA_MAP[key]
        recalc_boll_indicators(data_list, boll_params_map[key])

    return jsonify({"msg": "ok", "symbol": symbol, "interval": interval})

########################
#   SocketIO 事件
########################

@socketio.on('change_symbol_interval')
def handle_change_symbol_interval(data):
    old_symbol = data.get('old_symbol')
    old_interval = data.get('old_interval')
    new_symbol = data.get('new_symbol')
    new_interval = data.get('new_interval')

    if old_symbol and old_interval:
        unsubscribe_kline(old_symbol, old_interval)

    if new_symbol and new_interval:
        subscribe_kline(new_symbol, new_interval)

    emit('change_symbol_interval_done', {
        "msg": f"已切换订阅到 {new_symbol}/{new_interval}"
    })

@socketio.on('connect')
def handle_connect():
    app.logger.info("客户端连接 SocketIO")

########################
#   Binance WS逻辑
########################

def subscribe_kline(symbol, interval):
    """若未订阅则订阅"""
    key = (symbol, interval)
    if key in SUBSCRIPTION_MAP:
        app.logger.info(f"已在订阅 {key}, 无需重复")
        return
    app.logger.info(f"开始订阅: {key}")

    handle = twm.start_kline_socket(callback=handle_kline_message, symbol=symbol, interval=interval)
    SUBSCRIPTION_MAP[key] = handle

def unsubscribe_kline(symbol, interval):
    key = (symbol, interval)
    if key in SUBSCRIPTION_MAP:
        handle = SUBSCRIPTION_MAP.pop(key)
        twm.stop_socket(handle)
        app.logger.info(f"已取消订阅: {key}")

def handle_kline_message(msg):
    if msg.get('e') != 'kline':
        return

    k = msg['k']
    if not k['x']:
        return  # 只在k线收盘时处理

    symbol = k['s']
    interval = k['i']
    open_time = k['t']
    close_price = float(k['c'])
    open_price = float(k['o'])
    high_price = float(k['h'])
    low_price = float(k['l'])
    volume = float(k['v'])

    t_sec = open_time // 1000
    key = (symbol, interval)

    if key not in KLINE_DATA_MAP:
        load_initial_klines(rest_client, symbol, interval, KLINE_DATA_MAP)

    data_list = KLINE_DATA_MAP[key]
    if data_list and data_list[-1]["time"] == t_sec:
        # 覆盖
        data_list[-1].update({
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume
        })
    else:
        # 新增
        data_list.append({
            "time": t_sec,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume
        })

    # 重新计算BOLL
    from trading_engine.strategies.boll_strategy import recalc_boll_indicators
    bparams = boll_params_map.get(key, DEFAULT_BOLL_PARAMS)
    recalc_boll_indicators(data_list, bparams)

    new_bar = data_list[-1]

    # ---- 若模拟盘开启, 执行策略并发交易事件 ----
    if paper_trading_enabled():
        trade_info = on_new_bar_boll_strategy(new_bar)
        # 若返回了交易信息(买/卖), 则通过SocketIO发给前端
        if trade_info is not None:
            socketio.emit('trade_event', trade_info)
        # 更新浮盈
        update_unrealized_pnl(close_price)
        # 也可在平仓时再 emit

    # 推送K线更新
    socketio.emit('kline_update', {
        "symbol": symbol,
        "interval": interval,
        "kline": new_bar
    })


if __name__ == '__main__':
    # 启动WS
    twm.start()
    # 默认订阅
    subscribe_kline(DEFAULT_SYMBOL, DEFAULT_INTERVAL)
    socketio.run(app, host=HOST, port=PORT, debug=True)
