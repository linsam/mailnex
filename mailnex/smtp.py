# SMTP library, because the one that came with python-2 was insufficient.
# In particular, the python-2 version didn't appear to provide any facilities
# for validating a server when using TLS (it accepted any certificate for
# anybody applied to any server, including self-signed certs).

import socket
import ssl
import codecs

def parseExtension(extensions, data):
    """Parses a single line of EHLO response data"""
    if b' ' in data:
        name, args = data.split(b" ", 1)
        # The extension data ought to be ASCII; so default decoding UTF-8 should be fine.
        name = name.decode()
        args = args.decode()
    else:
        name = data.decode()
        args = None
    extensions[name] = args

def parseExtensions(extensions, data):
    """Parses all lines of EHLO response data"""
    for line in data.split(b'\r\n'):
        if len(line) < 3:
            print("Error:", line)
            return False
        if len(line) < 4:
            # Something is wrong. Should be 3 digit status followed by hyphen
            # or space. We can conclude the server erroneously sent us a line
            # with just the 3 byte code.
            if line[0:1] != b'2':
                print("Server unhappy:", line)
                return False
            break
        elif line[3:4] == b'-':
            # Multiline data
            parseExtension(extensions, line[4:])
            continue
        elif line[3:4] == b' ':
            # Last line
            parseExtension(extensions, line[4:])
            if line[0:1] != b'2':
                print("Server unhappy:", line)
                return False
            break
        else:
            print("Invalid response", line)
            return False

# NONE: Do not use TLS
SEC_NONE = 0
# STARTTLS: Connect without TLS, then enhance to TLS using STARTTLS command.
# Require TLS to work
SEC_STARTTLS = 1
# Initiate the socket with TLS, then proceed to normal SMTP
SEC_SSL = 2

DISCONNECTED = 0
CONNECTED = 1
AUTENTICATED = 2

class smtpClient(object):
    def __init__(self):
        object.__init__(self)
        self.state = DISCONNECTED
        self.cacerts = None
    def connect(self, host, port=587, secure=SEC_STARTTLS):
        targets = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, 0)
        # TODO: should we iterate through targets in order, randomly, or
        # randomly by address family (that is, try IPv6 first, then IPv4, then
        # whatever is left)?
        for i in targets:
            print("Trying", i[4][0],i[3]) # address, canonical name (if available)
            s = socket.socket(*i[:3])
            if secure == SEC_SSL:
                oldSock = s
                s = ssl.SSLSocket(s, ca_certs=self.ca_certs, cert_reqs=ssl.CERT_REQUIRED if self.ca_certs else ssl.CERT_NONE)
            else:
                oldSock = None
            try:
                s.connect(i[4])
                self._negotiate(s, host, secure)
                break
            except socket.error as ev:
                print("Failed, socket error: ", ev.strerror, ev)
                continue
        else:
            # TODO: Provide some more info. Ideally, we'd have some
            # differentiation between, say, connection refused vs timed out vs
            # no route, etc.
            # May be difficult due to multiple connection attempts.
            raise Exception("unable to connect")
    def _negotiate(self, s, host, security):
        r = s.recv(1024)
        if not r.startswith(b"2"):
            raise Exception("Server unhappy: {}".format(r))
        # TODO: Check if hostname isn't fqdn; warn user? fail?
        #myhostname=socket.gethostname()
        myhostname="ehlo.thunderbird.net" # K9 mail uses this
        s.send(b"EHLO %s\r\n"%(myhostname.encode()))
        r = s.recv(1024)
        extensions = {}
        r = parseExtensions(extensions, r)
        if r == False:
            raise Exception("Failed to parse extensions")
        if security == SEC_STARTTLS :
            if 'STARTTLS' not in extensions:
                # TODO: Try sending STARTTLS anyway; could be a downgrade
                # attack attempt and we might get through, or it could be a
                # misconfigured proxy, and the command might also get through
                raise Exception("Server reports no TLS capability")
            s.send(b"STARTTLS\r\n")
            r = s.recv(1024)
            if r[0:1] != b"2":
                raise Exception("Failed to start TLS: {}".format(r))
            oldSock = s
            # TODO: Handle older python without the default context, like our
            # imap lib does (e.g. as on Ubuntu 14.04)
            if self.cacerts:
                import os
                if not os.access(self.cacerts, os.R_OK):
                    print("WARNING: cannot access", self.cacerts)
                context = ssl.create_default_context(cafile=self.cacerts)
            else:
                context = ssl.create_default_context()
            s = context.wrap_socket(s, server_hostname=host)
            # Now that we are secure, re-get extensions
            s.send(b"EHLO %s\r\n"%(myhostname.encode()))
            r = s.recv(1024)
            extensions = {}
            r = parseExtensions(extensions, r)
            if r == False:
                raise Exception("Failed to parse extensions after STARTTLS")
        self.sock = s
        self.extensions = extensions
        self.state = CONNECTED
        # Notes:
        # Root CA for the connection can be found in self.sock.context.get_ca_certs()
        # Peer certificate can be found in self.sock.getpeercert()
        # Encryption algo can be found in self.sock.cipher()
        # I don't know how to get from the peer to the CA (a la Firefox or
        # Chrome's cert chain view)
    def login(self, username, password):
        s = self.sock
        if 'AUTH' in self.extensions and 'PLAIN' in self.extensions['AUTH']:
            authstr = b"AUTH PLAIN %s\r\n"%(codecs.encode("{}\x00{}\x00{}".format(username, username, password).encode(), 'base64')).replace(b'\n',b'')
            s.send(authstr)
            r = s.recv(1024)
            if r[0:1] != b'2':
                print("failed", r)
                return False
            self.state = AUTENTICATED
        else:
            raise Exception("No auth")
    def sendmail(self, from_, to, message):
        # TODO: some basic validation of from and to
        s = self.sock
        s.send(b"MAIL FROM:<%s>\r\n"%(from_))
        r = s.recv(1024)
        if r[0:1] != b'2': raise Exception("bad from line: {}".format(r))
        for i in to:
            s.send(b"RCPT TO:<%s>\r\n"%(i))
            r = s.recv(1024)
            # TODO: Collect failed recipients into a list to give caller.
            # If we fail on the first, the user can get frustrated correcting
            # 1 mistake at a time instead of correcting multiple at once
            if r[0:1] != b'2': raise Exception("bad to: {}, {}".format(i, r))
        s.send(b"DATA\r\n")
        r = s.recv(1024)
        if not r.startswith(b'354'): raise Exception("Unexpected {}".format(r))
        # TODO: normalize line endings?
        # TODO: correct line length?
        for line in message.split(b'\n'):
            if line.endswith(b'\r'):
                line = line[:-1]
            if line.startswith(b"."):
                line = b'.' + line
            s.send(line + b'\r\n')
        s.send(b'.\r\n') # terminate message
        r = s.recv(1024)
        if r[0:1] != b'2': raise Exception("Failed to send data: {}".format(r))
    def quit(self):
        s = self.sock
        s.send(b"QUIT\r\n")
        r = s.recv(1024)
        if r[0:1] != b'2': raise Exception("Failed to quit: {}".format(r))
        self.sock = None
        self.state = DISCONNECTED
        return
