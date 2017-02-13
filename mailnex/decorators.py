from functools import wraps

# Some decorators
def needsConnection(func):
    @wraps(func)
    def needsConnectionWrapper(self, *args, **kwargs):
        if not self.C.connection:
            print("no connection. Try the 'folder' command.")
        else:
            return func(self, *args, **kwargs)
    return needsConnectionWrapper

def shortcut(name):
    """Marks a function as having a shortcut.

    Actually, we'll just passthrough right now. This will just document intent for now.

    Eventually, we can add implementation without going through all the functions again to add it."""
    def wrap1(func):
        @wraps(func)
        def shortcutWrapper(self, *args, **kwargs):
            return func(self, *args, **kwargs)
        return shortcutWrapper
    return wrap1

def optionalNeeds(var, message=None):
    """Marks a command as being optional, needing the given variable to be true to be active.

    If a message is specified, it will be displayed when the command is attempted."""
    def wrap1(func):
        @wraps(func)
        def optionalNeedsWrapper(self, *args, **kwargs):
            if not var:
                print("command '%s' is unavailable" % func.__name__[3:])
                if message:
                    print(message)
                print("See 'help optional_packages' for more information")
            else:
                return func(self, *args, **kwargs)
        return optionalNeedsWrapper
    return wrap1

def argsToMessageList(func):
    """This decorator causes a call to self.parseMessageList on the single (non-self) argument, then calls the real function with the list as a single array parameter or None.

    If no arguments are provided, uses None. This allows a command to distinguish between arguments that produce no patch, and a lack of arguments.

    If the message list evaluates to out of bounds, prints a diagnostic and
    doesn't call the wrapped function (e.g. "at EOF" when trying to display
    the next message after the last). This isn't foolproof, and relies on
    parseMessageList to detect it.

    We could just return the current message when nothing is given, but that would make the updateMessageSelectionAtEnd decorator much harder to implement, since it should unmark on no argument (that is, clear the lastList).

    As well, some commands behave differently with or without an argument. E.g. headers does different selection updates and displays markers differently.
    """
    @wraps(func)
    def argsToMessageListWrapper(self, args):
        if args:
            msglist = self.parseMessageList(args)
            if len(msglist) == 1:
                if msglist[0] == 0:
                    print("Nothing before the first message")
                    return None
                elif msglist[0] >= self.C.lastMessage + 1:
                    print("at EOF")
                    return None
        else:
            msglist = None
        return func(self, msglist)
    return argsToMessageListWrapper

# Constants for updateMessageSelectionAtEnd:
UMSAE_DEFAULT = 0
UMSAE_NEXT_IS_CURRENT = 1 # 'from' behavior. NEXT and CURRENT set the same
UMSAE_RETURNS_CURRENT = 2 # 'next' behavior. CURRENT isn't the last in the list.

def updateMessageSelectionAtEnd(UMSAE_style):
    """This decorator updates message selections after the wrapped function completes, but only if no exception is raised.

    The wrapped function must take a message list as its first non-self parameter. (for example, wrap this with argsToMessageList)

    Most commands select the last message of the message list as the current message and update the previous message to the previously current message, and update the marked message list to be the given message list.
    """
    def wrap1(func):
        @wraps(func)
        def updateMessageSelectionAtEnd(self, msglist, *args, **kwargs):
            # First, cache the current message; the command we run might change
            # it, but we need it to update the last message correctly. We'll also
            # use it to restore the current message if the function fails.
            previouslyCurrent = self.C.currentMessage
            try:
                res = func(self, msglist, *args, **kwargs)
            except Exception:
                # First restore the current message if we can, but don't fail if
                # we can't.
                try:
                    self.C.currentMessage = previouslyCurrent
                except:
                    pass
                # Next pass it on up
                raise
            # We successfully finished (well, didn't have an exception), so
            # update the values
            # However, don't update if the message list was empty
            if msglist is None:
                self.C.lastList = []
            else:
                self.C.lastList = msglist
                if len(msglist):
                    self.C.prevMessage = previouslyCurrent
                    self.C.currentMessage = msglist[-1]
            if UMSAE_style == UMSAE_DEFAULT:
                self.C.nextMessage = self.C.currentMessage + 1
            elif UMSAE_style == UMSAE_NEXT_IS_CURRENT:
                self.C.nextMessage = self.C.currentMessage
            elif UMSAE_style == UMSAE_RETURNS_CURRENT:
                self.C.currentMessage = res
                self.C.nextMessage = self.C.currentMessage + 1
                # Functions using this style forfeit their ability to return
                # data to caller. Most commonly, the caller is the command
                # loop, and any truth value results in exiting, so this isn't
                # a problem.
                res = None
            else:
                raise Exception("Unknown UMSAE style")
            return res
        return updateMessageSelectionAtEnd
    return wrap1

def showExceptions(func):
    """This decorator displays exceptions and returns to normal operation.

    It should only wrap commands, not the functions that commands call, because we cannot know what a
    valid return value would be. It takes do_* argument 'args' only, to reduce the likelihood of
    mis-application. As such, it should probably be the outermost wrapper.

    This will print a short exception unless the debug setting contains the exception flag, in which
    case a full stack trace will be shown."""
    @wraps(func)
    def showExceptionsWrapper(self, args):
        if not hasattr(self.C, "excTrack"):
            self.C.excTrack = True
        if self.C.excTrack:
            self.C.excTrack = False
            topException = True
        else:
            topException = False

        result = None
        try:
            result = func(self, args)
        except Exception as ev:
            if not topException:
                raise
            import traceback
            if not self.C.settings.debug.exception:
                # TODO: Print the exception type hierarchy. E.g.
                # "exceptions.KeyError" instead of just "KeyError", since some
                # modules may define same-named exceptions with different
                # meanings.
                # TODO: Fix lstrip, it converts 'do_open' to 'pen' because it
                # is a set of characters to strip, not a string to strip.
                print("Error occurred in command '{}': {}".format(func.__name__.lstrip('do_'), traceback.format_exception_only(type(ev), ev)[-1]))
            else:
                traceback.print_exc()
            print("Warning: mailnex may now be in an inconsistent state due to the above error.")
        if topException:
            self.C.excTrack = True
        return result
    return showExceptionsWrapper

