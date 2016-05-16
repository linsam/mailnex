import cmd
import prompt_toolkit

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
                line = prompt_toolkit.prompt(self.prompt)
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

        # Copied from the python2.7 version of the super class (cmd), then
        # modified to use prompt_toolkit and to use cmdSingle
        self.preloop()
        # handle completion key passing?
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
