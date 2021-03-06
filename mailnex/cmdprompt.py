import cmd
import prompt_toolkit

# TODO: Make pygments optional?
from pygments.lexer import Lexer
from pygments.token import *
from prompt_toolkit.layout.lexers import PygmentsLexer
from pygments.token import Token
from pygments.styles.tango import TangoStyle
from prompt_toolkit.styles import style_from_pygments
from pygments.lexers import HtmlLexer
from prompt_toolkit.key_binding.manager import KeyBindingManager
from prompt_toolkit.keys import Keys

import pygments.style

import signal
import pyuv
import sys
import six

class ptk_pyuv_wrapper(prompt_toolkit.eventloop.base.EventLoop):
    """A prompt_toolkit compatible event loop that wraps pyuv (libuv) as the real event loop.

    Options:
        realloop - optional. If given, we'll use it, otherwise we'll create a new pyuv.Loop.

    """
    def __init__(self, realloop=None):
        # prompt_toolkit....PosixEventLoop never bothers to init its super, so maybe we
        # shouldn't either?
        if realloop is None:
            realloop = pyuv.Loop()
        self.realloop = realloop
        self.pending_async = []
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
        return self.realloop.run()
    def ttyPause(self):
        """Disable receive on the tty.

        This is useful when spawning another program that needs the tty (e.g. less or vim).

        Note: prompt_toolkit calls stop when the prompt completes, and we
        remove tty and sigwinch watchers on stop and restore on run again
        (when asking for another prompt), so in those cases, this needn't be
        called. This probably doesn't hold true for 'full screen' application
        mode.

        Call ttyResume() to restore input.
        """
        self.tty.stop_read()
    def ttyResume(self):
        """Restore receive on the tty.

        See ttyPause()."""
        self.tty.start_read(self.ttyread)
    def sigwinch(self, event, signum):
        # We don't worry about all that executor threading stuffs that
        # prompt_toolkit does, because libuv (is supposed to) give us signals
        # in the main thread and not in a signal context (which is one of the
        # whole points of doing a self-pipe).
        self._callbacks.terminal_size_changed()
    def ttyread(self, event, data, error):
        if data is None:
            self.tty.close()
            self.realloop.stop()
        else:
            try:
                # TODO: Obtain this from user preference, fall back on stdin
                self.inputstream.feed(six.text_type(data, sys.stdin.encoding))
            except Exception:
                # If we don't stop, we can get into an exception loop such
                # that every character the user types posts an exception and
                # they cannot exit without spawning another terminal to run a
                # kill command against this program.
                self.tty.close()
                self.realloop.stop()
                raise
    # Other stuff prompt_toolkit wants us to have :-(
    def add_reader(self, fd, callback):
        print "add_reader called", fd, callback
    def remove_reader(self, fd):
        print "remove_reader called", fd
    def close(self):
        self.sigw.close()
        self.tty.close()
    def stop(self):
        self.sigw.close()
        self.tty.close()
        self.realloop.stop()
    def call_from_executor(self, callback, _max_postpone_until=None):
        #TODO: Mess with max postpone? PosixEventLoop uses a pipe to schedule
        # a callback for execution.
        # We'll just call the function via pyuv Async and be done with it.
        def wrapper(handle):
            handle.close()
            callback()
            i = self.pending_async.index(handle)
            del self.pending_async[i]
        a = pyuv.Async(self.realloop, wrapper)
        # If we don't store a somewhere ourselves, libuv never calls the
        # callback. I suspect it is getting garbage collected if we don't keep
        # a reference ourselves.
        self.pending_async.append(a)
        a.send()
    def run_in_executor(self, callback):
        # PosixEventLoop creates a thread function to call the callback and
        # gives that to the executor... Apparently prompt_toolkit might rely
        # on this so that it doesn't process autocompletions during paste in a
        # heavy manner. TODO: Revisit this (they have a note about i/o vs cpu
        # preferencing)
        def wrapper(handle):
            handle.close()
            callback()
            i = self.pending_async.index(handle)
            del self.pending_async[i]
        a = pyuv.Async(self.realloop, wrapper)
        self.pending_async.append(a)
        a.send()

