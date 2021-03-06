import pymysql
import numpy as np
import pandas as pd
import datetime
import time
from crawler import Crawler
import json
import asyncio


def is_today(time_tick, n_time=None):
    if n_time is None:
        return time_tick.date() == datetime.datetime.now().date()
    else:
        return time_tick.date() == n_time.date()


def print_transactions(ids, price, amount):
    for i in range(len(ids)):
        println = 'buy  ' if amount[i] > 0 else 'sell '
        print(println + ids[i] + ' for ' + str(abs(amount[i])) + ' on ' + str(price[i]))


class BasicStrategy:
    def __init__(self, trade_id, socket=None):
        self.socket = socket
        self.crawler = Crawler()
        self.trade_id = str(trade_id)
        self.stock_list = self.crawler.stock_list
        self.order = [[], []]
        self.socket_msg = dict()
        self.n_time = None
        self.p_time = None
        self.userDB = pymysql.connect(user='root',
                                      password='123456',
                                      database='userdb',
                                      use_unicode=True)
        self.userDB._autocommit = False

        self.position = None
        self.get_position()
        self.unit = 100
        self.scale = 1000000
        self.cash = 0
        self.net_value = 0
        self.length = 0
        self.round = 0
        self.output_dims = len(self.stock_list) - 1
        self.replay_buffer = None
        self.model = None

        self.sh50 = None
        self.init_state = None

    def get_position(self):
        self.position = pd.DataFrame(0, columns=['total', 'today', 'available', 'deal_price', 'curr_price'],
                                     index=self.stock_list).astype(float)
        """
        total = 'total possession on stock_i'
        today = 'amount of stock_i bought on today (apply to t+1 policy)'
        available = total - today
        deal_price = 'the MOST RECENT deal price'
        :return: DataFrame
                total   today   available   deal_price  curr_price
        stock1  *       *       *           *           *
        stock2  *       *       *           *           *
        ...
        stock_n *       *       *           *           *
        """
        # get account info
        cursor = self.userDB.cursor()
        cursor.execute('select valid_cash from trade_list where t_id= %s', self.trade_id)
        self.cash = np.ravel(cursor.fetchall())[0]
        cursor.close()

        # get total position info
        cursor = self.userDB.cursor()
        cursor.execute('select stock_id,volume from position where t_id= %s', self.trade_id)
        for i in cursor:
            self.position.loc[i[0]]['total'] = float(i[1])
        cursor.close()

        # get today transaction info
        cursor = self.userDB.cursor()
        cursor.execute('select stock_id,volume,direction,price,transaction_datetime ' +
                       'from trade_detail where t_id= %s', self.trade_id)
        for i in cursor:
            if i[2] == 'buy' and is_today(i[4], self.n_time):
                self.position.loc[i[0]]['today'] += float(i[1])
                self.position.loc[i[0]]['deal_price'] = float(i[3])
        cursor.close()
        self.position['available'] = self.position['total'] - self.position['today']

        # set total asset info
        self.set_total_asset()

    def set_total_asset(self):
        self.net_value = self.cash + np.sum(self.position['total'] * self.position['curr_price'])
        cursor = self.userDB.cursor()
        cursor.execute('update trade_list set total_asset=%s,valid_cash=%s where t_id= %s',
                       [str(self.net_value), str(self.cash), self.trade_id])
        cursor.execute('commit')
        cursor.close()
        print('current Net Value:\t', self.net_value)
        print(self.position['total'].values.tolist())

    def process_tick(self, s_id, curr):
        ids = np.asarray(self.order[0], dtype=str)
        amount = np.asarray(self.order[1])
        price = np.asarray([
            curr[s_id == ids[i], 1] if amount[i] > 0 else curr[s_id == ids[i], 0] for i in range(len(ids))
        ]).ravel()

        transaction_buy = np.sum((amount * price)[amount > 0] * 5e-4)
        transaction_sell = -np.sum((amount * price)[amount < 0] * 1.5e-3)
        '''
        transaction_buy = 0
        transaction_sell = 0
        '''
        cost_buy = np.sum((amount * price)[amount > 0])
        cost_sell = np.sum((amount * price)[amount < 0])
        if self.cash < transaction_buy + transaction_sell + cost_buy:
            ids, price, amount = ids[amount < 0], price[amount < 0], amount[amount < 0]
            while len(ids) > 0 and self.cash < transaction_sell:
                amount[0] += self.unit
                ids, price, amount = ids[amount < 0], price[amount < 0], amount[amount < 0]
                transaction_sell = -np.sum((amount * price) * 1.5e-3)
            cost_sell = np.sum((amount * price)[amount < 0])
            cost = cost_sell + transaction_sell
            transaction_cost = transaction_sell
            self.socket_msg['Sell'] = str(-cost_sell)
            self.socket_msg['Buy'] = str(0)
        else:
            cost = cost_sell + transaction_sell + cost_buy + transaction_buy
            transaction_cost = transaction_sell + transaction_buy
            self.socket_msg['Sell'] = str(-cost_sell)
            self.socket_msg['Buy'] = str(cost_buy)

        self.cash -= cost
        print('current Order Cost:\t', transaction_cost)
        if len(ids) == 0:
            return -1
        self.position.loc[ids[amount > 0], 'today'] += amount[amount > 0]
        self.position.loc[ids[amount < 0], 'available'] += amount[amount < 0]
        self.position.loc[ids, 'total'] += amount

        cursor = self.userDB.cursor()
        delete_sql = 'delete from position where t_id= %s and stock_id= %s'
        insert_sql = 'insert into position (volume,t_id,stock_id) values (%s,%s,%s)'
        update_sql = 'update position set volume=%s where t_id= %s and stock_id= %s'
        detail_sql = 'insert into trade_detail values (%s,%s,%s,%s,%s,%s)'

        delete_list = np.logical_and(amount < 0, self.position.loc[ids, 'total'].values == 0)
        insert_list = np.logical_and(amount > 0, self.position.loc[ids, 'total'].values == amount)
        update_list = np.logical_not(np.logical_or(delete_list, insert_list))

        direct_str = np.asarray(['buy' if i > 0 else 'sell' for i in amount])
        insert_update = np.asarray([(str(self.position.loc[ids[i], 'total']), self.trade_id, ids[i]) for i in
                                    range(len(ids))])
        delete = np.asarray([(self.trade_id, ids[i]) for i in range(len(ids))])
        detail = np.asarray(
            [(self.trade_id, ids[i], str(self.n_time), str(amount[i]), direct_str[i], str(price[i])) for i in
             range(len(ids))])

        cursor.executemany(delete_sql, delete[delete_list].tolist())
        cursor.executemany(update_sql, insert_update[update_list].tolist())
        cursor.executemany(insert_sql, insert_update[insert_list].tolist())
        cursor.executemany(detail_sql, detail.tolist())
        cursor.execute('commit')
        cursor.close()
        print_transactions(ids, price, amount)

    def process(self):
        if self.p_time is not None:
            if not self.p_time.date() == self.n_time.date():
                self.get_position()
        self.p_time = self.n_time
        print(self.n_time)
        start_time = time.time()
        s_id = np.asarray([tick[0] for tick in self.crawler.tick])
        curr = np.asarray([tick[2:5] for tick in self.crawler.tick], dtype=float)
        if self.init_state is None:
            self.init_state = curr
        self.socket_msg['market_info'] = [{
            'stock_id': ID, 'buy': str((buy - init_buy) / init_buy), 'sell': str((sell - init_sell) / init_sell)
        } for ID, buy, sell, init_buy, init_sell in
            zip(s_id, curr[:, 0], curr[:, 1], self.init_state[:, 0], self.init_state[:, 1])]
        if len(self.order[0]) > 0:
            self.process_tick(s_id, curr)
        self.order = self.get_order(s_id, curr)
        end_time = time.time()
        self.position.loc[s_id, 'curr_price'] = curr[:, 0]
        self.set_total_asset()
        print('<<<<<<<<<< process_tick uses ' + str(end_time - start_time) + 's >>>>>>>>>>')

    async def run(self):
        while True:
            start_time = time.time()
            if self.crawler.get_info():
                end_time = time.time()
                self.n_time = self.crawler.timestamp
                print('<<<<<<<<<< get_tick uses ' + str(end_time - start_time) + 's >>>>>>>>>>')
                self.process()
                await self.send_socket()
            time.sleep(0.1)

    async def test(self):
        # get begin and end timestamps from trade_list
        cursor = self.userDB.cursor()
        cursor.execute('select begin_datetime,end_datetime from trade_list where t_id=%s', self.trade_id)
        [begin, end] = cursor.fetchall()[0]

        # get all timestamps through BEGIN and END from crawl_data
        cursor = self.crawler.conn.cursor()
        cursor.execute(
            'select datetime from crawl_data where datetime between %s and %s group by datetime order by datetime',
            [begin, end])
        timestamps = [item[0] for item in cursor]
        cursor.close()

        # get market details
        cursor = self.crawler.conn.cursor()
        cursor.execute(
            'select id,price,buy,sell,amount from crawl_data where datetime between %s and %s order by datetime,id',
            [begin, end])
        ticks = [item for item in cursor]
        ticks = [ticks[i * (self.output_dims + 1):(i + 1) * (self.output_dims + 1)] for i in range(len(timestamps))]
        cursor.close()
        ticks = dict(zip(timestamps, ticks))  # wrap up all ticks
        print('find total ' + str(len(timestamps)) + ' timestamps, test back starts now')
        self.n_time = timestamps[0]
        self.get_position()
        for self.n_time in timestamps:
            self.crawler.tick = ticks[self.n_time]
            print('<<<<<<<<<< get_tick  >>>>>>>>>>')
            self.process()
            await self.send_socket()
            time.sleep(0.5)

    async def send_socket(self):
        sh50 = self.position['curr_price'].values.ravel()[0]
        if self.sh50 is None:
            self.sh50 = [sh50, sh50]
        else:
            self.sh50[1] = sh50
        self.socket_msg['curr_time'] = str(self.n_time)
        self.socket_msg['net_value'] = self.net_value
        self.socket_msg['sh50'] = self.sh50[1] / self.sh50[0] * self.scale
        position = (self.position['total'] * self.position['curr_price']).values.ravel()
        s_list = self.position.index.values.ravel()[position > 0]
        position = position[position > 0].astype(np.int).astype(np.str)
        self.socket_msg['position'] = [{'Target': 'Cash', 'Volume': str(self.cash)}]
        self.socket_msg['position'].extend([{'Target': i, 'Volume': j} for i, j in zip(s_list, position)])
        await self.socket.send(json.dumps(self.socket_msg))

    def load_model(self):
        pass

    def get_pred(self):
        return []

    def pred2amount(self, y_pred, y_curr, position):
        return []

    def get_order(self, s_id, curr):
        print('round: ', self.round)
        ids, amount = [], []
        avail = self.position.loc[s_id, 'total'].values[1:].ravel()
        if self.round < self.length - 1:
            print('get_data')
            self.replay_buffer[self.round] = curr[1:].T
        else:
            print('broking')
            self.replay_buffer[-1] = curr[1:].T
            y_pred = self.get_pred()
            amount = self.pred2amount(y_pred, curr[1:, 1].ravel(), avail)
            ids = np.asarray(s_id)[1:]
            ids = ids[amount != 0]
            amount = amount[amount != 0]
            self.replay_buffer[:-1] = self.replay_buffer[1:]
        if self.round == 0:
            self.baseline = curr[0, 0]
            print('baseline:\t', self.scale)
        else:
            print('baseline:\t', curr[0, 0] / self.baseline * self.scale)
        self.round += 1
        return ids, amount


if __name__ == '__main__':
    from modules.MLP import Strategy

    broker = Strategy(8)
    broker.test()
