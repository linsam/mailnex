import sys
import select

p = select.poll()
p.register(sys.stdin, select.POLLIN)
sys.stdin.setblocking(0)
for i in range(10):
    r = p.poll(2 * 1000)
    print "polled:", r
    for ev in r:
        if ev[0] == 0:
            print " data",sys.stdin.read(1024)
