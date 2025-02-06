# File: crypto/trading_engine/strategies/base_strategy.py

class BaseStrategy:
    def __init__(self, config=None):
        self.config = config or {}

    def on_init(self):
        pass

    def on_bar(self, bar):
        pass

    def on_tick(self, tick):
        pass
