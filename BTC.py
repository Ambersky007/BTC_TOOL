# -*- coding: utf-8 -*-
# 编码声明：确保文件支持中文，Python识别UTF-8格式
"""
盈利 ≥ 1.5% 立即市价平仓
优先级最高：比回撤止盈、硬止损都优先触发
开多价格 = 最近 3 根收盘 1 分钟 K 线的 最低点（下沿）
开空价格 = 最近 3 根收盘 1 分钟 K 线的 最高点（上沿）
挂单超时从 35s → 120s
禁用了盘口价格偏移就撤单697行，添加了return
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
# 生成日志文件名：trade_log_日期_时间.log
log_filename = f"trade_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logger = logging.getLogger()  # 创建日志对象
logger.setLevel(logging.INFO)  # 日志级别：INFO（记录普通信息）
logger.handlers.clear()  # 清空默认处理器，避免重复日志
# 创建文件处理器：写入日志文件，编码UTF-8支持中文
file_handler = logging.FileHandler(log_filename, encoding='utf-8')
# 日志格式：时间 | 日志内容
formatter = logging.Formatter('%(asctime)s | %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)  # 把文件处理器添加到日志对象


# 自定义日志函数：同时打印控制台+写入文件
def log(msg):
    try:
        print(msg)  # 控制台打印
        logger.info(str(msg))  # 写入日志文件
    except Exception as e:  # 异常捕获：防止日志报错导致程序崩溃
        print(f"[LOG ERROR] {e} | 原始: {msg}")


# ======================
# 语音提醒已移除
# ======================
# ======================
# 币安配置与全局开关
# ======================
API_KEY = "Nzv1A7tK7kGpFXyJobupTljiWIVu5EulI6oDHL22g8Oxu0a7nckNbU6tIkrJ1jbX"  # 币安API Key
SECRET_KEY = "rwfqhUnk2tVocsufVoPr0TeJXlmOMJRdeG52OS6bYMNxSdmK83rcoo80axRMm8aN"  # 币安API Secret
# 🔥🔥🔥 全局模式切换开关 🔥🔥🔥
# True = 本地撮合模式 (使用实盘行情，本地模拟成交与持仓统计)
# False = API真实模式 (使用测试网行情与API查询持仓)
USE_LOCAL_SIMULATION = True
LEVERAGE = 20  # 本地撮合模式下的假设杠杆倍数，用于计算ROI
# 测试网配置（API模式时使用）
BASE_URL_TESTNET = "https://testnet.binancefuture.com"
WS_URL_TESTNET = "wss://stream.binancefuture.com/stream"
# 实战网配置（本地撮合模式获取真实行情时使用）
BASE_URL_REAL = "https://fapi.binance.com"
WS_URL_REAL = "wss://fstream.binance.com/stream"
# 根据开关自动选择配置
BASE_URL = BASE_URL_REAL if USE_LOCAL_SIMULATION else BASE_URL_TESTNET
WS_URL = WS_URL_REAL if USE_LOCAL_SIMULATION else WS_URL_TESTNET
SYMBOL = "BTCUSDT"  # 交易对：BTC/USDT 永续合约
WS_SYMBOL = "btcusdt"  # WebSocket专用符号：小写格式
# ======================
# 策略参数（完全保留）
# ======================
WINDOW = 10 # 价格缓存窗口：保存最近10个价格，越大越迟顿
MAX_RANGE = 0.0003  # 最大价格区间（策略备用）
MIN_PROFIT = 0.0003  # 最小盈利阈值
TRAILING_PROFIT = 0.0003  # 移动止盈阈值
STOP_LOSS = 0.0003  # 止损阈值
MAX_HOLD_TIME = 300  # 最大持仓时间：300秒强制平仓
ORDER_TIMEOUT = 120  # 挂单超时：120秒未成交自动撤单
SLIPPAGE = 5  # 滑点（备用）
PRICE_STEP = 0.5  # 价格步长：BTC价格精度0.5
MAX_CONTINUOUS_LOSS = 3  # 最大连续亏损次数：3次后暂停交易
LOSS_RESET_SEC = 300  # 亏损冷却时间：300秒后恢复交易
last_ws_time = 0  # WebSocket最后接收数据时间（心跳监控）
trade_buffer = []  # 成交数据缓存（备用）
VOLUME_WINDOW = 0.8  # 成交量窗口（备用）
VOLUME_THRESHOLD = 2.5  # 成交量阈值（备用）
FAST_CANCEL_SEC = 2  # 快速撤单时间（备用）
cooldown_until = 0  # 交易冷却时间：冷却期内不允许开仓
COOLDOWN_SEC = 2  # 冷却时长：平仓后2秒不允许开仓
# 🔥🔥🔥 核心修改：仓位与资金管理 🔥🔥🔥
INITIAL_BALANCE = 10000.0  # 本地撮合模式初始模拟资金(USDT)
POSITION_RATIO = 0.3  # 开仓占总资金比例：30%
LOT_SIZE = 0.001  # BTCUSDT下单步长(精度)，请根据实际情况修改，币安BTCUSDT通常是0.001
account_balance = INITIAL_BALANCE  # 全局账户余额(本地模式为初始值，API模式会动态同步)
QTY = 0.01  # 保留作为最小默认值备用，实际开仓会使用动态计算
# ======================
# 全局变量 - API模式
# ======================
price_buffer = []  # 价格缓存列表：存储最近N个价格
tick_buffer = []  # 短期价格缓存：判断动量
position = None  # 当前持仓状态：None=无持仓 long=多单 short=空单
entry_price = 0  # 开仓均价
loss_count = 0  # 连续亏损次数
loss_reset_time = 0  # 亏损重置时间
entry_time = 0  # 开仓时间
current_price = None  # 当前标记价格
raw_price = None  # 原始成交价格
last_processed_price = None  # 最后处理价格（备用）
last_fill_time = 0  # 最后成交时间
last_check_time = 0  # 最后检查订单时间
# 🔥 核心修复1：使用 RLock 替代 Lock，防止同一线程死锁导致 WS 断线
lock = threading.RLock()  # 线程锁：防止多线程同时修改全局变量
active_order = None  # 当前挂单ID：有值=正在挂单
order_price = 0  # 挂单价格
order_time = 0  # 挂单时间
trend_buffer = []  # 趋势缓存（备用）
binance_time_offset = 0  # 本地时间与币安服务器时间差
orderbook = {"bids": [], "asks": []}  # 盘口数据：bids买一到买五 asks卖一到卖五
last_trade_side = "neutral"  # 最后一笔成交方向：buy/sell/neutral
pressure_start_time = 0  # 盘口压力开始时间
pressure_side = None  # 盘口压力方向
reconnect_delay = 5  # WebSocket重连延迟：初始5秒，指数递增
last_price_print_time = 0  # 最后打印价格时间
last_print_price = None  # 最后打印价格（备用）
trade_lock = False  # 交易锁：防止重复下单（核心风控）
last_entry_log_time = 0  # 上一次打印开仓条件时间
last_holding_log_time = 0  # 上一次打印持仓条件时间
# 新增：KDJ 相关
k_list = []
d_list = []
j_list = []
# 新增：最近成交方向与量能统计
trade_volume_buffer = []  # 存放格式: {'side': 'buy'/'sell', 'qty': float}
# 新增：1分钟K线缓存 (只保留最近3根已收盘的)
kline_1m_closed = []  # 存放格式: {'open':, 'high':, 'low':, 'close':, 'volume':}
# 新增：处理标志位
processing = False
# 🔥 核心修复3：新增交易队列，将耗时 REST API 操作移出 WebSocket 线程
trade_queue = queue.Queue()
# API模式止盈辅助变量
max_profit_pct = 0.0
max_price_since_entry = 0.0
min_price_since_entry = 999999.0
time_at_max_profit = 0.0  # 新增：记录达到最高盈利的时间点
# ======================
# 🔥 新增：全局变量 - 本地撮合模式
# ======================
local_position = None
local_entry_price = 0.0
local_active_order = None  # 格式: "BUY_70839.0" 或 "SELL_70839.0"
local_order_price = 0.0
local_order_time = 0.0
local_trade_lock = False
# 本地模式止盈辅助变量
local_max_profit_pct = 0.0
local_max_price = 0.0
local_min_price = 999999.0
# 🔥 新增：Excel 交易记录列表与部分成交状态
trade_records = []
partial_filled_flag = False  # 标记当前挂单是否发生过部分成交
actual_position_amt = 0.0  # 记录API模式下的实际持仓数量(兼容部分成交)，初始为0


# ======================
# 时间同步（保留，官方规范）
# ======================
# 同步本地时间与币安服务器时间：避免签名超时错误
def sync_binance_time():
    global binance_time_offset
    try:
        # 无论哪种模式，同步时间都向真实网请求，保证签名有效
        res = requests.get("https://fapi.binance.com/fapi/v1/time", timeout=3)
        server_time = res.json()["serverTime"]
        local_time = int(time.time() * 1000)
        # 计算时间差：本地时间+偏移量=服务器时间
        binance_time_offset = server_time - local_time
        log("[时间同步] 完成")
    except:
        binance_time_offset = 0  # 同步失败则使用本地时间


# 获取符合币安要求的时间戳（毫秒）
def get_timestamp():
    return int(time.time() * 1000) + binance_time_offset


# ======================
# 签名（官方规范：字母排序）
# ======================
# 终极修复 -1022 签名错误：统一生成签名 URL，杜绝 requests 自动编码和排序导致签名失败
def build_signed_url(base_url, params):
    # 1. 统一转为字符串，过滤 None，防止布尔值或浮点数引发隐藏bug
    str_params = {k: str(v) for k, v in params.items() if v is not None}
    # 2. 严格按照 ASCII 字母表排序拼接 (币安强制要求)
    query = '&'.join([f"{k}={str_params[k]}" for k in sorted(str_params)])
    # 3. HMAC SHA256 加密生成签名
    signature = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    # 4. 返回可直接请求的完整 URL
    return f"{base_url}?{query}&signature={signature}"


# 价格格式化：保留1位小数（符合BTC精度）
def format_price(p):
    return float(f"{p:.1f}")


# 价格标准化：按步长取整（防止下单价格不符合规则）
def normalize_price(price):
    return round(price / PRICE_STEP) * PRICE_STEP


# ======================
# 🔥 新增：动态仓位计算核心函数
# ======================
def get_dynamic_qty(price):
    """根据账户余额的30%和当前价格，计算下单数量，并按交易所步长向下取整"""
    if not price or price <= 0 or account_balance <= 0:
        return LOT_SIZE  # 异容处理返回最小步长
    # 目标名义价值 = 账户余额 * 30%
    target_notional = account_balance * POSITION_RATIO
    # 计算原始数量
    raw_qty = target_notional / price
    # 按步长向下取整 (例如 0.0234 -> 0.023)，防止报错 LOT_SIZE precision
    steps = math.floor(raw_qty / LOT_SIZE)
    final_qty = steps * LOT_SIZE
    # 极端情况保底：如果算出来小于1个步长，则赋值1个步长
    if final_qty < LOT_SIZE:
        final_qty = LOT_SIZE
    return final_qty


# 同步账户余额 (API模式向交易所请求，本地模式直接读初始值)
def sync_balance():
    global account_balance
    if USE_LOCAL_SIMULATION:
        account_balance = INITIAL_BALANCE
        log(f"[余额同步] 本地模式，初始资金: {account_balance} U")
        return
    # API模式：真实请求
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
# 盘口/交易函数（完全保留）
# ======================
# 判断盘口偏向：多头/空头/中性
def orderbook_bias():
    try:
        bids = orderbook["bids"][:3]  # 取买1-买3
        asks = orderbook["asks"][:3]  # 取卖1-卖3
        bids_vol = sum(float(b[1]) for b in bids)  # 买盘总成交量
        asks_vol = sum(float(a[1]) for a in asks)  # 卖盘总成交量
        # 买盘>卖盘1.5倍：多头强势
        if bids_vol > asks_vol * 1.5:
            return "bull"
        # 卖盘>买盘1.5倍：空头强势
        elif asks_vol > bids_vol * 1.5:
            return "bear"
        else:
            return "neutral"
    except:
        return "neutral"


# 盘口压力比率：卖盘总量/买盘总量
def orderbook_pressure_ratio():
    try:
        bids = orderbook["bids"][:3]
        asks = orderbook["asks"][:3]
        bv = sum(float(b[1]) for b in bids)
        av = sum(float(a[1]) for a in asks)
        return av / bv if bv > 0 else 999
    except:
        return 1


# 更新成交量（备用函数）
def update_trade_flow(p, q, is_sell): pass


# 获取成交量突增信号（备用函数）
def get_volume_spike(): return None


# ======================
# 🔥 新增：策略辅助函数
# ======================
def compute_kdj(prices, period=9, k_period=3, d_period=3):
    global k_list, d_list, j_list
    if len(prices) < period:
        return 50, 50, 50
    high = max(prices[-period:])
    low = min(prices[-period:])
    close = prices[-1]
    if high == low:
        rsv = 50
    else:
        rsv = (close - low) / (high - low) * 100
    if not k_list:
        k = rsv
        d = rsv
    else:
        k = (2 * k_list[-1] + rsv) / 3
        d = (2 * d_list[-1] + k) / 3
    j = 3 * k - 2 * d
    k_list.append(k)
    d_list.append(d)
    j_list.append(j)
    if len(k_list) > 20:
        k_list.pop(0)
        d_list.pop(0)
        j_list.pop(0)
    return k, d, j


# 判断长上影线
def is_long_upper_shadow(k):
    body = abs(k['close'] - k['open'])
    if body == 0: body = 0.0001
    upper_shadow = k['high'] - max(k['close'], k['open'])
    return upper_shadow > body * 2


# 判断长下影线
def is_long_lower_shadow(k):
    body = abs(k['close'] - k['open'])
    if body == 0: body = 0.0001
    lower_shadow = min(k['close'], k['open']) - k['low']
    return lower_shadow > body * 2


# 计算J值平均变动
def calc_avg_j_deltas(j_hist, direction='up'):
    if len(j_hist) < 2: return 0
    deltas = []
    for i in range(len(j_hist) - 1):
        delta = j_hist[i] - j_hist[i + 1]  # j_hist索引0为最新
        if direction == 'up' and delta > 0:
            deltas.append(delta)
        elif direction == 'down' and delta < 0:
            deltas.append(abs(delta))
    return sum(deltas) / len(deltas) if deltas else 0


# 更新逐笔成交带量数据
def update_trade_volume(side, qty):
    trade_volume_buffer.append({'side': side, 'qty': float(qty)})
    if len(trade_volume_buffer) > 10:  # 多缓存一点以防不够
        trade_volume_buffer.pop(0)


# 买量主导判断 (近5笔)
def is_buy_dominant():
    if len(trade_volume_buffer) < 5: return False
    buy_vol = sum(t['qty'] for t in trade_volume_buffer[-5:] if t['side'] == 'buy')
    sell_vol = sum(t['qty'] for t in trade_volume_buffer[-5:] if t['side'] == 'sell')
    return buy_vol > sell_vol


# 卖量主导判断 (近5笔)
def is_sell_dominant():
    if len(trade_volume_buffer) < 5: return False
    buy_vol = sum(t['qty'] for t in trade_volume_buffer[-5:] if t['side'] == 'buy')
    sell_vol = sum(t['qty'] for t in trade_volume_buffer[-5:] if t['side'] == 'sell')
    return sell_vol > buy_vol


# ======================
# 持仓/下单/撤单/平仓 (双模式整合)
# ======================
# 同步当前持仓状态：从币安服务器查询真实持仓
def sync_position():
    global position, entry_price, active_order, actual_position_amt
    if USE_LOCAL_SIMULATION:
        # 本地模式不需要向API同步，直接读取本地变量打印
        if local_position:
            log(f"[本地持仓] {local_position} | 数量:{actual_position_amt} | 均价:{local_entry_price}")
        else:
            log("[本地持仓] None")
        return
    # API模式原有逻辑
    try:
        url = f"{BASE_URL}/fapi/v2/positionRisk"
        p = {"timestamp": get_timestamp(), "recvWindow": 5000}
        url = build_signed_url(url, p)
        h = {"X-MBX-APIKEY": API_KEY}
        # 发送GET请求获取持仓数据
        d = requests.get(url, headers=h, timeout=3).json()
        # 核心修复：如果 API 返回错误字典，直接打印并返回，防止 string indices 报错
        if isinstance(d, dict) and d.get("code") is not None:
            log(f"[持仓同步失败] API返回错误: code={d.get('code')}, msg={d.get('msg')}")
            if d.get("code") == -1021:
                sync_binance_time()
            return
        # 遍历持仓列表，找到BTCUSDT
        for item in d:
            if item['symbol'] == SYMBOL:
                amt = float(item['positionAmt'])  # 持仓数量
                ep = float(item['entryPrice'])  # 开仓均价
                if amt != 0:  # 有持仓
                    position = "long" if amt > 0 else "short"
                    entry_price = ep
                    actual_position_amt = abs(amt)  # 🔥 更新实际持仓数量
                    active_order = None  # 有持仓则清空挂单
                    log(f"[API持仓] {position} | 数量:{actual_position_amt} | 均价:{entry_price}")
                else:  # 无持仓
                    position = None
                    entry_price = 0
                    actual_position_amt = 0.0  # 重置为0
                    log("[API持仓] None")
    except Exception as e:
        log(f"[持仓同步失败] {e}")


# 持仓监控线程：每8秒打印一次持仓和盈亏
def position_monitor():
    while True:
        try:
            # 定期同步余额和持仓，保证动态计算准确
            sync_balance()
            if USE_LOCAL_SIMULATION:
                pos = local_position
                ep = local_entry_price
                if pos:
                    amt = actual_position_amt if actual_position_amt > 0 else get_dynamic_qty(ep)
                    price = raw_price if raw_price else ep
                    notional = abs(amt) * price  # 持仓名义价值
                    # 计算浮盈
                    pnl = (price - ep) * amt if pos == "long" else (ep - price) * abs(amt)
                    # 币安标准 ROI
                    margin = abs(amt) * ep / LEVERAGE if LEVERAGE > 0 else 0
                    roi = pnl / margin if margin > 0 else 0
                    # 计算持仓时间
                    hold_time = time.time() - entry_time if entry_time > 0 else 0
                    log(f"[本地持仓监控] {pos} | 数量:{amt} | 价值:{notional:.2f}U | 均价:{ep:.1f} | 浮盈:{pnl:.2f}U | ROI:{roi:.2%} | 杠杆:{LEVERAGE}x | 持仓时间:{hold_time:.1f}秒")
                else:
                    log(f"[本地持仓监控] 无持仓 | 账户余额:{account_balance} U | 预计开仓量:{get_dynamic_qty(raw_price if raw_price else 60000)}")
            else:
                # API模式原有逻辑
                url = f"{BASE_URL}/fapi/v2/positionRisk"
                p = {"timestamp": get_timestamp(), "recvWindow": 5000}
                url = build_signed_url(url, p)
                h = {"X-MBX-APIKEY": API_KEY}
                d = requests.get(url, headers=h, timeout=3).json()
                # 核心修复：如果 API 返回错误字典，直接打印并跳过本次循环
                if isinstance(d, dict) and d.get("code") is not None:
                    log(f"[持仓监控异常] API返回错误: code={d.get('code')}, msg={d.get('msg')}")
                    if d.get("code") == -1021:
                        sync_binance_time()
                else:
                    for item in d:
                        if item['symbol'] == SYMBOL:
                            amt = float(item['positionAmt'])
                            ep = float(item['entryPrice'])
                            leverage = float(item['leverage'])  # 杠杆倍数
                            if amt != 0:  # 有持仓
                                pos = "long" if amt > 0 else "short"
                                price = raw_price if raw_price else ep
                                notional = abs(amt) * price
                                pnl = (price - ep) * amt if pos == "long" else (ep - price) * abs(amt)
                                margin = abs(amt) * ep / leverage if leverage > 0 else 0
                                roi = pnl / margin if margin > 0 else 0
                                # 计算持仓时间
                                hold_time = time.time() - entry_time if entry_time > 0 else 0
                                log(f"[API持仓监控] {pos} | 数量:{abs(amt)} | 价值:{notional:.2f}U | 均价:{ep:.1f} | 浮盈:{pnl:.2f}U | ROI:{roi:.2%} | 杠杆:{leverage}x | 持仓时间:{hold_time:.1f}秒")
                            else:
                                log(f"[API持仓监控] 无持仓 | 账户余额:{account_balance} U | 预计开仓量:{get_dynamic_qty(raw_price if raw_price else 60000)}")
        except Exception as e:
            log(f"[持仓监控异常] {e}")
        time.sleep(8)  # 每8秒执行一次


# 发送限价单：指定价格挂单
def send_limit_order(side, price, reduce=False):
    # 修复：将 global 声明移到函数最开头，避免分支导致 SyntaxError
    global local_active_order, local_order_time, local_order_price, local_trade_lock
    global active_order, order_time, order_price, trade_lock, partial_filled_flag
    if USE_LOCAL_SIMULATION:
        # 终极防重修复：非平仓单，只要有持仓或挂单或已上锁，直接拦截请求
        if not reduce and (local_position is not None or local_active_order is not None or local_trade_lock):
            log(f"[本地拦截重复下单] 侧边:{side} | 状态: 持仓={local_position}, 挂单={local_active_order}, 锁={local_trade_lock}")
            return
        local_trade_lock = True
        local_active_order = f"{side}_{format_price(price)}"
        local_order_time = time.time()
        local_order_price = price
        log(f"✅ 本地挂单成功 {side} | 价格:{price} | 预计数量:{get_dynamic_qty(price)}")
        return
    # API模式
    # 终极防重修复：非平仓单，只要有持仓或挂单或已上锁，直接拦截请求
    if not reduce and (position is not None or active_order is not None or trade_lock):
        log(f"[拦截重复下单] 侧边:{side} | 状态: 持仓={position}, 挂单={active_order}, 锁={trade_lock}")
        return
    # 核心修复：前置锁！在发起网络请求前瞬间上锁，彻底杜绝网络延迟导致的重复下单
    trade_lock = True
    partial_filled_flag = False  # 新下单重置部分成交标记
    # 🔥 核心修改：开仓用30%动态计算，平仓用实际持仓量
    order_qty = get_dynamic_qty(price) if not reduce else actual_position_amt
    if order_qty <= 0:
        log(f"[拦截下单] 数量为0 (余额:{account_balance}, 价格:{price})")
        trade_lock = False
        return
    url = f"{BASE_URL}/fapi/v1/order"
    # 组装下单参数
    p = {
        "symbol": SYMBOL,
        "side": side,  # 买卖方向：BUY/SELL
        "type": "LIMIT",  # 订单类型：限价单
        "timeInForce": "GTC",  # 生效时间：一直有效直至成交/撤单
        "quantity": str(order_qty),  # 下单数量 (动态)
        "price": str(format_price(price)),  # 下单价格
        "timestamp": get_timestamp(),
        "recvWindow": 5000  # 修复：补全容错窗口，防-1021报错
    }
    if reduce:  # 平仓单标记
        p["reduceOnly"] = "true"
    url = build_signed_url(url, p)
    headers = {"X-MBX-APIKEY": API_KEY}
    try:
        r = requests.post(url, headers=headers, timeout=3)
        res = r.json()
        # 下单成功：记录订单信息
        if "orderId" in res:
            active_order = res["orderId"]
            order_time = time.time()
            order_price = price
            log(f"✅ API挂单成功 {res} | 数量:{order_qty}")
        else:
            log(f"❌ API挂单失败返回: {res}")
            trade_lock = False  # 下单失败，释放锁
            # 如果是时间戳错误，立刻同步时间
            if isinstance(res, dict) and res.get("code") == -1021:
                sync_binance_time()
    except Exception as e:
        log(f"❌ API挂单异常: {e}")
        trade_lock = False  # 异常，释放锁


# 发送市价单：以当前市场价立即成交
def send_market_order(side, reduce=False, qty=None):
    # 修复：将 global 声明移到函数最开头
    global local_position, local_entry_price, local_trade_lock
    global local_max_profit_pct, local_max_price, local_min_price
    global entry_time, time_at_max_profit
    global active_order, actual_position_amt
    # 🔥 核心修改：如果是平仓，强制使用实际持仓量；如果是开仓，使用动态30%计算量
    if reduce:
        close_qty = actual_position_amt if actual_position_amt > 0 else qty
    else:
        close_qty = get_dynamic_qty(raw_price) if qty is None else qty
    if close_qty <= 0:
        log(f"[拦截市价单] 数量为0 (平仓:{reduce}, 余额:{account_balance}, 价格:{raw_price})")
        return False
    if USE_LOCAL_SIMULATION:
        if reduce:
            log(f"[本地市价平仓成功] {side} | 价格:{raw_price} | 数量:{close_qty}")
            local_position = None;
            local_entry_price = 0.0
            local_max_profit_pct = 0.0;
            local_max_price = 0.0;
            local_min_price = 999999.0
            local_trade_lock = False
            entry_time = 0
            time_at_max_profit = 0.0
            actual_position_amt = 0.0  # 重置本地持仓量
        else:
            log(f"[本地市价开仓成功] {side} | 价格:{raw_price} | 数量:{close_qty}")
            local_position = "long" if side == "BUY" else "short"
            local_entry_price = raw_price
            actual_position_amt = close_qty  # 记录开仓数量
            # 开仓时初始化极值价格为开仓价，确保最高盈利计算准确
            local_max_profit_pct = 0.0;
            local_max_price = raw_price;
            local_min_price = raw_price
            local_trade_lock = False
            # 记录开仓时间和最高盈利起点时间
            entry_time = time.time()
            time_at_max_profit = entry_time
        return True
    # API模式
    # 核心修复：必须在循环内部重新生成时间戳和签名，否则重试必定报 -1021/-1022
    for i in range(3):
        try:
            p = {
                "symbol": SYMBOL, "side": side, "type": "MARKET",
                "quantity": str(close_qty), "timestamp": get_timestamp(),  # 🔥 使用动态数量
                "recvWindow": 5000  # 修复：补全容错窗口
            }
            if reduce: p["reduceOnly"] = "true"
            url = build_signed_url(f"{BASE_URL}/fapi/v1/order", p)
            h = {"X-MBX-APIKEY": API_KEY}
            r = requests.post(url, headers=h, timeout=5)
            res = r.json()
            # 校验交易所是否确认下单成功
            if res.get("status") == "NEW" or res.get("orderId"):
                active_order = None
                log(f"[API市价平仓成功] {side} | 数量:{close_qty} | 订单ID: {res.get('orderId')}")
                return True
            else:
                log(f"[API市价下单异常] 返回: {res}")
                # 如果是时间戳错误，立刻同步时间
                if isinstance(res, dict) and res.get("code") == -1021:
                    sync_binance_time()
        except Exception as e:
            log(f"[API市价失败] 重试 {i + 1}/3 | 错误: {e}")
        time.sleep(0.5)  # 稍微等半秒再重试
    log("❌ API市价平仓彻底失败，请手动检查仓位！")
    sync_position()  # 彻底失败，强制向交易所同步真实仓位
    return False


# 撤销当前挂单
def cancel_order():
    # 修复：将 global 声明移到函数最开头
    global local_active_order, local_order_price, local_order_time, local_trade_lock
    global active_order, order_price, order_time, trade_lock, partial_filled_flag
    if USE_LOCAL_SIMULATION:
        if not local_active_order: return  # 无挂单直接返回
        local_active_order = None;
        local_order_price = 0;
        local_order_time = 0;
        local_trade_lock = False
        log("[本地撤单成功]")
        return
    # API模式
    if not active_order: return  # 无挂单直接返回
    try:
        p = {"symbol": SYMBOL, "orderId": active_order, "timestamp": get_timestamp(), "recvWindow": 5000}
        url = build_signed_url(f"{BASE_URL}/fapi/v1/order", p)
        h = {"X-MBX-APIKEY": API_KEY}
        res = requests.delete(url, headers=h, timeout=3).json()
        # 修复：校验撤单结果，只有确认撤单成功或订单已不存在，才清空本地状态，防止幽灵挂单
        if res.get("status") in ["CANCELED", "EXPIRED"] or res.get("code") == -2011:
            active_order = None;
            order_price = 0;
            order_time = 0;
            trade_lock = False;
            partial_filled_flag = False
            # 撤单后同步真实仓位，确保止盈止损基于正确的数量运行
            sync_position()
            log("[API撤单成功]")
        else:
            log(f"[API撤单失败] 保留挂单状态, 返回: {res}")
    except Exception as e:
        log(f"[API撤单异常] 保留挂单状态, 错误: {e}")
        # 异常时绝不能清空 active_order，否则会变成幽灵挂单


# 快速撤单：挂单价格偏离盘口过大立即撤单
def check_fast_cancel():
  """  # 修复：将 global 声明移到函数最开头
    global local_order_price, order_price
    if USE_LOCAL_SIMULATION:
        if not local_active_order: return
        try:
            if not orderbook["bids"] or not orderbook["asks"]: return
            best_bid = float(orderbook["bids"][0][0])
        except:
            return
        # 挂单价格与买一价差超过50个步长，撤单
        if abs(local_order_price - best_bid) > 50 * PRICE_STEP:
            cancel_order()
    else:
        if not active_order: return
        try:
            if not orderbook["bids"] or not orderbook["asks"]: return
            best_bid = float(orderbook["bids"][0][0])
        except:
            return
        # 挂单价格与买一价差超过50个步长，撤单
        if abs(order_price - best_bid) > 50 * PRICE_STEP:
            cancel_order()
