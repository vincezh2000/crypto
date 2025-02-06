# File: crypto/trading_engine/strategies/boll_strategy.py

import math

def compute_bollinger(data_list, period, k):
    length = len(data_list)
    if period<=0:
        return ([None]*length, [None]*length, [None]*length)

    sums=0.0
    sums_sq=0.0
    queue=[]
    up_list=[]
    mid_list=[]
    low_list=[]

    for i, bar in enumerate(data_list):
        c= float(bar["close"])
        queue.append(c)
        sums+= c
        sums_sq+= c*c

        if i<period-1:
            up_list.append(None)
            mid_list.append(None)
            low_list.append(None)
        else:
            mean = sums/period
            var = (sums_sq/period)- mean*mean
            std = math.sqrt(var) if var>0 else 0
            up = mean + k*std
            low= mean - k*std

            up_list.append(up)
            mid_list.append(mean)
            low_list.append(low)

            oldest= queue.pop(0)
            sums-= oldest
            sums_sq-= oldest*oldest

    return up_list, mid_list, low_list


def recalc_boll_indicators(data_list, params):
    period= params["period"]
    k_val= params["k"]
    up, mid, low = compute_bollinger(data_list, period, k_val)
    for i in range(len(data_list)):
        data_list[i]["bb_upper"] = up[i]
        data_list[i]["bb_middle"]= mid[i]
        data_list[i]["bb_lower"] = low[i]
