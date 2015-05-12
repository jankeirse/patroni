#!/usr/bin/env python

import logging
import os
import signal
import sys
import time
import yaml

from helpers.etcd import Etcd
from helpers.postgresql import Postgresql
from helpers.ha import Ha


def sigterm_handler(signo, stack_frame):
    sys.exit()


class Governor:

    def __init__(self, config):
        self.nap_time = config['loop_wait']
        self.etcd = Etcd(config['etcd'])
        self.postgresql = Postgresql(config['postgresql'])
        self.ha = Ha(self.postgresql, self.etcd)

    def initialize(self):
        # wait for etcd to be available
        while not self.etcd.touch_member(self.postgresql.name, self.postgresql.connection_string):
            logging.info('waiting on etcd')
            time.sleep(5)

        # is data directory empty?
        if self.postgresql.data_directory_empty():
            # racing to initialize
            if self.etcd.race('/initialize', self.postgresql.name):
                self.postgresql.initialize()
                self.etcd.take_leader(self.postgresql.name)
                self.postgresql.start()
                self.postgresql.create_replication_user()
            else:
                while True:
                    leader = self.etcd.current_leader()
                    if leader and self.postgresql.sync_from_leader(leader):
                        self.postgresql.write_recovery_conf(leader)
                        self.postgresql.start()
                        break
                    time.sleep(5)

    def run(self):
        while True:
            logging.info(self.ha.run_cycle())
            time.sleep(self.nap_time)


def main():
    if len(sys.argv) < 2 or not os.path.isfile(sys.argv[1]):
        print('Usage: {} config.yml'.format(sys.argv[0]))
        return

    with open(sys.argv[1], 'r') as f:
        config = yaml.load(f)

    governor = Governor(config)
    try:
        governor.initialize()
        governor.run()
    finally:
        governor.postgresql.stop()


if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=logging.INFO)
    signal.signal(signal.SIGTERM, sigterm_handler)
    main()