"""

  return
    # 挂单超时撤单：部分成交等5秒，未成交等10秒
def check_order_timeout():
    if USE_LOCAL_SIMULATION:
        if local_active_order and time.time() - local_order_time > ORDER_TIMEOUT:
            cancel_order()
    else:
        if active_order:
            # 🔥 部分成交等5秒，未成交等10秒
            timeout = 5 if partial_filled_flag else ORDER_TIMEOUT
            if time.time() - order_time > timeout:
                cancel_order()


# 检查订单是否成交
def check_order_filled():
    # 修复：将 global 声明移到函数最开头，彻底解决 SyntaxError
    global local_active_order, local_position, local_entry_price, local_trade_lock
    global local_max_profit_pct, local_max_price, local_min_price
    global entry_time, time_at_max_profit
    global active_order, last_fill_time, trade_lock, max_profit_pct, max_price_since_entry, min_price_since_entry
    global partial_filled_flag, actual_position_amt
    if USE_LOCAL_SIMULATION:
        if not local_active_order: return
        side, price_str = local_active_order.split("_")
        order_p = float(price_str)
        # 本地撮合成交判断：BUY单，当前价跌破挂单价视为成交；SELL单，当前价突破挂单价视为成交
        filled = False
        if side == "BUY" and raw_price <= order_p:
            filled = True
        elif side == "SELL" and raw_price >= order_p:
            filled = True
        if filled:
            local_position = "long" if side == "BUY" else "short"
            local_entry_price = order_p
            actual_position_amt = get_dynamic_qty(order_p)  # 🔥 记录本地开仓数量
            local_active_order = None
            local_trade_lock = False
            # 开仓时初始化极值价格为开仓价，确保最高盈利计算准确
            local_max_profit_pct = 0.0;
            local_max_price = order_p;
            local_min_price = order_p
            # 记录开仓时间和最高盈利起点时间
            entry_time = time.time()
            time_at_max_profit = entry_time
            log(f"🎉 [本地限价单成交] {side} | 成交价:{order_p} | 数量:{actual_position_amt}")
        return
    # API模式
    if not active_order: return
    try:
        p = {"symbol": SYMBOL, "orderId": active_order, "timestamp": get_timestamp(), "recvWindow": 5000}
        url = build_signed_url(f"{BASE_URL}/fapi/v1/order", p)
        h = {"X-MBX-APIKEY": API_KEY}
        d = requests.get(url, headers=h, timeout=3).json()
        # 🔥 全部成交
        if d.get("status") == "FILLED":
            active_order = None;
            trade_lock = False;
            partial_filled_flag = False
            last_fill_time = time.time();
            entry_time = time.time()
            max_profit_pct = 0.0;
            max_price_since_entry = entry_price;
            min_price_since_entry = entry_price
            time_at_max_profit = entry_time
            sync_position()  # 同步最新持仓与实际数量
        # 🔥 部分成交处理：保留持仓继续止盈止损，未成交部分等5秒后撤单
        elif d.get("status") == "PARTIALLY_FILLED":
            if not partial_filled_flag:
                partial_filled_flag = True
                order_time = time.time()  # 重置计时器，开始5秒倒计时
                log(f"⚠️ [订单部分成交] 已成交:{d.get('executedQty')}, 剩余挂单等5秒后撤单，持仓进入止盈止损！")
                # 立即同步持仓，让止盈止损基于实际数量开始工作
                sync_position()
                entry_time = time.time()
                max_profit_pct = 0.0;
                max_price_since_entry = entry_price;
                min_price_since_entry = entry_price
                time_at_max_profit = entry_time
        elif isinstance(d, dict) and d.get("code") == -1021:
            log(f"[查单失败] API返回时间戳错误，正在同步时间")
            sync_binance_time()
    except:
        pass


# ======================
# 策略逻辑
# ======================
# 检测价格区间：返回最近N个价格的最高价、最低价
def detect_zone():
    if len(price_buffer) < WINDOW: return None
    return max(price_buffer), min(price_buffer)


# 更新短期价格缓存：用于判断动量
def update_tick(p):
    tick_buffer.append(p)
    if len(tick_buffer) > 5: tick_buffer.pop(0)


# 动量判断：价格趋势是否符合开仓方向
def momentum_ok(side):
    try:
        avg = sum(tick_buffer[-3:]) / 3
        return current_price > avg if side == "long" else current_price < avg
    except:
        return True


# 判断趋势：上涨/下跌/横盘（≈1分钟周期）
def get_trend():
    try:
        avg = sum(price_buffer[-10:]) / 10
        return "up" if current_price > avg else "down" if current_price < avg else "neutral"
    except:
        return "neutral"


# 🔥 新增：保存交易记录到Excel (智能兜底CSV)
def save_trade_records():
    if not trade_records:
        return
    try:
        df = pd.DataFrame(trade_records)
        # 优先尝试保存为xlsx
        try:
            filename = "trade_records.xlsx"
            df.to_excel(filename, index=False)
        except ImportError:
            # 如果没有openpyxl，降级保存为csv
            filename = "trade_records.csv"
            df.to_csv(filename, index=False, encoding='utf-8-sig')
            log("[存储降级] 未安装openpyxl，交易记录已保存为CSV格式。如需Excel格式，请执行: pip install openpyxl")
    except Exception as e:
        log(f"[记录保存失败] {e}")


# 平仓函数：市价平仓，重置持仓状态，统计盈亏
def close_position(reason):
    global loss_count, cooldown_until, loss_reset_time, entry_time, time_at_max_profit
    if USE_LOCAL_SIMULATION:
        pos = local_position;
        ep = local_entry_price
    else:
        pos = position;
        ep = entry_price
    with lock:
        if not pos: return
    # 计算收益率
    pnl = (raw_price - ep) / ep if pos == "long" else (ep - raw_price) / ep
    # 🔥 新增：在平仓前记录交易数据 (兼容部分成交数量)
    if entry_time > 0:
        close_qty = actual_position_amt if actual_position_amt > 0 else get_dynamic_qty(ep)
        pnl_abs = (raw_price - ep) * close_qty if pos == "long" else (ep - raw_price) * close_qty
        margin = abs(close_qty) * ep / LEVERAGE if LEVERAGE > 0 else 0
        roi = pnl_abs / margin if margin > 0 else 0
        record = {
            "开仓时间": datetime.fromtimestamp(entry_time).strftime('%Y-%m-%d %H:%M:%S'),
            "平仓时间": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "方向": "多" if pos == "long" else "空",
            "数量": close_qty,
            "开仓价格": ep,
            "平仓价格": raw_price,
            "盈亏(U)": round(pnl_abs, 2),
            "收益率": f"{roi:.2%}",
            "平仓原因": reason
        }
        trade_records.append(record)
        save_trade_records()  # 每次平仓后保存到Excel/CSV
    # 亏损：连续亏损次数+1
    if pnl < 0:
        loss_count += 1
        if loss_count >= MAX_CONTINUOUS_LOSS:
            loss_reset_time = time.time()
    else:
        loss_count = 0  # 盈利：重置亏损次数
    log(f"[平仓触发] {reason} | 当前盈亏:{pnl:.2%} | 平仓数量:{close_qty}")
    cancel_order()  # 先撤掉未成交的挂单
    # 核心修复：防阻塞强制平仓机制（保留本地模式简化，API模式完整重试）
    if USE_LOCAL_SIMULATION:
        success = send_market_order("SELL" if pos == "long" else "BUY", True)
        if success:
            cooldown_until = time.time() + COOLDOWN_SEC
            entry_time = 0
            time_at_max_profit = 0.0
            log(f"[平仓完成] 状态已重置")
    else:
        retry_count = 0
        MAX_RETRY = 10
        while retry_count < MAX_RETRY:
            retry_count += 1
            success = send_market_order("SELL" if pos == "long" else "BUY", True)
            if success:
                cooldown_until = time.time() + COOLDOWN_SEC
                trade_lock = False
                entry_time = 0
                time_at_max_profit = 0.0
                log(f"[平仓完成] 状态已重置 (共尝试 {retry_count} 次)")
                return
            else:
                log(f"⚠️ 平仓失败，3秒后发起第 {retry_count + 1} 次强平！")
                sync_position()  # 强制同步真实仓位
                if not position:  # 同步发现其实已经平仓了，直接退出
                    trade_lock = False
                    entry_time = 0
                    time_at_max_profit = 0.0
                    return
                time.sleep(3)
        log("🚨🚨🚨 严重警告：连续10次强平失败，交易线程放弃重试，请人工干预！🚨🚨🚨")
        trade_lock = False


# 🔥🔥🔥 终极强化版开仓逻辑 (完全重写)
def v18_entry_logic():
    global cooldown_until, loss_count, last_entry_log_time
    now = time.time()
    # 统一拦截判断
    if USE_LOCAL_SIMULATION:
        if local_trade_lock or local_position is not None or local_active_order is not None: return
        if now - local_order_time < 2: return
    else:
        if trade_lock or position is not None or active_order is not None: return
        if now - order_time < 2: return
    # 基础条件
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
    # 必须有3根已收盘1分钟K线数据
    if len(kline_1m_closed) < 3: return
    k0, k1, k2 = kline_1m_closed[-1], kline_1m_closed[-2], kline_1m_closed[-3]
    # 趋势过滤（顺势核心）
    trend = get_trend()
    allow_long = trend in ["up", "neutral"]
    allow_short = trend in ["down", "neutral"]
    # 盘口
    bias = orderbook_bias()
    # KDJ
    if len(j_list) < 2: return
    j0, j1 = j_list[-1], j_list[-2]
    # ============ 开多条件判定 ============
    long_cond_trend = allow_long
    long_cond_bias = bias != "bear"
    # 避雷：近3根无长上影线
    long_cond_shadow = not (is_long_upper_shadow(k0) or is_long_upper_shadow(k1) or is_long_upper_shadow(k2))
    # 量能：非连续缩量上涨
    is_3_rise = (k0['close'] > k0['open']) and (k1['close'] > k1['open']) and (k2['close'] > k2['open'])
    is_3_vol_down = (k0['volume'] < k1['volume']) and (k1['volume'] < k2['volume'])
    long_cond_volume = not (is_3_rise and is_3_vol_down)
    # KDJ：J <= 50, 上升, 力度达标
    long_cond_kdj_dir = j0 > j1
    long_cond_kdj_val = j0 <= 70
    current_j_up = j0 - j1 if long_cond_kdj_dir else 0
    avg_j_up = calc_avg_j_deltas(j_list, 'up')
    long_cond_kdj_force = current_j_up >= (avg_j_up * 0.2) if avg_j_up > 0 else long_cond_kdj_dir
    # 订单流：买量>卖量
    long_cond_flow = is_buy_dominant()
    long_ok = long_cond_trend and long_cond_bias and long_cond_shadow and long_cond_volume and long_cond_kdj_val and long_cond_kdj_dir and long_cond_kdj_force and long_cond_flow
    # ============ 开空条件判定 ============
    short_cond_trend = allow_short
    short_cond_bias = bias != "bull"
    # 避雷：近3根无长下影线
    short_cond_shadow = not (is_long_lower_shadow(k0) or is_long_lower_shadow(k1) or is_long_lower_shadow(k2))
    # 量能：非连续缩量下跌
    is_3_drop = (k0['close'] < k0['open']) and (k1['close'] < k1['open']) and (k2['close'] < k2['open'])
    short_cond_volume_1 = not (is_3_drop and is_3_vol_down)
    # 量能：非放量滞跌
    avg_vol_3 = (k0['volume'] + k1['volume'] + k2['volume']) / 3.0
    avg_body_3 = (abs(k0['close'] - k0['open']) + abs(k1['close'] - k1['open']) + abs(k2['close'] - k2['open'])) / 3.0
    is_volume_rise_short = k0['volume'] > avg_vol_3 * 1.2
    is_slow_drop = abs(k0['close'] - k0['open']) < avg_body_3 * 0.6
    short_cond_volume_2 = not (is_volume_rise_short and is_slow_drop)
    short_cond_volume = short_cond_volume_1 and short_cond_volume_2
    # KDJ：J >= 50, 下降, 力度达标
    short_cond_kdj_dir = j0 < j1
    short_cond_kdj_val = j0 >= 30
    current_j_down = j1 - j0 if short_cond_kdj_dir else 0
    avg_j_down = calc_avg_j_deltas(j_list, 'down')
    short_cond_kdj_force = current_j_down >= (avg_j_down * 0.2) if avg_j_down > 0 else short_cond_kdj_dir
    # 订单流：卖量>买量
    short_cond_flow = is_sell_dominant()
    short_ok = short_cond_trend and short_cond_bias and short_cond_shadow and short_cond_volume and short_cond_kdj_val and short_cond_kdj_dir and short_cond_kdj_force and short_cond_flow
    # 构造日志
    if now - last_entry_log_time >= 2:
        log_long = (
            f"[开多条件] 顺势:{'✅' if long_cond_trend else '❌'} | "
            f"盘口非空:{'✅' if long_cond_bias else '❌'} | "
            f"无长上影:{'✅' if long_cond_shadow else '❌'} | "
            f"非缩量涨:{'✅' if long_cond_volume else '❌'} | "
            f"J≤50:{'✅' if long_cond_kdj_val else '❌'}({j0:.1f}) | "
            f"J上升:{'✅' if long_cond_kdj_dir else '❌'} | "
            f"J力度达标:{'✅' if long_cond_kdj_force else '❌'}(当前{current_j_up:.1f}/需{avg_j_up * 0.4:.1f}) | "
            f"买量主导:{'✅' if long_cond_flow else '❌'}"
        )
        log_short = (
            f"[开空条件] 顺势:{'✅' if short_cond_trend else '❌'} | "
            f"盘口非多:{'✅' if short_cond_bias else '❌'} | "
            f"无长下影:{'✅' if short_cond_shadow else '❌'} | "
            f"量能过关:{'✅' if short_cond_volume else '❌'}(非缩量跌:{'✅' if short_cond_volume_1 else '❌'},非滞跌:{'✅' if short_cond_volume_2 else '❌'}) | "
            f"J≥50:{'✅' if short_cond_kdj_val else '❌'}({j0:.1f}) | "
            f"J下降:{'✅' if short_cond_kdj_dir else '❌'} | "
            f"J力度达标:{'✅' if short_cond_kdj_force else '❌'}(当前{current_j_down:.1f}/需{avg_j_down * 0.4:.1f}) | "
            f"卖量主导:{'✅' if short_cond_flow else '❌'}"
        )
        log(log_long)
        log(log_short)
        log("-" * 120)
        last_entry_log_time = now
    # 下单执行

    if long_ok:
        # 开多 = 最近3根收盘K线 最低点（下影线最低）
        min_3k = min(k['low'] for k in kline_1m_closed)
        send_limit_order("BUY", normalize_price(min_3k))
        return
    if short_ok:
        # 开空 = 最近3根收盘K线 最高点（上影线最高）
        max_3k = max(k['high'] for k in kline_1m_closed)
        send_limit_order("SELL", normalize_price(max_3k))
        return


def process_price():
    global current_price, raw_price, price_buffer, last_price_print_time
    global loss_count, last_check_time, processing, last_holding_log_time
    global local_max_profit_pct, local_max_price, local_min_price, max_profit_pct, max_price_since_entry, min_price_since_entry
    global time_at_max_profit
    # 🔒 防止并发执行（关键）
    if processing: return
    processing = True
    try:
        now = time.time()
        # 打印价格
        if now - last_price_print_time >= 5:
            log(f"[价格] {raw_price}")
            last_price_print_time = now
        # 连续亏损冷却
        if loss_count >= MAX_CONTINUOUS_LOSS:
            if now - loss_reset_time < LOSS_RESET_SEC: return
            loss_count = 0
        should_close = False
        close_reason = ""
        with lock:
            # 获取当前模式的持仓数据
            if USE_LOCAL_SIMULATION:
                cur_pos = local_position
                cur_ep = local_entry_price
            else:
                cur_pos = position
                cur_ep = entry_price
            if cur_pos and cur_ep > 0:
                # 🔥 修复1：当前盈亏 = (当前价 - 成交价) / 成交价 （带多空方向）
                pr = (raw_price - cur_ep) / cur_ep if cur_pos == "long" else (cur_ep - raw_price) / cur_ep
                # 🔥 修复2：最高盈利 = 自开仓以来的历史最高收益率
                if USE_LOCAL_SIMULATION:
                    if cur_pos == "long":
                        if raw_price > local_max_price:
                            local_max_price = raw_price
                            time_at_max_profit = time.time()
                    else:
                        if raw_price < local_min_price:
                            local_min_price = raw_price
                            time_at_max_profit = time.time()
                    cur_max_profit_pct = (local_max_price - cur_ep) / cur_ep if cur_pos == "long" else (
                                                                                                                   cur_ep - local_min_price) / cur_ep
                else:
                    if cur_pos == "long":
                        if raw_price > max_price_since_entry:
                            max_price_since_entry = raw_price
                            time_at_max_profit = time.time()
                    else:
                        if raw_price < min_price_since_entry:
                            min_price_since_entry = raw_price
                            time_at_max_profit = time.time()
                    cur_max_profit_pct = (max_price_since_entry - cur_ep) / cur_ep if cur_pos == "long" else (
                                                                                                                         cur_ep - min_price_since_entry) / cur_ep
                # 1. 硬止损：亏损 >= 0.05% (匹配超短线止盈，20倍杠杆对应本金1%亏损)
                if pr <= -0.0005:
                    should_close = True
                    close_reason = "1%硬止损"

                # ==============================================
                # 🔥🔥🔥 新增：1.5% 固定止盈（优先级最高）
                # ==============================================
                if not should_close and pr >= 0.015:
                    should_close = True
                    close_reason = f"✅ 固定止盈：盈利≥1.5% 强制平仓 | 当前盈利:{pr:.2%}"

                # ==============================================
                # 🔥🔥🔥 新增：高优先级回撤止盈（70% 立即平仓）
                # ==============================================
                cond_profit_valid = cur_max_profit_pct > 0.0002
                cond_retracement_70 = pr <= cur_max_profit_pct * 0.7  # 回撤70%

                if not should_close and cond_profit_valid and cond_retracement_70:
                    should_close = True
                    close_reason = f"🔥 高优先级止盈：盈利回撤70%强制平仓 | 最高:{cur_max_profit_pct:.2%} 当前:{pr:.2%}"

                # 原有回撤止盈（保留，作为次级条件）
                time_since_max = time.time() - time_at_max_profit if time_at_max_profit > 0 else 0
                cond_no_new_high = time_since_max > 20
                cond_retracement = pr <= cur_max_profit_pct * 0.8

                if not should_close and cond_profit_valid and cond_no_new_high and cond_retracement:
                    should_close = True
                    close_reason = f"回撤止盈(最高{cur_max_profit_pct:.2%}回落至{pr:.2%}, 距最高盈利{time_since_max:.1f}秒)"

                # 🔥 更新止盈止损状态打印
                if now - last_holding_log_time >= 2:
                    retracement_ratio_str = "N/A"
                    if cur_max_profit_pct > 0:
                        retracement_ratio = pr / cur_max_profit_pct
                        retracement_ratio_str = f"{retracement_ratio:.2%}"
                    log(
                        f"[止盈止损状态] {cur_pos} | 当前盈亏:{pr:.2%} | 最高盈利:{cur_max_profit_pct:.2%} | "
                        f"盈利>0.02%:{'✅' if cond_profit_valid else '❌'} | "
                        f"超20秒无新高:{'✅' if cond_no_new_high else '❌'} | "
                        f"回撤比(当前/最高):{retracement_ratio_str}≤70%:{'✅' if cond_retracement_70 else '❌'} | "
                        f"距最高盈利时间:{time_since_max:.1f}秒"
                    )
                    last_holding_log_time = now
        # 在锁外执行平仓网络请求
        if should_close:
            close_position(close_reason)
            return
        # 检查挂单状态
        if time.time() - last_check_time > 3:
            check_order_filled()
            last_check_time = time.time()
        check_fast_cancel()
        check_order_timeout()
        # 有挂单不再开仓 (双模式判断)
        has_order = local_active_order if USE_LOCAL_SIMULATION else active_order
        if has_order: return
        v18_entry_logic()
    finally:
        processing = False


# 核心修复3：独立的交易执行线程，避免 REST API 阻塞 WebSocket 心跳
def trade_worker():
    """独立线程消费队列，执行交易逻辑，防止网络卡顿导致 WS 断线"""
    while True:
        try:
            # 阻塞获取任务，没有数据时线程挂起不占用CPU
            task = trade_queue.get()
            if task == "PROCESS":
                process_price()
                # 修复：处理完一次后，清空积压的冗余任务，确保下一次拿到最新行情
                with trade_queue.mutex:
                    trade_queue.queue.clear()
        except Exception as e:
            log(f"[交易线程异常] {e}")


# ======================
# WebSocket 相关
# ======================
# WebSocket连接成功回调函数
def on_open(ws):
    global reconnect_delay
    reconnect_delay = 5  # 重置重连延迟
    log(f"[WebSocket] 连接成功 ✅ (模式: {'本地撮合-实盘行情' if USE_LOCAL_SIMULATION else 'API测试网'})")
    sync_position()
    # 订阅的行情频道：逐笔成交/标记价格/盘口深度/1/5/15/30分钟/1小时K线
    params = [
        f"{WS_SYMBOL}@aggTrade",
        f"{WS_SYMBOL}@markPrice",
        f"{WS_SYMBOL}@depth5@100ms",
        f"{WS_SYMBOL}@kline_1m",
        f"{WS_SYMBOL}@kline_5m",
        f"{WS_SYMBOL}@kline_15m",
        f"{WS_SYMBOL}@kline_30m",
        f"{WS_SYMBOL}@kline_1h"
    ]
    # 发送订阅消息
    sub_msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
    ws.send(json.dumps(sub_msg))


# WebSocket接收消息回调：处理行情数据
def on_message(ws, msg):
    global orderbook, raw_price, current_price, last_trade_side, last_ws_time, kline_1m_closed
    # 更新心跳时间：只要收到数据就说明连接正常
    last_ws_time = time.time()
    try:
        data = json.loads(msg)
        # 币安组合流：提取真实数据
        if "stream" in data and "data" in data: data = data["data"]
        # 订阅成功回执
        if "result" in data and data.get("id") == 1:
            log("[WebSocket] 行情订阅成功 ✅")
            return
        # 标记价格更新：核心价格数据源
        if data.get("e") == "markPriceUpdate":
            raw_price = float(data["p"]);
            current_price = raw_price
            with lock:
                price_buffer.append(raw_price)
                if len(price_buffer) > WINDOW: price_buffer.pop(0)
                tick_buffer.append(raw_price)
                if len(tick_buffer) > 5: tick_buffer.pop(0)
            compute_kdj(price_buffer)
            trade_queue.put("PROCESS")
        # 盘口深度更新
        if data.get("e") == "depthUpdate":
            orderbook["bids"] = data["b"];
            orderbook["asks"] = data["a"]
        # 逐笔成交更新
        if data.get("e") == "aggTrade":
            last_trade_side = "sell" if data["m"] else "buy"
            raw_price = float(data["p"]);
            current_price = raw_price
            # 更新最近5笔带量成交
            update_trade_volume(last_trade_side, data["q"])
            with lock:
                price_buffer.append(raw_price)
                if len(price_buffer) > WINDOW: price_buffer.pop(0)
                tick_buffer.append(raw_price)
                if len(tick_buffer) > 5: tick_buffer.pop(0)
            trade_queue.put("PROCESS")
        # 🔥 新增：1分钟K线收盘更新 (策略核心数据源)
        if data.get("e") == "kline" and data.get("k", {}).get("i") == "1m":
            k_data = data["k"]
            if k_data["x"]:  # 只有K线收盘时才记录，确保数据完整不重复
                new_k = {
                    "open": float(k_data["o"]),
                    "high": float(k_data["h"]),
                    "low": float(k_data["l"]),
                    "close": float(k_data["c"]),
                    "volume": float(k_data["v"])
                }
                kline_1m_closed.append(new_k)
                if len(kline_1m_closed) > 3:
                    kline_1m_closed.pop(0)
    except Exception as e:
        # 解析异常也更新心跳，避免误判断连
        last_ws_time = time.time()


# WebSocket断开连接回调
def on_close(ws, close_status_code, close_msg):
    log(f"[WebSocket] 断开，自动重连中...")


# WebSocket心跳监控：10秒无数据报警
def ws_watchdog():
    global last_ws_time
    while True:
        time.sleep(5)
        if time.time() - last_ws_time > 10:
            log("⚠️ WebSocket可能断流（10秒无数据）")


# WebSocket错误回调
def on_error(ws, error):
    log(f"[WebSocket 错误] {error}")


# 启动WebSocket：自带断线自动重连
def start_ws():
    def run():
        global reconnect_delay
        while True:
            try:
                # 创建WebSocket客户端
                ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close
                )
                # 保持连接：30秒发一次ping保活
                ws.run_forever(
                    ping_interval=30,
                    ping_timeout=15,
                    ping_payload="ping"
                )
            except Exception as e:
                log(f"[WebSocket 异常] {e}")
            # 重连延迟：指数递增，最大30秒
            reconnect_delay = min(reconnect_delay * 1.5, 30)
            time.sleep(reconnect_delay)

    # 启动WebSocket线程（守护线程）
    threading.Thread(target=run, daemon=True).start()


# ======================
# 主程序
# ======================
if __name__ == "__main__":
    log("======================================")
    mode_str = "本地撮合 (实盘行情)" if USE_LOCAL_SIMULATION else "API真实查询 (测试网)"
    log(f"        V18 专业优化版 启动成功")
    log(f"        当前运行模式: {mode_str}")
    log(f"        仓位控制: 总资金 {POSITION_RATIO * 100:.0f}% 动态开仓")
    log("======================================")
    sync_binance_time()  # 同步币安服务器时间
    sync_balance()  # 🔥 同步初始账户余额
    sync_position()  # 同步初始持仓
    start_ws()  # 启动WebSocket行情接收
    # 启动独立的交易执行工作线程
    threading.Thread(target=trade_worker, daemon=True).start()
    # 启动持仓监控线程
    threading.Thread(target=position_monitor, daemon=True).start()
    # 启动WebSocket心跳监控线程
    threading.Thread(target=ws_watchdog, daemon=True).start()
    try:
        # 主线程保持运行，防止程序退出
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("[程序退出]")