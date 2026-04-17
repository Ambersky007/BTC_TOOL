# -*- coding: utf-8 -*-
# 编码声明：确保文件支持中文，Python识别UTF-8格式

"""
硬止损修改为1个ATR：在 process_price 中动态获取ATR值，按持仓方向计算1个ATR的亏损比例作为硬止损（兜底0.3%），适应不同波动率行情。
修复盈亏比失衡：有效止盈门槛从0.02%提高到0.05%，回撤平仓灵敏度从70%放宽至50%，让利润有更多空间奔跑。
过滤KDJ中间带假信号：做多J值必须≤30（超卖），做空J值必须≥70（超买）。
防止放量下杀接飞刀：增加了对短线放量下杀的过滤条件。
震荡模式状态标记：增加 is_current_range 全局变量，平仓时自动重置。
"""

import websocket  # WebSocket库：实时接收币安行情、成交、盘口数据
import json  # 处理JSON数据：解析币安推送的行情信息
import time  # 时间控制：延时、时间戳、超时判断
import hmac  # 加密算法：币安API签名验证
import hashlib  # 哈希算法：配合hmac生成签名
import threading  # 多线程：同时运行行情接收、持仓监控、策略逻辑
import requests  # HTTP请求：调用币安REST API（下单、撤单、查持仓）
import logging  # 日志系统：记录交易日志到文件
from datetime import datetime  # 日期时间：生成日志文件名、时间格式
import uuid  # 唯一ID：备用（本代码未直接使用）
import queue  # 队列：用于解耦WebSocket与交易逻辑，防止阻塞心跳
import pandas as pd  # 数据处理：用于生成交易记录表
import math  # 数学计算：用于动态计算下单数量时的取整

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
BASE_URL = BASE_URL_REAL if USE_LOCAL_SIMULATION else BASE_URL_TESTNET
WS_URL = WS_URL_REAL if USE_LOCAL_SIMULATION else WS_URL_TESTNET
SYMBOL = "BTCUSDT"
WS_SYMBOL = "btcusdt"
QTY = 0.01
# ======================
# 策略参数
# ======================
WINDOW = 10
MAX_RANGE = 0.0003
MIN_PROFIT = 0.0003
TRAILING_PROFIT = 0.0003
STOP_LOSS = 0.0003
MAX_HOLD_TIME = 300
ORDER_TIMEOUT = 30
RANGE_ORDER_TIMEOUT = 60  # 🔥 新增：震荡模式挂单等待时间（秒），比普通的ORDER_TIMEOUT长
SLIPPAGE = 5
PRICE_STEP = 0.5
MAX_CONTINUOUS_LOSS = 3
LOSS_RESET_SEC = 180
last_ws_time = 0
trade_buffer = []
VOLUME_WINDOW = 0.8
VOLUME_THRESHOLD = 2.5
FAST_CANCEL_SEC = 2
cooldown_until = 0
COOLDOWN_SEC = 2
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
last_processed_price = None
last_fill_time = 0
last_check_time = 0
lock = threading.RLock()
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
processing = False
trade_queue = queue.Queue()
max_profit_pct = 0.0
max_price_since_entry = 0.0
min_price_since_entry = 999999.0
time_at_max_profit = 0.0
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
account_balance = INITIAL_BALANCE
# 🔥 新增：记录当前开仓策略名称及是否为震荡模式
current_entry_strategy = ""
is_current_range = False


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
def get_dynamic_qty(price):
    if not price or price <= 0 or account_balance <= 0:
        return LOT_SIZE
    target_notional = account_balance * POSITION_RATIO
    raw_qty = target_notional / price
    steps = math.floor(raw_qty / LOT_SIZE)
    final_qty = steps * LOT_SIZE
    if final_qty < LOT_SIZE:
        final_qty = LOT_SIZE
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
            if isinstance(d, dict) and d.get("code") == -1021:
                sync_binance_time()
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


def orderbook_pressure_ratio():
    try:
        bids = orderbook["bids"][:3]
        asks = orderbook["asks"][:3]
        bv = sum(float(b[1]) for b in bids)
        av = sum(float(a[1]) for a in asks)
        return av / bv if bv > 0 else 999
    except:
        return 1


def update_trade_flow(p, q, is_sell): pass


def get_volume_spike(): return None


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


def is_long_upper_shadow(k):
    body = abs(k['close'] - k['open'])
    if body == 0: body = 0.0001
    upper_shadow = k['high'] - max(k['close'], k['open'])
    return upper_shadow > body * 2


