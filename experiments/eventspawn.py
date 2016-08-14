#!/usr/bin/env python2

# This experiment is to play with spawning a pager while keeping our event
# loop going.

from __future__ import print_function
from __future__ import unicode_literals


import pyuv
import sys

def finish(proc, status, signal):
    print("finish_cb",status,signal)
    proc.close()
    proc.loop.stop()

loop = pyuv.Loop()
#child = pyuv.Process(loop)
com = pyuv.Pipe(loop, True)
stdin = pyuv.StdIO(stream=com, flags=pyuv.UV_CREATE_PIPE|pyuv.UV_READABLE_PIPE)
stdout = pyuv.StdIO(fd=sys.stdout.fileno(), flags=pyuv.UV_INHERIT_FD)
stderr = pyuv.StdIO(fd=sys.stderr.fileno(), flags=pyuv.UV_INHERIT_FD)
child = pyuv.Process.spawn(loop, "less", exit_callback=finish, stdio=[stdin, stdout, stderr])
com.write(b"This is some test data\n\nWhat else can I say?\n")
com.close()
loop.run()
print("Loop complete")
