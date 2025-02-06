# File: crypto/read_data.py

import logging

def load_initial_klines(rest_client, symbol, interval, KLINE_DATA_MAP, limit=500):
    key = (symbol, interval)
    try:
        klines = rest_client.get_klines(symbol=symbol, interval=interval, limit=limit)
        data_list = []
        for k in klines:
            t_open = int(k[0])
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
    except Exception as e:
        logging.error(f"加载历史K线异常: {e}")
