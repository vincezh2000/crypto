import datetime
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import logging
from binance import ThreadedWebsocketManager
from binance.client import Client
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'secret!')
socketio = SocketIO(app, cors_allowed_origins="*")

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', 8000))

rest_client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

# ============ 默认设置 ============
DEFAULT_SYMBOL = 'BTCUSDT'
DEFAULT_INTERVAL = '1m'

# 记录当前正在订阅的 (symbol, interval) => handle, 以便需要时取消订阅
SUBSCRIPTION_MAP = {}  # key: (symbol, interval), value: socket handle

# ============ 全局存储(数据+参数) ============

# 对每个 (symbol, interval) 单独存储 Boll 参数
# 如果用户没设置过，就使用一个“默认值”
DEFAULT_BOLL_PARAMS = {"period": 20, "k": 2.0}
boll_params = {
    # (symbol, interval): { "period": int, "k": float }
}

# 保存所有历史数据(含指标)
# KLINE_DATA_MAP[(symbol, interval)] = [
#   {
#     "time": ...,
#     "open": ...,
#     "high": ...,
#     "low": ...,
#     "close": ...,
#     "volume": ...,
#     "ma7": ...,
#     "ma25": ...,
#     "ma99": ...,
#     "bb_upper": ...,
#     "bb_middle": ...,
#     "bb_lower": ...
#   }, ...
# ]
KLINE_DATA_MAP = {}


# ============ 工具函数(后端统一计算MA, BOLL等) ============
def compute_ma(data_list, period):
    """计算简单移动平均线MA(period)。"""
    result = []
    if period <= 0:
        return [None]*len(data_list)

    window_sum = 0.0
    queue = []
    for i, item in enumerate(data_list):
        c = float(item["close"])
        queue.append(c)
        window_sum += c
        if i < period - 1:
            result.append(None)
        else:
            avg = window_sum / period
            result.append(avg)
            old = queue.pop(0)
            window_sum -= old
    return result

def compute_bollinger(data_list, period, k):
    """计算布林带(upper, middle, lower)。"""
    length = len(data_list)
    if period <= 0:
        return ([None]*length, [None]*length, [None]*length)

    prices = [float(d["close"]) for d in data_list]
    sums = 0.0
    sums_sq = 0.0
    queue = []

    upper_list = []
    middle_list = []
    lower_list = []

    for i, p in enumerate(prices):
        queue.append(p)
        sums += p
        sums_sq += p*p

        if i < period - 1:
            upper_list.append(None)
            middle_list.append(None)
            lower_list.append(None)
        else:
            mean = sums / period
            var = (sums_sq / period) - (mean * mean)
            std = var ** 0.5
            up = mean + k * std
            low = mean - k * std

            upper_list.append(up)
            middle_list.append(mean)
            lower_list.append(low)

            # 移除队首
            old = queue.pop(0)
            sums -= old
            sums_sq -= old * old

    return upper_list, middle_list, lower_list

def recalc_indicators(symbol, interval):
    """
    重新计算 (symbol, interval) 的 MA(7,25,99) + Boll(由boll_params)。
    """
    key = (symbol, interval)
    data_list = KLINE_DATA_MAP.get(key, [])
    if not data_list:
        return

    # 先取该 symbol/interval 的布林参数, 如果没有就用默认
    bparams = boll_params.get(key, DEFAULT_BOLL_PARAMS)
    p_boll = bparams["period"]
    k_boll = bparams["k"]

    # MA
    ma7 = compute_ma(data_list, 7)
    ma25 = compute_ma(data_list, 25)
    ma99 = compute_ma(data_list, 99)
    # BOLL
    bb_up, bb_mid, bb_low = compute_bollinger(data_list, p_boll, k_boll)

    for i in range(len(data_list)):
        data_list[i]["ma7"] = ma7[i]
        data_list[i]["ma25"] = ma25[i]
        data_list[i]["ma99"] = ma99[i]
        data_list[i]["bb_upper"] = bb_up[i]
        data_list[i]["bb_middle"] = bb_mid[i]
        data_list[i]["bb_lower"] = bb_low[i]


