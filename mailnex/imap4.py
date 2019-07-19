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
re_untagdat = re.compile(r'\* ([a-zA-Z]+) ?(.*)', re.DOTALL)

class imap4Exception(Exception):
    """Root exception for all exceptions raised by this imap4 module"""
class imap4NoConnect(imap4Exception):
    """Exception for connection failure"""

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
        self.maxlinelen = 50 * 1024 * 1024
        self.cb_fetch = None
        self.cb_search = None
        self.debug = False
        self.ca_certs = None
        self.idling = False
        # Callbacks dictionary
        self.cbs = {}
    def close(self):
        # TODO: Issue a logout. Wait for server to finish?
        if self.socket:
            self.socket.close()
        # Reset all attributes
        self.__init__()
    def setCaCerts(self, certs):
        """Use to enable certificate verification for SSL and STARTTLS sessions.

        Set to a string containing the path and filename of the CA certificates.
        Set to None to disable certificate verification.

        For example, set to /etc/ssl/certs/ca-certificates.crt
        """
        self.ca_certs = certs
    def setCB(self, name, function):
        # This function exists so that we can have a static interface while
        # experimenting with changing the backend.
        self.cbs[name] = function
    def clearCB(self, name):
        try:
            del self.cbs[name]
        except KeyError:
            # Don't worry if the callback doesn't exist
            pass
    def processCodes(self, status, code, string):
        # Assert code[0] == '[' and code[-1] == ']'
        codes = code[1:-1].split()
        codename = codes[0].upper()
        # These are 'resp-text-code' in the IMAP ABNF
        # IMAP4rev1 codes
        if codename == "ALERT":
            # TODO: Log, show to screen, something. The user is supposed to
            # see the output!
            # TODO: callback for alert message? Log it
            # ourselves? Both?
            pass
        elif codename == 'CAPABILITY':
            caps = codes[1:]
            if self.debug:
                print "Capabilities:", caps
            self.caps = caps
        elif codename == "BADCHARSET":
            pass
            # TODO: callback for bad charset?
        elif codename == "PERMANENTFLAGS":
            # Make this an assert?
            if codes[1][0] == '(':
                codes[1] = codes[1][1:]
            else:
                raise imap4Exception("Malformed PERMANENTFLAGS")
            # Make this an assert?
            if codes[-1][-1] == ')':
                codes[-1] = codes[-1][:-1]
            else:
                raise imap4Exception("Malformed PERMANENTFLAGS")
            self.permflags = codes[1:]
        elif codename == "READ-ONLY":
            self.rw = False
        elif codename == "READ-WRITE":
            self.rw = True
        elif codename == "TRYCREATE":
            # TODO: callback for trycreate?
            pass
        elif codename == "UIDNEXT":
            self.uidnext = int(codes[1], 10)
        elif codename == "UIDVALIDITY":
            self.uidvalidity = int(codes[1], 10)
        elif codename == "UNSEEN":
            self.unseen = int(codes[1], 10)
        # RFC 4551 CONDSTORE additions
        elif codename == "HIGHESTMODSEQ":
            self.highestmodseq = int(codes[1], 10)
        elif codename == "NOMODSEQ":
            self.highestmodseq = None
        # RFC 5162 QRESYNC additions
        elif codename == "CLOSED":
            pass
            # Also VANISHED, but that shouldn't happen unless we explicitly
            # enable QRESYNC
        # RFC5530 section 6 list
        #   2060
        #       * NEWNAME
        #   2221
        #       * REFERRAL
        #   3501
        #       * PARSE
        #   3516
        #       * UNKNOWN-CTE
        #   4315
        #       * UIDNOTSTICKY
        #       * APPENDUID
        #       * COPYUID
        #   4467
        #       * URLMECH
        #   4469
        #       * TOOBIG
        #       * BADURL
        #   4551
        #       * MODIFIED
        #   4978
        #       * COMPRESSIONACTIVE
        #   5182
        #       * NOTSAVED
        #   5255
        #       * BADCOMPARATOR
        #   5257
        #       * ANNOTATE
        #       * ANNOTATIONS
        #   5259
        #       * TEMPFAIL
        #       * MAXCONVERTMESSAGES
        #       * MAXCONVERTPARTS
        #   5267
        #       * NOUPDATE
        #   5464
        #       * METADATA
        #   5465
        #       * NOTIFICATIONOVERFLOW
        #       * BADEVENT
        #   5466
        #       * UNDEFINED-FILTER
        #
        # RFC5530 "IMAP Response Codes" additions
        # * UNAVAILABLE
        # * AUTHENTICATIONFAILED
        # * AUTHORIZATIONFAILED
        # * EXPIRED
        # * PRIVACYREQUIRED
        # * CONTACTADMIN
        # * NOPERM
        # * INUSE
        # * EXPUNGEISSUED
        # * CORRUPTION
        # * SERVERBUG
        # * CLIENTBUG
        # * CANNOT
        # * LIMIT
        # * OVERQUOTA
        # * ALREADYEXISTS
        # * NONEXISTENT
        else:
            print("unknown code '%s'; ignoring" % codename)

    def doIdle(self):
        """Enter idle mode.

        Caller is responsible for calling doIdleData() when the socket is ready to receive.

        Raises an exception if connection doesn't support IDLE capability;
        caller should then poll with NOOP commands to get updates.
        """
        if not 'IDLE' in self.caps:
            raise Exception("IMAP connection lacks IDLE capability")
        self.tag += 1
        tagstr = "T{}".format(self.tag)
        self.socket.send("{} idle\r\n".format(tagstr))
        self.idling = True
        # TODO: START: common code for get a line from the IMAP connection
        line = ""
        linelen = 0
        while True:
            data = self.socket.recv(1)
            thislen = len(data)
            if thislen == 0:
                self.close()
                raise imap4Exception("Server connection lost? 0 length read occured")
            line += data
            linelen += thislen
            # TODO: Timeout if X seconds have passed and yet we don't have a
            # completed request.
            # Probably requires a select or better yet, eventloop integration.
            if self.maxlinelen and linelen > self.maxlinelen:
                # TODO: Try to cleanup by flushing? Let something higher take
                # care of it?
                raise imap4Exception("Server response too long (at %i, which exceeds maxlinelen %i)" % (len(line), self.maxlinelen))
            if line.endswith('\r\n'):
                if self.debug:
                    print("doIdle recvline: {}".format(repr(line)))
                # TODO: END: common code for get a line from the IMAP connection
                if not line.startswith("+ "):
                    line = ""
                    linelen = 0
                    # TODO: timeout? limit number of lines we'll wait for?
                    continue
                break
    def doIdleData(self):
        # TODO: START: common code for get a line from the IMAP connection
        line = ""
        linelen = 0
        while True:
            data = self.socket.recv(1)
            thislen = len(data)
            if thislen == 0:
                self.close()
                raise imap4Exception("Server connection lost? 0 length read occured")
            line += data
            linelen += thislen
            # TODO: Timeout if X seconds have passed and yet we don't have a
            # completed request.
            # Probably requires a select or better yet, eventloop integration.
            if self.maxlinelen and linelen > self.maxlinelen:
                # TODO: Try to cleanup by flushing? Let something higher take
                # care of it?
                raise imap4Exception("Server response too long (at %i, which exceeds maxlinelen %i)" % (len(line), self.maxlinelen))
            if line.endswith('\r\n'):
                if self.debug:
                    print("doIdleData recvline: {}".format(repr(line)))
                # TODO: END: common code for get a line from the IMAP connection
                self.processUntagged(line)
                break

    def doSimpleCommand(self, cmd):
        """Do a simple command. Send an autogenerated tag and wait for a matching tagged response.

        Does not support doing concurrent outstanding commands.
        Does not support continuation commands (receipt of a continuation response will raise
        and exception)"""
        if self.idling:
            # TODO: check for tagged completion of idle command after sending
            # done?
            self.socket.send("done\r\n")
        # TODO: Allow tags to be templated or something.
        self.tag += 1
        tagstr = "T%i" % self.tag
        self.socket.send("%s %s\r\n" % (tagstr, cmd))
        # TODO: START: common code for get a line from the IMAP connection
        line = ""
        linelen = 0
        while True:
            data = self.socket.recv(1)
            thislen = len(data)
            if thislen == 0:
                self.close()
                raise imap4Exception("Server connection lost? 0 length read occured")
            line += data
            linelen += thislen
            # TODO: Timeout if X seconds have passed and yet we don't have a
            # completed request.
            # Probably requires a select or better yet, eventloop integration.
            if self.maxlinelen and linelen > self.maxlinelen:
                # TODO: Try to cleanup by flushing? Let something higher take
                # care of it?
                raise imap4Exception("Server response too long (at %i, which exceeds maxlinelen %i)" % (len(line), self.maxlinelen))
            if line.endswith('\r\n'):
                if self.debug:
                    print("doSimpleCommand recvline: {}".format(repr(line)))
                # TODO: END: common code for get a line from the IMAP connection
                # Strip the line ending off
                line = line[:-2]
                # We got a whole line. Process it.
                if line.startswith("+"):
                    raise imap4Exception("Continuation required")
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
                segment = line.rfind('{')
                if segment != -1 and line.endswith('}') and line[segment + 1 : -1].isdigit():
                    count = int(line[segment + 1 : -1],10)
                    # Restore CRLF, this isn't actually the end of this data
                    # 'line'
                    line += "\r\n"
                    # NOTE: The count is the number of bytes to read after the
                    # initial CRLF. We put the CRLF back into the stream so
                    # that higher parsers keep the correct format.

                    # Read the rest of the literal
                    while count:
                        partial = self.socket.recv(count)
                        if partial == "":
                            # TODO: Might have been SSL layer stuff. Figure
                            # out how to check if the socket is actually dead.
                            raise imap4Exception("Lost socket?")
                        line += partial
                        count -= len(partial)
                    # Now that we are done with the literal, resume normal
                    # processing
                    continue
                if line.startswith(tagstr):
                    # This is 'response-tagged' in the IMAP ABNF
                    # This line completes an in-progress transaction
                    a = re.match(re_tagged, line)
                    tag, status, code, string = a.groups()
                    if code:
                        self.processCodes(status, code, string)
                    if self.debug:
                        print("tag",line)
                    if tag != tagstr:
                        # Log a warning
                        print("Unexpected tag %s received; was waiting for %s" % (tag, tagstr))
                        # Keep waiting for *our* tag
                        continue
                    if status.upper() != 'OK':
                        # TODO: Use our own exception class
                        # Ideally, we'd have one kind of exception for NO and
                        # another for BAD, and one for whatever else we might
                        # get back.
                        e = imap4Exception("IMAP error: %s" % string)
                        e.imap_status = status
                        e.imap_code = code
                        e.imap_string = string
                        raise e
                    if self.idling:
                        # TODO: maybe only return to idling after a delay?
                        # Could use a timer?
                        # If we go back to idling immediately, there's a lot
                        # of back-and-forth when the program using this lib
                        # does back-to-back simple commands, wasting bandwidth
                        # and time.
                        self.doIdle()
                    return status, code, string
                else:
                    # Process this line, but keep going
                    self.processUntagged(line)
                    line = ""
    def processUntagged(self, line):
        # Note: Untagged can be more than just OK,NO,BAD, etc.
        #       Can also be results, e.g. * FLAGS (\Answered \Seen)
        #       or value results, e.g. * 6347 EXISTS
        #                              * 0 RECENT
        #print("notag",line)
        # Start by looking for response-cond-state
        r = re.match(re_untagged, line)
        if r is not None:
            status, code, string = r.groups()
            # TODO, look for content to cache and/or callback
            if self.debug:
                print("response",status,code,string)
            if code:
                self.processCodes(status, code, string)
        else:
            # Other format. Look for content to cache and/or callback
            # We'll start with message-data and mailbox-data that
            # have a numerical ID at the beginning. Note that
            # message-data and mailbox-data don't have codes
            r = re.match(re_numdat, line)
            if r is not None:
                num, typ, data = r.groups()
                # message-data
                if typ.upper() == "FETCH":
                    if self.debug:
                        print("FETCH for %s" % num, data)
                    if self.cb_fetch:
                        self.cb_fetch(num, data)
                    if "fetch" in self.cbs:
                        self.cbs["fetch"](num, data)
                elif typ.upper() == "EXPUNGE":
                    if self.debug:
                        print("EXPUNGE for %s" % num)
                    if "expunge" in self.cbs:
                        self.cbs['expunge'](num, data)
                # numerical mailbox-data
                elif typ.upper() == "EXISTS":
                    if self.debug:
                        print("Exists: %s" % num)
                    self.exists = int(num, 10)
                    if "exists" in self.cbs:
                        self.cbs["exists"](int(num, 10))
                elif typ.upper() == "RECENT":
                    if self.debug:
                        print("Recent: %s" % num)
                    self.recent = int(num, 10)
                else:
                    print("uknown numerical '%s'" % typ.upper(), line)
            else:
                # Finally, we'll try mailbox-data, message-data,
                # or capability-data without the leading number.
                # Note that in the base spec, all of the
                # message-data have a leading nz-number
                r = re.match(re_untagdat, line)
                if r is not None:
                    typ, data = r.groups()
                    # capability-data
                    if typ.upper() == "CAPABILITY":
                        self.caps = data.split()
                    # mailbox-data
                    elif typ.upper() == "FLAGS":
                        self.flags = data.split() #TODO should this be parsed for literals or quoted strings?
                    elif typ.upper() == "LIST":
                        if "list" in self.cbs:
                            self.cbs["list"](line)
                    elif typ.upper() == "LSUB":
                        if "lsub" in self.cbs:
                            self.cbs["lsub"](line)
                    elif typ.upper() == "SEARCH":
                        if self.cb_search:
                            self.cb_search(typ, data)
                    elif typ.upper() == "STATUS":
                        # TODO: callback
                        pass
                    # message-data
                    # (none)
                    else:
                        print("Unknown non-numerical '%s'" % typ.upper(), line)
                else:
                    print("nomatch",line)
    def connect(self, host, **kwargs):
        # TODO: Try base port with STARTTLS, then SSL port, then base port
        # without TLS? 
        # TODO: Use the eventloop; support interruption via other event (e.g.
        # command pipe)
        port = 143
        useSsl = False
        if 'port' in kwargs:
            val = kwargs['port']
            # Catch where someone gave us None or 0 to mean 'use default'
            # instead of not passing it in the first place
            if val:
                port = val
        # TODO: This violates the scheme provided by the user. If they wanted
        # imap at port 993, this breaks, and if they want imaps at a port
        # other than 993, this breaks. Connection mode should be a kwarg, and
        # default to starttls if not given.
        if port == 993:
            useSsl = True
        targets = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, 0)
        # TODO: should we iterate through targets in order, randomly, or
        # randomly by address family (that is, try IPv6 first, then IPv4, then
        # whatever is left)?
        for i in targets:
            print "Trying", i[4][0],i[3] # address, canonical name (if available)
            s = socket.socket(*i[:3])
            if useSsl:
                oldSock = s
                s = ssl.SSLSocket(s, ca_certs=self.ca_certs, cert_reqs=ssl.CERT_REQUIRED if self.ca_certs else ssl.CERT_NONE)
            else:
                oldSock = None
            try:
                s.connect(i[4])
                self._negotiate(s, host)
                break
            except socket.error as ev:
                print "  ", ev.strerror
                continue
            except imap4Exception as ev:
                print "  error with imap negotiation"
                continue
        else:
            # TODO: Provide some more info. Ideally, we'd have some
            # differentiation between, say, connection refused vs timed out vs
            # no route, etc.
            # May be difficult due to multiple connection attempts.
            raise imap4NoConnect("unable to connect")
        return

    def _negotiate(self, s, host):
        try:
            r = s.recv(1024)
            a = re.match(re_untagged, r)
            if a is None:
                # TODO: Log the response?
                raise imap4Exception("Bad response from server")
            status, code, string = a.groups()
            if code:
                self.processCodes(status, code, string)
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
                raise imap4Exception("Unexpected response from server")
            self.socket = s
            self.hostname = host
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
    def isTls(self):
        if isinstance(self.socket, ssl.SSLSocket):
            return True
        return False
    def starttls(self):
        if self.state != STATE_UNAUTH:
            raise imap4Exception("Bad client state for command")
        if self.isTls():
            raise imap4Exception("Already in TLS mode")
        # Run STARTTLS command, wait for go ahead
        res, code, string = self.doSimpleCommand("STARTTLS")
        if res != 'OK':
            raise imap4Exception("No TLS on server")
        # TODO: Support client certificate
        self.origsocket = self.socket
        # So, the best practice here is to use an SSLContext to wrap the
        # connection, and then to use the default context which does nice
        # things like enabling host name checking, disabling questional
        # features (like compression and low-strength hashes).
        # Unfortunately, the version of python in Ubuntu 14.04 doesn't include
        # these wonderful things, leaving people to try to do their own
        # implementation of the same or not support it at all. Since writing
        # one's own security code is risky, we'll warn if we cannot use the
        # new stuff, but use the new stuff if it is available.
        if not hasattr(ssl, "create_default_context"):
            if not hasattr(ssl, "SSLContext"):
                # TODO: We should probably fail here unless the user really wants us
                # to go on.
                print("WARNING: old python SSL detected. Host checking is *NOT* occuring, and some best practices aren't followed!")
                self.socket = ssl.wrap_socket(self.socket, ca_certs=self.ca_certs, cert_reqs=ssl.CERT_REQUIRED if self.ca_certs else ssl.CERT_NONE)
            else:
                raise imap4Exception("TBD: SSLContext-able without default context")
        else:
            # Based on information from https://mail.python.org/pipermail/python-dev/2013-November/130649.html
            if (self.ca_certs):
                # TODO: This appears to *add* the given certs file to the
                # default set instead of replacing it. What if the user wants
                # *only* the given ca? How do we have the user convey that to
                # us? How do we convey that to the ssl library?
                context = ssl.create_default_context(cafile=self.ca_certs)
            else:
                context = ssl.create_default_context()
            self.socket = context.wrap_socket(self.socket, server_hostname=self.hostname)
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
            raise imap4Exception("Failed to select box")
    def getheaders(self, message):
        res, code, string = self.doSimpleCommand("fetch %s (BODY.PEEK[HEADER])" % message)
        if res != 'OK':
            raise imap4Exception("Failed to fetch headers")
    def fetch(self, message, what):
        """Generic fetcher. Given an IMAP spec of messages (not UIDs), fetch the 'what' from them.

        message: IMAP message list. E.g. '4:10' will get messages 4, 5, 6, 7, 8, 9, and 10
        what: Set of what to fetch. E.g. '(ENVELOPE)' will get info about the sender, date, and subject
            The 'what' must be wrapped in parenthesis and be a space separated
            list of fetchable items in IMAP format. This is really a simple
            passthrough, designed to be somewhat compatible with imaplib (I
            know, that was a non-goal)
        """
        oldcb = self.cb_fetch
        fetchlist = []
        def fetch_cb(message, data):
            fetchlist.append((message, data))
        self.cb_fetch = fetch_cb
        try:
            res, code, string = self.doSimpleCommand("fetch %s %s" % (message, what))
        except Exception:
            #TODO: log, not print
            print("Failed to fetch %s %s" % (what, message))
            raise
        finally:
            self.cb_fetch = oldcb
        if res != 'OK':
            raise imap4Exception("Failed to fetch %s: %s %s" % (message, res, string))
        return fetchlist
    def uidfetch(self, message, what):
        """Generic fetcher. Given an IMAP spec of UIDs, fetch the 'what' from them.

        message: IMAP message list. E.g. '4:10' will get messages 4, 5, 6, 7,
        8, 9, and 10, if they exist. UIDs are not necessarily contiguous.
        Non-existent UIDs will simply not be returned.
        what: Set of what to fetch. E.g. '(ENVELOPE)' will get info about the sender, date, and subject
            The 'what' must be wrapped in parenthesis and be a space separated
            list of fetchable items in IMAP format. This is really a simple
            passthrough.
        """
        oldcb = self.cb_fetch
        fetchlist = []
        # TODO: I think unsolicited fetch messages are allowed to come in
        # during UID FETCH. We should verify. If so, this needs to somehow
        # check that the message was actually part of our fetch before adding
        # it to the list, and probably also forward the messages to the
        # original CB.
        def fetch_cb(message, data):
            fetchlist.append((message, data))
        self.cb_fetch = fetch_cb
        res, code, string = self.doSimpleCommand("uid fetch %s %s" % (message, what))
        self.cb_fetch = oldcb
        if res != 'OK':
            raise imap4Exception("Failed to uid fetch: %s %s" % (res, string))
        return fetchlist
    def getCapabilities(self):
        res, code, string = self.doSimpleCommand("CAPABILITY")
        if res != "OK":
            raise imap4Exception("Failed to get capability: %s %s" %(res, string))
        return self.caps
    def search(self, charset, query):
        searchres = []
        def cb(typ, data):
            searchres.extend(data.split())
        oldsearch = self.cb_search
        self.cb_search = cb
        res, code, string = self.doSimpleCommand("SEARCH CHARSET %s %s" % (charset, query))
        self.cb_search = oldsearch
        if res != "OK":
            raise imap4Exception("Failed to do search: %s %s" % (res, string))
        return searchres