def is_long_lower_shadow(k):
    body = abs(k['close'] - k['open'])
    if body == 0: body = 0.0001
    lower_shadow = min(k['close'], k['open']) - k['low']
    return lower_shadow > body * 2


def calc_avg_j_deltas(j_hist, direction='up'):
    if len(j_hist) < 2: return 0
    deltas = []
    for i in range(len(j_hist) - 1):
        delta = j_hist[i] - j_hist[i + 1]
        if direction == 'up' and delta > 0:
            deltas.append(delta)
        elif direction == 'down' and delta < 0:
            deltas.append(abs(delta))
    return sum(deltas) / len(deltas) if deltas else 0


def update_trade_volume(side, qty):
    trade_volume_buffer.append({'side': side, 'qty': float(qty)})
    if len(trade_volume_buffer) > 10: trade_volume_buffer.pop(0)


def is_buy_dominant():
    if len(trade_volume_buffer) < 5: return False
    buy_vol = sum(t['qty'] for t in trade_volume_buffer[-5:] if t['side'] == 'buy')
    sell_vol = sum(t['qty'] for t in trade_volume_buffer[-5:] if t['side'] == 'sell')
    return buy_vol > sell_vol


def is_sell_dominant():
    if len(trade_volume_buffer) < 5: return False
    buy_vol = sum(t['qty'] for t in trade_volume_buffer[-5:] if t['side'] == 'buy')
    sell_vol = sum(t['qty'] for t in trade_volume_buffer[-5:] if t['side'] == 'sell')
    return sell_vol > buy_vol


# ATR计算函数
def compute_atr(klines, period=7):
    if len(klines) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(klines)):
        high = klines[i]['high']
        low = klines[i]['low']
        prev_close = klines[i - 1]['close']
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
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
            # 启动时预热KDJ
            if len(kline_1m_closed) >= 9:
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


def get_15m_trend():
    if not kline_15m_closed: return "neutral"
    last_k = kline_15m_closed[-1]
    is_doji = abs(last_k['close'] - last_k['open']) / last_k['open'] < 0.0002 if last_k['open'] > 0 else False
    if is_doji:
        return "neutral"
    elif last_k['close'] >= last_k['open']:
        return "up"
    else:
        return "down"


# ======================
# 持仓/下单/撤单/平仓
# ======================
def sync_position():
    global position, entry_price, active_order, actual_position_amt
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
        for item in d:
            if item['symbol'] == SYMBOL:
                amt = float(item['positionAmt'])
                ep = float(item['entryPrice'])
                if amt != 0:
                    position = "long" if amt > 0 else "short"
                    entry_price = ep
                    actual_position_amt = abs(amt)
                    active_order = None
                    log(f"[API持仓] {position} | 数量:{actual_position_amt} | 均价:{entry_price}")
                else:
                    position = None
                    entry_price = 0
                    actual_position_amt = QTY
                    log("[API持仓] None")
    except Exception as e:
        log(f"[持仓同步失败] {e}")


def position_monitor():
    while True:
        try:
            sync_balance()
            if USE_LOCAL_SIMULATION:
                pos = local_position
                ep = local_entry_price
                if pos:
                    amt = QTY
                    price = raw_price if raw_price else ep
                    notional = abs(amt) * price
                    pnl = (price - ep) * amt if pos == "long" else (ep - price) * abs(amt)
                    margin = abs(amt) * ep / LEVERAGE if LEVERAGE > 0 else 0
                    roi = pnl / margin if margin > 0 else 0
                    hold_time = time.time() - entry_time if entry_time > 0 else 0
                    log(f"[本地持仓监控] {pos} | 数量:{abs(amt)} | 价值:{notional:.2f}U | 均价:{ep:.1f} | 浮盈:{pnl:.2f}U | ROI:{roi:.2%} | 杠杆:{LEVERAGE}x | 持仓时间:{hold_time:.1f}秒")
                else:
                    log("[本地持仓监控] 无持仓")
            else:
                url = f"{BASE_URL}/fapi/v2/positionRisk"
                p = {"timestamp": get_timestamp(), "recvWindow": 5000}
                url = build_signed_url(url, p)
                h = {"X-MBX-APIKEY": API_KEY}
                d = requests.get(url, headers=h, timeout=3).json()
                if isinstance(d, dict) and d.get("code") is not None:
                    log(f"[持仓监控异常] API返回错误: code={d.get('code')}, msg={d.get('msg')}")
                    if d.get("code") == -1021: sync_binance_time()
                else:
                    for item in d:
                        if item['symbol'] == SYMBOL:
                            amt = float(item['positionAmt'])
                            ep = float(item['entryPrice'])
                            leverage = float(item['leverage'])
                            if amt != 0:
                                pos = "long" if amt > 0 else "short"
                                price = raw_price if raw_price else ep
                                notional = abs(amt) * price
                                pnl = (price - ep) * amt if pos == "long" else (ep - price) * abs(amt)
                                margin = abs(amt) * ep / leverage if leverage > 0 else 0
                                roi = pnl / margin if margin > 0 else 0
                                hold_time = time.time() - entry_time if entry_time > 0 else 0
                                log(f"[API持仓监控] {pos} | 数量:{abs(amt)} | 价值:{notional:.2f}U | 均价:{ep:.1f} | 浮盈:{pnl:.2f}U | ROI:{roi:.2%} | 杠杆:{leverage}x | 持仓时间:{hold_time:.1f}秒")
                            else:
                                log("[API持仓监控] 无持仓")
        except Exception as e:
            log(f"[持仓监控异常] {e}")
        time.sleep(8)