# ============ 加载历史K线 ============
def load_initial_klines(symbol, interval, limit=1000):
    """
    从Binance获取历史K线, 存入KLINE_DATA_MAP, 并计算指标.
    """
    key = (symbol, interval)
    try:
        klines = rest_client.get_klines(symbol=symbol, interval=interval, limit=limit)
        data_list = []
        for k in klines:
            t_open = int(k[0])  # 毫秒时间戳
            t_sec = t_open // 1000
            data_list.append({
                "time": t_sec,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        KLINE_DATA_MAP[key] = data_list
        recalc_indicators(symbol, interval)
    except Exception as e:
        app.logger.error(f"加载历史K线异常: {e}")


# ============ WebSocket回调: handle_kline_message ============
def handle_kline_message(msg):
    if msg.get('e') != 'kline':
        return
    k = msg['k']
    if not k['x']:  # 只在k线收盘后才处理
        return

    symbol = k['s']     # e.g. 'BTCUSDT'
    interval = k['i']   # e.g. '1m'
    open_time = k['t']  # 毫秒
    close_price = float(k['c'])
    open_price = float(k['o'])
    high_price = float(k['h'])
    low_price = float(k['l'])
    volume = float(k['v'])

    t_sec = open_time // 1000
    key = (symbol, interval)

    # 如果本地没数据，先加载
    if key not in KLINE_DATA_MAP:
        load_initial_klines(symbol, interval, limit=1000)

    data_list = KLINE_DATA_MAP[key]
    if data_list and data_list[-1]["time"] == t_sec:
        # 覆盖最后一根
        data_list[-1].update({
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume
        })
    else:
        # 追加
        data_list.append({
            "time": t_sec,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume
        })

    # 重新计算
    recalc_indicators(symbol, interval)
    new_bar = data_list[-1]

    # 推送给前端
    socketio.emit('kline_update', {
        "symbol": symbol,
        "interval": interval,
        "kline": new_bar
    })


# ============ 动态订阅(取消订阅) ============
def subscribe_kline(symbol, interval):
    """
    如果已在订阅, 不重复订
    如果没订, 启动新订阅
    """
    key = (symbol, interval)
    if key in SUBSCRIPTION_MAP:
        app.logger.info(f"已在订阅 {key}, 无需重复")
        return

    app.logger.info(f"开始订阅: {key}")
    handle = twm.start_kline_socket(callback=handle_kline_message, symbol=symbol, interval=interval)
    SUBSCRIPTION_MAP[key] = handle

def unsubscribe_kline(symbol, interval):
    """
    取消对 (symbol, interval) 的订阅
    """
    key = (symbol, interval)
    if key in SUBSCRIPTION_MAP:
        handle = SUBSCRIPTION_MAP.pop(key)
        twm.stop_socket(handle)
        app.logger.info(f"已取消订阅: {key}")


# ============ Flask 路由 ============
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_klines')
def get_klines():
    """
    返回后端存储的 (symbol, interval) 全部K线 + 指标
    """
    symbol = request.args.get('symbol', DEFAULT_SYMBOL)
    interval = request.args.get('interval', DEFAULT_INTERVAL)
    key = (symbol, interval)

    if key not in KLINE_DATA_MAP:
        load_initial_klines(symbol, interval, limit=1000)

    data_list = KLINE_DATA_MAP.get(key, [])
    return jsonify({"symbol": symbol, "interval": interval, "data": data_list})

@app.route('/set_boll_params', methods=['POST'])
def set_boll_params():
    """
    POST Body: {
      "symbol": "BTCUSDT",
      "interval": "1m",
      "period": 30,
      "k": 2.5
    }
    针对指定 (symbol, interval) 更新Boll参数, 并重算指标
    """
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
    boll_params[key] = { "period": period, "k": float(k_val) }
    # 如果 (symbol, interval) 已有数据, 重算
    if key in KLINE_DATA_MAP:
        recalc_indicators(symbol, interval)

    return jsonify({"msg": "ok", "symbol": symbol, "interval": interval, "period": period, "k": k_val})

# ============ SocketIO 事件: 前端请求“切换Symbol/Interval” ============
@socketio.on('change_symbol_interval')
def handle_change_symbol_interval(data):
    """
    前端告诉后端: "我要切换成 (symbol, interval)"
    后端取消旧订阅(如果有), 并订阅新的 (symbol, interval).
    """
    old_symbol = data.get('old_symbol')
    old_interval = data.get('old_interval')
    new_symbol = data.get('new_symbol')
    new_interval = data.get('new_interval')

    if old_symbol and old_interval:
        unsubscribe_kline(old_symbol, old_interval)

    if new_symbol and new_interval:
        subscribe_kline(new_symbol, new_interval)

    # 也可以在这里立即 load_initial_klines(new_symbol, new_interval),
    # 但我们也可以等前端调用 /get_klines 来懒加载

    emit('change_symbol_interval_done', {
        "msg": f"已切换订阅到 {new_symbol}/{new_interval}"
    })


@socketio.on('connect')
def handle_connect():
    app.logger.info("客户端连接了 SocketIO")


if __name__ == '__main__':
    # 启动 WebSocket Manager
    twm.start()
    # 默认先订阅一个
    subscribe_kline(DEFAULT_SYMBOL, DEFAULT_INTERVAL)

    socketio.run(app, host=HOST, port=PORT, debug=True)
