# -*- coding: utf-8 -*-
# V25 市场结构+资金费率+爆仓区 驱动引擎
import websocket
import json
import time
import hmac
import hashlib
import threading
import requests
import logging
from datetime import datetime
import uuid
import queue
import pandas as pd
import math

# ======================
# 日志系统（完全保留）
# ======================
log_filename = f"trade_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.handlers.clear()
file_handler = logging.FileHandler(log_filename, encoding='utf-8')
formatter = logging.Formatter('%(asctime)s | %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


def log(msg):
    try:
        print(msg)
        logger.info(str(msg))
    except Exception as e:
        print(f"[LOG ERROR] {e} | 原始: {msg}")


# ======================
# 币安配置与全局开关
# ======================
API_KEY = "Nzv1A7tK7kGpFXyJobupTljiWIVu5EulI6oDHL22g8Oxu0a7nckNbU6tIkrJ1jbX"
SECRET_KEY = "rwfqhUnk2tVocsufVoPr0TeJXlmOMJRdeG52OS6bYMNxSdmK83rcoo80axRMm8aN"
USE_LOCAL_SIMULATION = True
LEVERAGE = 20
BASE_URL_TESTNET = "https://testnet.binancefuture.com"
WS_URL_TESTNET = "wss://stream.binancefuture.com/stream"
BASE_URL_REAL = "https://fapi.binance.com"
WS_URL_REAL = "wss://fstream.binance.com/stream"
BASE_URL = BASE_URL_TESTNET if USE_LOCAL_SIMULATION else BASE_URL_REAL
WS_URL = WS_URL_TESTNET if USE_LOCAL_SIMULATION else WS_URL_REAL
SYMBOL = "BTCUSDT"
WS_SYMBOL = "btcusdt"
QTY = 0.01
# ======================
# 策略参数 (V25-市场结构+费率反转版)
# ======================
WINDOW = 10
ORDER_TIMEOUT = 200
RANGE_ORDER_TIMEOUT = 200
SLIPPAGE = 5
PRICE_STEP = 5
MAX_CONTINUOUS_LOSS = 3
LOSS_RESET_SEC = 180
FAST_CANCEL_SEC = 2
cooldown_until = 0
COOLDOWN_SEC = 2
# V25 核心参数
SCALP_SL = 0.015  # 1.5% 硬止损
MIN_TP_PNL = 0.0006
MIN_TP_MAX_PROFIT = 0.0006
ZONE_VALIDITY_SEC = 120
# ======================
# 全局变量 - API模式
# ======================
price_buffer = []
tick_buffer = []
position = None
entry_price = 0
loss_count = 0
loss_reset_time = 0
entry_time = 0
current_price = None
raw_price = None
mark_price = None
last_processed_price = None
last_fill_time = 0
last_check_time = 0
lock = threading.RLock()
data_lock = threading.Lock()
active_order = None
order_price = 0
order_time = 0
last_trade_side = "neutral"
reconnect_delay = 5
last_price_print_time = 0
last_print_price = None
trade_lock = False
last_entry_log_time = 0
last_holding_log_time = 0
k_list = []
d_list = []
j_list = []
trade_volume_buffer = []
kline_1m_closed = []
kline_3m_closed = []  # 🚀 新增：3分钟K线
kline_15m_closed = []
last_process_time = 0.0
PROCESS_INTERVAL = 0.2
trade_queue = queue.Queue()
max_profit_pct = 0.0
max_price_since_entry = 0.0
min_price_since_entry = 999999.0
time_at_max_profit = 0.0
official_unrealized_profit = 0.0
official_isolated_margin = 0.0
listenKey = None
# ======================
# 全局变量 - 本地撮合模式
# ======================
local_position = None
local_entry_price = 0.0
local_active_order = None
local_order_price = 0.0
local_order_time = 0.0
local_trade_lock = False
local_max_profit_pct = 0.0
local_max_price = 0.0
local_min_price = 999999.0
trade_records = []
partial_filled_flag = False
actual_position_amt = QTY
INITIAL_BALANCE = 10000.0
POSITION_RATIO = 0.3
LOT_SIZE = 0.001
FIXED_NOTIONAL = 300.0
account_balance = INITIAL_BALANCE
current_entry_strategy = ""
current_zone_score = 0
# 🚀 V25 新增：资金费率与时间
current_funding_rate = 0.0
next_funding_minutes = 60.0


# ======================
# V23 流动性结构系统 (保留辅助)
# ======================
def get_dynamic_zone_validity(atr):
    if atr < 50:
        return 180
    elif atr < 120:
        return 120
    else:
        return 60


class LiquidityZone:
    def __init__(self, high, low, atr=0):
        self.high = high
        self.low = low
        self.touch_count = 0
        self.swept_up = False
        self.swept_down = False
        self.creation_time = time.time()
        self.validity_window = get_dynamic_zone_validity(atr)


liquidity_zone = None
prev_price_v23 = None
ws_app = None
user_data_ws_app = None


# ======================
# 时间同步与签名
# ======================
def sync_binance_time():
    global binance_time_offset
    try:
        res = requests.get("https://fapi.binance.com/fapi/v1/time", timeout=3)
        server_time = res.json()["serverTime"]
        local_time = int(time.time() * 1000)
        binance_time_offset = server_time - local_time
        log("[时间同步] 完成")
    except:
        binance_time_offset = 0


def get_timestamp():
    return int(time.time() * 1000) + binance_time_offset


def build_signed_url(base_url, params):
    str_params = {k: str(v) for k, v in params.items() if v is not None}
    query = '&'.join([f"{k}={str_params[k]}" for k in sorted(str_params)])
    signature = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{base_url}?{query}&signature={signature}"


def format_price(p):
    return float(f"{p:.1f}")


def normalize_price(price):
    return round(price / PRICE_STEP) * PRICE_STEP


# ======================
# 动态仓位与余额
# ======================
def get_dynamic_qty(price, ratio=POSITION_RATIO, notional_multiplier=1.0):
    if not price or price <= 0: return LOT_SIZE
    if FIXED_NOTIONAL and FIXED_NOTIONAL > 0:
        target_notional = FIXED_NOTIONAL * notional_multiplier
    else:
        if account_balance <= 0: return LOT_SIZE
        target_notional = account_balance * ratio
    raw_qty = target_notional / price
    steps = math.floor(raw_qty / LOT_SIZE)
    final_qty = steps * LOT_SIZE
    if final_qty < LOT_SIZE: final_qty = LOT_SIZE
    return final_qty


def sync_balance():
    global account_balance
    if USE_LOCAL_SIMULATION:
        account_balance = INITIAL_BALANCE
        return
    try:
        url = f"{BASE_URL}/fapi/v2/balance"
        p = {"timestamp": get_timestamp(), "recvWindow": 5000}
        url = build_signed_url(url, p)
        h = {"X-MBX-APIKEY": API_KEY}
        d = requests.get(url, headers=h, timeout=3).json()
        if isinstance(d, list):
            for item in d:
                if item.get("asset") == "USDT":
                    account_balance = float(item.get("balance", 0))
                    return
    except Exception as e:
        log(f"[余额同步异常] {e}")


# ======================
# 盘口/交易辅助
# ======================
def orderbook_bias():
    try:
        bids = orderbook["bids"][:3]
        asks = orderbook["asks"][:3]
        bids_vol = sum(float(b[1]) for b in bids)
        asks_vol = sum(float(a[1]) for a in asks)
        if bids_vol > asks_vol * 1.5:
            return "bull"
        elif asks_vol > bids_vol * 1.5:
            return "bear"
        else:
            return "neutral"
    except:
        return "neutral"


def compute_kdj(prices, period=9, k_period=3, d_period=3):
    global k_list, d_list, j_list
    if len(prices) < period: return 50, 50, 50
    high = max(prices[-period:])
    low = min(prices[-period:])
    close = prices[-1]
    if high == low:
        rsv = 50
    else:
        rsv = (close - low) / (high - low) * 100
    if not k_list:
        k, d = rsv, rsv
    else:
        k = (2 * k_list[-1] + rsv) / 3
        d = (2 * d_list[-1] + k) / 3
    j = 3 * k - 2 * d
    k_list.append(k);
    d_list.append(d);
    j_list.append(j)
    if len(k_list) > 20: k_list.pop(0); d_list.pop(0); j_list.pop(0)
    return k, d, j


def compute_atr(klines, period=7):
    if len(klines) < period + 1: return 0.0
    trs = []
    for i in range(1, len(klines)):
        high = klines[i]['high']
        low = klines[i]['low']
        prev_close = klines[i - 1]['close']
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period: return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period


def fetch_initial_klines():
    global kline_1m_closed, kline_3m_closed, kline_15m_closed
    log("[历史K线] 正在拉取初始1m, 3m, 15m数据...")
    try:
        url_1m = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval=1m&limit=15"
        res_1m = requests.get(url_1m, timeout=5).json()
        if isinstance(res_1m, list):
            for k in res_1m[:-1]:
                kline_1m_closed.append({
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5])
                })
            if len(kline_1m_closed) >= 9:
                with data_lock:
                    closes = [k['close'] for k in kline_1m_closed]
                    compute_kdj(closes)
        # 🚀 V25: 拉取3分钟K线
        url_3m = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval=3m&limit=25"
        res_3m = requests.get(url_3m, timeout=5).json()
        if isinstance(res_3m, list):
            for k in res_3m[:-1]:
                kline_3m_closed.append({
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5])
                })
        url_15m = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval=15m&limit=3"
        res_15m = requests.get(url_15m, timeout=5).json()
        if isinstance(res_15m, list):
            for k in res_15m[:-1]:
                kline_15m_closed.append({
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5])
                })
    except Exception as e:
        log(f"[历史K线] 拉取失败: {e}")


