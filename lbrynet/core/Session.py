import logging
import miniupnpc
from lbrynet.core.BlobManager import DiskBlobManager
from lbrynet.dht import node
from lbrynet.core.PeerManager import PeerManager
from lbrynet.core.RateLimiter import RateLimiter
from lbrynet.core.client.DHTPeerFinder import DHTPeerFinder
from lbrynet.core.server.DHTHashAnnouncer import DHTHashAnnouncer
from lbrynet.core.PaymentRateManager import BasePaymentRateManager, NegotiatedPaymentRateManager
from lbrynet.core.BlobAvailability import BlobAvailabilityTracker
from twisted.internet import threads, defer, reactor

log = logging.getLogger(__name__)


class Session(object):
    """This class manages all important services common to any application that uses the network.

    the hash announcer, which informs other peers that this peer is
    associated with some hash. Usually, this means this peer has a
    blob identified by the hash in question, but it can be used for
    other purposes.

    the peer finder, which finds peers that are associated with some
    hash.

    the blob manager, which keeps track of which blobs have been
    downloaded and provides access to them,

    the rate limiter, which attempts to ensure download and upload
    rates stay below a set maximum

    upnp, which opens holes in compatible firewalls so that remote
    peers can connect to this peer.
    """

    def __init__(self, blob_data_payment_rate, db_dir=None, node_id=None, peer_manager=None,
                 dht_node_port=None, known_dht_nodes=None, peer_finder=None, hash_announcer=None,
                 blob_dir=None, blob_manager=None, peer_port=None, use_upnp=True,
                 rate_limiter=None, wallet=None, dht_node_class=None, blob_tracker_class=None,
                 payment_rate_manager_class=None, is_generous=True, external_ip=None):
        """@param blob_data_payment_rate: The default payment rate for blob data

        @param db_dir: The directory in which levelDB files should be stored

        @param node_id: The unique ID of this node

        @param peer_manager: An object which keeps track of all known
            peers. If None, a PeerManager will be created

        @param dht_node_port: The port on which the dht node should
            listen for incoming connections

        @param known_dht_nodes: A list of nodes which the dht node
        should use to bootstrap into the dht

        @param peer_finder: An object which is used to look up peers
            that are associated with some hash. If None, a
            DHTPeerFinder will be used, which looks for peers in the
            distributed hash table.

        @param hash_announcer: An object which announces to other
            peers that this peer is associated with some hash.  If
            None, and peer_port is not None, a DHTHashAnnouncer will
            be used. If None and peer_port is None, a
            DummyHashAnnouncer will be used, which will not actually
            announce anything.

        @param blob_dir: The directory in which blobs will be
            stored. If None and blob_manager is None, blobs will be
            stored in memory only.

        @param blob_manager: An object which keeps track of downloaded
            blobs and provides access to them. If None, and blob_dir
            is not None, a DiskBlobManager will be used, with the
            given blob_dir.  If None and blob_dir is None, a
            TempBlobManager will be used, which stores blobs in memory
            only.

        @param peer_port: The port on which other peers should connect
            to this peer

        @param use_upnp: Whether or not to try to open a hole in the
            firewall so that outside peers can connect to this peer's
            peer_port and dht_node_port

        @param rate_limiter: An object which keeps track of the amount
            of data transferred to and from this peer, and can limit
            that rate if desired

        @param wallet: An object which will be used to keep track of
            expected payments and which will pay peers.  If None, a
            wallet which uses the Point Trader system will be used,
            which is meant for testing only
        """

        self.db_dir = db_dir
        self.node_id = node_id
        self.peer_manager = peer_manager or PeerManager()
        self.dht_node_port = dht_node_port
        self.known_dht_nodes = known_dht_nodes
        if self.known_dht_nodes is None:
            self.known_dht_nodes = []
        self.blob_dir = blob_dir
        self.blob_tracker = None
        self.blob_tracker_class = blob_tracker_class or BlobAvailabilityTracker
        self.peer_port = peer_port
        self.use_upnp = use_upnp
        self.rate_limiter = rate_limiter or RateLimiter()
        self.external_ip = external_ip
        self.upnp_redirects = []
        self.wallet = wallet
        self.dht_node_class = dht_node_class or node.Node
        self.dht_node = self.dht_node_class(
            udpPort=self.dht_node_port,
            node_id=self.node_id,
            externalIP=self.external_ip
        )
        self.peer_finder = peer_finder or DHTPeerFinder(self.dht_node, self.peer_manager)
        self.hash_announcer = hash_announcer or DHTHashAnnouncer(self.dht_node, self.peer_port)
        self.blob_manager = blob_manager or DiskBlobManager(self.hash_announcer,
                                                    self.blob_dir,
                                                    self.db_dir)
        self.blob_tracker = self.blob_tracker_class(self.blob_manager,
                                                    self.peer_finder,
                                                    self.dht_node)
        self.base_payment_rate_manager = BasePaymentRateManager(blob_data_payment_rate)

        self.payment_rate_manager_class = payment_rate_manager_class or NegotiatedPaymentRateManager
        self.is_generous = is_generous
        self.payment_rate_manager = self.payment_rate_manager_class(
            self.base_payment_rate_manager,
            self.blob_tracker,
            self.is_generous)

    @defer.inlineCallbacks
    def setup(self):
        """Create the blob directory and database if necessary, start all desired services"""

        log.info("Starting session.")

        if self.wallet is None:
            from lbrynet.core.PTCWallet import PTCWallet
            self.wallet = PTCWallet(self.db_dir)

        if self.use_upnp is True:
            yield self._try_upnp()

        log.info("Starting DHT")

        hosts = []
        for host, port in self.known_dht_nodes:
            h = yield reactor.resolve(host)
            hosts.append((h, port))

        yield self.dht_node.joinNetwork(tuple(hosts))
        self.peer_finder.run_manage_loop()
        yield self.blob_manager.setup()
        self.hash_announcer.run_manage_loop()

        self.rate_limiter.start()
        yield self.wallet.start()
        yield self.blob_tracker.start()

    @defer.inlineCallbacks
    def shut_down(self):
        """Stop all services"""
        log.info('Stopping session.')
        yield self.blob_tracker.stop()
        yield self.dht_node.stop()
        yield self.rate_limiter.stop()
        yield self.peer_finder.stop()
        yield self.hash_announcer.stop()
        yield self.wallet.stop()
        yield self.blob_manager.stop()
        yield self._unset_upnp()

    def _try_upnp(self):
        log.debug("In _try_upnp")

        def threaded_try_upnp():
            u = miniupnpc.UPnP()
            num_devices_found = u.discover()
            if num_devices_found > 0:
                u.selectigd()
                if self.peer_port is not None:
                    if u.getspecificportmapping(self.peer_port, 'TCP') is None:
                        u.addportmapping(
                            self.peer_port, 'TCP', u.lanaddr, self.peer_port,
                            'LBRY peer port', '')
                        self.upnp_redirects.append((self.peer_port, 'TCP'))
                        log.info("Set UPnP redirect for TCP port %d", self.peer_port)
                    else:
                        # see comment below
                        log.warning("UPnP redirect already set for TCP port %d", self.peer_port)
                        self.upnp_redirects.append((self.peer_port, 'TCP'))
                if self.dht_node_port is not None:
                    if u.getspecificportmapping(self.dht_node_port, 'UDP') is None:
                        u.addportmapping(
                            self.dht_node_port, 'UDP', u.lanaddr, self.dht_node_port,
                            'LBRY DHT port', '')
                        self.upnp_redirects.append((self.dht_node_port, 'UDP'))
                        log.info("Set UPnP redirect for UDP port %d", self.dht_node_port)
                    else:
                        # TODO: check that the existing redirect was
                        # put up by an old lbrynet session before
                        # grabbing it if such a disconnected redirect
                        # exists, then upnp won't work unless the
                        # redirect is appended or is torn down and set
                        # back up. a bad shutdown of lbrynet could
                        # leave such a redirect up and cause problems
                        # on the next start.  this could be
                        # problematic if a previous lbrynet session
                        # didn't make the redirect, and it was made by
                        # another application
                        log.warning("UPnP redirect already set for UDP port %d", self.dht_node_port)
                        self.upnp_redirects.append((self.dht_node_port, 'UDP'))
                return True
            raise Exception("UPnP failed")

        @defer.inlineCallbacks
        def upnp_failed(err):
            log.warning("UPnP failed. Reason: %s", err.getErrorMessage())
            yield self._get_external_ip()
            defer.returnValue(False)

        d = threads.deferToThread(threaded_try_upnp)
        d.addErrback(upnp_failed)
        return d

    def _unset_upnp(self):
        log.info("Unsetting upnp for session")

        def threaded_unset_upnp():
            u = miniupnpc.UPnP()
            num_devices_found = u.discover()
            if num_devices_found > 0:
                u.selectigd()
                for port, protocol in self.upnp_redirects:
                    if u.getspecificportmapping(port, protocol) is None:
                        log.warning(
                            "UPnP redirect for %s %d was removed by something else.",
                            protocol, port)
                    else:
                        u.deleteportmapping(port, protocol)
                        log.info("Removed UPnP redirect for %s %d.", protocol, port)
                self.upnp_redirects = []

        d = threads.deferToThread(threaded_unset_upnp)
        d.addErrback(lambda err: str(err))
        return d
