#!/usr/bin/env python2
from __future__ import unicode_literals

print "hi"

from prompt_toolkit import prompt
from prompt_toolkit.contrib.completers import WordCompleter
from prompt_toolkit.completion import Completer, Completion



#sts = open("/dev/pts/28", "w")
sts = None

#mycompl = WordCompleter(['test', 'help', 'headers', 'print', 'pipe', 'show'])
class MyCompl(Completer):
    def get_completions(self, document, complete_event):
        if sts:
            print >>sts, "get_completions"
            print >>sts, "",repr(document)
            print >>sts, "",repr(complete_event)
            print >>sts, "",dir(document)
            print >>sts, " wbefore",document.get_word_before_cursor()
            print >>sts, " before",document.current_line_before_cursor
            print >>sts, " after",document.current_line_after_cursor
        this_word = document.get_word_before_cursor()
        start_of_line = document.current_line_before_cursor.strip()
        commands = ['test', 'help', 'headers', 'print', 'pipe', 'show']
        if this_word == start_of_line:
            # This is a command
            for cmd in commands:
                if cmd.startswith(this_word):
                    yield Completion(cmd, start_position=-len(this_word))
        else:
            pass
            # This is an argument to a command. TODO
            #yield nothing

try:
    a = prompt("gimme some lovin: ", completer=MyCompl())
except KeyboardInterrupt:
    print "aborted"
else:
    print "you said", repr(a)
