import cmd
import prompt_toolkit

# TODO: Make pygments optional?
from pygments.lexer import RegexLexer
from pygments.token import *
from prompt_toolkit.layout.lexers import PygmentsLexer
from pygments.token import Token
from pygments.styles.tango import TangoStyle
from prompt_toolkit.styles import style_from_pygments
from pygments.lexers import HtmlLexer
import pygments.style

class PromptLexer(RegexLexer):
    """Basic lexer for our command line."""
    name = 'Prompt'
    aliases = ['prompt']
    filenames = []
    tokens = {
            'root': [
                # Commands. TODO: Auto generate this list
                (r'^print\b', Generic.Inserted),
                (r'^quit\b', Generic.Inserted),
                (r'^help\b', Generic.Inserted),
                (r'^headers\b', Generic.Inserted),
                # Other stuff
                (r'^[^ ]* ', Generic.Heading),
                # I cannot get the next to match. If I end with '$' instead of
                # '\n', or no ending after '.*', python hangs on what looks
                # like an infinitely expanding malloc loop.
                (r'.*\n', Text),
                ]
            }

class PromptPygStyle(pygments.style.Style):
    """A Simple style for our interactive prompt's user text."""
    # according to docs, default_style is the style inherited by all token
    # types. I haven't made this do anything, so we'll leave it blank.
    default_style = ''
    styles = {
            Generic.Inserted: 'italic #88f',
            Generic.Heading: 'bold #8f8',
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
        start_of_line = document.current_line_before_cursor.strip()
        if this_word == start_of_line:
            for i in self.cmd.completenames(this_word):
                yield prompt_toolkit.completion.Completion(i, start_position=-len(this_word))

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

    def __init__(self):
        cmd.Cmd.__init__(self)
        self.completer = Completer(self)

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
                line = prompt_toolkit.prompt(
                        self.prompt,
                        lexer=PygmentsLexer(PromptLexer),
                        style=prompt_style,
                        completer=self.completer
                        )
            except EOFError:
                line = 'EOF'
        line = self.precmd(line)
        stop = self.onecmd(line)
        stop = self.postcmd(stop, line)

    
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
                self.cmdSingle()
            self.postloop()
        finally:
            # Any cleanup here? We aren't using readline at all...
            pass
