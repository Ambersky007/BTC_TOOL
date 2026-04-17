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
import queue
import pandas as pd

# ======================
# 日志系统
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
# 🔥 核心配置：一键切换交易对 🔥
# ======================
CURRENT_SYMBOL = "CLUSDT"
SYMBOL_CONFIG = {
    "BTCUSDT": {"ws_symbol": "btcusdt", "price_precision": 1},
    "XAGUSDT": {"ws_symbol": "xagusdt", "price_precision": 3},
    "XAUSDT": {"ws_symbol": "xausdt", "price_precision": 2},
    "CLUSDT": {"ws_symbol": "clusdt", "price_precision": 4},
}
if CURRENT_SYMBOL not in SYMBOL_CONFIG:
    log(f"❌ 错误：交易对 {CURRENT_SYMBOL} 未在 SYMBOL_CONFIG 中配置精度！")
    exit()
SYMBOL = CURRENT_SYMBOL
WS_SYMBOL = SYMBOL_CONFIG[SYMBOL]["ws_symbol"]
PRICE_PRECISION = SYMBOL_CONFIG[SYMBOL]["price_precision"]
# ======================
# 币安实盘配置
# ======================
API_KEY = "Nzv1A7tK7kGpFXyJobupTljiWIVu5EulI6oDHL22g8Oxu0a7nckNbU6tIkrJ1jbX"
SECRET_KEY = "rwfqhUnk2tVocsufVoPr0TeJXlmOMJRdeG52OS6bYMNxSdmK83rcoo80axRMm8aN"
# BASE_URL = "https://fapi.binance.com" #实盘
BASE_URL = "https://testnet.binancefuture.com"  # 测试盘
WS_URL = "wss://fstream.binance.com/stream"
# ======================
# 🔥 止盈止损参数
# ======================
LEVERAGE = 20
STOP_LOSS_PCT = -0.001  # 本地硬止损：亏损0.1%平仓 (程序端执行)
TRAILING_PROFIT_TRIGGER = 0.0002
TRAILING_PROFIT_TIMEOUT = 20
TRAILING_PROFIT_RATIO = 0.8
NATIVE_STOP_LOSS_PCT = 0.03  # 🔥 新增：原生止损幅度 3% (交易所端执行，防插针爆仓)
# ======================
# 全局变量
# ======================
raw_price = None
position = None
entry_price = 0
actual_position_amt = 0
binance_time_offset = 0
last_ws_time = 0
trade_records = []
max_price_since_entry = 0.0
min_price_since_entry = 999999.0
time_at_max_profit = 0.0
native_sl_order_id = None  # 🔥 新增：记录原生止损单的OrderID，用于撤单
lock = threading.RLock()
processing = False
trade_queue = queue.Queue()
last_price_print_time = 0
last_holding_log_time = 0


# ======================
# 基础工具函数
# ======================
def sync_binance_time():
    global binance_time_offset
    try:
        # 注意：即使测试盘，时间同步也建议走主网，或者保持与BASE_URL一致
        res = requests.get(f"{BASE_URL}/fapi/v1/time", timeout=3)
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
    fmt = f"{{:.{PRICE_PRECISION}f}}"
    return float(fmt.format(p))


