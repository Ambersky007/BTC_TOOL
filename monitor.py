'''
安装依赖
pip install ccxt
pip install orjson
pip install coincurve

该类功能
1、实现实时交易流监控
2、实现实时报价流监控
'''

import ccxt
import ccxt.pro
import asyncio
import datetime
import json

# 缓存到数据库
import AsyncBuffer
buffer = AsyncBuffer.AsyncBuffer(qdb_url='http::addr=192.168.1.170:9000;')

import logging
from logging.handlers import RotatingFileHandler
# 1. 创建日志记录器 (Logger)
logger = logging.getLogger("logger")
logger.setLevel(logging.DEBUG)  # 设置总门槛级别

# 2. 创建格式化器 (Formatter)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 3. 创建文件处理器 (FileHandler) - 保存到硬盘
file_handler = logging.FileHandler('monitor.log', encoding='utf-8')
file_handler.setLevel(logging.WARNING)  # 文件只记录 WARNING 及以上的严重问题
file_handler.setFormatter(formatter)

# 4. 创建控制台处理器 (StreamHandler) - 打印到屏幕
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG) # 屏幕显示所有调试信息
console_handler.setFormatter(formatter)

# 5. 每个文件最大 10MB，保留最近的 7 个备份文件
rotating_file_handler = RotatingFileHandler('monitor.log', maxBytes=10*1024*1024, backupCount=7)

# 6. 将处理器添加到记录器
logger.addHandler(file_handler)
logger.addHandler(console_handler)
logger.addHandler(rotating_file_handler)

# 实时订单流
'''
[{
	'info': {
		'e': 'trade',
		'E': 1774705421780,
		'T': 1774705421780,
		's': 'XAGUSDT',
		't': 59327619,
		'p': '69.8600',
		'q': '1.000',
		'X': 'MARKET',
		'm': False
	},
	'timestamp': 1774705421780,
	'datetime': '2026-03-28T13:43:41.780Z',
	'symbol': 'XAG/USDT:USDT',
	'id': '59327619',
	'order': None,
	'type': None,
	'side': 'buy',
	'takerOrMaker': None,
	'price': 69.86,
	'amount': 1.0,
	'cost': 69.86,
	'fee': {
		'cost': None,
		'currency': None
	},
	'fees': []
}]
'''
async def watch_trades(exchange, symbol, enable_buffer=False):
    while True:
        try:
            trades = await exchange.watch_trades(symbol)
            # print(f"trades: {trades}")
            # print(f"trades: {len(trades)}")
            if enable_buffer:
                for trade in trades:
                    # 既然你最终要存入 QuestDB，直接构造精简结构
                    item_data = {
                        'ts': trade.get('timestamp'),
                        'datetime': trade.get('datetime'),
                        'id': trade.get('id'),
                        'order': trade.get('order'),
                        'type': trade.get('type'),
                        'side': trade.get('side'),
                        'takerOrMaker': trade.get('takerOrMaker'),
                        'price': trade.get('price'),
                        'amount': trade.get('amount'),
                        'marketName': trade.get('info', {}).get('X'), # 币安 市场类型。MARKET 通常指现货或标准市场交易。
                    }
                    item = {
                        'name': 'trade_ccxt',
                        'symbol': symbol,
                        'data': item_data
                    }
                    await buffer.add(item)
        except Exception as e:
            # 错误日志
            logger.error(f"Error in watch_trades: {type(e).__name__}: {str(e)}")
            await asyncio.sleep(1)

# 实时报价流
'''
示例输出：
{
	'bids': [
		[69.85, 13054.635],
		[69.84, 5201.664],
		[69.83, 4847.78],
		[69.82, 7344.06],
		[69.81, 20421.882]
	],
	'asks': [
		[69.86, 353.026],
		[69.87, 28140.98],
		[69.88, 62746.198],
		[69.89, 1229.212],
		[69.9, 1548.962]
	],
	'timestamp': 1774705266993,
	'datetime': '2026-03-28T13:41:06.993Z',
	'nonce': 10207845686502,
	'symbol': 'XAG/USDT:USDT'
}
'''
async def watch_order_book(exchange, symbol, enable_buffer=False):
    while True:
        try:
            order_book = await exchange.watch_order_book(symbol, limit=10)
            # 输出 order_book
            # print(f"order_book: {order_book}")
            # print(f"order_book: {len(order_book)}")
            if enable_buffer:
                item_data = {
                    'bids': json.dumps(order_book.get('bids'), separators=(',', ':')),
                    'asks': json.dumps(order_book.get('asks'), separators=(',', ':')),
                    # 'bids': order_book['bids'],
                    # 'asks': order_book['asks'],
                    'ts': order_book.get('timestamp'),
                    'datetime': order_book.get('datetime'),
                    'nonce': order_book.get('nonce'),
                }
                # index = 0
                # for b in order_book['bids']:
                #     index += 1
                #     formatted_data[f'bids_p{index}'] = b[0]
                #     formatted_data[f'bids_v{index}'] = b[1]
                # index = 0
                # for a in order_book['asks']:
                #     index += 1
                #     formatted_data[f'asks_p{index}'] = a[0]
                #     formatted_data[f'asks_v{index}'] = a[1]
                item = {
                    'name': 'order_book_ccxt',
                    'symbol': symbol,
                    'data': item_data
                }
                await buffer.add(item)
        except Exception as e:
            # 错误日志
            logger.error(f"Error in watch_order_book: {type(e).__name__}: {str(e)}")
            await asyncio.sleep(1)

