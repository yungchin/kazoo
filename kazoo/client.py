"""Kazoo Zookeeper Client

"""
import inspect
import logging
import os
from collections import namedtuple
from functools import partial

import zookeeper

from kazoo.exceptions import ConfigurationError
from kazoo.exceptions import ZookeeperStoppedError
from kazoo.exceptions import err_to_exception
from kazoo.handlers.threading import SequentialThreadingHandler
from kazoo.retry import KazooRetry
from kazoo.handlers.util import thread

log = logging.getLogger(__name__)

ZK_OPEN_ACL_UNSAFE = {"perms": zookeeper.PERM_ALL, "scheme": "world",
                       "id": "anyone"}


# Setup Zookeeper logging thread
_logging_pipe = os.pipe()
zookeeper.set_log_stream(os.fdopen(_logging_pipe[1], 'w'))


@thread
def _loggingthread():
    """Zookeeper logging redirect

    Zookeeper by default logs directly out. This thread handles reading off
    the pipe that the above `set_log_stream` call designates so that the
    Zookeeper logging output can be turned into Python logging statements
    under the `Zookeeper` name.

    """
    r, w = _logging_pipe
    log = logging.getLogger('ZooKeeper').log
    f = os.fdopen(r)
    levels = dict(ZOO_INFO=logging.INFO,
                  ZOO_WARN=logging.WARNING,
                  ZOO_ERROR=logging.ERROR,
                  ZOO_DEBUG=logging.DEBUG,
                  )
    while 1:
        line = f.readline().strip()
        try:
            if '@' in line:
                level, message = line.split('@', 1)
                level = levels.get(level.split(':')[-1])

                if 'Exceeded deadline by' in line and level == logging.WARNING:
                    level = logging.DEBUG

            else:
                level = None

            if level is None:
                log(logging.INFO, line)
            else:
                log(level, message)
        except Exception, v:
            logging.getLogger('ZooKeeper').exception("Logging error: %s", v)


def validate_path(path):
    if not path.startswith('/'):
        raise ValueError("invalid path '%s'. must start with /" % path)


def _generic_callback(async_result, handle, code, *args):
    if code != zookeeper.OK:
        exc = err_to_exception(code)
        async_result.set_exception(exc)
    else:
        if not args:
            result = None
        elif len(args) == 1:
            result = args[0]
        else:
            # if there's two, the second is a stat object
            args = list(args)
            if len(args) == 2:
                args[1] = ZnodeStat(**args[1])
            result = tuple(args)

        async_result.set(result)


def _exists_callback(async_result, handle, code, stat):
    if code not in (zookeeper.OK, zookeeper.NONODE):
        exc = err_to_exception(code)
        async_result.set_exception(exc)
    else:
        async_result.set(stat)


class KazooState(object):
    """High level connection state values

    States inspired by Netflix Curator.

    .. attribute:: SUSPENDED

        The connection has been lost but may be recovered.
        We should operate in a "safe mode" until then.

    .. attribute:: CONNECTED

        The connection is alive and well.

    .. attribute:: LOST

        The connection has been confirmed dead. Any ephemeral nodes
        will need to be recreated upon re-establishing a connection.

    """
    SUSPENDED = "SUSPENDED"
    CONNECTED = "CONNECTED"
    LOST = "LOST"


class KeeperState(object):
    """Zookeeper State

    Represents the Zookeeper state. Watch functions will recieve a
    :class:`KeeperState` attribute as their state argument.

    .. attribute:: ASSOCIATING

        The Zookeeper ASSOCIATING state

    .. attribute:: AUTH_FAILED

        Authentication has failed, this is an unrecoverable error.

    .. attribute:: CONNECTED

        Zookeeper is  connected

    .. attribute:: CONNECTING

        Zookeeper is currently attempting to establish a connection

    .. attribute:: EXPIRED_SESSION

        The prior session was invalid, all prior ephemeral nodes are
        gone.

    """
    ASSOCIATING = zookeeper.ASSOCIATING_STATE
    AUTH_FAILED = zookeeper.AUTH_FAILED_STATE
    CONNECTED = zookeeper.CONNECTED_STATE
    CONNECTING = zookeeper.CONNECTING_STATE
    EXPIRED_SESSION = zookeeper.EXPIRED_SESSION_STATE


