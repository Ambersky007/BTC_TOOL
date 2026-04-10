# -*- coding: utf-8 -*-
import websocket
import json
import time
import hmac
import hashlib
import threading
import requests
import logging
from datetime import datetime
import pyttsx3
import uuid

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
# 语音提醒（完全保留）
# ======================
engine = pyttsx3.init()
last_speak_time = 0


def speak_once(text):
    global last_speak_time
    if time.time() - last_speak_time > 2:
        try:
            engine.say(text)
            engine.runAndWait()
        except Exception as e:
            log(f"语音播报失败: {str(e)}")
        last_speak_time = time.time()


# ======================
# 币安配置（官方合规修复）
# ======================
API_KEY = "Nzv1A7tK7kGpFXyJobupTljiWIVu5EulI6oDHL22g8Oxu0a7nckNbU6tIkrJ1jbX"
SECRET_KEY = "rwfqhUnk2tVocsufVoPr0TeJXlmOMJRdeG52OS6bYMNxSdmK83rcoo80axRMm8aN"
#测试网
BASE_URL = "https://testnet.binancefuture.com"
WS_URL   = "wss://stream.binancefuture.com/stream"
#实战网
# BASE_URL = "https://fapi.binance.com"
# WS_URL   = "wss://fstream.binance.com/stream"

SYMBOL = "BTCUSDT"
WS_SYMBOL = "btcusdt"  # 新增：WebSocket小写符号
QTY = 0.004

# ======================
# 策略参数（完全保留）
# ======================
WINDOW = 20
MAX_RANGE = 0.0003
MIN_PROFIT = 0.0003
TRAILING_PROFIT = 0.0003
STOP_LOSS = 0.0003
MAX_HOLD_TIME = 15
ORDER_TIMEOUT = 10
SLIPPAGE = 5
PRICE_STEP = 0.5
MAX_CONTINUOUS_LOSS = 3
LOSS_RESET_SEC = 60
last_ws_time = 0

trade_buffer = []
VOLUME_WINDOW = 0.8
VOLUME_THRESHOLD = 2.5

FAST_CANCEL_SEC = 0.8
cooldown_until = 0
COOLDOWN_SEC = 2

# ======================
# 全局变量（完全保留）+ 🔥 修复1：新增交易锁
# ======================
price_buffer = []
tick_buffer = []
position = None
entry_price = 0
max_profit = 0.0
loss_count = 0
loss_reset_time = 0
entry_time = 0
current_price = None
raw_price = None
last_processed_price = None
last_fill_time = 0
last_check_time = 0
lock = threading.Lock()
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

loss_count = 0
trade_lock = False  # 🔥 新增：交易锁，防止重复下单


# ======================
# 时间同步（保留，官方规范）
# ======================
def sync_binance_time():
    global binance_time_offset
    try:
        res = requests.get("https://testnet.binancefuture.com/fapi/v1/time", timeout=3)
        server_time = res.json()["serverTime"]
        local_time = int(time.time() * 1000)
        binance_time_offset = server_time - local_time
        log("[时间同步] 完成")
    except:
        binance_time_offset = 0


def get_timestamp():
    return int(time.time() * 1000) + binance_time_offset


