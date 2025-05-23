"""Core module to calculate aggregate data"""

import json
import logging
import queue as q
from datetime import datetime

from pkg_resources import resource_string
from typing import Collection

from .aggregator import Aggregator, DataPoller
from .chopper import TimeChopper
from yandextank.common.interfaces import AggregateResultListener, StatsReader

from yandextank.contrib.netort.netort.data_processing import Drain, Chopper, get_nowait_from_queue

logger = logging.getLogger(__name__)


class LoggingListener(AggregateResultListener):
    """Log aggregated results"""

    def on_aggregated_data(self, data, stats):
        logger.info("Got aggregated sample:\n%s", json.dumps(data, indent=2))
        logger.info("Stats:\n%s", json.dumps(stats, indent=2))


class TankAggregator(object):
    """
    Plugin that manages aggregation and stats collection
    """

    SECTION = 'aggregator'

    @staticmethod
    def get_key():
        return __file__

    def __init__(self, generator, poller: DataPoller, termination_timeout: float = 60):
        # AbstractPlugin.__init__(self, core, cfg)
        """

        :type generator: GeneratorPlugin
        """
        self.generator = generator
        self.listeners = []  # [LoggingListener()]
        self.results = q.Queue()
        self.stats_results = q.Queue()
        self.data_cache = {}
        self.stat_cache = {}
        self.reader = None
        self.stats_reader = None
        self.drain = None
        self.stats_drain = None
        self.termination_timeout = termination_timeout
        self.poller = poller

    @staticmethod
    def load_config():
        return json.loads(resource_string(__name__, 'config/phout.json').decode('utf8'))

    def start_test(self):
        self.reader = self.generator.get_reader()
        self.stats_reader = self.generator.get_stats_reader()
        aggregator_config = self.load_config()

        if self.reader and self.stats_reader:
            readers = self.reader
            if not isinstance(readers, Collection):
                readers = [readers]

            sources = [self.poller.poll(r) for r in readers]
            pipeline = Aggregator(TimeChopper(sources), aggregator_config)
            self.drain = Drain(pipeline, self.results)
            self.drain.start()

            stats_drain_source = Chopper(self.poller.poll(self.stats_reader))
            self.stats_drain = Drain(stats_drain_source, self.stats_results)
            self.stats_drain.start()
        else:
            logger.warning("Generator not found. Generator must provide a reader and a stats_reader interface")

    def _collect_data(self, end=False):
        """
        Collect data, cache it and send to listeners
        """
        data = get_nowait_from_queue(self.results)
        stats = get_nowait_from_queue(self.stats_results)
        logger.debug("Data timestamps: %s" % [d.get('ts') for d in data])
        logger.debug("Stats timestamps: %s" % [d.get('ts') for d in stats])
        for item in data:
            ts = item['ts']
            if ts in self.stat_cache:
                # send items
                data_item = item
                stat_item = self.stat_cache.pop(ts)
                self.__notify_listeners(data_item, stat_item)
            else:
                self.data_cache[ts] = item
        for item in stats:
            ts = item['ts']
            if ts in self.data_cache:
                # send items
                data_item = self.data_cache.pop(ts)
                stat_item = item
                self.__notify_listeners(data_item, stat_item)
            else:
                self.stat_cache[ts] = item
        if end and len(self.data_cache) > 0:
            logger.info('Timestamps without stats:')
            for ts, data_item in sorted(self.data_cache.items(), key=lambda i: i[0]):
                logger.info(ts)
                self.__notify_listeners(data_item, StatsReader.stats_item(ts, 0, 0))

    def is_aggr_finished(self):
        return self.drain._finished.is_set() and self.stats_drain._finished.is_set()

    def is_test_finished(self):
        self._collect_data()
        return -1

    def end_test(self, retcode):
        timeouter = _Timeouter(self.termination_timeout)
        retcode = self.generator.end_test(retcode)
        if self.stats_reader:
            logger.info('Closing stats reader')
            self.stats_reader.close()
        if self.drain:
            timeout = timeouter.get_remaining_timeout()
            logger.info('Waiting for gun drain to finish for %f seconds', timeout)
            self.drain.join(timeout)
            if self.drain.is_alive():
                logger.warning('The gun drain didn\'t finish in time. Some data might be lost.')
            self.drain.close()

            timeout = timeouter.get_remaining_timeout()
            logger.info('Waiting for stats drain to finish for %f seconds', timeout)
            self.stats_drain.join(timeout)
            if self.drain.is_alive():
                logger.warning('The stats drain didn\'t finish in time. Some data might be lost.')
            self.stats_drain.close()
        logger.info('Collecting remaining data')
        self._collect_data(end=True)
        return retcode

    def add_result_listener(self, listener):
        self.listeners.append(listener)

    def __notify_listeners(self, data, stats):
        """notify all listeners about aggregate data and stats"""
        for listener in self.listeners:
            listener.on_aggregated_data(data, stats)


class _Timeouter:
    def __init__(self, total_timeout: float):
        self.total_timeout = total_timeout
        self.started = datetime.now()

    def get_remaining_timeout(self) -> float:
        return max(0.1, self.total_timeout - (datetime.now() - self.started).seconds)