# ======================
# 🚀 V25核心：资金费率与市场结构
# ======================
def fetch_funding_rate_loop():
    global current_funding_rate, next_funding_minutes
    while True:
        try:
            url = f"{BASE_URL}/fapi/v1/premiumIndex?symbol={SYMBOL}"
            res = requests.get(url, timeout=3).json()
            if isinstance(res, list): res = res[0]
            current_funding_rate = float(res.get("lastFundingRate", 0))
            next_funding_time_ms = int(res.get("nextFundingTime", 0))
            local_now_ms = int(time.time() * 1000) + binance_time_offset
            diff_ms = next_funding_time_ms - local_now_ms
            next_funding_minutes = max(0, diff_ms / 60000.0)
        except Exception as e:
            log(f"[资金费率] 拉取失败: {e}")
        time.sleep(60)


def get_funding_context():
    return current_funding_rate, next_funding_minutes


def funding_filter():
    rate, minutes = get_funding_context()
    tag = "neutral"
    allow_long = True
    allow_short = True
    if rate > 0.004:  # 0.4%
        if minutes < 5:
            tag = "reversal_long_setup"
            allow_long = True
            allow_short = False
        elif minutes < 30:
            tag = "crowded_long"
            allow_long = False
    elif rate < -0.004:
        if minutes < 5:
            tag = "reversal_short_setup"
            allow_short = True
            allow_long = False
        elif minutes < 30:
            tag = "crowded_short"
            allow_short = False
    return allow_long, allow_short, tag


def detect_market_structure_3m():
    with data_lock:
        klines = list(kline_3m_closed[-20:])
    if len(klines) < 5:
        return {"range_high": 0, "range_low": 0, "width": 0, "sweep_up": False, "sweep_down": False}
    highs = [k['high'] for k in klines[:-2]]
    lows = [k['low'] for k in klines[:-2]]
    range_high = max(highs) if highs else 0
    range_low = min(lows) if lows else 0
    width = range_high - range_low
    last = klines[-1]
    sweep_up = last['high'] > range_high and last['close'] < range_high
    sweep_down = last['low'] < range_low and last['close'] > range_low
    return {
        "range_high": range_high,
        "range_low": range_low,
        "width": width,
        "sweep_up": sweep_up,
        "sweep_down": sweep_down
    }


def liquidation_zone():
    with data_lock:
        klines = list(kline_3m_closed[-30:])
    if not klines:
        return 0, 999999
    highs = [k['high'] for k in klines]
    lows = [k['low'] for k in klines]
    return min(lows), max(highs)


