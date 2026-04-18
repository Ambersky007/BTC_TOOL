# -*- coding: utf-8 -*-
# 根据胜率动态仓位
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
# 策略参数 (V23-流动性实盘 - 剃头皮微利版)
# ======================
WINDOW = 10
ORDER_TIMEOUT = 200
RANGE_ORDER_TIMEOUT = 200
SLIPPAGE = 5
PRICE_STEP = 5  # 或10
MAX_CONTINUOUS_LOSS = 3
LOSS_RESET_SEC = 180
last_ws_time = 0
last_aggTrade_time = 0
trade_buffer = []
FAST_CANCEL_SEC = 2
cooldown_until = 0
COOLDOWN_SEC = 2
# V23 核心参数
SCALP_SL = 0.015  # 1.5% 硬止损
MIN_TP_PNL = 0.0006  # 0.06% 最小保底止盈门槛，必须覆盖手续费
MIN_TP_MAX_PROFIT = 0.0006  # 0.06% 流动性止盈必须达到的最高盈利门槛
ZONE_VALIDITY_SEC = 120  # 基础时效，实际将由动态函数覆盖
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
trend_buffer = []
binance_time_offset = 0
orderbook = {"bids": [], "asks": []}
last_trade_side = "neutral"
pressure_start_time = 0
pressure_side = None
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
kline_15m_closed = []
# 核心修复1：移除布尔锁，改用时间锁节流
last_process_time = 0.0
PROCESS_INTERVAL = 0.2  # 200ms节流
trade_queue = queue.Queue()
max_profit_pct = 0.0
max_price_since_entry = 0.0
min_price_since_entry = 999999.0
time_at_max_profit = 0.0
# 🚀 实盘官方盈亏数据源
official_unrealized_profit = 0.0  # 官方未实现盈亏
official_isolated_margin = 0.0  # 官方逐仓保证金
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
FIXED_NOTIONAL = 300.0  # 🚀 新增：固定开仓金额(U)，设置为0或None则使用原动态比例逻辑
account_balance = INITIAL_BALANCE
current_entry_strategy = ""
current_zone_score = 0  # 🚀 新增：记录当前区间评分


# ======================
# V23 流动性结构系统
# ======================
# 核心修复2：动态区间时效
def get_dynamic_zone_validity(atr):
    if atr < 50:
        return 180  # 慢行情
    elif atr < 120:
        return 120
    else:
        return 60  # 快行情


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
user_data_ws_app = None  # 🚀 用户数据流WS


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
        log(f"[余额同步] 本地模式，初始资金: {account_balance} U")
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
                    log(f"[余额同步] API可用余额: {account_balance} U")
                    return
        else:
            log(f"[余额同步失败] 返回异常: {d}")
            if isinstance(d, dict) and d.get("code") == -1021: sync_binance_time()
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
    global kline_1m_closed, kline_15m_closed
    log("[历史K线] 正在拉取初始1分钟和15分钟K线数据...")
    try:
        url_1m = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval=1m&limit=15"
        res_1m = requests.get(url_1m, timeout=5).json()
        if isinstance(res_1m, list):
            for k in res_1m[:-1]:
                kline_1m_closed.append({
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5])
                })
            log(f"[历史K线] 成功加载 {len(kline_1m_closed)} 根 1分钟K线")
            if len(kline_1m_closed) >= 9:
                with data_lock:
                    closes = [k['close'] for k in kline_1m_closed]
                    compute_kdj(closes)
        url_15m = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval=15m&limit=3"
        res_15m = requests.get(url_15m, timeout=5).json()
        if isinstance(res_15m, list):
            for k in res_15m[:-1]:
                kline_15m_closed.append({
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5])
                })
            log(f"[历史K线] 成功加载 {len(kline_15m_closed)} 根 15分钟K线")
    except Exception as e:
        log(f"[历史K线] 拉取失败: {e}，等待WebSocket推送积累...")