class EventType(object):
    """Zookeeper Event

    Represents a Zookeeper event. Events trigger watch functions which
    will recieve a :class:`EventType` attribute as their event argument.

    .. attribute:: NOTWATCHING

        This event type was added to Zookeeper in the event that watches
        get overloaded. It's never been used though and will likely be
        removed in a future Zookeeper version. **This event will never
        actually be set, don't bother testing for it.**

    .. attribute:: SESSION

        A Zookeeper session event. Watch functions do not recieve session
        events. A session event watch can be registered with
        :class:`KazooClient` during creation that can recieve these events.
        It's recommended to add a listener for connection state changes
        instead.

    .. attribute:: CREATED

        A node has been created.

    .. attribute:: DELETED

        A node has been deleted.

    .. attribute:: CHANGED

        The data for a node has changed.

    .. attribute:: CHILD

        The children under a node has changed (a child was added or removed).
        This event does not indicate the data for a child node has changed,
        which must have its own watch established.

    """
    NOTWATCHING = zookeeper.NOTWATCHING_EVENT
    SESSION = zookeeper.SESSION_EVENT
    CREATED = zookeeper.CREATED_EVENT
    DELETED = zookeeper.DELETED_EVENT
    CHANGED = zookeeper.CHANGED_EVENT
    CHILD = zookeeper.CHILD_EVENT


class WatchedEvent(namedtuple('WatchedEvent', ('type', 'state', 'path'))):
    """A change on ZooKeeper that a Watcher is able to respond to.

    The :class:`WatchedEvent` includes exactly what happened, the current state
    of ZooKeeper, and the path of the znode that was involved in the event. An
    instance of :class:`WatchedEvent` will be passed to registered watch
    functions.

    .. attribute:: type

        A :class:`EventType` attribute indicating the event type.

    .. attribute:: state

        A :class:`KeeperState` attribute indicating the Zookeeper state.

    .. attribute:: path

        The path of the node for the watch event.

    """


class ZnodeStat(namedtuple('ZnodeStat', ('aversion', 'ctime', 'cversion',
                                         'czxid', 'dataLength',
                                         'ephemeralOwner', 'mtime', 'mzxid',
                                         'numChildren', 'pzxid', 'version'))):
    """A ZnodeStat structure with convenience properties

    When getting the value of a node from Zookeeper, the properties for the
    node known as a "Stat structure" will be retrieved. The
    :class:`ZnodeStat` object provides access to the standard Stat
    properties and additional properties that are more readable and
    use Python time semantics (seconds since epoch instead of ms).

    .. note::

        The original Zookeeper Stat name is in parens next to the name when it
        differs from the convenience attribute. These are **not functions**,
        just attributes.

    .. attribute:: creation_transaction_id (czxid)

        The transaction id of the change that caused this znode to be created.

    .. attribute:: last_modified_transaction_id (mzxid)

        The transaction id of the change that last modified this znode.

    .. attribute:: created (ctime)

        The time in seconds from epoch when this node was created. (ctime is
        in milliseconds)

    .. attribute:: last_modified (mtime)

        The time in seconds from epoch when this znode was last modified.
        (mtime is in milliseconds)

    .. attribute:: version

        The number of changes to the data of this znode.

    .. attribute:: acl_version (aversion)

        The number of changes to the ACL of this znode.

    .. attribute:: owner_session_id (ephemeralOwner)

        The session id of the owner of this znode if the znode is an
        ephemeral node. If it is not an ephemeral node, it will be `None`.
        (ephemeralOwner will be 0 if its not ephemeral)

    .. attribute:: data_length (dataLength)

        The length of the data field of this znode.

    .. attribute:: children_count (numChildren)

        The number of children of this znode.

    """
    @property
    def acl_version(self):
        return self.aversion

    @property
    def children_version(self):
        return self.cversion

    @property
    def created(self):
        return self.ctime / 1000.0

    @property
    def last_modified(self):
        return self.mtime / 1000.0

    @property
    def owner_session_id(self):
        return self.ephemeralOwner or None

    @property
    def creation_transaction_id(self):
        return self.czxid

    @property
    def last_modified_transaction_id(self):
        return self.mzxid

    @property
    def data_length(self):
        return self.dataLength

    @property
    def children_count(self):
        return self.numChildren