def v25_engine():
    structure = detect_market_structure_3m()
    fund_long, fund_short, fund_tag = funding_filter()
    liq_low, liq_high = liquidation_zone()
    allow_long = False
    allow_short = False
    # 结构过窄过滤
    if structure["width"] < 80:
        return False, False, liq_low, liq_high, fund_tag, structure
    # 1. 费率反转逻辑
    if fund_tag == "reversal_long_setup" and structure["sweep_down"]:
        allow_long = True
    if fund_tag == "reversal_short_setup" and structure["sweep_up"]:
        allow_short = True
    # 2. 顺扫单逻辑
    if structure["sweep_up"] and fund_short:
        allow_short = True
    if structure["sweep_down"] and fund_long:
        allow_long = True
    return allow_long, allow_short, liq_low, liq_high, fund_tag, structure


# ======================
# 持仓/下单/撤单/平仓
# ======================
def sync_position():
    global position, entry_price, active_order, actual_position_amt, official_unrealized_profit, official_isolated_margin
    if USE_LOCAL_SIMULATION:
        if local_position:
            pass
        return
    try:
        url = f"{BASE_URL}/fapi/v2/positionRisk"
        p = {"timestamp": get_timestamp(), "recvWindow": 5000}
        url = build_signed_url(url, p)
        h = {"X-MBX-APIKEY": API_KEY}
        d = requests.get(url, headers=h, timeout=3).json()
        if isinstance(d, dict) and d.get("code") is not None:
            if d.get("code") == -1021: sync_binance_time()
            return
        with lock:
            for item in d:
                if item['symbol'] == SYMBOL:
                    amt = float(item['positionAmt'])
                    ep = float(item['entryPrice'])
                    if amt != 0:
                        position = "long" if amt > 0 else "short"
                        entry_price = ep
                        actual_position_amt = abs(amt)
                        active_order = None
                        official_unrealized_profit = float(item['unRealizedProfit'])
                        official_isolated_margin = float(item['isolatedMargin'])
                    else:
                        position = None
                        entry_price = 0
                        actual_position_amt = QTY
                        official_unrealized_profit = 0.0
                        official_isolated_margin = 0.0
    except Exception as e:
        log(f"[持仓同步失败] {e}")


def position_monitor():
    while True:
        try:
            sync_balance()
            if USE_LOCAL_SIMULATION:
                pos = local_position;
                ep = local_entry_price
            else:
                with lock:
                    pos = position;
                    ep = entry_price
            if pos and ep > 0:
                if USE_LOCAL_SIMULATION:
                    with data_lock:
                        price = raw_price if (raw_price and time.time() - last_aggTrade_time <= 3) else mark_price
                        if not price: price = ep
                    amt = actual_position_amt
                    pnl = (price - ep) * amt if pos == "long" else (ep - price) * amt
                    margin = abs(amt) * ep / LEVERAGE if LEVERAGE > 0 else 0
                    roi = pnl / margin if margin > 0 else 0
                    log(f"[本地账户监控] {pos} | 数量:{amt} | 均价:{ep:.1f} | 现价:{price:.1f} | 浮盈:{pnl:.2f}U | ROI:{roi:.2%}")
                else:
                    with lock:
                        off_pnl = official_unrealized_profit
                        off_margin = official_isolated_margin
                    roi = off_pnl / off_margin if off_margin > 0 else 0
                    log(f"[实盘账户监控-官方] {pos} | 数量:{actual_position_amt} | 均价:{ep:.1f} | 官方浮盈:{off_pnl:.2f}U | 官方ROI:{roi:.2%}")
        except Exception as e:
            log(f"[持仓监控异常] {e}")
        time.sleep(8)


def send_limit_order(side, price, reduce=False, custom_qty=None):
    global local_active_order, local_order_time, local_order_price, local_trade_lock
    global active_order, order_time, order_price, trade_lock, partial_filled_flag
    if USE_LOCAL_SIMULATION:
        if not reduce and (local_position is not None or local_active_order is not None or local_trade_lock): return
        local_trade_lock = True
        local_active_order = f"{side}_{format_price(price)}"
        local_order_time = time.time();
        local_order_price = price
        log(f"✅ 本地挂单成功 {side} | 价格:{price} | 数量:{custom_qty if custom_qty else '默认'} | 平仓单:{reduce}")
        return
    if not reduce and (position is not None or active_order is not None or trade_lock): return
    trade_lock = True;
    partial_filled_flag = False
    order_qty = custom_qty if custom_qty else (get_dynamic_qty(price) if not reduce else actual_position_amt)
    actual_notional = order_qty * price
    if order_qty <= 0: trade_lock = False; return
    url = f"{BASE_URL}/fapi/v1/order"
    p = {
        "symbol": SYMBOL, "side": side, "type": "LIMIT", "timeInForce": "GTC",
        "quantity": str(order_qty), "price": str(format_price(price)),
        "timestamp": get_timestamp(), "recvWindow": 5000
    }
    if reduce: p["reduceOnly"] = "true"
    url = build_signed_url(url, p)
    headers = {"X-MBX-APIKEY": API_KEY}
    try:
        r = requests.post(url, headers=headers, timeout=3)
        res = r.json()
        if "orderId" in res:
            active_order = res["orderId"];
            order_time = time.time();
            order_price = price
            log(f"✅ API挂单成功 {res} | 数量:{order_qty} | 名义金额:{actual_notional:.2f}U | 平仓单:{reduce}")
        else:
            log(f"❌ API挂单失败返回: {res}");
            trade_lock = False
            if isinstance(res, dict) and res.get("code") == -1021: sync_binance_time()
    except Exception as e:
        log(f"❌ API挂单异常: {e}");
        trade_lock = False


def send_market_order(side, reduce=False, qty=None):
    global local_position, local_entry_price, local_trade_lock
    global local_max_profit_pct, local_max_price, local_min_price
    global entry_time, time_at_max_profit
    global active_order, actual_position_amt
    close_qty = qty if qty is not None else actual_position_amt
    if USE_LOCAL_SIMULATION:
        if reduce:
            local_position = None;
            local_entry_price = 0.0;
            local_max_profit_pct = 0.0
            local_max_price = 0.0;
            local_min_price = 999999.0;
            local_trade_lock = False
            entry_time = 0;
            time_at_max_profit = 0.0
        else:
            local_position = "long" if side == "BUY" else "short"
            local_entry_price = raw_price;
            local_max_profit_pct = 0.0
            local_max_price = raw_price;
            local_min_price = raw_price;
            local_trade_lock = False
            entry_time = time.time();
            time_at_max_profit = entry_time
            actual_position_amt = get_dynamic_qty(raw_price)
        return True
    for i in range(3):
        try:
            p = {
                "symbol": SYMBOL, "side": side, "type": "MARKET",
                "quantity": str(close_qty), "timestamp": get_timestamp(), "recvWindow": 5000
            }
            if reduce: p["reduceOnly"] = "true"
            url = build_signed_url(f"{BASE_URL}/fapi/v1/order", p)
            h = {"X-MBX-APIKEY": API_KEY}
            r = requests.post(url, headers=h, timeout=5)
            res = r.json()
            if res.get("status") == "NEW" or res.get("orderId"):
                active_order = None
                return True
            else:
                if isinstance(res, dict) and res.get("code") == -1021: sync_binance_time()
        except Exception as e:
            time.sleep(0.5)
    sync_position()
    return False