# ======================
# 签名（官方规范：字母排序）
# ======================
def sign(params):
    query = '&'.join([
        f"{k}={params[k]}"
        for k in sorted(params)
        if params[k] is not None
    ])
    return hmac.new(
        SECRET_KEY.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()


def format_price(p):
    return float(f"{p:.1f}")


def normalize_price(price):
    return round(price / PRICE_STEP) * PRICE_STEP


# ======================
# 盘口/交易函数（完全保留）
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


# ======================
# 持仓/下单/撤单/平仓（完全保留）
# ======================
def sync_position():
    global position, entry_price, active_order

    try:
        url = f"{BASE_URL}/fapi/v2/positionRisk"
        p = {"timestamp": get_timestamp()}
        p['signature'] = sign(p)
        h = {"X-MBX-APIKEY": API_KEY}

        d = requests.get(url, headers=h, params=p, timeout=3).json()

        for item in d:
            if item['symbol'] == SYMBOL:
                amt = float(item['positionAmt'])
                ep = float(item['entryPrice'])

                if amt != 0:
                    position = "long" if amt > 0 else "short"
                    entry_price = ep
                    active_order = None

                    log(f"[持仓] {position} | 数量:{abs(amt)} | 均价:{entry_price}")

                else:
                    position = None
                    entry_price = 0
                    log("[持仓] None")

    except Exception as e:
        log(f"[持仓同步失败] {e}")

def position_monitor():
    while True:
        try:
            url = f"{BASE_URL}/fapi/v2/positionRisk"
            p = {"timestamp": get_timestamp()}
            p['signature'] = sign(p)
            h = {"X-MBX-APIKEY": API_KEY}

            d = requests.get(url, headers=h, params=p, timeout=3).json()

            for item in d:
                if item['symbol'] == SYMBOL:
                    amt = float(item['positionAmt'])
                    ep = float(item['entryPrice'])
                    leverage = float(item['leverage'])

                    if amt != 0:
                        pos = "long" if amt > 0 else "short"

                        price = raw_price if raw_price else ep

                        notional = abs(amt) * price

                        pnl = (price - ep) * amt if amt > 0 else (ep - price) * abs(amt)

                        log(f"[持仓监控] {pos} | 数量:{abs(amt)} | 价值:{notional:.2f}U | 均价:{ep:.1f} | 浮盈:{pnl:.2f}U | 杠杆:{leverage}x")

                    else:
                        log("[持仓监控] 无持仓")

        except Exception as e:
            log(f"[持仓监控异常] {e}")

        time.sleep(8)

def send_limit_order(side, price, reduce=False):
    global active_order, order_time, order_price, trade_lock  # 🔥 加入trade_lock

    url = f"{BASE_URL}/fapi/v1/order"

    p = {
        "symbol": SYMBOL,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": str(QTY),
        "price": str(format_price(price)),
        "timestamp": get_timestamp()
    }

    if reduce:
        p["reduceOnly"] = "true"

    query = '&'.join([f"{k}={p[k]}" for k in sorted(p)])

    signature = hmac.new(
        SECRET_KEY.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

    url = f"{url}?{query}&signature={signature}"

    headers = {
        "X-MBX-APIKEY": API_KEY
    }

    try:
        r = requests.post(url, headers=headers, timeout=3)
        res = r.json()

        if "orderId" in res:
            active_order = res["orderId"]
            order_time = time.time()
            order_price = price
            trade_lock = True   # 🔥 修复3：下单成功上锁
            log(f"✅ 挂单成功 {res}")
        else:
            log(f"❌ 挂单失败返回: {res}")

    except Exception as e:
        log(f"❌ 挂单异常: {e}")


def send_market_order(side, reduce=False):
    global active_order
    with lock:
        url = f"{BASE_URL}/fapi/v1/order"
        p = {
            "symbol": SYMBOL, "side": side, "type": "MARKET",
            "quantity": QTY, "timestamp": get_timestamp()
        }
        if reduce: p["reduceOnly"] = "true"
    p['signature'] = sign(p)
    h = {"X-MBX-APIKEY": API_KEY}
    try:
        requests.post(url, headers=h, params=p, timeout=2)
        active_order = None
        log(f"[市价成交]{side}")
    except:
        log("[市价失败]")


def cancel_order():
    global active_order, order_price, order_time, trade_lock  # 🔥 加入trade_lock
    if not active_order: return
    try:
        url = f"{BASE_URL}/fapi/v1/order"
        p = {"symbol": SYMBOL, "orderId": active_order, "timestamp": get_timestamp()}
        p['signature'] = sign(p)
        h = {"X-MBX-APIKEY": API_KEY}
        requests.delete(url, headers=h, params=p, timeout=3)
    except:
        pass
    active_order = None
    order_price = 0
    order_time = 0
    trade_lock = False  # 🔥 修复4：撤单释放锁
    log("[撤单]")


def check_fast_cancel():
    global order_price

    if not active_order:
        return

    try:
        if not orderbook["bids"] or not orderbook["asks"]:
            return

        best_bid = float(orderbook["bids"][0][0])

    except:
        return

    if abs(order_price - best_bid) > 3 * PRICE_STEP:
        cancel_order()


def check_order_timeout():
    if active_order and time.time() - order_time > ORDER_TIMEOUT:
        cancel_order()


def check_order_filled():
    global active_order, last_fill_time, entry_time, trade_lock  # 🔥 加入trade_lock
    if not active_order: return
    try:
        url = f"{BASE_URL}/fapi/v1/order"
        p = {"symbol": SYMBOL, "orderId": active_order, "timestamp": get_timestamp()}
        p['signature'] = sign(p)
        h = {"X-MBX-APIKEY": API_KEY}
        d = requests.get(url, headers=h, params=p, timeout=3).json()
        if d.get("status") == "FILLED":
            active_order = None
            trade_lock = False   # 🔥 修复4：成交释放锁
            last_fill_time = time.time()
            entry_time = time.time()
            sync_position()
    except:
        pass


# ======================
# 策略逻辑（100% 完全保留，一行未改）
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


def close_position(reason):
    global position, entry_price, max_profit, loss_count, cooldown_until
    global pressure_side, pressure_start_time
    global loss_reset_time, trade_lock  # 🔥 加入trade_lock
    with lock:
        if not position: return
    pnl = (raw_price - entry_price) / entry_price if position == "long" else (entry_price - raw_price) / entry_price
    if pnl < 0:
        loss_count += 1
        if loss_count >= MAX_CONTINUOUS_LOSS:
            loss_reset_time = time.time()
    else:
        loss_count = 0
    log(f"[平仓] {reason} 盈亏:{pnl:.2%}")
    cancel_order()
    send_market_order("SELL" if position == "long" else "BUY", True)
    position = None
    entry_price = 0
    max_profit = 0
    pressure_side = None
    pressure_start_time = 0
    cooldown_until = time.time() + COOLDOWN_SEC
    trade_lock = False  # 🔥 修复4：平仓释放锁


# 🔥 修复2：v18_entry_logic 加锁控制
def v18_entry_logic():
    global cooldown_until, loss_count, order_time, trade_lock

    # 🔥 交易锁开启，直接拒绝开仓
    if trade_lock:
        return

    now = time.time()

    if now - order_time < 2:
        return

    cond1 = now >= cooldown_until
    cond2 = position is None and active_order is None

    try:
        if not orderbook["bids"] or not orderbook["asks"]:
            return

        best_bid = float(orderbook["bids"][0][0])
        best_ask = float(orderbook["asks"][0][0])

        spread = best_ask - best_bid
        cond4 = spread <= 2 * PRICE_STEP

    except Exception as e:
        log(f"[获取盘口失败] {e}")
        return

    cond5 = len(price_buffer) >= 5

    if not (cond1 and cond2 and cond4 and cond5):
        return

    bias = orderbook_bias()

    try:
        rh = max(price_buffer[-5:])
        rl = min(price_buffer[-5:])
        cl = raw_price < rh
        cs = raw_price > rl
    except:
        return

    if bias != "bear" and cl:
        send_limit_order("BUY", best_bid + PRICE_STEP)

    if bias != "bull" and cs:
        send_limit_order("SELL", best_ask - PRICE_STEP)


def process_price():
    global current_price, raw_price, price_buffer, max_profit, last_price_print_time
    global pressure_start_time, pressure_side, loss_count, last_check_time

    now = time.time()

    if now - last_price_print_time >= 5:
        log(f"[价格] {raw_price}")
        last_price_print_time = now

    if loss_count >= MAX_CONTINUOUS_LOSS:
        if now - loss_reset_time < LOSS_RESET_SEC:
            return
        loss_count = 0

    with lock:
        if position and entry_price > 0:
            pr = (raw_price - entry_price) / entry_price if position == "long" else (
                entry_price - raw_price) / entry_price

            if pr > max_profit:
                max_profit = pr

            if max_profit > 0 and pr <= max_profit * 0.15:
                close_position("止盈")
                return

            rt = orderbook_pressure_ratio()

            if position == "long":
                if rt > 2 and last_trade_side == "sell":
                    if pressure_side != "short":
                        pressure_side = "short"
                        pressure_start_time = now
                    elif now - pressure_start_time > 0.5:
                        close_position("反转止损")
                        return
                else:
                    pressure_side = None

            if position == "short":
                if rt < 0.5 and last_trade_side == "buy":
                    if pressure_side != "long":
                        pressure_side = "long"
                        pressure_start_time = now
                    elif now - pressure_start_time > 0.5:
                        close_position("反转止损")
                        return
                else:
                    pressure_side = None

            if now - entry_time > MAX_HOLD_TIME:
                close_position("超时")
                return

    if time.time() - last_check_time > 3:
        check_order_filled()
        last_check_time = time.time()

    check_fast_cancel()

    if active_order:
        log("[状态] 有挂单等待成交...")
        return

    v18_entry_logic()


# ======================
# 第二步：重写 on_open 函数（你要求的完整版本）
# ======================
def on_open(ws):
    print("✅ WebSocket 已连接")
    global reconnect_delay
    reconnect_delay = 5
    log("[WebSocket] 连接成功 ✅")
    sync_position()

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

    sub_msg = {
        "method": "SUBSCRIBE",
        "params": params,
        "id": 1
    }

    ws.send(json.dumps(sub_msg))


def on_message(ws, msg):
    global orderbook, raw_price, current_price, last_trade_side, loss_count
    global last_ws_time  # 🔥 强制声明全局，这是核心修复
    # 修复1：全局心跳 —— 任何消息都更新
    last_ws_time = time.time()

    try:
        data = json.loads(msg)

        # 处理组合流
        if "stream" in data and "data" in data:
            data = data["data"]

        # 订阅成功
        if "result" in data and data.get("id") == 1:
            log("[WebSocket] 行情订阅成功 ✅")
            return

        # 标记价格（稳定更新价格）
        if data.get("e") == "markPriceUpdate":
            raw_price = float(data["p"])
            current_price = raw_price
            with lock:
                price_buffer.append(raw_price)
                if len(price_buffer) > WINDOW:
                    price_buffer.pop(0)
                update_tick(raw_price)
            process_price()

        # 盘口深度
        if data.get("e") == "depthUpdate":
            orderbook["bids"] = data["b"]
            orderbook["asks"] = data["a"]

        # 逐笔成交
        if data.get("e") == "aggTrade":
            last_trade_side = "sell" if data["m"] else "buy"
            raw_price = float(data["p"])
            current_price = raw_price

            with lock:
                price_buffer.append(raw_price)
                if len(price_buffer) > WINDOW:
                    price_buffer.pop(0)
                update_tick(raw_price)

            process_price()

    except Exception as e:
        # 解析错误也要更新心跳，避免误判断流
        last_ws_time = time.time()
        # log(f"[解析错误] {e}")


def on_close(ws, close_status_code, close_msg):
    log(f"[WebSocket] 断开，自动重连中...")

def ws_watchdog():
    global last_ws_time
    while True:
        time.sleep(5)
        if time.time() - last_ws_time > 10:
            log("⚠️ WebSocket可能断流（10秒无数据）")

def on_error(ws, error):
    log(f"[WebSocket 错误] {error}")


# ======================
# 第三步：注册 on_open 到 WebSocketApp
# ======================
def start_ws():
    def run():
        global reconnect_delay
        while True:
            try:
                ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close
                )

                ws.run_forever(
                    ping_interval=30,
                    ping_timeout=15,
                    ping_payload="ping"
                )

            except Exception as e:
                log(f"[WebSocket 异常] {e}")

            reconnect_delay = min(reconnect_delay * 1.5, 30)
            time.sleep(reconnect_delay)

    threading.Thread(target=run, daemon=True).start()

# ======================
# 主程序（完全保留）
# ======================
if __name__ == "__main__":
    log("======================================")
    log("        V18 官方合规版 启动成功")
    log("======================================")
    sync_binance_time()
    sync_position()
    start_ws()
    threading.Thread(target=position_monitor, daemon=True).start()
    # 修复3：删除冗余的price_monitor线程
    threading.Thread(target=ws_watchdog, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("[程序退出]")