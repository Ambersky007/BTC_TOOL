import asyncio
import time
from questdb.ingress import Sender, TimestampNanos
'''
缓存列表，定时保存到questdb

白银期货合约查询示例：
SELECT *,to_timezone(timestamp, 'Asia/Shanghai') as bj_time FROM XAGUSDT_trade_ccxt ORDER BY timestamp DESC limit 1000;
SELECT *,to_timezone(timestamp, 'Asia/Shanghai') as bj_time FROM XAGUSDT_trade_ccxt WHERE ts>=1775349747949 ORDER BY timestamp ASC limit 1000;
SELECT *,to_timezone(timestamp, 'Asia/Shanghai') as bj_time FROM XAGUSDT_bids_asks_ccxt ORDER BY timestamp DESC limit 1000;
SELECT *,to_timezone(timestamp, 'Asia/Shanghai') as bj_time FROM XAGUSDT_order_book_ccxt ORDER BY timestamp DESC limit 1000;

'''
class AsyncBuffer:
    def __init__(self, qdb_url='http::addr=127.0.0.1:9000;', interval=5):
        self.qdb_url = qdb_url
        self.items = []
        self.interval = interval

    async def add(self, item):
        self.items.append(item)

    async def submit_data(self, to_submit):
        if to_submit:
            submit_data = {}
            for item in to_submit:
                table_name = f'{item["symbol"]}_{item["name"]}'
                # table_name 需要去掉斜杠/和冒号:
                table_name = table_name.replace('/', '_').replace(':', '_')
                if table_name not in submit_data:
                    submit_data[table_name] = []
                submit_data[table_name].append(item)
            # 提交数据到qdb
            try:
                with Sender.from_conf(self.qdb_url) as sender:
                    for table_name, data in submit_data.items():
                        try:
                            # TODO: 提交数据到qdb
                            print(f"提交数据到qdb: {table_name}, 数量：{len(data)}")
                            for row in data:
                                row_data = row['data']
                                # print(f"提交数据到qdb: {table_name}, 行数据：{row_data}")
                                sender.row(
                                    table_name,
                                    symbols={"symbol": table_name},
                                    columns=row_data,
                                    at=TimestampNanos.now(),
                                )
                        except Exception as e:
                            print(f"提交数据到qdb失败Inner: {e}")
                        sender.flush()
            except Exception as e:
                print(f"提交数据到qdb失败Outer: {e}")

    async def run_forever(self):
        while True:
            await asyncio.sleep(self.interval)
            if self.items:
                to_submit = self.items[:]
                self.items.clear()
                print(f"提交数据到队列: {len(to_submit)} 条")
                await self.submit_data(to_submit)
# 在主循环中启动任务即可