def cancel_order():
    global local_active_order, local_order_price, local_order_time, local_trade_lock
    global active_order, order_price, order_time, trade_lock, partial_filled_flag
    if USE_LOCAL_SIMULATION:
        if not local_active_order: return
        local_active_order = None;
        local_order_price = 0;
        local_order_time = 0;
        local_trade_lock = False
        return
    if not active_order: return
    try:
        p = {"symbol": SYMBOL, "orderId": active_order, "timestamp": get_timestamp(), "recvWindow": 5000}
        url = build_signed_url(f"{BASE_URL}/fapi/v1/order", p)
        h = {"X-MBX-APIKEY": API_KEY}
        res = requests.delete(url, headers=h, timeout=3).json()
        if res.get("status") in ["CANCELED", "EXPIRED"] or res.get("code") == -2011:
            active_order = None;
            order_price = 0;
            order_time = 0;
            trade_lock = False;
            partial_filled_flag = False
            sync_position()
    except Exception as e:
        pass


def check_order_timeout():
    if USE_LOCAL_SIMULATION:
        if local_active_order:
            timeout = RANGE_ORDER_TIMEOUT if "震荡" in current_entry_strategy else ORDER_TIMEOUT
            if time.time() - local_order_time > timeout: cancel_order()
    else:
        if active_order:
            base_timeout = RANGE_ORDER_TIMEOUT if "震荡" in current_entry_strategy else ORDER_TIMEOUT
            timeout = 5 if partial_filled_flag else base_timeout
            if time.time() - order_time > timeout: cancel_order()


def is_price_valid():
    if not raw_price or not mark_price: return False
    diff = abs(raw_price - mark_price) / raw_price
    return diff < 0.0005


def check_order_filled():
    global local_active_order, local_position, local_entry_price, local_trade_lock
    global local_max_profit_pct, local_max_price, local_min_price
    global entry_time, time_at_max_profit
    global active_order, last_fill_time, trade_lock, max_profit_pct, max_price_since_entry, min_price_since_entry
    global partial_filled_flag, actual_position_amt
    if USE_LOCAL_SIMULATION:
        if not local_active_order: return
        side, price_str = local_active_order.split("_")
        order_p = float(price_str)
        filled = False
        if is_price_valid():
            with data_lock:
                check_price = raw_price
            if side == "BUY" and check_price and check_price <= order_p:
                filled = True
            elif side == "SELL" and check_price and check_price >= order_p:
                filled = True
        is_reduce = (side == "SELL" and local_position == "long") or (side == "BUY" and local_position == "short")
        if filled:
            if is_reduce:
                local_position = None;
                local_entry_price = 0.0;
                local_max_profit_pct = 0.0
                local_max_price = 0.0;
                local_min_price = 999999.0;
                local_trade_lock = False
                local_active_order = None;
                entry_time = 0;
                time_at_max_profit = 0.0
            else:
                local_position = "long" if side == "BUY" else "short"
                local_entry_price = order_p;
                actual_position_amt = get_dynamic_qty(order_p)
                local_active_order = None;
                local_trade_lock = False
                local_max_profit_pct = 0.0;
                local_max_price = order_p;
                local_min_price = order_p
                entry_time = time.time();
                time_at_max_profit = entry_time
        return
    if not active_order: return
    try:
        p = {"symbol": SYMBOL, "orderId": active_order, "timestamp": get_timestamp(), "recvWindow": 5000}
        url = build_signed_url(f"{BASE_URL}/fapi/v1/order", p)
        h = {"X-MBX-APIKEY": API_KEY}
        d = requests.get(url, headers=h, timeout=3).json()
        is_reduce = d.get("reduceOnly", False)
        if d.get("status") == "FILLED":
            active_order = None;
            trade_lock = False;
            partial_filled_flag = False;
            last_fill_time = time.time()
            if is_reduce:
                entry_time = 0;
                time_at_max_profit = 0.0
                sync_position()
            else:
                entry_time = time.time();
                max_profit_pct = 0.0
                max_price_since_entry = entry_price;
                min_price_since_entry = entry_price;
                time_at_max_profit = entry_time
                sync_position()
        elif d.get("status") == "PARTIALLY_FILLED":
            if not partial_filled_flag:
                partial_filled_flag = True;
                order_time = time.time()
            sync_position()
            entry_time = time.time();
            max_profit_pct = 0.0
            max_price_since_entry = entry_price;
            min_price_since_entry = entry_price;
            time_at_max_profit = entry_time
        elif isinstance(d, dict) and d.get("code") == -1021:
            sync_binance_time()
    except:
        pass


# ======================
# V25 执行层逻辑
# ======================
def update_liquidity_zone():
    global liquidity_zone
    with data_lock:
        if len(kline_1m_closed) < 10: return
        highs = [k['high'] for k in kline_1m_closed[-10:]]
        lows = [k['low'] for k in kline_1m_closed[-10:]]
        new_high = max(highs);
        new_low = min(lows)
        atr = compute_atr(kline_1m_closed, 7)
        if liquidity_zone:
            is_expired = (time.time() - liquidity_zone.creation_time) > liquidity_zone.validity_window
            drifted = abs(new_high - liquidity_zone.high) > PRICE_STEP * 5 or abs(
                new_low - liquidity_zone.low) > PRICE_STEP * 5
            if is_expired or drifted:
                liquidity_zone = LiquidityZone(new_high, new_low, atr)
            else:
                liquidity_zone.high = new_high;
                liquidity_zone.low = new_low
        else:
            liquidity_zone = LiquidityZone(new_high, new_low, atr)