# ======================
# 🔥 新增：交易所原生止损单管理
# ======================
def place_native_stop_loss(ep, pos, qty):
    """在币安服务器挂单：STOP_MARKET 止损单"""
    global native_sl_order_id
    side = "SELL" if pos == "long" else "BUY"
    # 计算止损触发价：多单向下3%，空单向上3%
    if pos == "long":
        stop_price = ep * (1 - NATIVE_STOP_LOSS_PCT)
    else:
        stop_price = ep * (1 + NATIVE_STOP_LOSS_PCT)
    stop_price = format_price(stop_price)
    try:
        p = {
            "symbol": SYMBOL,
            "side": side,
            "type": "STOP_MARKET",  # 触发后按市价平仓
            "stopPrice": str(stop_price),
            "quantity": str(qty),
            "reduceOnly": "true",  # 只减仓，防止反向开仓
            "timestamp": get_timestamp(),
            "recvWindow": 5000
        }
        url = build_signed_url(f"{BASE_URL}/fapi/v1/order", p)
        h = {"X-MBX-APIKEY": API_KEY}
        res = requests.post(url, headers=h, timeout=5).json()
        if "orderId" in res:
            native_sl_order_id = res["orderId"]
            log(f"🛡️ [原生止损挂单成功] 方向:{side} | 触发价:{stop_price} | 数量:{qty} | 订单ID:{native_sl_order_id}")
        else:
            log(f"❌ [原生止损挂单失败] 返回:{res}")
    except Exception as e:
        log(f"❌ [原生止损挂单异常] {e}")


def cancel_native_stop_loss():
    """撤销币安服务器的原生止损单"""
    global native_sl_order_id
    if not native_sl_order_id: return
    order_id_to_cancel = native_sl_order_id
    native_sl_order_id = None  # 先清空防重入
    try:
        p = {"symbol": SYMBOL, "orderId": order_id_to_cancel, "timestamp": get_timestamp(), "recvWindow": 5000}
        url = build_signed_url(f"{BASE_URL}/fapi/v1/order", p)
        h = {"X-MBX-APIKEY": API_KEY}
        res = requests.delete(url, headers=h, timeout=3).json()
        if res.get("status") in ["CANCELED", "EXPIRED"] or res.get("code") == -2011:
            log(f"🗑️ [原生止损撤单成功] ID:{order_id_to_cancel}")
        else:
            log(f"⚠️ [原生止损撤单返回] {res}")
    except Exception as e:
        log(f"❌ [原生止损撤单异常] {e}")


# ======================
# 持仓同步与监控
# ======================
def sync_position():
    global position, entry_price, actual_position_amt
    global max_price_since_entry, min_price_since_entry, time_at_max_profit
    prev_position = position
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
                    # 🔥 检测到新开仓：初始化追踪变量 + 挂原生止损单
                    if prev_position is None:
                        max_price_since_entry = 0.0
                        min_price_since_entry = 999999.0
                        time_at_max_profit = time.time()
                        log(f"✅ [检测到新开仓] {SYMBOL} {position} | 数量:{actual_position_amt} | 均价:{format_price(ep)} | 已启动监控")
                        place_native_stop_loss(ep, position, actual_position_amt)
                else:
                    if position is not None:
                        log("✅ [检测到平仓] 当前已无持仓，停止监控")
                        cancel_native_stop_loss()  # 🔥 仓位没了，撤销原生止损单
                    position = None
                    entry_price = 0
                    actual_position_amt = 0
                return
        if position is not None:
            log("✅ [检测到平仓] 当前已无持仓，停止监控")
            cancel_native_stop_loss()  # 🔥 仓位没了，撤销原生止损单
        position = None
        entry_price = 0
        actual_position_amt = 0
    except Exception as e:
        log(f"[持仓同步失败] {e}")


def position_monitor():
    while True:
        try:
            sync_position()
            sleep_time = 8 if position else 30
            if position and entry_price > 0 and raw_price:
                pnl = (raw_price - entry_price) * actual_position_amt if position == "long" else (
                                                                                                             entry_price - raw_price) * actual_position_amt
                margin = actual_position_amt * entry_price / LEVERAGE if LEVERAGE > 0 else 0
                roi = pnl / margin if margin > 0 else 0
                log(f"[持仓确认] {SYMBOL} {position} | 浮盈:{pnl:.2f}U | ROI:{roi:.2%} | 均价:{format_price(entry_price)}")
        except Exception as e:
            log(f"[持仓监控异常] {e}")
        time.sleep(sleep_time)