def PromptLexerFactory(cmd_obj):
    class PromptLexer(Lexer):
        """Basic lexer for our command line."""
        def __init__(self, **options):
            self.options = options
            Lexer.__init__(self, **options)
        def get_tokens_unprocessed(self, text):
            raise Exception("Just use get_tokens!")
        def get_tokens(self, text):
            if not self.cmd.lexerEnabled:
                return [(Token.Text, text)]
            res = []
            data=text.split(" ", 1)
            command = data[0] if len(data) else ""
            rest = data[1] if len(data) == 2 else ""
            if len(data) == 0:
                return []
            if len(data) > 0:
                if u"do_{}".format(command) in dir(self.cmd):
                    res.append((Generic.Inserted, command))
                else:
                    res.append((Token.Text, text))
                    return res
            if len(data) > 1:
                if u"lex_{}".format(command) in dir(self.cmd):
                    # TODO: require each command to add the space?
                    # Looks simpler, codewise, to do it here. But, since
                    # this generates its own token, does Pygments waste
                    # terminal bandwidth sending extra codes for the one
                    # space?
                    res.append((Token.Text, " "))
                    getattr(self.cmd, "lex_{}".format(command))(text, rest, res)
                else:
                    res.append((Token.Text, " " + rest))
            return res
        cmd = cmd_obj
        name = 'Prompt'
        aliases = ['prompt']
        filenames = []
    return PromptLexer

class PromptPygStyle(pygments.style.Style):
    """A Simple style for our interactive prompt's user text."""
    # according to docs, default_style is the style inherited by all token
    # types. I haven't made this do anything, so we'll leave it blank.
    default_style = ''
    styles = {
            Generic.Inserted: '#88f',
            Generic.Heading: 'bold #8f8',
            Generic.Error: 'bold #f00',
            Text: 'bold #ccf',
            # Error is used for, at least, text that doesn't match any token
            # (when using a RegexLexer derivitive). As such, it is used for
            # text that is being actively typed that doesn't match anything
            # *yet*. Should probably leave it as unformatted.
            #Error: 'italic #004',
            }

#prompt_style = style_from_pygments(TangoStyle, {
prompt_style = style_from_pygments(PromptPygStyle, {
    Token.Text: '#888888',
    })

class Completer(prompt_toolkit.completion.Completer):
    def __init__(self, cmd):
        prompt_toolkit.completion.Completer.__init__(self)
        self.cmd = cmd

    def get_completions(self, document, complete_event):
        this_word = document.get_word_before_cursor()
        start_of_line = document.current_line_before_cursor.lstrip()
        if this_word == start_of_line:
            for i in self.cmd.completenames(this_word):
                # Other useful completion parameters:
                #   display=<string>       - use different text in popup list
                #   display_meta=<string>  - show additional info in popup
                #   (like source of this completion. Might be used to show
                #   which address book an address completion is from.
                yield prompt_toolkit.completion.Completion(i, start_position=-len(this_word))
            raise StopIteration
        start_words = document.current_line.split(None,1)
        command = start_words[0]
        symbol = u"compl_{}".format(command)
        if symbol in dir(self.cmd):
            gen = getattr(self.cmd, symbol)(document, complete_event)
            while True:
                yield gen.next()