def detect_momentum_exhaustion():
    with data_lock:
        if len(tick_buffer) < 5: return False
        diffs = [tick_buffer[i] - tick_buffer[i - 1] for i in range(1, len(tick_buffer))]
        if not diffs or diffs[0] == 0: return False
        return abs(diffs[-1]) < abs(diffs[0]) * 0.5


def get_smart_entry_prices():
    with data_lock:
        if len(kline_1m_closed) < 5: return None, None
        highs = [k['high'] for k in kline_1m_closed[-5:]]
        lows = [k['low'] for k in kline_1m_closed[-5:]]
    avg_high = sum(highs) / 5
    avg_low = sum(lows) / 5
    range_high = max(highs)
    range_low = min(lows)
    mid = (range_high + range_low) / 2
    width = range_high - range_low
    offset = max(5, width * 0.1)
    buy_price = avg_low + offset * 0.5
    buy_price = min(buy_price, mid - offset * 0.2)
    sell_price = avg_high - offset * 0.5
    sell_price = max(sell_price, mid + offset * 0.2)
    return normalize_price(buy_price), normalize_price(sell_price)


def log_entry_conditions():
    global last_entry_log_time
    now = time.time()
    if now - last_entry_log_time < 2: return
    allow_long, allow_short, liq_low, liq_high, fund_tag, structure = v25_engine()
    rate, mins = get_funding_context()
    log(f"[V25开仓雷达] 结构宽:{structure['width']:.1f} | 上扫:{structure['sweep_up']} | 下扫:{structure['sweep_down']} | "
        f"费率:{rate * 100:.3f}%({fund_tag}) | 剩余:{mins:.1f}m | 允许多:{allow_long} | 允许空:{allow_short} | "
        f"爆仓区:[{liq_low:.1f}-{liq_high:.1f}]")
    last_entry_log_time = now


def v25_entry():
    global current_entry_strategy
    if USE_LOCAL_SIMULATION:
        if local_active_order: return
    else:
        if active_order: return
    allow_long, allow_short, liq_low, liq_high, fund_tag, structure = v25_engine()
    if not allow_long and not allow_short:
        return
    # 结算前2分钟保护
    if fund_tag in ["reversal_long_setup", "reversal_short_setup"]:
        rate, minutes = get_funding_context()
        if minutes < 2:
            return
    price = raw_price
    if not price or price <= 0: return
    buy_price, sell_price = get_smart_entry_prices()
    if buy_price is None or sell_price is None: return
    # 盈亏比校验 (复用V23逻辑)
    stop_distance = price * SCALP_SL
    with data_lock:
        if len(kline_1m_closed) < 5: return
        highs = [k['high'] for k in kline_1m_closed[-5:]]
        lows = [k['low'] for k in kline_1m_closed[-5:]]
    zone_width = max(highs) - min(lows)
    rr_ratio = zone_width / stop_distance if stop_distance > 0 else 0
    if zone_width < PRICE_STEP * 6:
        cond_rr = rr_ratio >= 1.1
    elif zone_width < PRICE_STEP * 20:
        cond_rr = rr_ratio >= 1.3
    else:
        cond_rr = rr_ratio >= 1.5
    if not cond_rr: return
    # 5K小实体过滤
    with data_lock:
        if len(kline_1m_closed) < 5: return
        atr = compute_atr(kline_1m_closed, 7)
        bodies = [abs(k['close'] - k['open']) for k in kline_1m_closed[-5:]]
        small_count = sum(1 for b in bodies if b < atr *1.1)
    cond1_5k = small_count >= 3
    # 挂单防抢跑约束
    with data_lock:
        if len(kline_1m_closed) < 3: return
        last_3_closes = [k['close'] for k in kline_1m_closed[-3:]]
        min_close = min(last_3_closes)
        max_close = max(last_3_closes)
    long_allowed = (buy_price <= min_close)
    short_allowed = (sell_price >= max_close)
    # 计算仓位
    notional_multiplier = 1.0
    if fund_tag in ["reversal_long_setup", "reversal_short_setup"]:
        notional_multiplier = 1.5  # 费率反转加重仓
    custom_qty = get_dynamic_qty(price, ratio=POSITION_RATIO, notional_multiplier=notional_multiplier)
    liq_range = liq_high - liq_low if liq_high > liq_low else 1
    # 多单执行
    if allow_long and cond1_5k and long_allowed:
        if price <= liq_low + liq_range * 0.3:
            if buy_price < price:
                current_entry_strategy = f"V25结构多({fund_tag})"
                send_limit_order("BUY", buy_price, custom_qty=custom_qty)
                return
    # 空单执行
    if allow_short and cond1_5k and short_allowed:
        if price >= liq_high - liq_range * 0.3:
            if sell_price > price:
                current_entry_strategy = f"V25结构空({fund_tag})"
                send_limit_order("SELL", sell_price, custom_qty=custom_qty)
                return


def detect_aggressive_reversal():
    if len(tick_buffer) < 10: return False
    diffs = [tick_buffer[i] - tick_buffer[i - 1] for i in range(1, len(tick_buffer))]
    cur_pos = local_position if USE_LOCAL_SIMULATION else position
    if not cur_pos: return False
    if cur_pos == "long":
        reverse_ticks = sum(1 for d in diffs[-10:] if d < 0)
    else:
        reverse_ticks = sum(1 for d in diffs[-10:] if d > 0)
    reverse_vol = 0.0
    avg_vol = 0.0
    with data_lock:
        if len(kline_1m_closed) >= 3:
            avg_vol = sum(k['volume'] for k in kline_1m_closed[-3:]) / 3
    if cur_pos == "long":
        for a in orderbook.get("asks", [])[:3]:
            try:
                reverse_vol += float(a[1])
            except:
                pass
    else:
        for b in orderbook.get("bids", [])[:3]:
            try:
                reverse_vol += float(b[1])
            except:
                pass
    return reverse_ticks >= 8 and reverse_vol > avg_vol * 4