# ======================
# 持仓/下单/撤单/平仓
# ======================
def sync_position():
    global position, entry_price, active_order, actual_position_amt, official_unrealized_profit, official_isolated_margin
    if USE_LOCAL_SIMULATION:
        if local_position:
            log(f"[本地持仓] {local_position} | 均价:{local_entry_price}")
        else:
            log("[本地持仓] None")
        return
    try:
        url = f"{BASE_URL}/fapi/v2/positionRisk"
        p = {"timestamp": get_timestamp(), "recvWindow": 5000}
        url = build_signed_url(url, p)
        h = {"X-MBX-APIKEY": API_KEY}
        d = requests.get(url, headers=h, timeout=3).json()
        if isinstance(d, dict) and d.get("code") is not None:
            log(f"[持仓同步失败] API返回错误: code={d.get('code')}, msg={d.get('msg')}")
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
                        log(f"[API持仓] {position} | 数量:{actual_position_amt} | 均价:{ep} | 官方未实现盈亏:{official_unrealized_profit}")
                    else:
                        position = None
                        entry_price = 0
                        actual_position_amt = QTY
                        official_unrealized_profit = 0.0
                        official_isolated_margin = 0.0
                        log("[API持仓] None")
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
            else:
                log("[账户监控] 无持仓")
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
            log(f"[本地市价平仓成功] {side} | 价格:{raw_price}")
            local_position = None;
            local_entry_price = 0.0;
            local_max_profit_pct = 0.0
            local_max_price = 0.0;
            local_min_price = 999999.0;
            local_trade_lock = False
            entry_time = 0;
            time_at_max_profit = 0.0
        else:
            log(f"[本地市价开仓成功] {side} | 价格:{raw_price}")
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
                log(f"[API市价平仓成功] {side} | 数量:{close_qty} | 订单ID: {res.get('orderId')}")
                return True
            else:
                log(f"[API市价下单异常] 返回: {res}")
                if isinstance(res, dict) and res.get("code") == -1021: sync_binance_time()
        except Exception as e:
            log(f"[API市价失败] 重试 {i + 1}/3 | 错误: {e}")
            time.sleep(0.5)
    log("❌ API市价平仓彻底失败，请手动检查仓位！")
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
        else:
            log(f"[API撤单失败] 保留挂单状态, 返回: {res}")
    except Exception as e:
        log(f"[API撤单异常] 保留挂单状态, 错误: {e}")


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


# 核心修复5：价格有效性检验，消灭幽灵成交
def is_price_valid():
    if not raw_price or not mark_price: return False
    diff = abs(raw_price - mark_price) / raw_price
    return diff < 0.0005  # 0.05%


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
        # 修复：必须价格有效且真实最新价穿过，才允许撮合
        if is_price_valid():
            with data_lock:
                check_price = raw_price  # 严格使用最新成交价
            if side == "BUY" and check_price and check_price <= order_p:
                filled = True
            elif side == "SELL" and check_price and check_price >= order_p:
                filled = True
        is_reduce = (side == "SELL" and local_position == "long") or (side == "BUY" and local_position == "short")
        if filled:
            if is_reduce:
                log(f"🎉 [本地限价平仓单成交] {side} | 成交价:{order_p}")
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
                log(f"🎉 [本地限价单成交] {side} | 成交价:{order_p} | 数量:{actual_position_amt}")
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
                log(f"🎉 [API限价平仓单成交] {d.get('side')} | 成交价:{d.get('avgPrice')}")
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
                log(f"🎉 [API限价单成交] {d.get('side')} | 成交价:{d.get('avgPrice')}")
        elif d.get("status") == "PARTIALLY_FILLED":
            if not partial_filled_flag:
                partial_filled_flag = True;
                order_time = time.time()
                log(f"⚠️ [订单部分成交] 已成交:{d.get('executedQty')}, 剩余挂单等5秒后撤单！")
            sync_position()
            entry_time = time.time();
            max_profit_pct = 0.0
            max_price_since_entry = entry_price;
            min_price_since_entry = entry_price;
            time_at_max_profit = entry_time
        elif isinstance(d, dict) and d.get("code") == -1021:
            log(f"[查单失败] API返回时间戳错误，正在同步时间");
            sync_binance_time()
    except:
        pass