class Callback(namedtuple('Callback', ('type', 'func', 'args'))):
    """A callback being triggered

    :param type: Type of the callback, can be 'session' or 'watch'
    :param func: Callback function
    :param args: Argument list for the callback function

    """


class KazooClient(object):
    """An Apache Zookeeper Python wrapper supporting alternate callback
    handlers and high-level functionality


    Watch functions registered with this class will not get session
    events, unlike the default Zookeeper watch's. They will also be
    called with a single argument, a :class:`WatchedEvent` instance.

    """
    def __init__(self, hosts='127.0.0.1:2181', namespace=None, watcher=None,
                 timeout=10.0, client_id=None, max_retries=None, handler=None):
        """Create a KazooClient instance

        :param hosts: List of hosts to connect to
        :param namespace: A Zookeeper namespace for nodes to be used under
        :param watcher: Set a default watcher. This will be called by the
                        actual default watcher that :class:`KazooClient`
                        establishes.
        :param timeout: The longest to wait for a Zookeeper connection
        :param client_id: A Zookeeper client id, used when re-establishing a
                          prior session connection
        :param handler: An instance of a class implementing the
                        :class:`~kazoo.interfaces.IHandler` interface for
                        callback handling

        """
        # remove any trailing slashes
        if namespace:
            namespace = namespace.rstrip('/')
        if namespace:
            validate_path(namespace)
        self.namespace = namespace

        self._needs_ensure_path = bool(namespace)

        self._hosts = hosts
        self._watcher = watcher
        self._provided_client_id = client_id

        # ZK uses milliseconds
        self._timeout = int(timeout * 1000)

        # Record the handler strategy used
        self._handler = handler if handler else SequentialThreadingHandler()

        if inspect.isclass(self._handler):
            raise ConfigurationError("Handler must be an instance of a class, "
                                     "not the class: %s" % self._handler)

        self._handle = None

        # We use events like twitter's client to track current and desired
        # state (connected, and whether to shutdown)
        self._live = self._handler.event_object()
        self._stopped = self._handler.event_object()
        self._stopped.set()

        self._connection_timed_out = False

        self.retry = KazooRetry(max_retries)

        # Curator like simplified state tracking, and listeners for state
        # transitions
        self.state = KazooState.LOST
        self.state_listeners = set()

    def _safe_call(self, func, async_result, *args, **kwargs):
        """Safely call a zookeeper function and handle errors related
        to a bad zhandle"""
        try:
            return func(self._handle, *args)
        except TypeError:
            # Handle was cleared or isn't set. If it was cleared and we are
            # supposed to be running, it means we had session expiration and
            # are attempting to reconnect. We treat it as a connection loss
            # in that case for appropriate behavior.
            if self._stopped.is_set():
                async_result.set_exception(
                    ZookeeperStoppedError(
                    "The Kazoo client is stopped, call connect before running "
                    " commands that talk to Zookeeper"))
            else:
                async_result.set_exception(
                    zookeeper.ConnectionLossException("connection loss"))

    def _assure_namespace(self):
        if self._needs_ensure_path:
            self.ensure_path('/')
            self._needs_ensure_path = False

    def add_listener(self, listener):
        """Add a function to be called for connection state changes

        This function will be called with a :class:`KazooState` instance
        indicating the new connection state.

        """
        if not (listener and callable(listener)):
            raise ConfigurationError("listener must be callable")
        self.state_listeners.add(listener)

    def remove_listener(self, listener):
        """Remove a listener function"""
        self.state_listeners.discard(listener)

    @property
    def connected(self):
        """Returns whether the Zookeeper connection has been established"""
        return self._live.is_set()

    @property
    def client_id(self):
        """Returns the client id for this Zookeeper session if connected"""
        if self._handle is not None:
            return zookeeper.client_id(self._handle)
        return None

    def _wrap_session_callback(self, func):
        def wrapper(handle, type, state, path):
            callback = Callback('session', func, (handle, type, state, path))
            self._handler.dispatch_callback(callback)
        return wrapper

    def _wrap_watch_callback(self, func):
        def wrapper(handle, type, state, path):
            # don't send session events to all watchers
            if state != zookeeper.SESSION_EVENT:
                event = WatchedEvent(type, state, path)
                callback = Callback('watch', func, (event,))
                self._handler.dispatch_callback(callback)
        return wrapper

    def _make_state_change(self, state):
        # skip if state is current
        if self.state == state:
            return
        self.state = state

        for listener in self.state_listeners:
            try:
                listener(state)
            except Exception:
                log.exception("Error in connection state listener")

    def _session_callback(self, handle, type, state, path):
        if type != EventType.SESSION:
            return

        if self._handle != handle:
            try:
                # latent handle callback from previous connection
                zookeeper.close(handle)
            except:
                pass
            return

        if self._stopped.is_set():
            return

        if state == KeeperState.CONNECTED:
            self._live.set()
            self._make_state_change(KazooState.CONNECTED)
        elif state in (KeeperState.EXPIRED_SESSION,
                       KeeperState.AUTH_FAILED):
            self._live.clear()
            self._handle = None
            self._make_state_change(KazooState.LOST)
            self.connect_async()
        else:
            # Connection lost
            self._live.clear()
            self._make_state_change(KazooState.SUSPENDED)

        if self._watcher:
            self._watcher(WatchedEvent(type, state, path))

    def _safe_close(self):
        if self._handle is not None:
            zh, self._handle = self._handle, None
            try:
                zookeeper.close(zh)
            except zookeeper.ZookeeperException:
                # Corrupt session or otherwise disconnected
                pass
            self._live.clear()
            self._make_state_change(KazooState.LOST)

    def connect_async(self):
        """Asynchronously initiate connection to ZK

        :returns: An AsyncResult instance for the configured handler
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        # If we're already connected, ignore
        if self._live.is_set():
            return

        # Make sure we're safely closed
        self._safe_close()

        # We've been asked to connect, clear the stop
        self._stopped.clear()

        cb = self._wrap_session_callback(self._session_callback)
        if self._provided_client_id:
            self._handle = zookeeper.init(self._hosts, cb, self._timeout,
                self._provided_client_id)
        else:
            self._handle = zookeeper.init(self._hosts, cb, self._timeout)
        return self._live

    def connect(self, timeout=None):
        """Initiate connection to ZK

        :param timeout: Time in seconds to wait for connection to succeed

        """
        event = self.connect_async()

        if not event:
            # Already connected
            return

        try:
            event.wait(timeout=timeout)
        except self._handler.timeout_error:
            self._connection_timed_out = True
            raise

    def stop(self):
        """Gracefully stop this Zookeeper session"""
        self._stopped.set()
        self._safe_close()

    def restart(self):
        """Stop and restart the Zookeeper session."""
        self._safe_close()
        self._stopped.clear()
        self.connect()

    def add_auth_async(self, scheme, credential):
        """Asynchronously send credentials to server

        :param scheme: authentication scheme (default supported: "digest")
        :param credential: the credential -- value depends on scheme
        :returns: AsyncResult object set on completion
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        async_result = self._handler.async_result()
        callback = partial(_generic_callback, async_result)

        self._safe_call(zookeeper.add_auth, scheme, credential, callback)
        return async_result

    def add_auth(self, scheme, credential):
        """Send credentials to server

        :param scheme: authentication scheme (default supported: "digest")
        :param credential: the credential -- value depends on scheme

        """
        return self.add_auth_async(scheme, credential).get()

    def create_async(self, path, value, acl=None, ephemeral=False, sequence=False):
        """Asynchronously create a ZNode

        :param path: path of node
        :param value: initial value of node
        :param acl: permissions for node
        :param ephemeral: boolean indicating whether node is ephemeral (tied
                          to this session)
        :param sequence: boolean indicating whether path is suffixed with a
                         unique index
        :returns: AsyncResult object set on completion with the real path of
                  the new node
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        flags = 0
        if ephemeral:
            flags |= zookeeper.EPHEMERAL
        if sequence:
            flags |= zookeeper.SEQUENCE
        if acl is None:
            acl = (ZK_OPEN_ACL_UNSAFE,)

        async_result = self._handler.async_result()
        callback = partial(_generic_callback, async_result)

        self._safe_call(zookeeper.acreate, async_result, path, value,
                        list(acl), flags, callback)
        return async_result

    def create(self, path, value, acl=None, ephemeral=False, sequence=False):
        """Create a ZNode

        :param path: path of node
        :param value: initial value of node
        :param acl: permissions for node
        :param ephemeral: boolean indicating whether node is ephemeral (tied
                          to this session)
        :param sequence: boolean indicating whether path is suffixed with a
                         unique index
        :returns: real path of the new node

        """
        return self.create_async(path, value, acl, ephemeral, sequence).get()

    def exists_async(self, path, watch=None):
        """Asynchronously check if a node exists

        :param path: path of node
        :param watch: optional watch callback to set for future changes to this
                      path
        :returns: stat of the node if it exists, else None
        :rtype: `dict` or `None`

        """
        async_result = self._handler.async_result()
        callback = partial(_exists_callback, async_result)
        watch_callback = self._wrap_watch_callback(watch) if watch else None

        self._safe_call(zookeeper.aexists, async_result, path, watch_callback,
                        callback)
        return async_result

    def exists(self, path, watch=None):
        """Check if a node exists

        :param path: path of node
        :param watch: optional watch callback to set for future changes to this
                      path
        :returns: stat of the node if it exists, else None
        :rtype: `dict` or `None`

        """
        return self.exists_async(path, watch).get()

    def get_async(self, path, watch=None):
        """Asynchronously get the value of a node

        :param path: path of node
        :param watch: optional watch callback to set for future changes to
                      this path
        :returns: AsyncResult set with tuple (value, :class:`ZnodeStat`) of node
                on success
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        async_result = self._handler.async_result()
        callback = partial(_generic_callback, async_result)
        watch_callback = self._wrap_watch_callback(watch) if watch else None

        self._safe_call(zookeeper.aget, async_result, path, watch_callback,
                        callback)
        return async_result

    def get(self, path, watch=None):
        """Get the value of a node

        :param path: path of node
        :param watch: optional watch callback to set for future changes to
                      this path
        :returns: tuple (value, :class:`ZnodeStat`) of node

        """
        return self.get_async(path, watch).get()

    def get_children_async(self, path, watch=None):
        """Asynchronously get a list of child nodes of a path

        :param path: path of node to list
        :param watch: optional watch callback to set for future changes to
                      this path
        :returns: AsyncResult set with list of child node names on success
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        async_result = self._handler.async_result()
        callback = partial(_generic_callback, async_result)
        watch_callback = self._wrap_watch_callback(watch) if watch else None

        self._safe_call(zookeeper.aget_children, async_result, path,
                        watch_callback, callback)
        return async_result

    def get_children(self, path, watch=None):
        """Get a list of child nodes of a path

        :param path: path of node to list
        :param watch: optional watch callback to set for future changes to this path
        :returns: list of child node names

        """
        return self.get_children_async(path, watch).get()

    def set_async(self, path, data, version=-1):
        """Set the value of a node

        If the version of the node being updated is newer than the supplied
        version (and the supplied version is not -1), a BadVersionException
        will be raised.

        :param path: path of node to set
        :param data: new data value
        :param version: version of node being updated, or -1
        :returns: AsyncResult set with new node stat on success
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        async_result = self._handler.async_result()
        callback = partial(_generic_callback, async_result)

        self._safe_call(zookeeper.aset, async_result, path, data, version,
                        callback)
        return async_result

    def set(self, path, data, version=-1):
        """Set the value of a node

        If the version of the node being updated is newer than the supplied
        version (and the supplied version is not -1), a BadVersionException
        will be raised.

        :param path: path of node to set
        :param data: new data value
        :param version: version of node being updated, or -1
        :returns: updated node stat

        """
        return self.set_async(path, data, version).get()

    def delete_async(self, path, version=-1):
        """Asynchronously delete a node

        :param path: path of node to delete
        :param version: version of node to delete, or -1 for any
        :returns: AyncResult set upon completion
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`
        """
        async_result = self._handler.async_result()
        callback = partial(_generic_callback, async_result)

        self._safe_call(zookeeper.adelete, async_result, path, version,
                        callback)
        return async_result

    def delete(self, path, version=-1):
        """Delete a node

        :param path: path of node to delete
        :param version: version of node to delete, or -1 for any

        """
        self.delete_async(path, version).get()
