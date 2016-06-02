#!/usr/bin/env python2
from __future__ import unicode_literals
import prompt_toolkit
import prompt_toolkit.shortcuts
import prompt_toolkit.eventloop.inputhook
import prompt_toolkit.eventloop.base
import prompt_toolkit.terminal.vt100_input
import pyuv
import signal
import sys
import six

debug = False

class myloop(prompt_toolkit.eventloop.base.EventLoop):
    """An attempt at making prompt_toolkit use libuv instead of its built-in eventloop.

    """
    def __init__(self, realloop):
        # prompt_toolkit....PosixEventLoop never bothers to init its super, so maybe we
        # shouldn't either?
        self.realloop = realloop
    def run(self, stdin, callbacks):
        # The PosixEventLoop basically sets up a callback for sigwinch to
        # monitor terminal resizes, and a callback for when stdin is ready.
        # libuv can do the stdin with the TTY handler, so we'll give that a
        # whirl
        self.sigw = pyuv.Signal(self.realloop)
        self.sigw.start(self.sigwinch, signal.SIGWINCH)
        self.tty = pyuv.TTY(self.realloop, sys.stdin.fileno(), True)
        self.tty.start_read(self.ttyread)
        self._callbacks = callbacks
        self.inputstream = prompt_toolkit.terminal.vt100_input.InputStream(callbacks.feed_key)
        #print dir(callbacks)
        return self.realloop.run()
    def sigwinch(event, signum):
        #print "whinch"
        # We don't worry about all that executor threading stuffs that
        # prompt_toolkit does, because libuv (is supposed to) give us signals
        # in the main thread and not in a signal context (which is one of the
        # whole points of doing a self-pipe).
        self._callbacks.terminal_size_changed()
    def ttyread(self, event, data, error):
        if data is None:
            self.tty.close()
            self.realloop.stop()
            if debug:
                print "Dying"
        else:
            if data == '\x03':
                # Ought to have been a sigint, but apparently prompt_toolkit
                # makes the terminal *very* raw. Anyway, I guess we'll
                # intercept this. We want to handle interruptions on our own,
                # not have prompt_toolkit kill off stdin. TODO: This is
                # unlikely to work well in practice, because '3' is a fairly
                # valid number to occur in terminal control sequences; that
                # is, it might not be from a user interrupt. Really, we should
                # let the interrupt generation be regular :-/
                self.realloop.stop()
            self.inputstream.feed(six.text_type(data))
    # Other stuff prompt_toolkit wants us to have :-(
    def add_reader(self, fd, callback):
        print "add_reader called", fd, callback
    def remove_reader(self, fd):
        print "remove_reader called", fd
    def close(self):
        if debug:
            print "closing"
        self.sigw.close()
        self.tty.close()
    def stop(self):
        if debug:
            print "Stopping"
        self.realloop.stop()
    def call_from_executor(self, callback, _max_postpone_until=None):
        #TODO: Mess with max postpone? PosixEventLoop uses a pipe to schedule
        # a callback for execution.
        # We'll just call the function and be done with it.
        callback()
    def run_in_executor(self, callback):
        # PosixEventLoop creates a thread function to call the callback and
        # gives that to the executor... Apparently prompt_toolkit might rely
        # on this so that it doesn't process autocompletions during paste in a
        # heavy manner. TODO: Revisit this
        self.call_from_executor(callback)


#loop = prompt_toolkit.shortcuts.create_eventloop() # TODO: pass hook
uvloop = pyuv.Loop()
loop = myloop(uvloop)

def sigint(event, signal):
    print "sigint"
    event.loop.stop()

def timevent(event):
    print "timeout"
    # TODO: Redraw prompt?

s = pyuv.Signal(uvloop)
s.start(sigint, signal.SIGINT)
t = pyuv.Timer(uvloop)
t.start(timevent, 0, 5)

res = prompt_toolkit.prompt("prompt> ", eventloop=loop)
print "res=",repr(res)
res = prompt_toolkit.prompt("prompt2> ", eventloop=loop)
print "res=",repr(res)