def send_limit_order(side, price, reduce=False):
    global local_active_order, local_order_time, local_order_price, local_trade_lock
    global active_order, order_time, order_price, trade_lock, partial_filled_flag
    if USE_LOCAL_SIMULATION:
        if not reduce and (local_position is not None or local_active_order is not None or local_trade_lock):
            log(f"[本地拦截重复下单] 侧边:{side} | 状态: 持仓={local_position}, 挂单={local_active_order}, 锁={local_trade_lock}")
            return
        local_trade_lock = True
        local_active_order = f"{side}_{format_price(price)}"
        local_order_time = time.time()
        local_order_price = price
        log(f"✅ 本地挂单成功 {side} | 价格:{price}")
        return
    if not reduce and (position is not None or active_order is not None or trade_lock):
        log(f"[拦截重复下单] 侧边:{side} | 状态: 持仓={position}, 挂单={active_order}, 锁={trade_lock}")
        return
    trade_lock = True
    partial_filled_flag = False
    order_qty = get_dynamic_qty(price) if not reduce else actual_position_amt
    if order_qty <= 0:
        log(f"[拦截下单] 数量为0 (余额:{account_balance}, 价格:{price})")
        trade_lock = False
        return
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
            active_order = res["orderId"]
            order_time = time.time()
            order_price = price
            log(f"✅ API挂单成功 {res} | 数量:{order_qty}")
        else:
            log(f"❌ API挂单失败返回: {res}")
            trade_lock = False
            if isinstance(res, dict) and res.get("code") == -1021: sync_binance_time()
    except Exception as e:
        log(f"❌ API挂单异常: {e}")
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
        log("[本地撤单成功]")
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
            log("[API撤单成功]")
        else:
            log(f"[API撤单失败] 保留挂单状态, 返回: {res}")
    except Exception as e:
        log(f"[API撤单异常] 保留挂单状态, 错误: {e}")


def check_fast_cancel():
    global local_order_price, order_price
    if USE_LOCAL_SIMULATION:
        if not local_active_order: return
        try:
            if not orderbook["bids"] or not orderbook["asks"]: return
            best_bid = float(orderbook["bids"][0][0])
        except:
            return
        if abs(local_order_price - best_bid) > 15 * PRICE_STEP: cancel_order()
    else:
        if not active_order: return
        try:
            if not orderbook["bids"] or not orderbook["asks"]: return
            best_bid = float(orderbook["bids"][0][0])
        except:
            return
        if abs(order_price - best_bid) > 15 * PRICE_STEP: cancel_order()


def check_order_timeout():
    if USE_LOCAL_SIMULATION:
        if local_active_order:
            # 根据策略名称动态判断超时时间
            timeout = RANGE_ORDER_TIMEOUT if "震荡" in current_entry_strategy else ORDER_TIMEOUT
            if time.time() - local_order_time > timeout: cancel_order()
    else:
        if active_order:
            # 根据策略名称动态判断超时时间
            base_timeout = RANGE_ORDER_TIMEOUT if "震荡" in current_entry_strategy else ORDER_TIMEOUT
            timeout = 5 if partial_filled_flag else base_timeout
            if time.time() - order_time > timeout: cancel_order()


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
        if side == "BUY" and raw_price <= order_p:
            filled = True
        elif side == "SELL" and raw_price >= order_p:
            filled = True
        if filled:
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
        if d.get("status") == "FILLED":
            active_order = None;
            trade_lock = False;
            partial_filled_flag = False;
            last_fill_time = time.time()
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
                log(f"⚠️ [订单部分成交] 已成交:{d.get('executedQty')}, 剩余挂单等5秒后撤单，持仓进入止盈止损！")
                sync_position();
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
# 策略逻辑与平仓
# ======================
def detect_zone():
    if len(price_buffer) < WINDOW: return None
    return max(price_buffer), min(price_buffer)


