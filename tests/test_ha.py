import etcd
import unittest

from mock import Mock, MagicMock, patch
from patroni.dcs import Cluster, Failover, Leader, Member
from patroni.etcd import Client, Etcd
from patroni.exceptions import DCSError, PostgresException
from patroni.ha import Ha
from test_etcd import socket_getaddrinfo, etcd_read, etcd_write, requests_get


def true(*args, **kwargs):
    return True


def false(*args, **kwargs):
    return False


def get_cluster(initialize, leader, members, failover):
    return Cluster(initialize, leader, 10, members, failover)


def get_cluster_not_initialized_without_leader():
    return get_cluster(None, None, [], None)


def get_cluster_initialized_without_leader(leader=False, failover=None):
    m = Member(0, 'leader', 28, {'conn_url': 'postgres://replicator:rep-pass@127.0.0.1:5435/postgres',
                                 'api_url': 'http://127.0.0.1:8008/patroni', 'xlog_location': 4})
    l = Leader(0, 0, m) if leader else None
    o = Member(0, 'other', 28, {'conn_url': 'postgres://replicator:rep-pass@127.0.0.1:5436/postgres',
                                'api_url': 'http://127.0.0.1:8011/patroni'})
    return get_cluster(True, l, [m, o], failover)


def get_cluster_initialized_with_leader(failover=None):
    return get_cluster_initialized_without_leader(leader=True, failover=failover)


def get_cluster_initialized_with_only_leader(failover=None):
    l = get_cluster_initialized_without_leader(leader=True, failover=failover).leader
    return get_cluster(True, l, [l], failover)


class MockPostgresql(Mock):

    name = 'postgresql0'
    role = 'replica'
    state = 'running'
    connection_string = 'postgres://foo@bar/postgres'
    server_version = '999999'
    scope = 'dummy'

    def is_healthy(self):
        return True

    def start(self):
        return True

    def is_healthiest_node(self, members):
        return True

    def is_leader(self):
        return True

    def xlog_position(self):
        return 0

    def last_operation(self):
        return 0

    def data_directory_empty(self):
        return False

    def bootstrap(self, *args, **kwargs):
        return True

    def check_replication_lag(self, last_leader_operation):
        return True

    def check_recovery_conf(self, leader):
        return False


class MockPatroni:

    def __init__(self, p, d):
        self.postgresql = p
        self.dcs = d
        self.api = Mock()
        self.tags = {}
        self.nofailover = None
        self.replicatefrom = None
        self.api.connection_string = 'http://127.0.0.1:8008'


def run_async(func, args=()):
    func(*args) if args else func()