# ======================
# V23 流动性核心引擎
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


def detect_sweep(price, prev_price, zone):
    if not zone or not prev_price: return None
    if price > zone.high and prev_price <= zone.high: return "up_sweep"
    if price < zone.low and prev_price >= zone.low: return "down_sweep"
    return None


def detect_absorption():
    with data_lock:
        if len(tick_buffer) < 5: return False
        avg = sum(tick_buffer[-5:]) / 5;
        last = tick_buffer[-1]
        volatility = max(tick_buffer[-5:]) - min(tick_buffer[-5:])
        return abs(last - avg) < (avg * 0.0003) and volatility < (avg * 0.001)


def detect_breakout():
    with data_lock:
        if len(kline_1m_closed) < 5: return False
        atr = compute_atr(kline_1m_closed, 7)
        k0 = kline_1m_closed[-1]
        vols = [k['volume'] for k in kline_1m_closed[-3:]]
        body = abs(k0['close'] - k0['open'])
        vol_spike = vols[-1] > (sum(vols) / 3) * 2
        return body > atr * 0.8 and vol_spike


def detect_touch(price, prev_price, zone):
    if not zone or not prev_price: return False
    buffer = 30
    if price > zone.high and prev_price <= zone.high: return True
    if price < zone.low and prev_price >= zone.low: return True
    if abs(price - zone.high) <= buffer: return True
    if abs(price - zone.low) <= buffer: return True
    return False


def detect_range():
    global current_zone_score
    with data_lock:
        if len(kline_1m_closed) < 10:
            current_zone_score = 0
            return False
        highs = [k['high'] for k in kline_1m_closed[-10:]]
        lows = [k['low'] for k in kline_1m_closed[-10:]]
        range_high = max(highs)
        range_low = min(lows)
        range_width = range_high - range_low
        atr = compute_atr(kline_1m_closed, 7)
        if atr <= 0 or range_width <= 0:
            current_zone_score = 0
            return False
        score1 = 0
        if atr * 2 <= range_width <= atr * 6:
            score1 = 25
        elif range_width < atr * 2:
            score1 = 10
        else:
            score1 = 5
        tolerance = 30
        touch_high = sum(1 for k in kline_1m_closed[-10:] if abs(k['high'] - range_high) <= tolerance)
        touch_low = sum(1 for k in kline_1m_closed[-10:] if abs(k['low'] - range_low) <= tolerance)
        total_touch = touch_high + touch_low
        score2 = 25 if total_touch >= 4 else (15 if total_touch >= 2 else 5)
        bodies = [abs(k['close'] - k['open']) for k in kline_1m_closed[-10:]]
        small_bodies = sum(1 for b in bodies if b < atr * 0.5)
        score3 = 20 if small_bodies >= 6 else (10 if small_bodies >= 3 else 5)
        ups = sum(1 for k in kline_1m_closed[-10:] if k['close'] > k['open'])
        downs = 10 - ups
        up_down_bias = abs(ups - downs) / 10.0
        score4 = 20 if up_down_bias < 0.3 else 5
        zone_touch = liquidity_zone.touch_count if liquidity_zone else 0
        score5 = 10 if zone_touch >= 2 else 5
        total_score = score1 + score2 + score3 + score4 + score5
        current_zone_score = total_score
        return total_score >= 50


def detect_state_pipeline():
    if detect_breakout(): return "breakout"
    if detect_range(): return "range"
    return "unknown"