def update_tick(p):
    tick_buffer.append(p)
    if len(tick_buffer) > 5: tick_buffer.pop(0)


def momentum_ok(side):
    try:
        avg = sum(tick_buffer[-3:]) / 3
        return current_price > avg if side == "long" else current_price < avg
    except:
        return True


def get_trend():
    try:
        avg = sum(price_buffer[-10:]) / 10
        return "up" if current_price > avg else "down" if current_price < avg else "neutral"
    except:
        return "neutral"


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
            log("[存储降级] 未安装openpyxl，交易记录已保存为CSV格式。如需Excel格式，请执行: pip install openpyxl")
    except Exception as e:
        log(f"[记录保存失败] {e}")


def close_position(reason):
    global loss_count, cooldown_until, loss_reset_time, entry_time, time_at_max_profit, current_entry_strategy, is_current_range
    if USE_LOCAL_SIMULATION:
        pos = local_position;
        ep = local_entry_price
    else:
        pos = position;
        ep = entry_price
    with lock:
        if not pos: return
    pnl = (raw_price - ep) / ep if pos == "long" else (ep - raw_price) / ep
    if entry_time > 0:
        close_qty = actual_position_amt if not USE_LOCAL_SIMULATION else QTY
        pnl_abs = (raw_price - ep) * close_qty if pos == "long" else (ep - raw_price) * close_qty
        margin = abs(close_qty) * ep / LEVERAGE if LEVERAGE > 0 else 0
        roi = pnl_abs / margin if margin > 0 else 0
        record = {
            "开仓时间": datetime.fromtimestamp(entry_time).strftime('%Y-%m-%d %H:%M:%S'),
            "平仓时间": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "开单策略": current_entry_strategy,
            "方向": "多" if pos == "long" else "空",
            "数量": close_qty,
            "开仓价格": ep,
            "平仓价格": raw_price,
            "盈亏(U)": round(pnl_abs, 2),
            "收益率": f"{roi:.2%}",
            "平仓原因": reason
        }
        trade_records.append(record)
        save_trade_records()
    if pnl < 0:
        loss_count += 1
        if loss_count >= MAX_CONTINUOUS_LOSS: loss_reset_time = time.time()
    else:
        loss_count = 0
    log(f"[平仓触发] {reason} | 当前盈亏:{pnl:.2%}")
    # 重置开仓状态
    is_current_range = False
    cancel_order()
    if USE_LOCAL_SIMULATION:
        success = send_market_order("SELL" if pos == "long" else "BUY", True)
        if success:
            cooldown_until = time.time() + COOLDOWN_SEC;
            entry_time = 0;
            time_at_max_profit = 0.0
            log(f"[平仓完成] 状态已重置")
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
                log(f"[平仓完成] 状态已重置 (共尝试 {retry_count} 次)");
                return
            else:
                log(f"⚠️ 平仓失败，3秒后发起第 {retry_count + 1} 次强平！")
                sync_position()
                if not position: trade_lock = False; entry_time = 0; time_at_max_profit = 0.0; return
                time.sleep(3)
        log("🚨🚨🚨 严重警告：连续10次强平失败，交易线程放弃重试，请人工干预！🚨🚨🚨")
        trade_lock = False