# 买一和卖一
'''
{
	'XAG/USDT:USDT': {
		'symbol': 'XAG/USDT:USDT',
		'timestamp': 1774698175440,
		'datetime': '2026-03-28T11:42:55.440Z',
		'high': None,
		'low': None,
		'bid': 69.85,
		'bidVolume': 3917.985,
		'ask': 69.86,
		'askVolume': 6907.213,
		'vwap': None,
		'open': None,
		'close': None,
		'last': None,
		'previousClose': None,
		'change': None,
		'percentage': None,
		'average': None,
		'baseVolume': None,
		'quoteVolume': None,
		'info': {
			'e': 'bookTicker',
			'u': 10207385162495,
			's': 'XAGUSDT',
			'b': '69.8500',
			'B': '3917.985',
			'a': '69.8600',
			'A': '6907.213',
			'T': 1774698175440,
			'E': 1774698175440
		},
		'indexPrice': None,
		'markPrice': None
	}
}
'''
async def watch_bids_asks(exchange, symbol, enable_buffer=False):
    while True:
        try:
            bids_asks = await exchange.watch_bids_asks([symbol])
            # 输出 bids_asks
            # print(f"bids_asks: {bids_asks}")
            # print(f"bids_asks: {len(bids_asks)}")
            if enable_buffer:
                for key, detail in bids_asks.items():
                    item_data = {
                        'ts': detail.get('timestamp'),
                        'datetime': detail.get('datetime'),
                        'bid': detail.get('bid'),
                        'bidVolume': detail.get('bidVolume'),
                        'ask': detail.get('ask'),
                        'askVolume': detail.get('askVolume'),
                    }
                    item = {
                        'name': 'bids_asks_ccxt',
                        'symbol': symbol,
                        'data': item_data,
                    }
                    await buffer.add(item)
        except Exception as e:
            # 错误日志
            logger.error(f"Error in watch_bids_asks: {type(e).__name__}: {str(e)}")
            await asyncio.sleep(1)

'''
[
    {
        'info':          { ... },                        // the original decoded JSON as is
        'symbol':        'BTC/USDT:USDT-231006-25000-P', // unified CCXT market symbol
        'contracts':     2,                              // the number of derivative contracts
        'contractSize':  0.001,                          // the contract size for the trading pair
        'price':         27038.64,                       // the average liquidation price in the quote currency
        'baseValue':     0.002,                          // value in the base currency (contracts * contractSize)
        'quoteValue':    54.07728,                       // value in the quote currency ((contracts * contractSize) * price)
        'timestamp':     1696996782210,                  // Unix timestamp in milliseconds
        'datetime':      '2023-10-11 03:59:42.000',      // ISO8601 datetime with milliseconds
    },
    ...
]
'''
# 监控 liquidations
# async def watch_liquidations(exchange, symbol, enable_buffer=False):
#     while True:
#         try:
#             # 监听强平流
#             liquidations = await exchange.watch_liquidations(symbol, limit=20)
#             print('liquidations = ', liquidations)
#             logger.info(f"liquidations: {str(liquidations)}")
            
#             if enable_buffer:
#                 # CCXT 的 watch_liquidations 返回的是一个列表 (List)，这与 watch_bids_asks (返回 Dict) 不同
#                 for detail in liquidations:
#                     # 使用白名单直接构造新字典，天然舍弃 info, datetime, fee 等冗余数据
#                     item_data = {
#                         'ts': detail.get('timestamp'),
#                         'datetime': detail.get('datetime'),
#                         'id': detail.get('id'),
#                         'price': detail.get('price'),
#                         'amount': detail.get('amount'),
#                         'side': detail.get('side'),
#                     }
                    
#                     item = {
#                         'name': 'liquidations_ccxt',
#                         'symbol': symbol,
#                         'data': item_data,
#                     }
#                     await buffer.add(item)
                    
#         except Exception as e:
#             logger.error(f"Error in watch_liquidations: {type(e).__name__}: {str(e)}")
#             await asyncio.sleep(1)

async def main():

    try:

        symbol = "XAGUSDT"

        enable_buffer = True

        exchange_public = ccxt.pro.binance({
            'options': {
                'defaultType': 'future',
            },
        })

        # exchange_public.watch_bids_asks
        # exchange_public.watch_liquidations
        # exchange_public.watch_position
        # exchange_public.watch_trades
        # exchange_public.watch_order_book
        # exchange_public.fetch_trades(symbol)
        # exchange_public.watch_liquidations

        # 实时订单流
        asyncio.ensure_future(watch_trades(exchange_public, symbol, enable_buffer))

        # 实时报价流
        asyncio.ensure_future(watch_order_book(exchange_public, symbol, enable_buffer))

        # 买一卖一
        asyncio.ensure_future(watch_bids_asks(exchange_public, symbol, enable_buffer))

        # 强平
        # asyncio.ensure_future(watch_liquidations(exchange_public, symbol, enable_buffer))

        if enable_buffer:
            print("buffer enabled")
            asyncio.ensure_future(buffer.run_forever())

    finally:
        # 这一步非常重要，防止连接泄露
        await exchange_public.close()

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        asyncio.ensure_future(main())
        # 日志记录启动时间
        logger.info("Started time: %s", datetime.datetime.now())
        loop.run_forever()
    except KeyboardInterrupt as e:
        print("KeyboardInterrupt")
    except Exception as e:
        logger.error("Exception: %s", e)
    finally:
        print("Close loop")
        loop.close()