# ======================
# V23 开仓条件诊断雷达
# ======================
def log_entry_conditions():
    global last_entry_log_time
    now = time.time()
    if now - last_entry_log_time < 2: return
    with data_lock:
        kline_len = len(kline_1m_closed)
        atr = compute_atr(kline_1m_closed, 7) if kline_len >= 7 else 0.0
        cond1_5k = False
        if kline_len >= 5 and atr > 0:
            bodies = [abs(k['close'] - k['open']) for k in kline_1m_closed[-5:]]
            small_count = sum(1 for b in bodies if b < atr * 0.7)
            cond1_5k = small_count >= 3
    state_pipeline = detect_state_pipeline()
    is_range_code = state_pipeline == "range"
    update_liquidity_zone()
    cond2_kline = kline_len >= 7
    cond2_atr = atr > 0
    zone_width = 0.0
    if liquidity_zone: zone_width = liquidity_zone.high - liquidity_zone.low
    cond2_zone_width = zone_width >= PRICE_STEP * 2
    cond2 = cond2_kline and cond2_atr and cond2_zone_width
    rr_ratio = 0.0
    cond3 = False
    required_width = 0.0
    if liquidity_zone and raw_price and raw_price > 0:
        stop_distance = raw_price * SCALP_SL
        rr_ratio = zone_width / stop_distance if stop_distance > 0 else 0
        if zone_width < PRICE_STEP * 6:
            cond3 = False
        elif zone_width < PRICE_STEP * 20:
            cond3 = rr_ratio >= 1.1
        else:
            cond3 = rr_ratio >= 1.3
    cond4_long = raw_price <= (liquidity_zone.low + PRICE_STEP * 2) if liquidity_zone else False
    cond4_short = raw_price >= (liquidity_zone.high - PRICE_STEP * 2) if liquidity_zone else False
    cond4 = cond4_long or cond4_short
    bias = orderbook_bias()
    touch_count = liquidity_zone.touch_count if liquidity_zone else 0
    cond5_long = (touch_count >= 1) and (bias != "bear" or detect_absorption()) and (last_trade_side != "sell")
    cond5_short = (touch_count >= 1) and (bias != "bull" or detect_absorption()) and (last_trade_side != "buy")
    cond5 = cond5_long or cond5_short
    zone_remaining_sec = 0.0
    cond6 = False
    if liquidity_zone:
        elapsed = now - liquidity_zone.creation_time
        zone_remaining_sec = max(0, liquidity_zone.validity_window - elapsed)
        cond6 = elapsed <= liquidity_zone.validity_window
    all_long = is_range_code and cond2 and cond3 and cond4_long and cond5_long and cond6
    all_short = is_range_code and cond2 and cond3 and cond4_short and cond5_short and cond6
    log(f"[开仓雷达] 1.震荡市:代码{'✅' if is_range_code else '❌'}(评分:{current_zone_score}) | 5K{'✅' if cond1_5k else '❌'} | "
        f"2.基础{'✅' if cond2 else '❌'}(K:{kline_len} A:{atr:.1f} W:{zone_width:.1f}) | "
        f"3.盈亏比{'✅' if cond3 else '❌'}({rr_ratio:.2f}≥1.3 需W>{required_width:.1f}) | "
        f"4.靠轨{'✅' if cond4 else '❌'}(多:{'✅' if cond4_long else '❌'} 空:{'✅' if cond4_short else '❌'}) | "
        f"5.过滤{'✅' if cond5 else '❌'}(偏:{bias} 触:{touch_count} 向:{last_trade_side}) | "
        f"6.时效{'✅' if cond6 else '❌'}(剩:{zone_remaining_sec:.0f}s) | "
        f"==> 综合: 多{'🟢' if all_long else '🔴'} 空{'🟢' if all_short else '🔴'}")
    last_entry_log_time = now


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


def detect_momentum_exhaustion():
    with data_lock:
        if len(tick_buffer) < 5: return False
        diffs = [tick_buffer[i] - tick_buffer[i - 1] for i in range(1, len(tick_buffer))]
        if not diffs or diffs[0] == 0: return False
        return abs(diffs[-1]) < abs(diffs[0]) * 0.5


