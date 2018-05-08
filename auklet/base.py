from __future__ import absolute_import, unicode_literals

import os
import io
import sys
import uuid
import json
import errno
import zipfile
import hashlib

from uuid import uuid4
from datetime import datetime
from contextlib import contextmanager
from collections import deque
from kafka import KafkaProducer
from kafka.errors import KafkaError
from auklet.stats import Event, SystemMetrics
from ipify import get_ip
from ipify.exceptions import IpifyException

try:
    # For Python 3.0 and later
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
except ImportError:
    # Fall back to Python 2's urllib2
    from urllib2 import urlopen, Request, HTTPError

__all__ = ['Client', 'Runnable', 'frame_stack', 'deferral', 'get_commit_hash',
           'get_mac', 'get_device_ip', 'setup_thread_excepthook',
           'get_abs_path']


class Client(object):
    producer_types = None
    brokers = None
    commit_hash = None
    mac_hash = None
    offline_fliename = "tmp/local.txt"
    abs_path = None

    def __init__(self, apikey=None, app_id=None,
                 base_url="https://api.auklet.io/", mac_hash=None):
        self.apikey = apikey
        self.app_id = app_id
        self.base_url = base_url
        self.send_enabled = True
        self.producer = None
        self._get_kafka_brokers()
        self.mac_hash = mac_hash
        self._create_file(self.offline_fliename)
        self.commit_hash, self.abs_path = get_commit_hash()
        if self._get_kafka_certs():
            try:
                self.producer = KafkaProducer(**{
                    "bootstrap_servers": self.brokers,
                    "ssl_cafile": "tmp/ck_ca.pem",
                    "ssl_certfile": "tmp/ck_cert.pem",
                    "ssl_keyfile": "tmp/ck_private_key.pem",
                    "security_protocol": "SSL",
                    "ssl_check_hostname": False,
                    "value_serializer": lambda m: b(json.dumps(m))
                })
            except KafkaError:
                # TODO log off to kafka if kafka fails to connect
                pass

    def _create_file(self, filename):
        if not os.path.exists(os.path.dirname(filename)):
            try:
                os.makedirs(os.path.dirname(filename))
            except OSError as exc:  # Guard against race condition
                if exc.errno != errno.EEXIST:
                    raise
        return True

    def _build_url(self, extension):
        return '%s%s' % (self.base_url, extension)

    def _get_kafka_brokers(self):
        url = Request(self._build_url("private/devices/config/"),
                      headers={"Authorization": "JWT %s" % self.apikey})
        res = urlopen(url)
        kafka_info = json.loads(u(res.read()))
        self.brokers = kafka_info['brokers']
        self.producer_types = {
            "monitoring": kafka_info['prof_topic'],
            "event": kafka_info['event_topic'],
            "log": kafka_info['log_topic']
        }

    def _get_kafka_certs(self):
        url = Request(self._build_url("private/devices/certificates/"),
                      headers={"Authorization": "JWT %s" % self.apikey})
        try:
            res = urlopen(url)
        except HTTPError as e:
            # Allow for accessing redirect w/o including the
            # Authorization token.
            res = urlopen(e.geturl())
        mlz = zipfile.ZipFile(io.BytesIO(res.read()))
        for temp_file in mlz.filelist:
            filename = "tmp/%s.pem" % temp_file.filename
            self._create_file(filename)
            f = open(filename, "wb")
            f.write(mlz.open(temp_file.filename).read())
        return True

    def _write_to_local(self, data):
        try:
            with open(self.offline_fliename, "a") as offline:
                offline.write(json.dumps(data))
                offline.write("\n")
        except IOError:
            # TODO determine what to do with data we fail to write
            return False

    def _produce_from_local(self):
        try:
            with open(self.offline_fliename, 'r+') as offline:
                lines = offline.read().splitlines()
                for line in lines:
                    loaded = json.loads(line)
                    if 'stackTrace' in loaded.keys():
                        self.produce(loaded, "event")
                    else:
                        self.produce(loaded)
                offline.truncate()
        except IOError:
            # TODO determine what to do if we can't read the file
            return False

    def build_event_data(self, type, traceback, tree):
        event = Event(type, traceback, tree, self.abs_path)
        event_dict = dict(event)
        event_dict['application'] = self.app_id
        event_dict['publicIP'] = get_device_ip()
        event_dict['id'] = str(uuid4())
        event_dict['timestamp'] = str(datetime.utcnow())
        event_dict['systemMetrics'] = dict(SystemMetrics())
        event_dict['macAddressHash'] = self.mac_hash
        event_dict['commitHash'] = self.commit_hash
        return event_dict

    def produce(self, data, data_type="monitoring"):
        if self.producer is not None:
            try:
                self.producer.send(self.producer_types[data_type],
                                   value=data)
                self._produce_from_local()
            except KafkaError:
                self._write_to_local(data)


