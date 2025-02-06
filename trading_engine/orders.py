# File: crypto/trading_engine/orders.py

# 全局保存K线数据(含指标)
KLINE_DATA_MAP = {}  # (symbol, interval)-> [ {time,open,high,low,close,volume,bb_upper,...},...]

# Boll参数映射
DEFAULT_BOLL_PARAMS = {"period": 20, "k": 2.0}
boll_params_map = {}  # (symbol, interval)-> { period, k }
