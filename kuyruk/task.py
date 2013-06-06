from __future__ import absolute_import
import os
import sys
import signal
import socket
import logging
from time import time
from uuid import uuid1
from types import MethodType
from datetime import datetime
from functools import wraps
from contextlib import contextmanager

from kuyruk import events, importer
from kuyruk.queue import Queue
from kuyruk.events import EventMixin
from kuyruk.exceptions import Timeout

logger = logging.getLogger(__name__)


def profile(f):
    """Logs the time spent while running the task."""
    @wraps(f)
    def inner(self, *args, **kwargs):
        start = time()
        result = f(self, *args, **kwargs)
        end = time()
        logger.info("%s finished in %i seconds." % (self.name, end - start))
        return result
    return inner


class Task(EventMixin):

    def __init__(self, f, kuyruk, queue='kuyruk', local=False,
                 eager=False, retry=0, max_run_time=None):
        self.f = f
        self.kuyruk = kuyruk
        self.queue = queue
        self.local = local
        self.eager = eager
        self.retry = retry
        self.max_run_time = max_run_time
        self.cls = None
        self.setup()

    def setup(self):
        """Convenience function for extending classes
        that run after __init__."""
        pass

    def __repr__(self):
        return "<Task of %r>" % self.name

    def __call__(self, *args, **kwargs):
        """When a fucntion is wrapped with a task decorator it will be
        converted to a Task object. By overriding __call__ method we are
        sending this task to queue instead of invoking the function
        without changing the client code.

        """
        self.send_signal(events.task_presend, args, kwargs, reverse=True)

        task_result = TaskResult(self)

        if self.eager or self.kuyruk.config.EAGER:
            task_result.result = self.apply(*args, **kwargs)
        else:
            host = kwargs.pop('kuyruk_host', None)
            local = kwargs.pop('kuyruk_local', None)
            task_result.id = self.send_to_queue(args, kwargs,
                                                host=host, local=local)

        self.send_signal(events.task_postsend, args, kwargs)

        return task_result

    def __get__(self, obj, objtype):
        """If the task is accessed from an instance via attribute syntax
        return a function for sending the task to queue, otherwise
        return the task itself.

        This is done for allowing a method to be converted to task without
        modifying the client code. When a function decorated inside a class
        there is no way of accessing that class at that time because methods
        are bounded at run time when they are accessed. The trick here is that
        we set self.cls when the Task is accessed first time via attribute
        syntax.

        """
        self.cls = objtype
        if obj:
            return MethodType(self.__call__, obj, objtype)
        return self

    def send_to_queue(self, args, kwargs, host=None, local=None):
        """
        Sends this task to queue.

        :param args: Arguments that will be passed to task on execution.
        :param kwargs: Keyword arguments that will be passed to task
            on execution.
        :param host: Send this task to specific host. ``host`` will be
            appended to the queue name.
        :param local: Send this task to this host. Hostname of this host will be
            appended to the queue name.
        :return: :const:`None`

        """
        queue = self.queue
        local_ = self.local

        if local is not None:
            local_ = local

        if host:
            queue = "%s.%s" % (self.queue, host)
            local_ = False

        with self.kuyruk.channel() as channel:
            queue = Queue(queue, channel, local_)
            desc = self.get_task_description(args, kwargs, queue.name)
            queue.send(desc)

        return desc['id']

    def get_task_description(self, args, kwargs, queue):
        """Return the dictionary to be sent to the queue."""

        # For class tasks; replace the first argument with the id of the object
        if self.cls:
            args = list(args)
            args[0] = args[0].id

        return {
            'id': uuid1().hex,
            'queue': queue,
            'args': args,
            'kwargs': kwargs,
            'module': self.module_name,
            'function': self.f.__name__,
            'class': self.class_name,
            'retry': self.retry,
            'sender_timestamp': datetime.utcnow(),
            'sender_hostname': socket.gethostname(),
            'sender_pid': os.getpid(),
            'sender_cmd': ' '.join(sys.argv),
        }

    def send_signal(self, signal, args, kwargs, reverse=False, **extra):
        """
        Sends a signal for each sender.
        This allows the user to register for a specific sender.

        """
        senders = (self, self.__class__, self.kuyruk)
        if reverse:
            senders = reversed(senders)

        for sender in senders:
            signal.send(sender, task=self, args=args, kwargs=kwargs, **extra)

    @profile
    def apply(self, *args, **kwargs):
        """Run the wrapped function and event handlers."""
        def send_signal(signal, reverse=False, **extra):
            self.send_signal(signal, args, kwargs, reverse, **extra)

        limit = (self.max_run_time or
                 self.kuyruk.config.MAX_TASK_RUN_TIME or 0)

        logger.debug("Applying %r, args=%r, kwargs=%r", self, args, kwargs)
        try:
            send_signal(events.task_prerun, reverse=True)
            with time_limit(limit):
                # Call wrapped function
                return_value = self.f(*args, **kwargs)
        except Exception:
            send_signal(events.task_failure, exc_info=sys.exc_info())
            raise
        else:
            send_signal(events.task_success, return_value=return_value)
        finally:
            send_signal(events.task_postrun)

    @property
    def name(self):
        """Location for the wrapped function.
        This value is by the worker to find the task.

        """
        if self.class_name:
            return "%s:%s.%s" % (
                self.module_name, self.class_name, self.f.__name__)
        else:
            return "%s:%s" % (self.module_name, self.f.__name__)

    @property
    def module_name(self):
        """Module name of the function wrapped."""
        name = self.f.__module__
        if name == '__main__':
            name = importer.get_main_module().name
        return name

    @property
    def class_name(self):
        """Name of the class if this is a class task,
        otherwise :const:`None`."""
        if self.cls:
            return self.cls.__name__


class TaskResult(object):
    """Insance of this class is returned after the task is sent to queue.
    Since Kuyruk does not support a result backend yet it will raise
    exception on any attribute or item access.

    """
    def __init__(self, task):
        self.task = task

    def __getattr__(self, item):
        raise Exception(item)

    def __getitem__(self, item):
        raise Exception(item)

    def __setitem__(self, key, value):
        raise Exception(key, value)

    def __repr__(self):
        return "<TaskResult of %r>" % self.task.name

    def __str__(self):
        return self.__repr__()


@contextmanager
def time_limit(seconds):
    def signal_handler(signum, frame):
        raise Timeout
    signal.signal(signal.SIGALRM, signal_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