# ======================
# 交易执行与记录
# ======================
def send_market_order(side):
    for i in range(3):
        try:
            p = {
                "symbol": SYMBOL, "side": side, "type": "MARKET",
                "quantity": str(actual_position_amt), "timestamp": get_timestamp(),
                "recvWindow": 5000, "reduceOnly": "true"
            }
            url = build_signed_url(f"{BASE_URL}/fapi/v1/order", p)
            h = {"X-MBX-APIKEY": API_KEY}
            r = requests.post(url, headers=h, timeout=5)
            res = r.json()
            if res.get("status") == "NEW" or res.get("orderId"):
                log(f"✅ [API市价平仓成功] {SYMBOL} {side} | 数量:{actual_position_amt}")
                return True
            else:
                log(f"❌ [API市价下单异常] 返回: {res}")
                if isinstance(res, dict) and res.get("code") == -1021:
                    sync_binance_time()
        except Exception as e:
            log(f"❌ [API市价失败] 重试 {i + 1}/3 | 错误: {e}")
        time.sleep(0.5)
    log("🚨 API市价平仓彻底失败，请手动平仓！")
    return False


def save_trade_records():
    if not trade_records: return
    try:
        df = pd.DataFrame(trade_records)
        try:
            df.to_excel("trade_records.xlsx", index=False)
        except ImportError:
            df.to_csv("trade_records.csv", index=False, encoding='utf-8-sig')
    except Exception as e:
        log(f"[记录保存失败] {e}")


def close_position(reason):
    pos = position
    ep = entry_price
    if not pos: return
    # 🔥 核心修改：本地程序决定平仓时，先撤掉交易所的3%原生止损单，防止冲突
    cancel_native_stop_loss()
    pr = (raw_price - ep) / ep if pos == "long" else (ep - raw_price) / ep
    pnl_abs = (raw_price - ep) * actual_position_amt if pos == "long" else (ep - raw_price) * actual_position_amt
    margin = actual_position_amt * ep / LEVERAGE if LEVERAGE > 0 else 0
    roi = pnl_abs / margin if margin > 0 else 0
    record = {
        "品种": SYMBOL,
        "平仓时间": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "方向": "多" if pos == "long" else "空",
        "数量": actual_position_amt,
        "开仓价格": format_price(ep),
        "平仓价格": format_price(raw_price),
        "盈亏(U)": round(pnl_abs, 2),
        "收益率": f"{roi:.2%}",
        "平仓原因": reason
    }
    trade_records.append(record)
    save_trade_records()
    log(f"[平仓触发] {SYMBOL} {reason} | 当前盈亏:{pr:.2%}")
    retry_count = 0
    MAX_RETRY = 10
    while retry_count < MAX_RETRY:
        retry_count += 1
        success = send_market_order("SELL" if pos == "long" else "BUY")
        if success:
            sync_position()
            return
        else:
            log(f"⚠️ 平仓失败，3秒后发起第 {retry_count + 1} 次强平！")
            sync_position()
            if not position: return
            time.sleep(3)
    log("🚨🚨🚨 严重警告：连续10次强平失败，请人工干预！🚨🚨🚨")