def scalp_entry():
    global current_entry_strategy
    if detect_breakout(): return
    atr = 0.0
    with data_lock:
        if len(kline_1m_closed) >= 7:
            atr = compute_atr(kline_1m_closed, 7)
            if atr > PRICE_STEP * 20: return
    if detect_state_pipeline() != "range": return
    if not liquidity_zone: return
    if time.time() - liquidity_zone.creation_time > liquidity_zone.validity_window: return
    buy_price, sell_price = get_smart_entry_prices()
    if buy_price is None or sell_price is None: return
    # 核心修复3：挂单防抢跑约束
    with data_lock:
        if len(kline_1m_closed) < 3: return
        last_3_closes = [k['close'] for k in kline_1m_closed[-3:]]
        min_close = min(last_3_closes)
        max_close = max(last_3_closes)
    # 多单约束：不高于近3根K线最低收盘价，且必须大于区间下轨
    long_allowed = (buy_price <= min_close) and (liquidity_zone and buy_price >= liquidity_zone.low)
    # 空单约束：不低于近3根K线最高收盘价，且必须小于区间上轨
    short_allowed = (sell_price >= max_close) and (liquidity_zone and sell_price <= liquidity_zone.high)
    price = raw_price
    zone = liquidity_zone
    bias = orderbook_bias()
    width = zone.high - zone.low
    offset = max(5, width * 0.1)
    exhaustion = detect_momentum_exhaustion()
    with data_lock:
        if len(kline_1m_closed) >= 1:
            last_k = kline_1m_closed[-1]
            body = abs(last_k['close'] - last_k['open'])
            if not exhaustion and body > atr * 0.7: return
    sweep = detect_sweep(price, prev_price_v23, zone)
    if sweep == "down_sweep" and price > zone.low:
        if long_allowed:  # 应用约束
            if buy_price < price:
                current_entry_strategy = "V23假跌破反转"
                send_limit_order("BUY", buy_price)
                return
    if sweep == "up_sweep" and price < zone.high:
        if short_allowed:  # 应用约束
            if sell_price > price:
                current_entry_strategy = "V23假突破反转"
                send_limit_order("SELL", sell_price)
                return
    notional_multiplier = 1.0
    if current_zone_score >= 70:
        notional_multiplier = 1.0
        strategy_suffix = "优质区"
    elif current_zone_score >= 50:
        notional_multiplier = 0.5
        strategy_suffix = "试探区"
    else:
        return
    custom_qty = get_dynamic_qty(price, ratio=POSITION_RATIO, notional_multiplier=notional_multiplier)
    if price < zone.low:
        pass
    elif price <= zone.low + PRICE_STEP * 2:
        if zone.touch_count >= 1 and (
                bias != "bear" or detect_absorption() or exhaustion) and last_trade_side != "sell":
            if not long_allowed: return  # 应用约束
            if buy_price >= price: return
            if buy_price < zone.low - offset: return
            current_entry_strategy = f"V23智能低吸({strategy_suffix}评分{current_zone_score})"
            send_limit_order("BUY", buy_price, custom_qty=custom_qty)
            return
    if price > zone.high:
        pass
    elif price >= zone.high - PRICE_STEP * 2:
        if zone.touch_count >= 1 and (bias != "bull" or detect_absorption() or exhaustion) and last_trade_side != "buy":
            if not short_allowed: return  # 应用约束
            if sell_price <= price: return
            if sell_price > zone.high + offset: return
            current_entry_strategy = f"V23智能高抛({strategy_suffix}评分{current_zone_score})"
            send_limit_order("SELL", sell_price, custom_qty=custom_qty)
            return


# 核心修复6：提前止损检测
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
            if pnl <= -SCALP_SL: close_position("V23硬止损(本地)"); return True
    else:
        with lock:
            pos = position;
            off_pnl = official_unrealized_profit;
            off_margin = official_isolated_margin
        if not pos: return False
        if off_margin > 0:
            roi = off_pnl / off_margin
            if roi <= -SCALP_SL:
                close_position("V23硬止损(官方盈亏触发)")
                return True
    # 修复6：提前止损触发
    if detect_aggressive_reversal():
        close_position("订单流提前止损")
        return True
    return False