class TestHa(unittest.TestCase):

    @patch('socket.getaddrinfo', socket_getaddrinfo)
    def setUp(self):
        with patch.object(Client, 'machines') as mock_machines:
            mock_machines.__get__ = Mock(return_value=['http://remotehost:2379'])
            self.p = MockPostgresql()
            self.p.can_create_replica_without_leader = MagicMock(return_value=False)
            self.e = Etcd('foo', {'ttl': 30, 'host': 'ok:2379', 'scope': 'test'})
            self.e.client.read = etcd_read
            self.e.client.write = etcd_write
            self.e.client.delete = Mock(side_effect=etcd.EtcdException())
            self.ha = Ha(MockPatroni(self.p, self.e))
            self.ha._async_executor.run_async = run_async
            self.ha.old_cluster = self.e.get_cluster()
            self.ha.cluster = get_cluster_not_initialized_without_leader()
            self.ha.load_cluster_from_dcs = Mock()

    def test_update_lock(self):
        self.p.last_operation = Mock(side_effect=PostgresException(''))
        self.assertTrue(self.ha.update_lock())

    def test_touch_member(self):
        self.p.xlog_position = Mock(side_effect=Exception)
        self.ha.touch_member()

    def test_start_as_replica(self):
        self.p.is_healthy = false
        self.assertEquals(self.ha.run_cycle(), 'started as a secondary')

    def test_recover_replica_failed(self):
        self.p.controldata = lambda: {'Database cluster state': 'in production'}
        self.p.is_healthy = false
        self.p.is_running = false
        self.p.follow = false
        self.assertEquals(self.ha.run_cycle(), 'started as a secondary')
        self.assertEquals(self.ha.run_cycle(), 'failed to start postgres')

    def test_recover_master_failed(self):
        self.p.follow = false
        self.p.is_healthy = false
        self.p.is_running = false
        self.ha.has_lock = true
        self.p.role = 'master'
        self.p.controldata = lambda: {'Database cluster state': 'in production'}
        self.assertEquals(self.ha.run_cycle(), 'started as readonly because i had the session lock')
        self.assertEquals(self.ha.run_cycle(), 'removed leader key after trying and failing to start postgres')

    @patch('sys.exit', return_value=1)
    @patch('patroni.ha.Ha.sysid_valid', MagicMock(return_value=True))
    def test_sysid_no_match(self, exit_mock):
        self.ha.run_cycle()
        exit_mock.assert_called_once_with(1)

    @patch.object(Cluster, 'is_unlocked', Mock(return_value=False))
    def test_start_as_readonly(self):
        self.p.is_leader = false
        self.p.is_healthy = true
        self.ha.has_lock = true
        self.assertEquals(self.ha.run_cycle(), 'promoted self to leader because i had the session lock')

    def test_acquire_lock_as_master(self):
        self.assertEquals(self.ha.run_cycle(), 'acquired session lock as a leader')

    def test_promoted_by_acquiring_lock(self):
        self.ha.is_healthiest_node = true
        self.p.is_leader = false
        self.assertEquals(self.ha.run_cycle(), 'promoted self to leader by acquiring session lock')

    def test_demote_after_failing_to_obtain_lock(self):
        self.ha.acquire_lock = false
        self.assertEquals(self.ha.run_cycle(), 'demoted self after trying and failing to obtain lock')

    def test_follow_new_leader_after_failing_to_obtain_lock(self):
        self.ha.is_healthiest_node = true
        self.ha.acquire_lock = false
        self.p.is_leader = false
        self.assertEquals(self.ha.run_cycle(), 'following new leader after trying and failing to obtain lock')

    def test_demote_because_not_healthiest(self):
        self.ha.is_healthiest_node = false
        self.assertEquals(self.ha.run_cycle(), 'demoting self because i am not the healthiest node')

    def test_follow_new_leader_because_not_healthiest(self):
        self.ha.is_healthiest_node = false
        self.p.is_leader = false
        self.assertEquals(self.ha.run_cycle(), 'following a different leader because i am not the healthiest node')

    def test_promote_because_have_lock(self):
        self.ha.cluster.is_unlocked = false
        self.ha.has_lock = true
        self.p.is_leader = false
        self.assertEquals(self.ha.run_cycle(), 'promoted self to leader because i had the session lock')

    def test_leader_with_lock(self):
        self.ha.cluster.is_unlocked = false
        self.ha.has_lock = true
        self.assertEquals(self.ha.run_cycle(), 'no action.  i am the leader with the lock')

    def test_demote_because_not_having_lock(self):
        self.ha.cluster.is_unlocked = false
        self.assertEquals(self.ha.run_cycle(), 'demoting self because i do not have the lock and i was a leader')

    def test_demote_because_update_lock_failed(self):
        self.ha.cluster.is_unlocked = false
        self.ha.has_lock = true
        self.ha.update_lock = false
        self.assertEquals(self.ha.run_cycle(), 'demoting self because i do not have the lock and i was a leader')

    def test_follow(self):
        self.ha.cluster.is_unlocked = false
        self.p.is_leader = false
        self.assertEquals(self.ha.run_cycle(), 'no action.  i am a secondary and i am following a leader')
        self.ha.patroni.replicatefrom = "foo"
        self.assertEquals(self.ha.run_cycle(), 'no action.  i am a secondary and i am following a leader')

    def test_no_etcd_connection_master_demote(self):
        self.ha.load_cluster_from_dcs = Mock(side_effect=DCSError('Etcd is not responding properly'))
        self.assertEquals(self.ha.run_cycle(), 'demoted self because DCS is not accessible and i was a leader')

    def test_bootstrap_from_leader(self):
        self.ha.cluster = get_cluster_initialized_with_leader()
        self.p.bootstrap = false
        self.assertEquals(self.ha.bootstrap(), 'trying to bootstrap from leader')

    def test_bootstrap_waiting_for_leader(self):
        self.ha.cluster = get_cluster_initialized_without_leader()
        self.assertEquals(self.ha.bootstrap(), 'waiting for leader to bootstrap')

    def test_bootstrap_without_leader(self):
        self.ha.cluster = get_cluster_initialized_without_leader()
        self.p.can_create_replica_without_leader = MagicMock(return_value=True)
        self.assertEquals(self.ha.bootstrap(), "trying to bootstrap without leader")

    def test_bootstrap_initialize_lock_failed(self):
        self.ha.cluster = get_cluster_not_initialized_without_leader()
        self.assertEquals(self.ha.bootstrap(), 'failed to acquire initialize lock')

    def test_bootstrap_initialized_new_cluster(self):
        self.ha.cluster = get_cluster_not_initialized_without_leader()
        self.e.initialize = true
        self.assertEquals(self.ha.bootstrap(), 'initialized a new cluster')

    def test_bootstrap_release_initialize_key_on_failure(self):
        self.ha.cluster = get_cluster_not_initialized_without_leader()
        self.e.initialize = true
        self.p.bootstrap = Mock(side_effect=PostgresException("Could not bootstrap master PostgreSQL"))
        self.assertRaises(PostgresException, self.ha.bootstrap)

    def test_reinitialize(self):
        self.ha.schedule_reinitialize()
        self.ha.schedule_reinitialize()
        self.ha.run_cycle()
        self.assertIsNone(self.ha._async_executor.scheduled_action)

        self.ha.cluster = get_cluster_initialized_with_leader()
        self.ha.has_lock = true
        self.ha.schedule_reinitialize()
        self.ha.run_cycle()
        self.assertIsNone(self.ha._async_executor.scheduled_action)

        self.ha.has_lock = false
        self.ha.schedule_reinitialize()
        self.ha.run_cycle()

    def test_restart(self):
        self.assertEquals(self.ha.restart(), (True, 'restarted successfully'))
        self.p.restart = false
        self.assertEquals(self.ha.restart(), (False, 'restart failed'))
        self.ha.schedule_reinitialize()
        self.assertEquals(self.ha.restart(), (False, 'reinitialize already in progress'))

    def test_restart_in_progress(self):
        self.ha._async_executor.schedule('restart', True)
        self.assertTrue(self.ha.restart_scheduled())
        self.assertEquals(self.ha.run_cycle(), 'not healthy enough for leader race')

        self.ha.cluster = get_cluster_initialized_with_leader()
        self.assertEquals(self.ha.run_cycle(), 'restart in progress')

        self.ha.has_lock = true
        self.assertEquals(self.ha.run_cycle(), 'updated leader lock during restart')

        self.ha.update_lock = false
        self.assertEquals(self.ha.run_cycle(), 'failed to update leader lock during restart')

    @patch('requests.get', requests_get)
    def test_manual_failover_from_leader(self):
        self.ha.has_lock = true
        self.ha.cluster = get_cluster_initialized_with_leader(Failover(0, 'blabla', ''))
        self.assertEquals(self.ha.run_cycle(), 'no action.  i am the leader with the lock')
        self.ha.cluster = get_cluster_initialized_with_leader(Failover(0, '', MockPostgresql.name))
        self.assertEquals(self.ha.run_cycle(), 'no action.  i am the leader with the lock')
        self.ha.cluster = get_cluster_initialized_with_leader(Failover(0, '', 'blabla'))
        self.assertEquals(self.ha.run_cycle(), 'no action.  i am the leader with the lock')
        f = Failover(0, MockPostgresql.name, '')
        self.ha.cluster = get_cluster_initialized_with_leader(f)
        self.assertEquals(self.ha.run_cycle(), 'manual failover: demoting myself')
        self.ha.fetch_node_status = lambda e: (e, True, True, 0, {'nofailover': 'True'})
        self.assertEquals(self.ha.run_cycle(), 'no action.  i am the leader with the lock')
        # manual failover from the previous leader to us won't happen if we hold the nofailover flag
        self.ha.cluster = get_cluster_initialized_with_leader(Failover(0, 'blabla', MockPostgresql.name))
        self.assertEquals(self.ha.run_cycle(), 'no action.  i am the leader with the lock')

    @patch('requests.get', requests_get)
    def test_manual_failover_process_no_leader(self):
        self.p.is_leader = false
        self.ha.cluster = get_cluster_initialized_without_leader(failover=Failover(0, '', MockPostgresql.name))
        self.assertEquals(self.ha.run_cycle(), 'promoted self to leader by acquiring session lock')
        self.ha.cluster = get_cluster_initialized_without_leader(failover=Failover(0, '', 'leader'))
        self.assertEquals(self.ha.run_cycle(), 'promoted self to leader by acquiring session lock')
        self.ha.fetch_node_status = lambda e: (e, True, True, 0, {})  # accessible, in_recovery
        self.assertEquals(self.ha.run_cycle(), 'following a different leader because i am not the healthiest node')
        self.ha.cluster = get_cluster_initialized_without_leader(failover=Failover(0, MockPostgresql.name, ''))
        self.assertEquals(self.ha.run_cycle(), 'following a different leader because i am not the healthiest node')
        self.ha.fetch_node_status = lambda e: (e, False, True, 0, {})  # inaccessible, in_recovery
        self.assertEquals(self.ha.run_cycle(), 'promoted self to leader by acquiring session lock')
        # set failover flag to True for all members of the cluster
        # this should elect the current member, as we are not going to call the API for it.
        self.ha.cluster = get_cluster_initialized_without_leader(failover=Failover(0, '', 'other'))
        self.ha.fetch_node_status = lambda e: (e, True, True, 0, {'nofailover': 'True'})  # accessible, in_recovery
        self.assertEquals(self.ha.run_cycle(), 'promoted self to leader by acquiring session lock')
        # same as previous, but set the current member to nofailover. In no case it should be elected as a leader
        self.ha.patroni.nofailover = True
        self.assertEquals(self.ha.run_cycle(), 'following a different leader because I am not allowed to promote')

    def test_is_healthiest_node(self):
        self.ha.state_handler.is_leader = false
        self.ha.patroni.nofailover = False
        self.ha.fetch_node_status = lambda e: (e, True, True, 0, {})
        self.assertTrue(self.ha.is_healthiest_node())

    def test__is_healthiest_node(self):
        self.assertTrue(self.ha._is_healthiest_node(self.ha.old_cluster.members))
        self.p.is_leader = false
        self.ha.fetch_node_status = lambda e: (e, True, True, 0, {})  # accessible, in_recovery
        self.assertTrue(self.ha._is_healthiest_node(self.ha.old_cluster.members))
        self.ha.fetch_node_status = lambda e: (e, True, False, 0, {})  # accessible, not in_recovery
        self.assertFalse(self.ha._is_healthiest_node(self.ha.old_cluster.members))
        self.ha.fetch_node_status = lambda e: (e, True, True, 1, {})  # accessible, in_recovery, xlog location ahead
        self.assertFalse(self.ha._is_healthiest_node(self.ha.old_cluster.members))
        self.p.check_replication_lag = false
        self.assertFalse(self.ha._is_healthiest_node(self.ha.old_cluster.members))
        self.ha.patroni.nofailover = True
        self.assertFalse(self.ha._is_healthiest_node(self.ha.old_cluster.members))
        self.ha.patroni.nofailover = False

    @patch('requests.get', requests_get)
    def test_fetch_node_status(self):
        member = Member(0, 'test', 1, {'api_url': 'http://127.0.0.1:8011/patroni'})
        self.ha.fetch_node_status(member)
        member = Member(0, 'test', 1, {'api_url': 'http://localhost:8011/patroni'})
        self.ha.fetch_node_status(member)

    def test_post_recover(self):
        self.p.is_running = false
        self.ha.has_lock = true
        self.assertEqual(self.ha.post_recover(), 'removed leader key after trying and failing to start postgres')
        self.ha.has_lock = false
        self.assertEqual(self.ha.post_recover(), 'failed to start postgres')
        self.p.is_running = true
        self.assertIsNone(self.ha.post_recover())
