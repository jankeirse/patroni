import logging
import os
import sys
import time
import yaml

from patroni.api import RestApiServer
from patroni.etcd import Etcd
from patroni.ha import Ha
from patroni.postgresql import Postgresql
from patroni.utils import setup_signal_handlers, reap_children
from patroni.zookeeper import ZooKeeper
from .version import __version__

logger = logging.getLogger(__name__)


class Patroni(object):
    PATRONI_CONFIG_VARIABLE = 'PATRONI_CONFIGURATION'

    def __init__(self, config):
        self.nap_time = config['loop_wait']
        self.tags = config.get('tags', dict())
        self.postgresql = Postgresql(config['postgresql'])
        self.dcs = self.get_dcs(self.postgresql.name, config)
        self.version = __version__
        self.api = RestApiServer(self, config['restapi'])
        self.ha = Ha(self)
        self.next_run = time.time()

    @property
    def nofailover(self):
        return self.tags.get('nofailover', False)

    @property
    def replicatefrom(self):
        return self.tags.get('replicatefrom')

    @property
    def clonefrom(self):
        return self.tags.get('clonefrom')

    @staticmethod
    def get_dcs(name, config):
        if 'etcd' in config:
            return Etcd(name, config['etcd'])
        if 'zookeeper' in config:
            return ZooKeeper(name, config['zookeeper'])
        raise Exception('Can not find suitable configuration of distributed configuration store')

    def schedule_next_run(self):
        self.next_run += self.nap_time
        current_time = time.time()
        nap_time = self.next_run - current_time
        if nap_time <= 0:
            self.next_run = current_time
        elif self.dcs.watch(nap_time):
            self.next_run = time.time()

    def run(self):
        self.api.start()
        self.next_run = time.time()

        while True:
            logger.info(self.ha.run_cycle())
            reap_children()
            self.schedule_next_run()


def main():
    logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=logging.INFO)
    logging.getLogger('requests').setLevel(logging.WARNING)
    setup_signal_handlers()

    # Patroni reads the configuration from the command-line argument if it exists, and from the environment otherwise.
    use_env = False
    use_file = (len(sys.argv) >= 2 and os.path.isfile(sys.argv[1]))
    if not use_file:
        config_env = os.environ.get(Patroni.PATRONI_CONFIG_VARIABLE)
        use_env = config_env is not None
        if not use_env:
            print('Usage: {0} config.yml'.format(sys.argv[0]))
            print('\tPatroni may also read the configuration from the {} environment variable'.
                  format(Patroni.PATRONI_CONFIG_VARIABLE))
            return

    if use_file:
        with open(sys.argv[1], 'r') as f:
            config = yaml.load(f)
    elif use_env:
        config = yaml.load(config_env)

    patroni = Patroni(config)
    try:
        patroni.run()
    except KeyboardInterrupt:
        pass
    finally:
        patroni.api.shutdown()
        patroni.postgresql.stop(checkpoint=False)
        patroni.dcs.delete_leader()