def manage_position():
    if USE_LOCAL_SIMULATION:
        pos = local_position;
        ep = local_entry_price
        if not pos: return False
        if ep > 0:
            with data_lock:
                price = raw_price if (raw_price and time.time() - last_aggTrade_time <= 3) else mark_price
                if not price: price = ep
            pnl = (price - ep) / ep if pos == "long" else (ep - price) / ep
            if pnl <= -SCALP_SL: close_position("V25硬止损(本地)"); return True
    else:
        with lock:
            pos = position;
            off_pnl = official_unrealized_profit;
            off_margin = official_isolated_margin
        if not pos: return False
        if off_margin > 0:
            roi = off_pnl / off_margin
            if roi <= -SCALP_SL:
                close_position("V25硬止损(官方盈亏触发)")
                return True
    if detect_aggressive_reversal():
        close_position("订单流提前止损")
        return True
    return False


# ======================
# 交易记录与平仓
# ======================
def save_trade_records():
    if not trade_records: return
    try:
        df = pd.DataFrame(trade_records)
        try:
            filename = "trade_records.xlsx"
            df.to_excel(filename, index=False)
        except ImportError:
            filename = "trade_records.csv"
            df.to_csv(filename, index=False, encoding='utf-8-sig')
    except Exception as e:
        log(f"[记录保存失败] {e}")


def close_position(reason):
    global loss_count, cooldown_until, loss_reset_time, entry_time, time_at_max_profit, current_entry_strategy
    global official_unrealized_profit, official_isolated_margin
    if USE_LOCAL_SIMULATION:
        pos = local_position;
        ep = local_entry_price
        if not pos: return
        close_qty = actual_position_amt
        with data_lock:
            exit_price = raw_price if (raw_price and time.time() - last_aggTrade_time <= 3) else mark_price
            if not exit_price: exit_price = raw_price
        pnl_abs = (exit_price - ep) * close_qty if pos == "long" else (ep - exit_price) * close_qty
        margin = abs(close_qty) * ep / LEVERAGE if LEVERAGE > 0 else 0
        roi = pnl_abs / margin if margin > 0 else 0
        record = {
            "开仓时间": datetime.fromtimestamp(entry_time).strftime('%Y-%m-%d %H:%M:%S'),
            "平仓时间": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "开单策略": current_entry_strategy, "方向": "多" if pos == "long" else "空",
            "数量": close_qty, "开仓价格": ep, "平仓价格": exit_price,
            "盈亏(U)": round(pnl_abs, 2), "收益率": f"{roi:.2%}", "平仓原因": reason
        }
        trade_records.append(record)
        save_trade_records()
    else:
        with lock:
            pos = position;
            ep = entry_price
            off_pnl_before = official_unrealized_profit
        if not pos: return
        close_qty = actual_position_amt
    if "硬止损" in reason or "提前止损" in reason:
        loss_count += 1
        if loss_count >= MAX_CONTINUOUS_LOSS:
            cooldown_until = time.time() + 60
            loss_count = 0
            loss_reset_time = time.time()
    else:
        loss_count = 0
    cancel_order()
    if USE_LOCAL_SIMULATION:
        success = send_market_order("SELL" if pos == "long" else "BUY", True)
        if success: cooldown_until = time.time() + COOLDOWN_SEC; entry_time = 0; time_at_max_profit = 0.0
    else:
        retry_count = 0;
        MAX_RETRY = 10
        while retry_count < MAX_RETRY:
            retry_count += 1
            success = send_market_order("SELL" if pos == "long" else "BUY", True)
            if success:
                cooldown_until = time.time() + COOLDOWN_SEC;
                trade_lock = False;
                entry_time = 0;
                time_at_max_profit = 0.0
                return
            else:
                sync_position()
                with lock:
                    if not position: trade_lock = False; entry_time = 0; time_at_max_profit = 0.0; return
            time.sleep(3)
        trade_lock = False