class Runnable(object):
    """The base class for runnable classes such as :class:`monitoring.
    MonitoringBase`.
    """

    #: The generator :meth:`run` returns.  It will be set by :meth:`start`.
    _running = None

    def is_running(self):
        """Whether the instance is running."""
        return self._running is not None

    def start(self, *args, **kwargs):
        """Starts the instance.
        :raises RuntimeError: has been already started.
        :raises TypeError: :meth:`run` is not canonical.
        """
        if self.is_running():
            raise RuntimeError('Already started')
        self._running = self.run(*args, **kwargs)
        try:
            yielded = next(self._running)
        except StopIteration:
            raise TypeError('run() must yield just one time')
        if yielded is not None:
            raise TypeError('run() must yield without value')

    def stop(self):
        """Stops the instance.
        :raises RuntimeError: has not been started.
        :raises TypeError: :meth:`run` is not canonical.
        """
        if not self.is_running():
            raise RuntimeError('Not started')
        running, self._running = self._running, None
        try:
            next(running)
        except StopIteration:
            # expected.
            pass
        else:
            raise TypeError('run() must yield just one time')

    def run(self, *args, **kwargs):
        """Override it to implement the starting and stopping behavior.
        An overriding method must be a generator function which yields just one
        time without any value.  :meth:`start` creates and iterates once the
        generator it returns.  Then :meth:`stop` will iterates again.
        :raises NotImplementedError: :meth:`run` is not overridden.
        """
        raise NotImplementedError('Implement run()')
        yield

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc_info):
        self.stop()


def frame_stack(frame):
    """Returns a deque of frame stack."""
    frames = deque()
    while frame is not None:
        frames.appendleft(frame)
        frame = frame.f_back
    return frames


def get_mac():
    mac_num = hex(uuid.getnode()).replace('0x', '').upper()
    mac = '-'.join(mac_num[i: i + 2] for i in range(0, 11, 2))
    return hashlib.md5(b(mac)).hexdigest()


def get_commit_hash():
    try:
        with open(".auklet/version", "r") as auklet_file:
            return auklet_file.read(), get_abs_path(auklet_file.name)
    except IOError:
        # TODO Error out app if no commit hash
        return ""


def get_abs_path(path):
    try:
        return os.path.abspath(path).split('.auklet')[0]
    except IndexError:
        return ''


def get_device_ip():
    try:
        return get_ip()
    except IpifyException:
        # TODO log to kafka if the ip service fails for any reason
        return None


def setup_thread_excepthook():
    import threading
    """
    Workaround for `sys.excepthook` thread bug from:
    http://bugs.python.org/issue1230540

    Call once from the main thread before creating any threads.
    """
    init_original = threading.Thread.__init__

    def init(self, *args, **kwargs):

        init_original(self, *args, **kwargs)
        run_original = self.run

        def run_with_except_hook(*args2, **kwargs2):
            try:
                run_original(*args2, **kwargs2)
            except Exception:
                sys.excepthook(*sys.exc_info())

        self.run = run_with_except_hook

    threading.Thread.__init__ = init


if sys.version_info < (3,):
    # Python 2 and 3 String Compatibility
    def b(x):
        return x

    def u(x):
        return x
else:
    # https://pythonhosted.org/six/#binary-and-text-data
    import codecs

    def b(x):
        # Produces a unicode string to encoded bytes
        return codecs.utf_8_encode(x)[0]

    def u(x):
        # Produces a byte string from a unicode object
        return codecs.utf_8_decode(x)[0]


@contextmanager
def deferral():
    """Defers a function call when it is being required.
    ::
       with deferral() as defer:
           sys.setprofile(f)
           defer(sys.setprofile, None)
           # do something.
    """
    deferred = []
    defer = lambda func, *args, **kwargs: deferred.append(
        (func, args, kwargs))
    try:
        yield defer
    finally:
        while deferred:
            func, args, kwargs = deferred.pop()
            func(*args, **kwargs)
