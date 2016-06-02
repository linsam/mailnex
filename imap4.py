import re
import ssl
import socket
# An attempt at our own imap lib.
# Goals: 
#   * Be runnable either in its own thread or via an eventloop
#   * Notify user (that is, the program using this lib) via callbacks (event
#     notification either way)
#   * Wrap some async operations synchronously (so, e.g. a caller can ask for
#     the contents of a specific message, and we can return that rather than
#     having to call back)
#   * Have an API for most (or all) standard IMAP commands and common
#     extensions, but allow any command to be executed and either return the
#     raw response, or a parsed hierarchy response as the user desires.
# Non-Goals:
#   * Compatibility with python standard imaplib - its API isn't at all easy
#     to integrate into anything beyond simple applications
#   * do everything - This should be enough of a wrapper to make it easy to
#     talk IMAP to an IMAP server by handling details like state machine
#     tracking, capabilities tracking, and data format conversions. It is not
#     intended to actually abstract away the protocol. The user should still
#     have an understanding of IMAP in order to use it. For example, it is
#     usually a Very Bad Idea to ask the server for a full recursive listing
#     of mailboxes automatically, because some servers root the IMAP diretory
#     in the user's home directory which can be quite large (UW-IMAP does
#     this, and I've used a system where full home enumeration took 15 minutes
#     on a good day and *never ended* on a bad day due to infinite recursion
#     from a symlink that pointed up, and the server didn't check for a
#     maximum depth or that it was in a loop). Instead, such walks should be
#     user directed, which requires operating the LIST command and knowing how
#     it works.


STATE_NOCON = 0
STATE_UNAUTH = 1
STATE_AUTH = 2
STATE_SELECT = 3
STATE_LOGOUT = 4

re_untagged = re.compile(r'\* (OK|NO|BAD|PREAUTH|BYE) (\[[^]]*\])? ?(.*)', re.DOTALL)
re_tagged = re.compile(r'([^ ]*) (OK|NO|BAD|PREAUTH|BYE) (\[[^]]*\])? ?(.*)', re.DOTALL)
re_continue = re.compile(r'\+ (OK|NO|BAD|PREAUTH|BYE) (\[[^]]*\])? ?(.*)')
re_numdat = re.compile(r'\* (\d+) ([a-zA-Z]+) ?(.*)', re.DOTALL)