# ======================
# 核心处理引擎 (微利极速兑现 + V25风控)
# ======================
def process_price():
    global last_process_time
    global current_price, raw_price, price_buffer, last_price_print_time
    global loss_count, last_check_time, last_holding_log_time
    global local_max_profit_pct, local_max_price, local_min_price
    global max_profit_pct, max_price_since_entry, min_price_since_entry
    global time_at_max_profit, official_unrealized_profit, official_isolated_margin
    now = time.time()
    if now - last_process_time < PROCESS_INTERVAL: return
    last_process_time = now
    if manage_position(): return
    price_anomaly = False
    try:
        with data_lock:
            agg_delay = now - last_aggTrade_time
            if raw_price and agg_delay <= 3:
                snap_price = raw_price
            elif mark_price:
                snap_price = mark_price
            else:
                snap_price = raw_price
        if snap_price is None: return
        if raw_price and mark_price:
            diff = abs(raw_price - mark_price) / raw_price
            if diff > 0.001:
                ws_delay = now - last_ws_time
                if ws_delay <= 1: price_anomaly = True
        if now - last_price_print_time >= 5:
            ws_delay = now - last_ws_time
            agg_delay = now - last_aggTrade_time
            log(f"[价格] Last:{raw_price} | Mark:{mark_price if mark_price else 'N/A'} | 延迟:{ws_delay:.1f}s | aggDelay:{agg_delay:.1f}s")
            last_price_print_time = now
        if loss_count >= MAX_CONTINUOUS_LOSS:
            if now - loss_reset_time < LOSS_RESET_SEC: return
            loss_count = 0
        update_liquidity_zone()
        if USE_LOCAL_SIMULATION:
            cur_pos = local_position;
            cur_ep = local_entry_price
            cur_max_price = local_max_price;
            cur_min_price = local_min_price
        else:
            with lock:
                cur_pos = position;
                cur_ep = entry_price
                cur_max_price = max_price_since_entry;
                cur_min_price = min_price_since_entry
        if not cur_pos or cur_ep <= 0:
            pass
        else:
            if USE_LOCAL_SIMULATION:
                pnl_abs = (snap_price - cur_ep) * actual_position_amt if cur_pos == "long" else (
                                                                                                            cur_ep - snap_price) * actual_position_amt
                margin = abs(actual_position_amt) * cur_ep / LEVERAGE if LEVERAGE > 0 else 0
                pr = pnl_abs / margin if margin > 0 else 0
                if pr <= -SCALP_SL:
                    close_position(f"硬止损({SCALP_SL * 100}%)")
                    return
                if cur_pos == "long":
                    if snap_price > cur_max_price: cur_max_price = snap_price; time_at_max_profit = time.time()
                else:
                    if snap_price < cur_min_price: cur_min_price = snap_price; time_at_max_profit = time.time()
                local_max_price = cur_max_price;
                local_min_price = cur_min_price
                max_pnl_abs = (cur_max_price - cur_ep) * actual_position_amt if cur_pos == "long" else (
                                                                                                                   cur_ep - cur_min_price) * actual_position_amt
                max_profit_pct = max_pnl_abs / margin if margin > 0 else 0
            else:
                with lock:
                    off_pnl = official_unrealized_profit
                    off_margin = official_isolated_margin
                if cur_pos == "long":
                    if snap_price > cur_max_price: cur_max_price = snap_price; time_at_max_profit = time.time()
                else:
                    if snap_price < cur_min_price: cur_min_price = snap_price; time_at_max_profit = time.time()
                with lock:
                    max_price_since_entry = cur_max_price
                    min_price_since_entry = cur_min_price
                pr = off_pnl / off_margin if off_margin > 0 else 0
                max_pnl_abs = (cur_max_price - cur_ep) * actual_position_amt if cur_pos == "long" else (
                                                                                                                   cur_ep - cur_min_price) * actual_position_amt
                margin = abs(actual_position_amt) * cur_ep / LEVERAGE if LEVERAGE > 0 else 0
                max_profit_pct = max_pnl_abs / margin if margin > 0 else 0
            status_parts = []
            MIN_ROI_COVER_FEE = 0.008
            if max_profit_pct > 0:
                retracement = pr / max_profit_pct if max_profit_pct > 0 else 0
                time_since_high = time.time() - time_at_max_profit
                if (USE_LOCAL_SIMULATION and pr < MIN_ROI_COVER_FEE) or (not USE_LOCAL_SIMULATION and off_pnl < 0):
                    status_parts.append(f"追踪止盈(手续费不足🟡 当前ROI:{pr:.2%}<{MIN_ROI_COVER_FEE * 100:.1f}%)")
                else:
                    if max_profit_pct < MIN_ROI_COVER_FEE * 1.2:
                        status_parts.append(
                            f"微利追踪(等待🟡 最高ROI:{max_profit_pct:.2%}<{MIN_ROI_COVER_FEE * 1.2 * 100:.1f}%)")
                    else:
                        trail_tp = (time_since_high >= 7) and (retracement <= 0.85)
                        status_parts.append(
                            f"极速追踪({'触发🔴' if trail_tp else '等待🟡'} 历高停顿{time_since_high:.0f}s/20s 回撤比{retracement:.2%}/65%)")
                        if trail_tp:
                            close_position("极速追踪止盈")
                            return
                        avg_vol_10k = 0.0
                        with data_lock:
                            if len(kline_1m_closed) >= 10: avg_vol_10k = sum(
                                k['volume'] for k in kline_1m_closed[-10:]) / 10
                        reverse_order_vol = 0.0
                        if cur_pos == "long":
                            for ask in orderbook.get("asks", [])[:3]:
                                try:
                                    reverse_order_vol += float(ask[1])
                                except:
                                    pass
                        elif cur_pos == "short":
                            for bid in orderbook.get("bids", [])[:3]:
                                try:
                                    reverse_order_vol += float(bid[1])
                                except:
                                    pass
                        is_retracement_over_15pct = retracement <= 0.85
                        is_huge_reverse_vol = reverse_order_vol > avg_vol_10k * 2.5 if avg_vol_10k > 0 else False
                        abs_tp = is_retracement_over_15pct and is_huge_reverse_vol
                        status_parts.append(
                            f"回撤遇阻({'触发🔴' if abs_tp else '等待🟡'} 回撤>15%:{'✅' if is_retracement_over_15pct else '❌'} 反向量:{reverse_order_vol:.1f}>2.5倍均量:{avg_vol_10k:.1f}:{'✅' if is_huge_reverse_vol else '❌'})")
                        if abs_tp:
                            close_position("绝对回撤保护(放量遇阻)")
                            return
            else:
                status_parts.append(f"追踪止盈(未启动🟢)")
            if now - last_holding_log_time >= 2:
                if USE_LOCAL_SIMULATION:
                    log(f"[本地持仓状态-ROI修正] {cur_pos} | 当前ROI:{pr:.2%} | 最高ROI:{max_profit_pct:.2%} | 状态面板: {' | '.join(status_parts)}")
                else:
                    with lock:
                        log(f"[实盘持仓状态-官方] {cur_pos} | 官方盈亏:{off_pnl:.2f}U (ROI:{pr:.2%}) | 极值最高ROI:{max_profit_pct:.2%} | 状态面板: {' | '.join(status_parts)}")
                last_holding_log_time = now
        if time.time() - last_check_time > 1:
            check_order_filled();
            last_check_time = time.time()
        check_order_timeout()
        has_position = local_position if USE_LOCAL_SIMULATION else position
        if not has_position:
            log_entry_conditions()
            has_order = local_active_order if USE_LOCAL_SIMULATION else active_order
            if not has_order and not price_anomaly and time.time() > cooldown_until:
                v25_entry()
    except Exception as e:
        log(f"[处理引擎异常] {e}")


def trade_worker():
    while True:
        try:
            task = trade_queue.get()
            if task == "PROCESS":
                process_price()
                while not trade_queue.empty():
                    try:
                        trade_queue.get_nowait()
                    except queue.Empty:
                        break
        except Exception as e:
            log(f"[交易线程异常] {e}")


# ======================
# WebSocket 相关 (行情与官方私有流)
# ======================
last_ws_time = 0
last_aggTrade_time = 0
orderbook = {"bids": [], "asks": []}


def on_open(ws):
    global reconnect_delay
    reconnect_delay = 5
    log(f"[WebSocket] 连接成功 ✅ (模式: {'本地撮合-测试网行情' if USE_LOCAL_SIMULATION else 'API实盘'})")
    sync_position()
    time.sleep(0.5)
    sync_balance()
    params = [
        f"{WS_SYMBOL}@aggTrade", f"{WS_SYMBOL}@markPrice", f"{WS_SYMBOL}@depth5@100ms",
        f"{WS_SYMBOL}@kline_1m", f"{WS_SYMBOL}@kline_3m", f"{WS_SYMBOL}@kline_15m"
    ]
    sub_msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
    ws.send(json.dumps(sub_msg))