def v18_entry_logic():
    global cooldown_until, loss_count, last_entry_log_time, current_entry_strategy, is_current_range
    now = time.time()
    if USE_LOCAL_SIMULATION:
        if local_trade_lock or local_position is not None or local_active_order is not None: return
        if now - local_order_time < 2: return
    else:
        if trade_lock or position is not None or active_order is not None: return
        if now - order_time < 2: return
    cond1 = now >= cooldown_until
    if not cond1: return
    try:
        best_bid = float(orderbook["bids"][0][0])
        best_ask = float(orderbook["asks"][0][0])
        spread = best_ask - best_bid
        cond4 = spread <= 3 * PRICE_STEP
    except:
        return
    if not cond4: return
    if len(kline_1m_closed) < 3: return
    k0, k1, k2 = kline_1m_closed[-1], kline_1m_closed[-2], kline_1m_closed[-3]
    trend_15m = get_15m_trend()
    allow_long = trend_15m in ["up", "neutral"]
    allow_short = trend_15m in ["down", "neutral"]
    bias = orderbook_bias()
    if len(j_list) < 2: return
    j0, j1 = j_list[-1], j_list[-2]
    # 🔥 修改：过滤KDJ中间带假信号，超买超卖更严格
    j_oversold = j0 <= 30
    j_overbought = j0 >= 70
    avg_vol_3 = (k0['volume'] + k1['volume'] + k2['volume']) / 3.0
    avg_body_3 = (abs(k0['close'] - k0['open']) + abs(k1['close'] - k1['open']) + abs(k2['close'] - k2['open'])) / 3.0
    # ========== 原有剃头皮条件提取 ==========
    pb_long_cond_trend = allow_long
    pb_long_cond_bias = bias != "bear"
    pb_long_cond_shadow = not (is_long_upper_shadow(k0) or is_long_upper_shadow(k1) or is_long_upper_shadow(k2))
    is_3_rise = (k0['close'] > k0['open']) and (k1['close'] > k1['open']) and (k2['close'] > k2['open'])
    is_3_vol_down = (k0['volume'] < k1['volume']) and (k1['volume'] < k2['volume'])
    pb_long_cond_volume = not (is_3_rise and is_3_vol_down)
    pb_long_cond_kdj_dir = j0 > j1
    current_j_up = j0 - j1 if pb_long_cond_kdj_dir else 0
    avg_j_up = calc_avg_j_deltas(j_list, 'up')
    pb_long_cond_kdj_force = current_j_up >= (avg_j_up * 0.1) if avg_j_up > 0 else pb_long_cond_kdj_dir
    pb_long_cond_flow = is_buy_dominant() or bias == "bull"
    pullback_long_ok = (
            pb_long_cond_trend and pb_long_cond_bias and pb_long_cond_shadow and
            pb_long_cond_volume and pb_long_cond_kdj_dir and pb_long_cond_kdj_force and
            pb_long_cond_flow and j_oversold
    )
    pb_short_cond_trend = allow_short
    pb_short_cond_bias = bias != "bull"
    pb_short_cond_shadow = not (is_long_lower_shadow(k0) or is_long_lower_shadow(k1) or is_long_lower_shadow(k2))
    is_3_drop = (k0['close'] < k0['open']) and (k1['close'] < k1['open']) and (k2['close'] < k2['open'])
    # 🔥 修改：增加对放量下杀的过滤，防接飞刀
    is_volume_surge_short = k0['volume'] > avg_vol_3 * 2.0
    pb_short_cond_volume_1 = not (
                is_3_drop and (k0['volume'] < k1['volume'] < k2['volume'])) and not is_volume_surge_short
    is_volume_rise_short = k0['volume'] > avg_vol_3 * 1.2
    is_slow_drop = abs(k0['close'] - k0['open']) < avg_body_3 * 0.6
    pb_short_cond_volume_2 = not (is_volume_rise_short and is_slow_drop)
    pb_short_cond_volume = pb_short_cond_volume_1 and pb_short_cond_volume_2
    pb_short_cond_kdj_dir = j0 < j1
    current_j_down = j1 - j0 if pb_short_cond_kdj_dir else 0
    avg_j_down = calc_avg_j_deltas(j_list, 'down')
    pb_short_cond_kdj_force = current_j_down >= (avg_j_down * 0.1) if avg_j_down > 0 else pb_short_cond_kdj_dir
    pb_short_cond_flow = is_sell_dominant() or bias == "bear"
    pullback_short_ok = (
            pb_short_cond_trend and pb_short_cond_bias and pb_short_cond_shadow and
            pb_short_cond_volume and pb_short_cond_kdj_dir and pb_short_cond_kdj_force and
            pb_short_cond_flow and j_overbought
    )
    # ========== 新增：震荡区间识别与条件放宽 ==========
    is_range_scalp = False
    upper_range = 0.0
    lower_range = 0.0
    long_met_count = 0
    short_met_count = 0
    lookback = 5
    atr_mult = 0.7
    last2_mult = 0.4
    if len(kline_1m_closed) >= lookback:
        mids = [(k['high'] + k['low']) / 2 for k in kline_1m_closed[-lookback:]]
        mid_mean = sum(mids) / len(mids)
        atr = compute_atr(kline_1m_closed, period=7)
        if atr > 0:
            upper_all = mid_mean + atr_mult * atr
            lower_all = mid_mean - atr_mult * atr
            upper_last2 = mid_mean + last2_mult * atr
            lower_last2 = mid_mean - last2_mult * atr
            good_count = sum(1 for m in mids if lower_all <= m <= upper_all)
            ok_group = good_count >= 3
            ok_last2 = (lower_last2 <= mids[-2] <= upper_last2) and (lower_last2 <= mids[-1] <= upper_last2)
            if ok_group and ok_last2:
                is_range_scalp = True
                upper_range = upper_all
                lower_range = lower_range
    long_ok = pullback_long_ok
    short_ok = pullback_short_ok
    # 震荡模式下放宽条件：满足3个即可
    if is_range_scalp:
        long_conds = [
            pb_long_cond_trend, pb_long_cond_bias, pb_long_cond_shadow,
            pb_long_cond_volume, pb_long_cond_kdj_dir, pb_long_cond_kdj_force, pb_long_cond_flow
        ]
        long_met_count = sum(long_conds)
        short_conds = [
            pb_short_cond_trend, pb_short_cond_bias, pb_short_cond_shadow,
            pb_short_cond_volume, pb_short_cond_kdj_dir, pb_short_cond_kdj_force, pb_short_cond_flow
        ]
        short_met_count = sum(short_conds)
        if not long_ok and long_met_count >= 3 and j_oversold:
            long_ok = True
            current_entry_strategy = "震荡放宽做多"
        if not short_ok and short_met_count >= 3 and j_overbought:
            short_ok = True
            current_entry_strategy = "震荡放宽做空"
    # ========== 日志打印 ==========
    if now - last_entry_log_time >= 2:
        log_long = (
            f"[开多条件] 15m阳/震:{'✅' if allow_long else '❌'} | J超卖≤30:{'✅' if j_oversold else '❌'} | "
            f"【剃头皮】盘口非空:{'✅' if pb_long_cond_bias else '❌'} 无上影:{'✅' if pb_long_cond_shadow else '❌'} 非缩量涨:{'✅' if pb_long_cond_volume else '❌'} J升:{'✅' if pb_long_cond_kdj_dir else '❌'} J力度:{'✅' if pb_long_cond_kdj_force else '❌'} 买主导:{'✅' if pb_long_cond_flow else '❌'} | "
            f"【震荡识别】{'✅震荡中(满足' + str(long_met_count) + '个)' if is_range_scalp else '❌无震荡'}"
        )
        log_short = (
            f"[开空条件] 15m阴/震:{'✅' if allow_short else '❌'} | J超买≥70:{'✅' if j_overbought else '❌'} | "
            f"【剃头皮】盘口非多:{'✅' if pb_short_cond_bias else '❌'} 无下影:{'✅' if pb_short_cond_shadow else '❌'} 防放量杀:{'✅' if not is_volume_surge_short else '❌'} 非缩量跌:{'✅' if pb_short_cond_volume else '❌'} J降:{'✅' if pb_short_cond_kdj_dir else '❌'} J力度:{'✅' if pb_short_cond_kdj_force else '❌'} 卖主导:{'✅' if pb_short_cond_flow else '❌'} | "
            f"【震荡识别】{'✅震荡中(满足' + str(short_met_count) + '个)' if is_range_scalp else '❌无震荡'}"
        )
        log(log_long);
        log(log_short);
        log("-" * 120)
        last_entry_log_time = now
    # ========== 执行下单 ==========
    if long_ok:
        mode = current_entry_strategy if current_entry_strategy else "向上剃头皮"
        current_entry_strategy = mode
        is_current_range = is_range_scalp  # 记录是否为震荡模式开仓
        if is_range_scalp and lower_range > 0:
            target_price = normalize_price(lower_range)  # 挂在震荡下轨
            log(f"🚀 开多 [{mode}] J={j0:.1f} | 震荡低点挂单:{target_price} (下轨:{lower_range:.1f})")
            send_limit_order("BUY", target_price)
        else:
            log(f"🚀 开多 [{mode}] J={j0:.1f} | 价格:{best_bid + PRICE_STEP}")
            send_limit_order("BUY", best_bid + PRICE_STEP)
        return
    if short_ok:
        mode = current_entry_strategy if current_entry_strategy else "向下剃头皮"
        current_entry_strategy = mode
        is_current_range = is_range_scalp  # 记录是否为震荡模式开仓
        if is_range_scalp and upper_range > 0:
            target_price = normalize_price(upper_range)  # 挂在震荡上轨
            log(f"🔻 开空 [{mode}] J={j0:.1f} | 震荡高点挂单:{target_price} (上轨:{upper_range:.1f})")
            send_limit_order("SELL", target_price)
        else:
            log(f"🔻 开空 [{mode}] J={j0:.1f} | 价格:{best_ask - PRICE_STEP}")
            send_limit_order("SELL", best_ask - PRICE_STEP)
        return


