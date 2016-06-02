import pyuv
import signal

l=pyuv.Loop()
t=pyuv.Timer(l)
global remain
remain = 4
def echo(arg):
    print "echo"
    global remain
    remain -= 1
    if remain <= 0:
        arg.stop()

s = pyuv.Signal(l)
def sighand(sighand, signum):
    print
    print "signum",signum,"received. Exiting"
    sighand.loop.stop()
    

s.start(sighand, signal.SIGINT)
t.start(echo, 0, 2)
l.run()
print "Post event loop"
