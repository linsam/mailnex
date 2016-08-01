#!/usr/bin/env python2
from __future__ import unicode_literals

print "hi"

searchcmd = "khard.py email -s"

from prompt_toolkit import prompt
from prompt_toolkit.contrib.completers import WordCompleter
from prompt_toolkit.completion import Completer, Completion
import subprocess



#sts = open("/dev/pts/31", "w")
sts = None

#mycompl = WordCompleter(['test', 'help', 'headers', 'print', 'pipe', 'show'])
class MyCompl(Completer):
    def get_completions(self, document, complete_event):
        if sts:
            print >>sts, "get_completions"
            print >>sts, "doc:",repr(document)
            print >>sts, "event:",repr(complete_event)
            print >>sts, "dir(doc):",dir(document)
            print >>sts, " wbefore",repr(document.get_word_before_cursor())
            print >>sts, " before",repr(document.current_line_before_cursor)
            print >>sts, " after",repr(document.current_line_after_cursor)
        before = document.current_line_before_cursor
        after = document.current_line_after_cursor
        # Simple first pass, use comma separation.
        # TODO: Actually parse emails or something.
        thisstart = before.split(',')[-1]
        thisend = after.split(',')[0]
        this = thisstart + thisend
        prefix = " " if this.startswith(" ") else ""
        this = this.strip()
        if sts:
            print >>sts, "Current mail", repr(this)
        s = subprocess.Popen(searchcmd.split() + [this], stdin=None, stdout=subprocess.PIPE)
        results=[]
        for i in range(10):
            res = s.stdout.readline().strip()
            if sts:
                print >>sts, "Line {} is {}".format(i, repr(res))
            if res == "":
                break
            # Skip header line
            if i == 0:
                continue
            results.append(res.split('\t'))
        s.stdout.close()
        res = s.wait()
        if sts:
            print >>sts, "subprocess ended with", res

        for res in results:
            completion = "{} <{}>,".format(res[1], res[0])
            yield Completion(prefix + completion, display=completion, start_position=-len(thisstart), display_meta=res[2])

try:
    a = prompt("email: ", completer=MyCompl())
except KeyboardInterrupt:
    print "aborted"
except EOFError:
    print "Cancelled"
else:
    print "you said", repr(a)