def process_price():
    global current_price, raw_price, price_buffer, last_price_print_time
    global loss_count, last_check_time, processing, last_holding_log_time
    global local_max_profit_pct, local_max_price, local_min_price, max_profit_pct, max_price_since_entry, min_price_since_entry
    global time_at_max_profit
    if processing: return
    processing = True
    try:
        now = time.time()
        if now - last_price_print_time >= 5:
            log(f"[价格] {raw_price}");
            last_price_print_time = now
        if loss_count >= MAX_CONTINUOUS_LOSS:
            if now - loss_reset_time < LOSS_RESET_SEC: return
            loss_count = 0
        should_close = False;
        close_reason = ""
        with lock:
            if USE_LOCAL_SIMULATION:
                cur_pos = local_position;
                cur_ep = local_entry_price
            else:
                cur_pos = position;
                cur_ep = entry_price
            if cur_pos and cur_ep > 0:
                pr = (raw_price - cur_ep) / cur_ep if cur_pos == "long" else (cur_ep - raw_price) / cur_ep
                # 🔥 修改：动态获取1个ATR作为硬止损
                current_atr = compute_atr(kline_1m_closed, period=7)
                atr_stop_loss_pct = current_atr / cur_ep if cur_ep > 0 and current_atr > 0 else 0.003  # 若ATR无效，兜底0.3%
                if pr <= -atr_stop_loss_pct:
                    should_close = True;
                    close_reason = f"1个ATR硬止损(ATR={current_atr:.1f}, 阈值={atr_stop_loss_pct:.2%})"
                if USE_LOCAL_SIMULATION:
                    if cur_pos == "long":
                        if raw_price > local_max_price: local_max_price = raw_price; time_at_max_profit = time.time()
                    else:
                        if raw_price < local_min_price: local_min_price = raw_price; time_at_max_profit = time.time()
                    cur_max_profit_pct = (local_max_price - cur_ep) / cur_ep if cur_pos == "long" else (
                                                                                                               cur_ep - local_min_price) / cur_ep
                else:
                    if cur_pos == "long":
                        if raw_price > max_price_since_entry: max_price_since_entry = raw_price; time_at_max_profit = time.time()
                    else:
                        if raw_price < min_price_since_entry: min_price_since_entry = raw_price; time_at_max_profit = time.time()
                    cur_max_profit_pct = (max_price_since_entry - cur_ep) / cur_ep if cur_pos == "long" else (
                                                                                                                     cur_ep - min_price_since_entry) / cur_ep
                # 🔥 修改：提高盈亏比，有效止盈门槛提高到0.05%，回撤50%强制平仓
                cond_profit_valid = cur_max_profit_pct > 0.0005
                cond_retracement_50 = pr <= cur_max_profit_pct * 0.5
                if not should_close and cond_profit_valid and cond_retracement_50:
                    should_close = True;
                    close_reason = f"🔥 高优先级止盈：盈利回撤50%强制平仓 | 最高:{cur_max_profit_pct:.2%} 当前:{pr:.2%}"
                time_since_max = time.time() - time_at_max_profit if time_at_max_profit > 0 else 0
                cond_no_new_high = time_since_max > 20
                cond_retracement = pr <= cur_max_profit_pct * 0.8
                if not should_close and cond_profit_valid and cond_no_new_high and cond_retracement:
                    should_close = True;
                    close_reason = f"回撤止盈(最高{cur_max_profit_pct:.2%}回落至{pr:.2%}, 距最高盈利{time_since_max:.1f}秒)"
                if now - last_holding_log_time >= 2:
                    retracement_ratio_str = "N/A"
                    if cur_max_profit_pct > 0: retracement_ratio = pr / cur_max_profit_pct; retracement_ratio_str = f"{retracement_ratio:.2%}"
                    log(
                        f"[止盈止损状态] {cur_pos} | 当前盈亏:{pr:.2%} | 最高盈利:{cur_max_profit_pct:.2%} | "
                        f"盈利>0.05%:{'✅' if cond_profit_valid else '❌'} | "
                        f"超20秒无新高:{'✅' if cond_no_new_high else '❌'} | "
                        f"回撤比(当前/最高):{retracement_ratio_str}≤50%:{'✅' if cond_retracement_50 else '❌'} | "
                        f"距最高盈利时间:{time_since_max:.1f}秒"
                    );
                    last_holding_log_time = now
        if should_close: close_position(close_reason); return
        if time.time() - last_check_time > 3: check_order_filled(); last_check_time = time.time()
        check_fast_cancel();
        check_order_timeout()
        has_order = local_active_order if USE_LOCAL_SIMULATION else active_order
        if has_order: return
        v18_entry_logic()
    finally:
        processing = False