def v21_scalp_main():
    if USE_LOCAL_SIMULATION:
        if local_active_order: return
    else:
        if active_order: return
    state = detect_state_pipeline()
    if state == "range": scalp_entry()


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
        pnl = (exit_price - ep) / ep if pos == "long" else (ep - exit_price) / ep
        if entry_time > 0:
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
    log(f"[平仓触发] {reason}")
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
                try:
                    log(f"[实盘平仓] 发送成功，等待官方结算更新。平仓前官方浮盈:{off_pnl_before:.2f}U")
                except:
                    pass
                return
            else:
                sync_position()
                with lock:
                    if not position: trade_lock = False; entry_time = 0; time_at_max_profit = 0.0; return
            time.sleep(3)
        log("🚨🚨🚨 严重警告：连续10次强平失败，交易线程放弃重试，请人工干预！🚨🚨🚨")
        trade_lock = False


# ======================
# 核心处理引擎 (防价格老化降级机制 + 微利极速兑现)
# ======================
def process_price():
    global last_process_time
    global current_price, raw_price, price_buffer, last_price_print_time
    global loss_count, last_check_time, last_holding_log_time
    global local_max_profit_pct, local_max_price, local_min_price
    global max_profit_pct, max_price_since_entry, min_price_since_entry
    global time_at_max_profit, official_unrealized_profit, official_isolated_margin
    # 核心修复1：时间锁节流，彻底替代布尔锁，杜绝死锁
    now = time.time()
    if now - last_process_time < PROCESS_INTERVAL:
        return
    last_process_time = now
    # 核心修复7：风控提到最高优先级，绝不被后续任何异常状态阻断
    if manage_position():
        return
    price_anomaly = False
    try:
        with data_lock:
            agg_delay = now - last_aggTrade_time
            if raw_price and agg_delay <= 3:
                snap_price = raw_price
            elif mark_price:
                if agg_delay > 3.5 and agg_delay < 4.0:
                    log(f"⚠️ aggTrade停滞{agg_delay:.1f}秒，降级使用mark_price估值")
                snap_price = mark_price
            else:
                snap_price = raw_price
        if snap_price is None:
            return
        if raw_price and mark_price:
            diff = abs(raw_price - mark_price) / raw_price
            if diff > 0.001:
                ws_delay = now - last_ws_time
                if ws_delay <= 1:
                    price_anomaly = True
        if now - last_price_print_time >= 5:
            ws_delay = now - last_ws_time
            agg_delay = now - last_aggTrade_time
            log(f"[价格] Last:{raw_price} | Mark:{mark_price if mark_price else 'N/A'} | 延迟:{ws_delay:.1f}s | aggDelay:{agg_delay:.1f}s")
            last_price_print_time = now
        if loss_count >= MAX_CONTINUOUS_LOSS:
            if now - loss_reset_time < LOSS_RESET_SEC:
                return
            loss_count = 0
        update_liquidity_zone()
        with data_lock:
            if liquidity_zone and raw_price:
                if detect_touch(raw_price, prev_price_v23, liquidity_zone):
                    liquidity_zone.touch_count = min(10, liquidity_zone.touch_count + 1)
        # 盈亏判定双轨制隔离
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
                    if snap_price > cur_max_price:
                        cur_max_price = snap_price
                        time_at_max_profit = time.time()
                else:
                    if snap_price < cur_min_price:
                        cur_min_price = snap_price
                        time_at_max_profit = time.time()
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
                        trail_tp = (time_since_high >= 20) and (retracement <= 0.65)
                        status_parts.append(
                            f"极速追踪({'触发🔴' if trail_tp else '等待🟡'} 历高停顿{time_since_high:.0f}s/20s 回撤比{retracement:.2%}/65%)")
                        if trail_tp:
                            close_position("极速追踪止盈")
                            return
                        avg_vol_10k = 0.0
                        with data_lock:
                            if len(kline_1m_closed) >= 10:
                                avg_vol_10k = sum(k['volume'] for k in kline_1m_closed[-10:]) / 10
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
            # price_anomaly 仅阻断开仓，绝不阻断上面的风控平仓
            if not has_order and not price_anomaly and time.time() > cooldown_until:
                v21_scalp_main()
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
def on_open(ws):
    global reconnect_delay
    reconnect_delay = 5
    log(f"[WebSocket] 连接成功 ✅ (模式: {'本地撮合-测试网行情' if USE_LOCAL_SIMULATION else 'API实盘'})")
    log("[状态校准] 正在重新同步持仓与余额，清理幽灵状态...")
    sync_position()
    time.sleep(0.5)
    sync_balance()
    params = [
        f"{WS_SYMBOL}@aggTrade", f"{WS_SYMBOL}@markPrice", f"{WS_SYMBOL}@depth5@100ms",
        f"{WS_SYMBOL}@kline_1m", f"{WS_SYMBOL}@kline_5m", f"{WS_SYMBOL}@kline_15m", f"{WS_SYMBOL}@kline_30m",
        f"{WS_SYMBOL}@kline_1h"
    ]
    sub_msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
    ws.send(json.dumps(sub_msg))