class CmdPrompt(cmd.Cmd):
    """Subclass of Cmd that uses prompt_toolkit instead of readline/raw_input.

    Purpose: Allow a richer command line editor and also allow event-loop based
    processing.

    The python readline library only exports a portion of the readline API, and
    uses it to wrap raw_input, preventing programs from doing asynchronous
    CLI parsing in a single thread. Additionally, readline lacks some of the
    features of prompt_toolkit, such as color highlighting and predictive
    completions.

    This class overrides the actual prompt display and input to work with
    an eventloop but continues to use cmd for completions and command processing.
    
    It also has a single pass function, which bridges the usecase between
    looping on commands and processing a string as if entered by the user
    (that is, you want to readline once, and process the command).
    """

    def get_title(self):
        return self.title
    def __init__(self, prompt=None, histfile=None, eventloop=None):
        cmd.Cmd.__init__(self)
        self.title = u"mailnex"
        self.completer = Completer(self)
        # ttyBusy tracks times when printing is a Bad Idea
        self.ttyBusy = False
        # lexerEnabled is a marker for the lexer to check before doing
        # interpretations. Currently, this just turns it off when the prompt
        # isn't for the command line but for composing messages.
        self.lexerEnabled = True
        if histfile:
            self.history = prompt_toolkit.history.FileHistory(histfile)
        else:
            self.history = prompt_toolkit.history.InMemoryHistory()
        if prompt is None:
            prompt = "> "
        self.prompt = prompt
        def gpt(cli):
            return [
                    (Token, self.prompt),
                    ]
        self.ptkevloop = ptk_pyuv_wrapper(eventloop)
        registry = KeyBindingManager.for_prompt().registry
        @registry.add_binding(Keys.ControlZ)
        def _(event):
            """Support backrounding ourselves."""
            # I'm surprised this isn't part of the default maps.
            #
            # Ideally, we shouldn't actually have to handle this ourselves; the
            # terminal should handle it for us. However, we are putting the
            # terminal into raw mode, so it won't. The next best thing would be
            # to try to get the actual background character the terminal would
            # use and use that. It is controlZ by default on every Unix system
            # I've used, but it is adjustable, with the 'stty' utility for
            # example.
            # TODO: Figure out how to use an appropriate key here, or allow it
            # to be customized.
            event.cli.suspend_to_background()
        self.cli = prompt_toolkit.interface.CommandLineInterface(
                application = prompt_toolkit.shortcuts.create_prompt_application(
                    u"",
                    #multiline = True,
                    get_prompt_tokens = gpt,
                    style = prompt_style,
                    lexer = PygmentsLexer(PromptLexerFactory(self)),
                    completer = self.completer,
                    history = self.history,
                    auto_suggest = prompt_toolkit.auto_suggest.AutoSuggestFromHistory(),
                    get_title = self.get_title,
                    get_bottom_toolbar_tokens=self.toolbar,
                    key_bindings_registry=registry,
                    ),
                eventloop = self.ptkevloop,
                output = prompt_toolkit.shortcuts.create_output(true_color = False),
        )
        # ui_lines is the number of lines occupied by the UI.
        # For example, 1 line for command prompt, 7 lines for completion menu,
        # 1 line for toolbar.
        self.ui_lines = 9
        self.status = {'unread': None}
    def toolbar(self, cli):
        return [
                (Token.Text, " Unread: "),
                (Generic.Heading, str(self.status['unread'])),
                ]

    def setPrompt(self, newprompt):
        """Set the prompt string"""
        self.prompt = newprompt

    def singleprompt(self, prompt, ispassword=False, default=u'', titlefunc=None, completer=None):
        origPrompt = self.prompt
        self.prompt = prompt
        origHistory = self.history
        origCompleter = self.cli.application.buffer.completer
        self.cli.application.buffer.completer = completer
        # TODO: Interpret titlefunc!
        tmphistory = prompt_toolkit.history.InMemoryHistory()
        # FIXME: This doesn't stop the up-arrow from seeing previously entered
        # text!
        # The reason is, up-arrow/down-arrow doesn't walk the History, it
        # walks the _working_lines of the Buffer, which aren't supposed to be
        # accessible outside. We can clear it by calling Buffer.reset(), but
        # then it cannot be restored when we are done here. Probably we should
        # be creating a new temporary Buffer object and attaching it to the
        # application instead!
        self.cli.application.buffer.history = tmphistory
        self.lexerEnabled = False
        self.cli.application.buffer.document = prompt_toolkit.document.Document(default)
        try:
            text = self.cli.run(False)
            text = text.text
        finally:
            self.prompt = origPrompt
            self.cli.application.buffer.history = origHistory
            self.cli.application.buffer.completer = origCompleter
            self.lexerEnabled = True
        return text

    def cmdSingle(self, intro=None):
        """Perform a single prompt-and-execute sequence.

        Only displays an intro if given (doesn't fall back to instance's intro).
        Doesn't call the preloop hook (since we aren't looping).
        """
        if intro is not None:
            self.stdout.write(str(intro)+"\n") # TODO: python 3 support?
        if self.cmdqueue:
            line = self.cmdqueue.pop(0)
        else:
            try:
                line = self.cli.run(True)
                line = line.text
            except EOFError:
                line = 'EOF'
        line = self.precmd(line)
        stop = self.onecmd(line)
        stop = self.postcmd(stop, line)
        return stop

    
    def cmdloop(self, intro=None):
        """Repeatedly issue a prompt, accept input, parse an initial prefix
        off the received input, and dispatch to action methods, passing them
        the remainder of the line as argument.

        """

        # Based on the python2.7 version of the super class (cmd).
        self.preloop()
        # TODO: handle completion key passing?
        try:
            if intro is not None:
                self.intro = intro
            if self.intro:
                self.stdout.write(str(self.intro)+"\n")
            stop = None
            while not stop:
                stop = self.cmdSingle()
            self.postloop()
        finally:
            # Any cleanup here? We aren't using readline at all...
            pass
    def runAProgramWithInput(self, args, data):
        """Run a program with the given input. Leaves stdout/stderr alone.

        This should be run when the prompt is inactive."""
        res=[]
        def finish(proc,status,signal):
            proc.close()
            proc.loop.stop()
            res.append(status)
        com = pyuv.Pipe(self.ptkevloop.realloop, True)
        stdio = [
                pyuv.StdIO(stream=com, flags=pyuv.UV_CREATE_PIPE | pyuv.UV_READABLE_PIPE),
                pyuv.StdIO(fd=sys.stdout.fileno(), flags=pyuv.UV_INHERIT_FD),
                pyuv.StdIO(fd=sys.stderr.fileno(), flags=pyuv.UV_INHERIT_FD),
                ]
        self.ttyBusy = True
        s = pyuv.Process.spawn(self.ptkevloop.realloop, args, stdio=stdio, exit_callback=finish)
        def closeWhenDone(handle, error):
            # TODO: Maybe report error?
            handle.close()
        com.write(data, closeWhenDone)
        self.ptkevloop.realloop.run()
        self.ttyBusy = False
        return res[0]
    def runAProgramAsFilter(self, args, data):
        """Run a program with the given input. Return the output. Leaves stderr alone.

        This should be run when the prompt is inactive."""
        res=[None, b""]
        done=[0]
        def finish(proc,status,signal):
            proc.close()
            proc.loop.stop()
            res[0] = status
        com = pyuv.Pipe(self.ptkevloop.realloop, True)
        como = pyuv.Pipe(self.ptkevloop.realloop, True)
        stdio = [
                pyuv.StdIO(stream=com, flags=pyuv.UV_CREATE_PIPE | pyuv.UV_READABLE_PIPE),
                pyuv.StdIO(stream=como, flags=pyuv.UV_CREATE_PIPE | pyuv.UV_WRITABLE_PIPE),
                pyuv.StdIO(fd=sys.stderr.fileno(), flags=pyuv.UV_INHERIT_FD),
                ]
        self.ttyBusy = True
        s = pyuv.Process.spawn(self.ptkevloop.realloop, args, stdio=stdio, exit_callback=finish)
        def closeWhenDone(handle, error):
            # TODO: Maybe report error?
            handle.close()
        def doneWriting(handle, error):
            # TODO: Maybe report error?
            if done[0]:
                closeWhenDone(handle, error)
            done[0] = True
        def readCb(handle, data, error):
            if data is None:
                handle.stop_read()
                if done[0]:
                    closeWhenDone(handle, error)
                done[0] = True
                return
            res[1] += data
        como.start_read(readCb)
        com.write(data, closeWhenDone)
        self.ptkevloop.realloop.run()
        self.ttyBusy = False
        return res
    def runAProgramStraight(self, args):
        """Run a program without anything special. Leaves stdin/stdout/stderr alone.

        This should be run when the prompt is inactive."""
        res=[]
        def finish(proc,status,signal):
            proc.close()
            proc.loop.stop()
            res.append(status)
        stdio = [
                pyuv.StdIO(fd=sys.stdin.fileno(), flags=pyuv.UV_INHERIT_FD),
                pyuv.StdIO(fd=sys.stdout.fileno(), flags=pyuv.UV_INHERIT_FD),
                pyuv.StdIO(fd=sys.stderr.fileno(), flags=pyuv.UV_INHERIT_FD),
                ]
        self.ttyBusy = True
        s = pyuv.Process.spawn(self.ptkevloop.realloop, args, stdio=stdio, exit_callback=finish)
        self.ptkevloop.realloop.run()
        self.ttyBusy = False
        return res[0]
    def runAProgramGetOutput(self, args):
        """Run a program obtaining standard output as a string. Leaves stdin/stderr alone.

        This should be run when the prompt is inactive."""
        res=[]
        def finish(proc,status,signal):
            proc.close()
            proc.loop.stop()
            res.append(status)
        com = pyuv.Pipe(self.ptkevloop.realloop, True)
        stdio = [
                pyuv.StdIO(fd=sys.stdin.fileno(), flags=pyuv.UV_INHERIT_FD),
                pyuv.StdIO(stream=com, flags=pyuv.UV_CREATE_PIPE | pyuv.UV_WRITABLE_PIPE),
                pyuv.StdIO(fd=sys.stderr.fileno(), flags=pyuv.UV_INHERIT_FD),
                ]
        self.ttyBusy = True
        s = pyuv.Process.spawn(self.ptkevloop.realloop, args, stdio=stdio, exit_callback=finish)
        self.ptkevloop.realloop.run()
        self.ttyBusy = False
        return res[0]