def trade_worker():
    while True:
        try:
            task = trade_queue.get()
            if task == "PROCESS":
                process_price()
                with trade_queue.mutex: trade_queue.queue.clear()
        except Exception as e:
            log(f"[交易线程异常] {e}")


# ======================
# WebSocket 相关
# ======================
def on_open(ws):
    global reconnect_delay
    reconnect_delay = 5
    log(f"[WebSocket] 连接成功 ✅ (模式: {'本地撮合-实盘行情' if USE_LOCAL_SIMULATION else 'API测试网'})")
    sync_position()
    params = [
        f"{WS_SYMBOL}@aggTrade", f"{WS_SYMBOL}@markPrice", f"{WS_SYMBOL}@depth5@100ms",
        f"{WS_SYMBOL}@kline_1m", f"{WS_SYMBOL}@kline_5m", f"{WS_SYMBOL}@kline_15m",
        f"{WS_SYMBOL}@kline_30m", f"{WS_SYMBOL}@kline_1h"
    ]
    sub_msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
    ws.send(json.dumps(sub_msg))


def on_message(ws, msg):
    global orderbook, raw_price, current_price, last_trade_side, last_ws_time, kline_1m_closed, kline_15m_closed
    last_ws_time = time.time()
    try:
        data = json.loads(msg)
        if "stream" in data and "data" in data: data = data["data"]
        if "result" in data and data.get("id") == 1: log("[WebSocket] 行情订阅成功 ✅"); return
        if data.get("e") == "markPriceUpdate":
            raw_price = float(data["p"]);
            current_price = raw_price
            with lock:
                price_buffer.append(raw_price)
                if len(price_buffer) > WINDOW: price_buffer.pop(0)
                tick_buffer.append(raw_price)
                if len(tick_buffer) > 5: tick_buffer.pop(0)
            trade_queue.put("PROCESS")
        if data.get("e") == "depthUpdate":
            orderbook["bids"] = data["b"];
            orderbook["asks"] = data["a"]
        if data.get("e") == "aggTrade":
            last_trade_side = "sell" if data["m"] else "buy"
            raw_price = float(data["p"]);
            current_price = raw_price
            update_trade_volume(last_trade_side, data["q"])
            with lock:
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