class imap4ClientConnection(object):
    # Connections can be happily in several states:
    #  * Not Authenticated
    #  * Authenticated
    #  * Selected
    #  * logout
    #
    # Upon establishing a connection, we will get a server greeting
    # and will be placed directly into one of "Not Authenticated" ("OK"
    # greeting), "Authenticated" (if we have pre-authorization, "PREAUTH"
    # greeting), or "logout" (rejected by "BYE" greating) states.
    #
    # From "Not Authenticated" we can either go to "logout" (via LOGOUT
    # command or connection loss)  or "Authenticated" (via LOGIN or
    # AUTHENTICATE command).
    #
    # From "Authenticated" we can go to "logout" or "selected" (via SELECT or
    # EXAMINE)
    #
    # From "selected" we can either go to "logout" or return to
    # "authenticated" (via CLOSE command, or failure in SELECT of EXAMINE)
    #
    # Commands in any state: CAPABILITY, NOOP, LOGOUT
    # Commands in "not authenticated": AUTHENTICATE, LOGIN, STARTTLS
    # Commands in "Authenticated" and "Selected" : SELECT, EXAMINE, CREATE,
    # DELETE, RENAME, SUBSCRIBE, UNSUBSCRIBE, LIST, LSUB, STATUS, APPEND
    # Commands in "Selected": CHECK, CLOSE, EXPUNGE, SEARCH, FETCH, STORE,
    # COPY, UID (UID COPY, UID FETCH, UID STORE, UID SEARCH)
    #
    # ###FIXME This section is wrong!###
    # The server can respond with a TAGed response (completes the command with
    # the same tag), un UNTAGged response (* instead of a tag number,
    # indicates information that may or may not be a result of a command,
    # including new messages or changes by another client), or a Continuation
    # response (+ instead of a tag number, indicates server needs more
    # information from client, e.g. incomplete command given. Used in
    # negotiations, for example).
    # ###END FIXME###
    #
    # The server responds in 3 forms: Status, data, and continueation
    # Status can be tagged or untagged. Tagged completes a command. Server
    # data is untagged.
    #
    # Statuses are OK, NO, BAD, PREAUTH, and BYE. OK, NO, and BAD can appear
    # in tagged and untagged messages. PREAUTH and BYE are only untagged.
    #
    # Statuses may have an optional code, which is in square brackets and can
    # be an atom followed by space and arguments. Codes include ALERT,
    # BADCHARSET, CAPABILITY, PARSE, PERMANENTFLAGS, READ-ONLY, READ-WRITE,
    # TRYCREATE, UIDNEXT, UIDVALIDITY, and UNSEEN.
    #
    # OK responses may also have text that can be presented to the user (or
    # not). Untagged is also used at initial connection to indicate that
    # authentication is needed.
    #
    # NO indicates an operational error when tagged, a warning when untagged.
    # Text can be presented to a user (spec doesn't say MAY, SHOULD, or MUST,
    # so I'll assume it is MAY like with OK)
    #
    # BAD indicates an error message. Tagged is a protocol error in the
    # command, untagged is a protocol error from an unknown point.
    #
    # PREAUTH is untagged and indicates on initial connection that you are in
    # the authenticated state
    #
    # BYE is untagged and indicates that the server is about to close the
    # connection. Human text may be displayed to the user. The client (us)
    # should continue reading responses until the connection is actually
    # closed (that is, BYE might not be the last response on the line)
    #
    #
    # Capabilities
    # ------------
    #
    #  RFC3501 (base IMAP spec used here):
    #   IMAP4rev1 - Supports this spec (must be present)
    #  RFC2595 (IMAP TLS, first 3 required by 3501)
    #   STARTTLS
    #   LOGINDISABLED
    #   AUTH=PLAIN
    #
    #
    def __init__(self):
        object.__init__(self)
        self.tag = 0
        self.state = STATE_NOCON
        self.socket = None
        self.caps = None
        self.maxlinelen = 10000
    def doSimpleCommand(self, cmd):
        """Do a simple command. Send an autogenerated tag and wait for a matching tagged response.

        Does not support doing concurrent outstanding commands.
        Does not support continuation commands (receipt of a continuation response will raise
        and exception)"""
        # TODO: Allow tags to be templated or something.
        self.tag += 1
        tagstr = "T%i" % self.tag
        self.socket.send("%s %s\r\n" % (tagstr, cmd))
        line = ""
        while True:
            line += self.socket.recv(1)
            if self.maxlinelen and len(line) > self.maxlinelen:
                # TODO: Try to cleanup by flushing? Let something higher take
                # care of it?
                raise Exception("Server response too long (exceeds maxlinelen)")
            if line.endswith('\r\n'):
                # We got a whole line. Process it.
                if line.startswith("+"):
                    raise Exception("Continuation required")
                # Any response can have a response code. initial codes can be
                # ALERT, BADCHARSET, CAPABILITY, PARSE, PERMANENTFLAGS,
                # READ-ONLY, READ-WRITE, TRYCREATE, UIDNEXT, UIDVALIDITY,
                # and UNSEEN. Others outside the base spec include
                # HIGHESTMODSEQ. We can receive anything, and are instructed
                # to ignore anything we don't recognize.
                #
                # ABNF response layout
                # a response is any number of continue-req or response-data
                # followed by a single response-done.
                # response-done is either a tagged response (tag sp
                # resp-cond-state crlf) or response-fatal ('*' sp 'BYE' sp
                # resp-text)
                # resp-cond-state is "OK" or "NO" or "BAD" followed by sp and
                # resp-text.
                # resp-text contains an optional resp-text-code in square
                # brackets, and always contains text.
                # 
                # response-data (not fatal or done) is a '*' sp followed by a
                # resp-cond-state, resp-cond-bye, mailbox-data, message-data
                # or capability-data followed by crlf
                #
                # mailbox-data is "FLAGS" with flag-list, "LIST" with
                # mailbox-list, "LSUB" with mailbox-list, "SEARCH" with a
                # space separated list of numbers, "STATUS" with mailbox an
                # optional status-att-list in parenthesis, a number followed
                # by "EXISTS", or a number followed by "RECENT"
                #
                # message-data is a number followed by "EXPUNGE" or "FETCH"
                # with msg-att. (such as FLAGS, ENVELOPE, BODY, etc)
                #
                # capability-data is "CAPABILITY" followed by a space
                # separated list of capabilities.
                #
                #
                #
                # TODO: If the line ends with '}', look back for '{' followed by digits. If we have it, we probably have a string literal, and should thus fetch the number of bytes in-the-raw
                end = line.split()[-1]
                if end.startswith('{') and end.endswith('}') and end[1:-1].isdigit():
                    count = int(end[1:-1],10)
                    while count:
                        partial = self.socket.recv(count)
                        if partial == "":
                            # TODO: Might have been SSL layer stuff. Figure
                            # out how to check if the socket is actually dead.
                            raise Exception("Lost socket?")
                        line += partial
                        count -= len(partial)
                    continue
                if line.startswith(tagstr):
                    # This line completes the transaction
                    # TODO: What if the tagged response has a sting literal in
                    # it? How to process that?
                    a = re.match(re_tagged, line)
                    tag, status, code, string = a.groups()
                    print("tag",line)
                    if tag != tagstr:
                        # Log a warning
                        print("Unexpected tag %s received; was waiting for %s" % (tag, tagstr))
                        # Keep waiting for *our* tag
                        continue
                    # TODO: Raise an exception for non OK replies
                    return status, code, string
                else:
                    # Process this line, but keep going
                    # Note: Untagged can be more than just OK,NO,BAD, etc.
                    #       Can also be results, e.g. * FLAGS (\Answered \Seen)
                    #       or value results, e.g. * 6347 EXISTS
                    #                              * 0 RECENT
                    #print("notag",line)
                    r = re.match(re_untagged, line)
                    if r is not None:
                        status, code, string = r.groups()
                        # TODO, look for content to cache and/or callback
                        print("response",status,code,string)
                        if code:
                            # TODO: break out into function call
                            c = code[1:-1]
                            cs = c.split()
                            cn = cs[0].upper()
                            # IMAP4rev1
                            if cn == "ALERT":
                                # TODO: callback for alert message? Log it
                                # ourselves? Both?
                                pass
                            elif cn == "BADCHARSET":
                                pass
                                # TODO: callback for bad charset?
                            elif cn == "PERMANENTFLAGS":
                                # Make this an assert?
                                if cs[1][0] == '(':
                                    cs[1] = cs[1][1:]
                                else:
                                    raise Exception("Malformed PERMANENTFLAGS")
                                # Make this an assert?
                                if cs[-1][-1] == ')':
                                    cs[-1] = cs[-1][:-1]
                                else:
                                    raise Exception("Malformed PERMANENTFLAGS")
                                self.permflags = cs[1:]
                            elif cn == "READ-ONLY":
                                self.rw = False
                            elif cn == "READ-WRITE":
                                self.rw = True
                            elif cn == "TRYCREATE":
                                # TODO: callback for trycreate?
                                pass
                            elif cn == "UIDNEXT":
                                self.uidnext = int(cs[1], 10)
                            elif cn == "UIDVALIDITY":
                                self.uidvalidity = int(cs[1], 10)
                            elif cn == "UNSEEN":
                                self.unseen = int(cs[1], 10)
                            # CONDSTORE
                            elif cn == "HIGHESTMODSEQ":
                                self.highestmodseq = int(cs[1], 10)
                            else:
                                print("unknown code '%s'; ignoring" % cn)
                    else:
                        # Other format. Look for content to cache and/or callback
                        r = re.match(re_numdat, line)
                        if r is not None:
                            num, typ, data = r.groups()
                            # message-data
                            if typ.upper() == "FETCH":
                                print("FETCH for %s" % num, data)
                                # TODO: callback for fetch data
                            elif typ.upper() == "EXPUNGE":
                                print("EXPUNGE for %s" % num)
                                # TODO: callback for expunge data
                            # numerical mailbox-data
                            elif typ.upper() == "EXISTS":
                                print("Exists: %s" % num)
                                self.exists = int(num, 10)
                            elif typ.upper() == "RECENT":
                                print("Recent: %s" % num)
                                self.recent = int(num, 10)
                            else:
                                print("uknown numerical '%s'" % typ.upper(), line)
                        else:
                            print("nomatch",line)
                    line = ""
    def connect(self, host, **kwargs):
        # TODO: Try base port with STARTTLS, then SSL port, then base port
        # without TLS? 
        # TODO: Use the eventloop; support interruption via other event (e.g.
        # command pipe)
        port = 143
        if 'port' in kwargs:
            port = kwargs['port']
        if port == 993:
            useSsl = True
        s = socket.socket()
        if useSsl:
            oldSock = s
            s = ssl.SSLSocket(s)
        else:
            oldSock = None
        try:
            s.connect((host, port))

            r = s.recv(1024)
            a = re.match(re_untagged, r)
            if a is None:
                # TODO: Log the response?
                raise Exception("Bad response from server")
            status, code, string = a.groups()
            if code == '[ALERT]':
                # TODO: Log 'string' with priority. We want the user to see
                # it.
                pass
            if status == "OK":
                # Transition to unauthenticated. Cache any capabilities. Add
                # message to info log
                self.state = STATE_UNAUTH
            elif status == "PREAUTH":
                # Transition to authenticated. Cache any capabilities. Add
                # message to info log
                self.state = STATE_AUTH
            elif status == "BYE":
                # Transition to disconnected. Show error string to user
                self.state = STATE_LOGOUT
            else:
                # TODO: Log the response?
                raise Exception("Unexpected response from server")
            if code and code.startswith('[CAPABILITY'):
                c = code.strip('[]')
                caps = c.split()[1:]
                print "Capabilities:", caps
                self.caps = caps
            self.socket = s
        except KeyboardInterrupt:
            print("Aborting connection")
            del s
            return
        self.recent = None
        self.exists = None
        # Flags is what might be reported. permflags is what we can expect to
        # set/unset non-volatily. permflags will have r'\*' if we can create
        # new flags.
        self.flags = None
        self.permflags = None
        self.unseen = None
        self.uidvalidity = None
        self.uidnext = None
        self.highestmodseq = None
    def starttls(self):
        if self.state != STATE_UNAUTH:
            raise Exception("Bad client state for command")
        if isinstance(self.socket, ssl.SSLSocket):
            raise Exception("Already in TLS mode")
        # Run STARTTLS command, wait for go ahead
        res, code, string = self.doSimpleCommand("STARTTLS")
        if res != 'OK':
            raise Exception("No TLS on server")
        # TODO: Support client certificate
        self.origsocket = self.socket
        self.socket = ssl.wrap_socket(self.socket)
        # TODO: Server Certificate checks and whatnot.
    def login(self, username, password):
        #self.socket.send("T%i LOGIN \"%s\" \"%s\"\r\n" % (self.tag, username, password))
        #self.tag += 1
        #r = self.socket.recv(1024)
        #print(r)
        res, code, string = self.doSimpleCommand("LOGIN \"%s\" \"%s\"" % (username, password))
        if (res == 'OK'):
            self.state = STATE_AUTH
    def select(self, box = None):
        if box is None:
            box = "INBOX"
        res, code, string = self.doSimpleCommand("SELECT %s" % box)
        if res != 'OK':
            raise Exception("Failed to select box")
    def getheaders(self, message):
        res, code, string = self.doSimpleCommand("fetch %s (BODY.PEEK[HEADER])" % message)
        if res != 'OK':
            raise Exception("Failed to select box")