# ======================
# 核心止盈止损逻辑
# ======================
def process_price():
    global raw_price, last_price_print_time
    global max_price_since_entry, min_price_since_entry, time_at_max_profit, last_holding_log_time, processing
    if processing: return
    processing = True
    try:
        now = time.time()
        if now - last_price_print_time >= 5:
            log(f"[{SYMBOL} 价格] {format_price(raw_price)}")
            last_price_print_time = now
        should_close = False
        close_reason = ""
        cur_pos = position
        cur_ep = entry_price
        if cur_pos and cur_ep > 0 and raw_price:
            pr = (raw_price - cur_ep) / cur_ep if cur_pos == "long" else (cur_ep - raw_price) / cur_ep
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
            # 本地0.1%硬止损 (在3%原生止损之前触发)
            if pr <= STOP_LOSS_PCT:
                should_close = True
                close_reason = f"本地硬止损(亏损{pr:.2%})"
            cond_profit_valid = cur_max_profit_pct > TRAILING_PROFIT_TRIGGER
            time_since_max = time.time() - time_at_max_profit if time_at_max_profit > 0 else 0
            cond_no_new_high = time_since_max > TRAILING_PROFIT_TIMEOUT
            cond_retracement = pr <= cur_max_profit_pct * TRAILING_PROFIT_RATIO
            if not should_close and cond_profit_valid and cond_no_new_high and cond_retracement:
                should_close = True
                close_reason = f"回撤止盈(最高{cur_max_profit_pct:.2%}回落至{pr:.2%}, 距最高盈利{time_since_max:.1f}秒)"
            if now - last_holding_log_time >= 2:
                log(
                    f"[{SYMBOL} 监控] {cur_pos} | 当前盈亏:{pr:.2%} | 最高盈利:{cur_max_profit_pct:.2%} | "
                    f"超{TRAILING_PROFIT_TIMEOUT}秒无新高:✅ | "
                    f"距最高盈利时间:{time_since_max:.1f}秒"
                )
                last_holding_log_time = now
        if should_close:
            close_position(close_reason)
    finally:
        processing = False


def trade_worker():
    while True:
        try:
            task = trade_queue.get()
            if task == "PROCESS":
                process_price()
                with trade_queue.mutex:
                    trade_queue.queue.clear()
        except Exception as e:
            log(f"[交易线程异常] {e}")


# ======================
# WebSocket 行情接收
# ======================
def on_open(ws):
    log(f"[WebSocket] 连接成功 ✅ (监控品种: {SYMBOL})")
    sync_position()
    params = [
        f"{WS_SYMBOL}@aggTrade",
        f"{WS_SYMBOL}@markPrice"
    ]
    sub_msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
    ws.send(json.dumps(sub_msg))


def on_message(ws, msg):
    global raw_price, last_ws_time
    last_ws_time = time.time()
    try:
        data = json.loads(msg)
        if "stream" in data and "data" in data: data = data["data"]
        if "result" in data and data.get("id") == 1:
            log(f"[WebSocket] {SYMBOL} 行情订阅成功 ✅")
            return
        if data.get("e") == "markPriceUpdate":
            raw_price = float(data["p"])
            trade_queue.put("PROCESS")
        if data.get("e") == "aggTrade":
            raw_price = float(data["p"])
            trade_queue.put("PROCESS")
    except Exception:
        last_ws_time = time.time()


def on_close(ws, close_status_code, close_msg):
    log("[WebSocket] 断开，自动重连中...")


def on_error(ws, error):
    log(f"[WebSocket 错误] {error}")


def ws_watchdog():
    while True:
        time.sleep(5)
        if time.time() - last_ws_time > 10:
            log("⚠️ WebSocket可能断流（10秒无数据）")


def start_ws():
    def run():
        reconnect_delay = 5
        while True:
            try:
                ws = websocket.WebSocketApp(
                    WS_URL, on_open=on_open, on_message=on_message,
                    on_error=on_error, on_close=on_close
                )
                ws.run_forever(ping_interval=30, ping_timeout=15, ping_payload="ping")
            except Exception as e:
                log(f"[WebSocket 异常] {e}")
            reconnect_delay = min(reconnect_delay * 1.5, 30)
            time.sleep(reconnect_delay)

    threading.Thread(target=run, daemon=True).start()


# ======================
# 主程序
# ======================
if __name__ == "__main__":
    log("======================================")
    log(f"   半自动止盈止损助手 启动成功")
    log(f"   当前监控品种: {SYMBOL} (精度:{PRICE_PRECISION}位小数)")
    log(f"   交易所原生保底止损: {NATIVE_STOP_LOSS_PCT:.1%}")
    log("======================================")
    sync_binance_time()
    start_ws()
    threading.Thread(target=trade_worker, daemon=True).start()
    threading.Thread(target=position_monitor, daemon=True).start()
    threading.Thread(target=ws_watchdog, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("[程序退出]")