def on_close(ws, close_status_code, close_msg): log(f"[WebSocket] 断开，自动重连中...")


def ws_watchdog():
    global last_ws_time
    while True:
        time.sleep(5)
        if time.time() - last_ws_time > 10: log("⚠️ WebSocket可能断流（10秒无数据）")


def on_error(ws, error): log(f"[WebSocket 错误] {error}")


def start_ws():
    def run():
        global reconnect_delay
        while True:
            try:
                ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message, on_error=on_error,
                                            on_close=on_close)
                ws.run_forever(ping_interval=30, ping_timeout=15, ping_payload="ping")
            except Exception as e:
                log(f"[WebSocket 异常] {e}")
            reconnect_delay = min(reconnect_delay * 1.5, 30)
            time.sleep(reconnect_delay)

    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    log("======================================")
    mode_str = "本地撮合 (实盘行情)" if USE_LOCAL_SIMULATION else "API真实查询 (测试网)"
    log(f"        V20 纯剃头皮策略 启动成功")
    log(f"        当前运行模式: {mode_str}")
    log(f"        策略: 拐点回调（剃头皮）| 仓位: 总资金 {POSITION_RATIO * 100:.0f}% | 硬止损: 1 ATR")
    log("======================================")
    sync_binance_time()
    fetch_initial_klines()
    sync_balance()
    sync_position()
    start_ws()
    threading.Thread(target=trade_worker, daemon=True).start()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=ws_watchdog, daemon=True).start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log("[程序退出]")