def on_message(ws, msg):
    global orderbook, raw_price, current_price, last_trade_side, last_ws_time, last_aggTrade_time, kline_1m_closed, kline_15m_closed, mark_price, prev_price_v23
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


# 🚀🚀🚀 实盘独有：币安用户数据流，获取官方零延迟真实盈亏 🚀🚀🚀
def start_user_data_stream():
    if USE_LOCAL_SIMULATION: return
    global listenKey, user_data_ws_app
    base_listen_url = f"{BASE_URL}/fapi/v1/listenKey"

    def keepalive_listen_key():
        while True:
            time.sleep(1800)
            try:
                requests.put(base_listen_url, headers={"X-MBX-APIKEY": API_KEY})
                log("[UserDataWS] ListenKey 续期成功")
            except Exception as e:
                log(f"[UserDataWS] ListenKey 续期失败: {e}")

    def on_user_data_open(ws):
        log("[UserDataWS] 私有流连接成功 ✅，实时接收官方盈亏推送")

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
            elif data.get("e") == "ORDER_TRADE_UPDATE":
                o = data.get("o", {})
                if o.get("s") == SYMBOL and o.get("X") == "FILLED":
                    log(f"[实盘成交回报-官方] 订单ID:{o.get('i')} | 方向:{o.get('S')} | 价格:{o.get('L')} | 数量:{o.get('l')} | 手续费:{o.get('n')}{o.get('N')}")
        except Exception as e:
            log(f"[UserDataWS] 解析异常: {e}")

    def on_user_data_error(ws, error):
        log(f"[UserDataWS] 错误: {error}")

    def on_user_data_close(ws, code, msg):
        log(f"[UserDataWS] 断开，5秒后重连...")
        time.sleep(5)
        start_user_data_stream()

    def run():
        global listenKey, user_data_ws_app
        try:
            res = requests.post(base_listen_url, headers={"X-MBX-APIKEY": API_KEY})
            listenKey = res.json().get("listenKey")
            if not listenKey:
                log(f"[UserDataWS] 创建ListenKey失败: {res.json()}")
                return
            threading.Thread(target=keepalive_listen_key, daemon=True).start()
            ws_url = f"wss://fstream.binance.com/ws/{listenKey}"
            user_data_ws_app = websocket.WebSocketApp(ws_url, on_open=on_user_data_open,
                                                      on_message=on_user_data_message,
                                                      on_error=on_user_data_error,
                                                      on_close=on_user_data_close)
            user_data_ws_app.run_forever(ping_interval=30, ping_timeout=15)
        except Exception as e:
            log(f"[UserDataWS] 启动异常: {e}")
            time.sleep(5)
            start_user_data_stream()

    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    log("======================================")
    mode_str = "本地撮合 (测试网行情)" if USE_LOCAL_SIMULATION else "实盘 (官方精准盈亏驱动)"
    log(f" V23 流动性剃头皮策略 (防卡死+防幽灵+防抢跑增强版) 启动成功")
    log(f" 当前运行模式: {mode_str}")
    log(f" 策略: 流动性+Sweep+区间评分 | 启动盈利:0.01% | 止损:{SCALP_SL * 100}% | 区间有效期:动态ATR自适应")
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
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log("[程序退出]")