def on_message(ws, msg):
    global orderbook, raw_price, current_price, last_trade_side, last_ws_time, last_aggTrade_time
    global kline_1m_closed, kline_3m_closed, kline_15m_closed, mark_price, prev_price_v23
    last_ws_time = time.time()
    try:
        data = json.loads(msg)
        if "stream" in data and "data" in data: data = data["data"]
        if "result" in data and data.get("id") == 1: log("[WebSocket] 行情订阅成功 ✅"); return
        if data.get("e") == "markPriceUpdate":
            mark_price = float(data["p"])
            trade_queue.put("PROCESS")
        if data.get("e") == "depthUpdate":
            orderbook["bids"] = data["b"];
            orderbook["asks"] = data["a"]
        if data.get("e") == "aggTrade":
            last_trade_side = "sell" if data["m"] else "buy"
            prev_price_v23 = raw_price
            raw_price = float(data["p"]);
            current_price = raw_price
            last_aggTrade_time = time.time()
            with data_lock:
                price_buffer.append(raw_price)
                if len(price_buffer) > WINDOW: price_buffer.pop(0)
                tick_buffer.append(raw_price)
                if len(tick_buffer) > 5: tick_buffer.pop(0)
            trade_queue.put("PROCESS")
        if data.get("e") == "kline":
            k_data = data["k"]
            if k_data["x"]:
                new_k = {
                    "open": float(k_data["o"]), "high": float(k_data["h"]), "low": float(k_data["l"]),
                    "close": float(k_data["c"]), "volume": float(k_data["v"])
                }
                with data_lock:
                    if k_data["i"] == "1m":
                        kline_1m_closed.append(new_k)
                        if len(kline_1m_closed) > 15: kline_1m_closed.pop(0)
                        if len(kline_1m_closed) >= 9:
                            closes = [k['close'] for k in kline_1m_closed]
                            compute_kdj(closes)
                    elif k_data["i"] == "3m":
                        kline_3m_closed.append(new_k)
                        if len(kline_3m_closed) > 25: kline_3m_closed.pop(0)
                    elif k_data["i"] == "15m":
                        kline_15m_closed.append(new_k)
                        if len(kline_15m_closed) > 2: kline_15m_closed.pop(0)
    except Exception as e:
        last_ws_time = time.time()


def on_close(ws, close_status_code, close_msg):
    log(f"[WebSocket] 断开，自动重连中...")


def ws_watchdog():
    global last_ws_time, ws_app
    while True:
        time.sleep(5)
        if time.time() - last_ws_time > 10:
            log("🚨 WS断流超过10秒，主动断开触发重连！")
            try:
                if ws_app: ws_app.close()
            except:
                pass
            last_ws_time = time.time()


def on_error(ws, error):
    log(f"[WebSocket 错误] {error}")


def start_ws():
    def run():
        global reconnect_delay, ws_app
        while True:
            try:
                ws_app = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message, on_error=on_error,
                                                on_close=on_close)
                ws_app.run_forever(ping_interval=30, ping_timeout=15, ping_payload="ping")
            except Exception as e:
                log(f"[WebSocket 异常] {e}")
                reconnect_delay = min(reconnect_delay * 1.5, 30)
                time.sleep(reconnect_delay)

    threading.Thread(target=run, daemon=True).start()


def start_user_data_stream():
    if USE_LOCAL_SIMULATION: return
    global listenKey, user_data_ws_app
    base_listen_url = f"{BASE_URL}/fapi/v1/listenKey"

    def keepalive_listen_key():
        while True:
            time.sleep(1800)
            try:
                requests.put(base_listen_url, headers={"X-MBX-APIKEY": API_KEY})
            except:
                pass

    def on_user_data_open(ws):
        log("[UserDataWS] 私有流连接成功 ✅")

    def on_user_data_message(ws, msg):
        global official_unrealized_profit, official_isolated_margin, position, entry_price, actual_position_amt
        try:
            data = json.loads(msg)
            if data.get("e") == "ACCOUNT_UPDATE":
                for pos_data in data.get("a", {}).get("P", []):
                    if pos_data.get("s") == SYMBOL:
                        amt = float(pos_data.get("pa", 0))
                        with lock:
                            if amt != 0:
                                position = "long" if amt > 0 else "short"
                                entry_price = float(pos_data.get("ep", 0))
                                actual_position_amt = abs(amt)
                                official_unrealized_profit = float(pos_data.get("up", 0))
                                official_isolated_margin = float(pos_data.get("iw", 0))
                            else:
                                position = None;
                                entry_price = 0;
                                actual_position_amt = QTY
                                official_unrealized_profit = 0.0;
                                official_isolated_margin = 0.0
                        trade_queue.put("PROCESS")
        except:
            pass

    def on_user_data_close(ws, code, msg):
        time.sleep(5)
        start_user_data_stream()

    def run():
        global listenKey, user_data_ws_app
        try:
            res = requests.post(base_listen_url, headers={"X-MBX-APIKEY": API_KEY})
            listenKey = res.json().get("listenKey")
            if not listenKey: return
            threading.Thread(target=keepalive_listen_key, daemon=True).start()
            ws_url = f"wss://fstream.binance.com/ws/{listenKey}"
            user_data_ws_app = websocket.WebSocketApp(ws_url, on_open=on_user_data_open,
                                                      on_message=on_user_data_message, on_close=on_user_data_close)
            user_data_ws_app.run_forever(ping_interval=30, ping_timeout=15)
        except:
            time.sleep(5)
            start_user_data_stream()

    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    log("======================================")
    mode_str = "本地撮合 (测试网行情)" if USE_LOCAL_SIMULATION else "实盘 (官方精准盈亏驱动)"
    log(f" V25 市场结构+资金费率驱动引擎 启动成功")
    log(f" 当前运行模式: {mode_str}")
    log(f" 核心: 3m扫单+费率反转+爆仓区预测 | 止损:{SCALP_SL * 100}%")
    log("======================================")
    sync_binance_time()
    fetch_initial_klines()
    sync_balance()
    sync_position()
    start_ws()
    start_user_data_stream()
    threading.Thread(target=trade_worker, daemon=True).start()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=ws_watchdog, daemon=True).start()
    threading.Thread(target=fetch_funding_rate_loop, daemon=True).start()  # 🚀 启动资金费率轮询
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log("[程序退出]")