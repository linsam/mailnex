#!/usr/bin/env python2

# Ubuntu 14.04 doesn't have xapian for python3. Quite possibly other modules
# aren't available as well.
#
# I might have to demarcate Ubuntu 16.04 and CentOS 7 as minimum OSes. Don't
# know *yet* if packages are supported there either.
#
# At any rate, might as well make this module as close to python 3 as possible
# for now, so that once we can change over, it'll be easier.
from __future__ import print_function
from __future__ import unicode_literals

#  Search indexing
#  ---------------
# Terms:
#  * Document - unit of search result. Documents have a 32bit id, data, terms,
#  and values.
#       * Data - binary blob, opaque to the engine. You can put the whole body
#       of a document here, or abstracts, or references to the real thing. For
#       email, this would probably be a message-id and/or mailfolder+UID
#       * terms - queryable data. Links to docuements, sortof. Terms can also
#       be field associated (e.g. sender vs body vs date)
#           * xapian supports mapping a field to multiple field prefixes
#           * We could use that such that To and CC map to recipient, and To,
#           CC, From, etc. map to person, etc.
#           * Check the Xapian conventions ('A' for author, etc) for a default
#           set of prefixes before running with our own.
#       * values - advanced topic for advanced searches 
#           * can be sorting keys
#           * can be weight (document importance)
#           * can be numeric values for range sort (dates come to mind)
#           * Values are stored into a slot between 0x0 and 0xfffffffe
#           * values contain opaque binary strings
#   * MSet - Match Set. A page (offset and count) of matches to a query.
#
# IMAP
# ----
#
# IMAP4r1: rfc2060, updated by 3501
# IDLE command: rfc2177
# Namespace: rfc2342
# CONDSTORE: rfc 4551 (multiple connection synchronization and date/sequence
#     based metadata updates. E.G. you can query which messages have changed
#     flags since last query)
# BINARY: rfc 3516 (fetch BINARY vs fetch BODY, saves on base64 encoding
#     transfers, for example.)
# COMPRESS: rfc 4978
#
#

# standard stuff
import os
import sys
import re
import threading
from . decorators import *
# xapian search engine
try:
    import xapian
    haveXapian = True
except ImportError:
    haveXapian = False
# various email helpers
from . import imap4
import email
import email.utils
import email.mime.text
import quopri
import mailbox
# password prompter
import getpass
# Password manager
import keyring
# Configuration and other directory management
import xdg.BaseDirectory
# shell helper
from . import cmdprompt
# Date handler
import dateutil.parser
# Color and other terminal stuffs
import blessings
# Ability to launch external viewers
import mailcap
# Other
import tempfile
import pyuv
import time
from . import settings
import subprocess
try:
    import gpgme
    haveGpgme = True
except ImportError:
    haveGpgme = False
import magic
from prompt_toolkit.completion import Completer, Completion

confFile = xdg.BaseDirectory.load_first_config("linsam.homelinux.com","mailnex","mailnex.conf")

# Enums
ATTR_NEW = 0
ATTR_UNREAD = 1
ATTR_NEWREAD = 2
ATTR_OLD = 3
ATTR_SAVED = 4
ATTR_PRESERVED = 5
ATTR_MBOXED = 6
ATTR_FLAGGED = 7
ATTR_ANSWERED = 8
ATTR_DRAFT = 9
ATTR_KILLED = 10
ATTR_THREAD_START = 11
ATTR_THREAD = 12
ATTR_JUNK = 13

class Context(object):
    """Holding place for runtime data.

    This is sort-of a drop place for things that could possibly be globals.
    Opens the possibility that we might support multiple sessions in a single
    run at some point."""
    def __init__(self):
        object.__init__(self)
        # The IMAP (or whatever) connection instance. Should be the
        # abstraction for the message store
        self.connection = None
        # list of messages from the last command
        self.lastList = None
        # list of messages making up the current virtual folder, if any
        self.virtfolder = None
        # Currently selected message. Should (must?) be in lastList
        self.currentMessage = None
        # lastMessage is the highest numbered message in the folder
        self.lastMessage = None
        # nextMessage is the message to be opened when no command is given. It
        # is usually the message following the current message, unless the
        # current message wasn't directly selected for viewing (e.g. freshly
        # opening a mailbox or using the 'h' command results in currentMessage
        # and nextMessage being the same).
        self.nextMessage = None
        # The message which was most recently the currentMessage. Is is used
        # by the previous message selector ';'
        self.prevMessage = None
        # The previously issued command that is repeatable. When None, the
        # default (print next) command is used when an empty prompt is
        # submitted. Used, for example, to retrieve more searh results by
        # pressing enter multiple times after a search command
        self.lastcommand = None
        # Instance of settings that tweak the behavior of mailnex, or store
        # user strings
        self.settings = None
        # Current path for DB index(es). Currently the path of the active
        # database.
        self.dbpath = None
        # Instance of blessings.Terminal or equivalent terminal formatting
        # package.
        self.t = None

        # Some parts of the program might put other stuff in here. For
        # example, the exception trace wrapper.

class nodate(object):
    """Stand-in for date objects that aren't dates"""
    def strftime(self, _):
        """Display conversion for non-dates.

        Shows question marks no matter what you ask for.
        """
        return "??"
    def astimezone(self, _):
        return self
    tzinfo="??"

def getResultPart(part, data):
    """Retrieve part's value from data.

    This is for flat arrays that are really key-value lists.
    e.g.
        [key1, val1, key2, val2, key3, val3...]

    Currently, this is a linear search, case insensitive."""
    part = part.lower()
    for i in range(0,len(data),2):
        if data[i].lower() == part:
            return data[i + 1]
    # Raising an exception because, after an IMAP requiest, not having the key
    # you asked for is an exceptional case, and there isn't a good return
    # value that also couldn't be in the array itself without doing something
    # weird like returning a class or something.
    raise Exception("Part %s not found" % part)

class MessageList(object):
    """Acts like a set, but automatically collapses ranges.

    A message list is a sorted list of non-overlaping ranges of message IDs.
    Message IDs can be added to the message list or removed from it. Removing
    will split or remove the range containing the ID. Adding will create, extend,
    or join a range to include the ID."""
    def __init__(self, iterable=None):
        object.__init__(self)
        # First pass, we'll do linear searches for our operations
        self.ranges = []
        if iterable:
            for i in iterable:
                self.add(i)
    def add(self, i):
        """Add a message ID to the message list"""
        # First, first pass, we won't collapse the ranges and we'll post-sort.
        # This is a terrible implementation
        # TODO: fix this to be more efficient and actually collapse ranges
        if (i, i) in self.ranges:
            return
        self.ranges.append((i,i))
        self.ranges.sort()
    def addRange(self, start, end):
        """Convenience function to add an inclusive range of messages in one go."""
        #TODO: We should be able to do similar to add but with ranges for faster operation.
        # First-pass, just loop on calling add.
        for i in range(start, end + 1):
            self.add(i)
    def remove(self, i):
        raise Exception("Not yet implemented")
    def imapListStr(self):
        """Return a string representation of the message list in IMAP format

        Eg, a range of 4 through 8 and 10 through 12 (4 through 12 excluding 9) would yield:

            4:8,10:12

        A range or 4 through 8 and 10 would yield:

            4:8,10

        A list with only 10 in it would yield:

            10
        """
        res = []
        for i in self.ranges:
            if i[0] == i[1]:
                res.append(str(i[0]))
            else:
                res.append(':'.join(map(str, i)))
        return ",".join(res)
    def iterate(self):
        """Returns an iterator that yeilds each message ID in turn"""
        for r in self.ranges:
            for i in range(r[0], r[1] + 1):
                yield i
        raise StopIteration()


class Envelope(object):
    # Envelope fields:
    #   0 - date
    #   1 - subject
    #   2 - from (list or NIL)
    #   3 - sender (list or NIL)
    #   4 - reply-to (list or NIL)
    #   5 - to (list or NIL)
    #   6 - cc (lis or NIL)
    #   7 - bcc (list or NIL)
    #   8 - in-reply-to
    #   9 - message-id
    #
    #   The elements of 2 through 7 consist of name,
    #   at-domain-list (aka source route; typically NIL), mailbox
    #   name, and host name.
    #   Unless it is a group name; see page 77 of RFC 3501 for
    #   details.
    def __init__(self, date, subject, from_, sender, replyTo, to, cc, bcc, inReplyTo, messageId):
        # Assign each of our arguments to attributes of the same name
        for i in "date subject from_ sender replyTo to cc bcc inReplyTo messageId".split():
            setattr(self, i, locals()[i])
    def print(self):
        for i in "date subject from_ sender replyTo to cc bcc inReplyTo messageId".split():
            print("%s: %s" % (i, getattr(self, i)))


class structureRoot(object):
    def __init__(self, tag, type_, subtype):
        object.__init__(self)
        self.tag = tag
        # Make types lower case for case insensitive comparisons elsewhere
        self.type_ = type_.lower()
        self.subtype = subtype.lower()
    def __repr__(self):
        return "<structure %s/%s>" % (self.type_, self.subtype)

class structureMultipart(structureRoot):
    def __init__(self, tag, subtype, parameters, disposition, language, location):
        """Create a multipart entry.

        @param tag name of this part (e.g. 1.5.3 for the third part of the fifth part of the first part)
        @param subtype variant of multipart (e.g. mixed, signed, alternative)
        (others as per IMAP spec)
        """
        structureRoot.__init__(self, tag, "multipart", subtype)
        # parameters
        self.disposition = disposition
        # language
        # location
        self.subs = []
        pass
    def addSub(self, sub):
        assert isinstance(sub, structureRoot), "%s isn't a structureRoot or similar" % type(sub)
        self.subs.append(sub)

class structureLeaf(structureRoot):
    def __init__(self, tag, type_, subtype, attrs, bid, description, encoding, size, *args):
        # Note, if type_=='text', args starts with encoded line count.
        # If type_=="message" and subtype=="rfc822", args starts with
        # envelope, body structure, and line count (see structureMessage
        # instead for this case)
        #
        # All *args end with md5, disposition, language, and location.
        structureRoot.__init__(self, tag, type_, subtype)
        if attrs:
            self.attrs = dictifyList(attrs)
        else:
            self.attrs = attrs
        self.bid = bid # Body ID
        self.description = description
        self.encoding = encoding
        self.size = size # octets of encoded message
        if type_.lower() == "text":
            self.lines, args = args[0], args[1:]
        self.md5, self.disposition, self.language, self.location = args

class structureMessage(structureRoot):
    def __init__(self, tag, type_, subtype, attrs, bid, description, encoding, size, envelope, subStruct, lines, md5, disposition, language, location):
        structureRoot.__init__(self, tag, type_, subtype)
        # attrs
        # bid
        # description
        self.encoding = encoding
        # size
        # envelope
        # substruct (auto-add? ignore?)
        # lines
        # md5
        self.disposition = disposition
        # language
        # location
        self.subs = []
    def addSub(self, sub):
        assert isinstance(sub, structureRoot), "%s isn't a structureRoot or similar" % type(sub)
        self.subs.append(sub)

def unpackStruct(data, options, depth=1, tag="", predesc=""):
    """Recursively unpack the structure of a message (as given by IMAP's BODYSTRUCTURE message-data)

    @param data hierarchy of BODYSTRUCTURE elements
    @depth starting depth (may be used for indenting output, or for debugging)
    @tag current identifier of parent. For the first call, this should be the message ID. It will be dot separated for sub parts.
    @predesc prefix description. Mostly used internally for when we hit a message/rfc822.
    @return array of parts, which may contain array of parts.
    """
    # This is slightly tricky because of the way they ordered it. I haven't
    # read every revision of IPMI, so I'm guessing this is the result of
    # attempting backwards compatibility as the spec grew.
    #
    # At any given layer, the first element is either a string indicating the
    # MIME type, or it is a parenthesized list describing a sub part.
    # If it is a parenthesized list for a subpart, then there are 1 or more of
    # these (a multipart apparently cannot be empty?), followed by a string of
    # the multipart subtype, then the multipart extension data elements: body
    # parameter list, disposition, language, and location (a URI)
    #
    # If it is a string, then the fields are type, subtype, mime attr list,
    # body id, disposition, encoding and size, followed by type-specific
    # extensions followed by regular extensions.
    #
    # text/* has encoded line count (not decoded; beware of this, it makes it
    # less than useful for showing a line count of what the user will see,
    # though it might be a good way to estimate it.
    #
    # message/rfc822 has envelope structure, body structure, line count
    #
    # Regular extensions are md5, disposition, language, and location (URI).
    #
    # Note that the envelope structure and body structure for message/rfc822
    # is the same format as would be retrieved with a fetch ENVELOPE or fetch
    # BODYSTRUCTURE. In particular, that means that for message/rfc822, we
    # don't recurse on the non-string initial list, but on the body structure
    # element at index 8
    extra = ""
    this = None
    if isinstance(data[0], list):
        # We are multipart
        for i in range(len(data)):
            if not isinstance(data[i], list):
                break
        info = data[i:]
        this = structureMultipart(tag, *info)
        if data[i + 2] and data[i + 2][0] and data[i + 2][0] == "attachment":
            extra = " (attachment)"
        elif data[i + 2] and data[i + 2][0] and data[i + 2][0] == "inline":
            extra = " (inline)"
        if options.debug.struct:
            print("%s   %s%s/%s%s" % (tag, predesc, "multipart", data[i], extra))
        j = 1
        for dat in data[:i]:
            this.addSub(unpackStruct(dat, options, depth + 1, tag + '.' + str(j)))
            j += 1
    else:
        # If we are message/rfc822, then we have further subdivision!
        if data[0].lower() == "message" and data[1].lower() == "rfc822":
            this = structureMessage(tag, *data)
            if data[11] and data[11][0] and data[11][0] == "attachment":
                extra = " (attachment)"
            elif data[11] and data[11][0] and data[11][0] == "inline":
                extra = " (inline)"
            this.addSub(unpackStruct(data[8], options, depth + 1, tag, "message/rfc822%s, which is " % (extra)))
        else:
            this = structureLeaf(tag, *data)
            if data[0].lower() == "text":
                if data[9] and data[9][0] and data[9][0] == "attachment":
                    extra = " (attachment)"
                elif data[9] and data[9][0] and data[9][0] == "inline":
                    extra = " (inline)"
            else:
                if data[8] and data[8][0] and data[8][0] == "attachment":
                    extra = " (attachment)"
                elif data[8] and data[8][0] and data[8][0] == "inline":
                    extra = " (inline)"
            if options.debug.struct:
                print("%s   %s%s/%s%s" % (tag, predesc, data[0], data[1], extra))
    return this

def flattenStruct(struct):
    """Return a dictionry whose keys are the sub-part numbers and values are structure parts.

    E.g. a key might be ".1.2"
    """
    parts = {}
    def pickparts(struct, allParts=True):
        # TODO: unify the code with do_structure? Make its own function?
        # We probably want that anyway since other parts would utilize
        # this, such as message reply/forwarding
        extra = ""
        skip = False
        if hasattr(struct, "disposition") and struct.disposition not in [None, "NIL"]:
            extra += " (%s)" % struct.disposition[0]
            if not allParts and struct.disposition[0].lower() == "attachment":
                skip = True
            dispattrs = dictifyList(struct.disposition[1])
            if 'filename' in dispattrs:
                extra += " (name: %s)" % dispattrs['filename']
        # TODO XXX: Preprocess control chars out of all strings before
        # display to terminal!
        parts[struct.tag] = struct
        #structureStrings.append("%s   %s/%s%s" % (struct.tag, struct.type_, struct.subtype, extra))
        innerTag = ".".join(struct.tag.split('.')[1:])
        # First pass, we'll just grab all text/plain parts. Later we'll
        # want to check disposition, and later we'll want to deal with
        # multipart/alternative better (and multipart/related)
        if not allParts and struct.type_ == "text" and struct.subtype == "plain":
            # TODO: write the following a bit more efficiently. Like,
            # split only once, use second part of return only, perhaps?
            if not skip:
                #fetchParts.append((innerTag, struct))
                pass
        if allParts and not isinstance(struct, structureMessage) and not hasattr(struct, "subs"):
            # Probably useful to display, not a multipart itself, or a
            # message (which *should* have subparts itself?)
            # TODO: Properly handle attached messages
            #fetchParts.append((innerTag, struct))
            pass
        if isinstance(struct, structureMessage):
            extra = ""
            # Switch to the inner for further processing
            struct = struct.subs[0]
            if hasattr(struct, "disposition") and struct.disposition not in [None, "NIL"]:
                extra += " (%s)" % struct.disposition[0]

            #structureStrings.append("%*s   `-> %s/%s%s" % (len(struct.tag), "", struct.type_, struct.subtype, extra))
            #fetchParts.append(("%s.HEADER" % innerTag, struct))
        if hasattr(struct, "subs"):
            for i in struct.subs:
                pickparts(i, allParts)
    pickparts(struct)
    return parts

def dictifyList(lst):
    # convert to list of key,val pairs, then to dictionary
    # See http://stackoverflow.com/a/1625023/4504704 (answer to
    # http://stackoverflow.com/questions/1624883/alternative-way-to-split-a-list-into-groups-of-n)
    # TODO: Probably possible that there could be duplicate keys. Probably
    # need a custom dictionary to handle that. Might also be handy to store
    # the original case of the key and value.
    return dict(zip(*(iter(map(lambda x: x.lower(),lst)),)*2))

def processImapData(text, options):
    """Process a set of IMAP data items

    The items are roughly space separated text that can be quoted and can contain
    lists of other items by wrapping in parenthses

    According to the RFC:
        Data can be an atom, number, string, parenthesized list, or NIL.
        An atom consists of one or more non-special characters.
        A number consists of digits
        A string is either literal (has a length in braces followed, followed
        by CRLF by data of that length) or quoted (surrounded by double
        quotes)
        A parenthesized list is a nesting structure for all of the data
        formats.
        NIL is like C's NULL or Python's None. Indicates absense of a
        parameter, distinct from an empty string or empty list.

        The special characters that aren't allowed in atoms are parenthesis,
        open curly brace, space, control characters (0x00-0x1f and 0x7f),
        list-wildcards (percent and asterisk), and quoted-specials (double
        quote and backslash).

        In a quote, a backslash provides for denoting a literal. E.g. "\"" is
        a string whose value is a double quote.

    This implementation is currently incomplete. It doesn't handle the \ escape
    in quoted strings and accepts quotes anywhere.
    """


    curlist=[]
    lset=[]
    lset.append(curlist)
    curtext=[]
    inquote = False
    inspace = True
    inbrace = False
    wasquoted = False
    literalRemain = 0
    literalSizeString = ""
    pos = -1
    last = len(text) - 1
    if options.debug.parse:
        print(" length:", last)
    while pos < last:
        pos += 1
        c = text[pos]
        if options.debug.parse:
            print(" Processing {} @ {}".format(repr(c), pos))
        if c == ' ' or c == '\t':
            if inquote:
                if options.debug.parse:
                    print(" keep space, we are quoted")
                curtext.append(c)
                continue
            if not inspace:
                if options.debug.parse:
                    print(" End of token. Append completed word to list:", curtext)
                inspace = True
                thisStr = b"".join(curtext)
                if not wasquoted and thisStr.lower() == b'nil':
                    curlist.append(None)
                else:
                    curlist.append(thisStr)
                wasquoted = False
                curtext=[]
                continue
            continue
        if inbrace:
            if c != '}':
                literalSizeString += c
                continue
            # Got close curly brace; process the literal
            if options.debug.parse:
                print("Literal size find:",literalSizeString)
            inbrace = False
            if literalSizeString.isdigit():
                literalRemain = int(literalSizeString)
                if options.debug.parse:
                    print("Start literal. %i remain" % literalRemain)
                    print("skipping", repr(text[pos:pos+3]))
                pos += 2
                curtext.append(text[pos+1:pos+literalRemain+1])
                pos += literalRemain
                if options.debug.parse:
                    print("Finished literal remain:", curtext)
                continue
            raise Exception("Invalid literal size %s" % repr(literalSizeString))
        if inspace and c == '{':
            inspace = False
            inbrace = True
            literalSizeString = ""
            continue
        inspace = False
        if c == '"':
            if inquote:
                # TODO: Does ending a quote terminate an atom?
                if options.debug.parse:
                    print(" Leaving quote")
                inquote = False
                wasquoted = True
            else:
                # TODO: Are we allowed to start a quote mid-atom?
                if options.debug.parse:
                    print(" Entering quote")
                inquote = True
            continue
        if c == '(':
            if inquote:
                if options.debug.parse:
                    print(" keep paren, we are quoted")
                curtext.append(c)
                continue
            if len(curtext):
                raise Exception("Need space before open paren?")
            if options.debug.parse:
                print(" start new list")
            curlist=[]
            lset.append(curlist)
            inspace = True
            continue
        if c == ')':
            if inquote:
                if options.debug.parse:
                    print(" keep paren, we are quoted")
                curtext.append(c)
                continue
            if len(curtext):
                if options.debug.parse:
                    print(" finish atom before finishing list", curtext)
                thisStr = b"".join(curtext)
                if not wasquoted and thisStr.lower() == b'nil':
                    curlist.append(None)
                else:
                    curlist.append(thisStr)
                wasquoted = False
                curtext=[]
            t = curlist
            lset.pop()
            if len(lset) < 1:
                raise Exception("Malformed input. Unbalanced parenthesis: too many close parenthesis")
            curlist = lset[-1]
            if options.debug.parse:
                print(" finish list", t)
            curlist.append(t)
            inspace = True
            continue
        if options.debug.parse:
            print(" normal character")
        curtext.append(c)
    if inquote:
        raise Exception("Malformed input. Reached end without a closing quote")
    if len(curtext):
        print("EOF, flush leftover text", curtext)
        thisStr = b"".join(curtext)
        if not wasquoted and thisStr.lower() == b'nil':
            curlist.append(None)
        else:
            curlist.append(thisStr)
    if len(lset) > 1:
        raise Exception("Malformed input. Unbalanced parentheses: Not enough close parenthesis")
    if options.debug.parse:
        print("lset", lset)
        print("cur", curlist)
        print("leftover", curtext)
    return curlist

def processHeaders(text):
    # TODO: Handle \n too?
    lines = text.split('\r\n')
    name = None
    value = ""
    headers = dict()
    for line in lines:
        if line.startswith(" ") or line.startswith("\t"):
            # continuation. Append to previous
            value += "\r\n" + line
            continue
        if name:
            if name not in headers:
                headers[name] = list()
            headers[name].append(value)
        if not ': ' in line:
            if not ':' in line:
                if line == "":
                    # end of headers
                    return headers
                print("I don't like line", repr(line))
            else:
                # poorly formed header, but I've seen it
                name, value = line.split(':', 1)
                name = name.lower()
        else:
            name, value = line.split(': ', 1)
            name = name.lower()
    return headers

def encodeEmail(fullmail):
    """Encode a full email address.

    This is needed because the email.headers.Header format encodes full
    strings instead of address parts. The result is both the name, box and
    host get wrapped up in a single string instead of separately as mandated
    by the spec. The result of this is some MTA (for example, postfix in
    default configuration) automatically adds an @host to addresses that lack
    the @ sign, and it doesn't do RFC2046 decoding to see that (and by that
    spec it shouldn't have to)

    So, this function encodes the user name part separate and passes the
    actual address (box and host) verbatim."""
    parts = email.utils.parseaddr(fullmail)
    p2 = (str(email.header.Header(parts[0],'utf-8')), parts[1])
    return email.utils.formataddr(p2)

class Cmd(cmdprompt.CmdPrompt):
    def help_hidden_commands(self):
        print("The following are hidden commands:")
        print()
        print("      -> print next message (usually 'print +' except the first time)")
        print("  h   -> headers")
        print("  p   -> print")
        print("  q   -> quit")
        print("  x   -> exit")
    def help_optional_packages(self):
        # TODO: format to the user's terminal
        print("Some commands require optional packages to be installed to enable")
        print("functionality. For example, indexing and searching the index require")
        print("that Xapian be installed with python bindings. Similarly,")
        print("cryptographically signing email requires python bindings for gpgme.")
        print()
        print("These are often unavailable via pip install, and must therefore either")
        print("be installed by hand or come from your system's package manager. As")
        print("such, if running mailnex from a virtual-env, the virtual-env needs")
        print("to be set to have access to the system packages (using the")
        print("'--system-site-packages' flag of the virtualenv tool). See the file")
        print("'INSTALL' that came with this program for more details.")
    def parseMessageList(self, args):
        """Parse a message list specification string into a list of matching message IDs.

        According to the mailx manpage (under "Specifying messages") many
        commands can take a list of message numbers and operate on those
        multiple messages.

        Values can be a space separated list of ranges, or special names. The
        ranges are inclusive. However, if using special names, you cannot give
        multiple.

        E.g.
            5                  -> 5
            5 10               -> 5,10
            5-10               -> 5,6,7,8,9,10
            1-3 5-8 10-12      -> 1,2,3,5,6,7,8,10,11,12
            :u                 -> all unread messages
            1-3 :u             -> invalid specification, though heirloom-mailx equates to just :u
            :u :f              -> invalid specification, heirloom-mailx shows an error

        The resulting list is always in numerical ascending order
            5 3 1 10           -> 1,3,5,10

        The list is in thread/sort order of the message list. Changing the
        sort order likely invalidates the lists.

        As an extension, we'll allow mixing these. I don't see any reason to
        not allow unread messages and also message 5-10, for example. Or a
        list including unread, flagged, and flagged unread messages (":u :f').
        Essentially, the list outside parenthesis is a union. In parenthesis
        is intersection by default (boolean AND) unless using the explicit or
        operator.

        The full list of specials, according to the man page, are:

            :n      All new messages
            :o      All old messages (not read or new)
            :u      All unread messages
            :d      All deleted messages (used in undelete command)
            :r      All read messages
            :f      All flagged messages
            :a      All answered messages (replied)
            :t      All messages marked as draft
            :k      All killed messages
            :j      All junk messages
            .       The current message
            ;       The previously current message (using ; over and over
                    bounces between the last 2 messages)
            ,       The parent of the current message (looking for the message
                    with the Message-Id matching the current In-Reply-To or
                    last References entry
            -       (hyphen) The next previous undeleted message for regular
                    commands or the next previus deleted message for undelete.
            +       The next undeleted message, or the next deleted message
                    for undelete.
            ^       The first undeleted message, or first deleted for the
                    undelete command.
            $       The last message
            &x      The message 'x' and all messages from the thread that
                    begins at it (in thread mode only). X defaults to '.' if
                    not given.
            *       (asterisk) All messages.
            `       (back tick) All messages listed in the previous command
            /str    All messages with 'str' in the subject field, case
                    insensitive (ASCII). If empty, use last search "of that
                    type", whatever that means
            addr    Messages from address 'addr', normally case sensitive for
                    complete email address. Some variables change the
                    processing.
            (cri)   Messages matching an IMAP-style SEARCH criterion.
                    Performed locally if necesary, even when not on IMAP
                    connections.  if 'cri' is empty, reuse last search.

        (cri) is a complicated beasty, see the full documentation for details (from mailx until we have our own).
        As a simplification, we might just pass these literally to the IMAP server.

        Some commands don't take a list, but set the list. For example, the
        implicit command only prints one message (usually the next message).
        When using the back-tick, it will start with the first message in the
        last list, and reset the list. A subsequent run of back tick selects
        the second message, but maintains the list. The next back tick run
        shows the third, and so on.

        We note that there might be additional criteria someone might want to
        assign a shortcut to (for example, some IMAP servers support custom
        flags or labels/tags for messages to help organize them), and only 10
        of the 26 letters of the ASCII alphabet are used, and none of the
        uppercase letters are used. For that matter, nothing stops us from
        using whole words after the ':'. heirloom-mailx only pays attention to
        the first letter (e.g. :uf is interpreted as :u, :fu is interpreted as
        :f). So, we'll support single letters for user shortcuts and full
        words for tag names. This can lead to ambiguity if you have a tag
        named by a single letter. My inclination is to allow the names to be
        quoted.

        In a similar vein, we'd like to be able to have saved lists, akin to
        Vim registers or marks. Since mailx already uses '`' to reference the
        last list, we'll use `x to reference named list x. For simplicity, we
        should keep the lists volatile (switching the active mailbox clears
        all lists). Some issues to resolve with having saved lists persist is
        handling changes to the mailbox. Even keeping it volatile, the mailbox
        IDs can change on us (e.g. another client deleting a message).
        Ideally, we'd detect this and correct the numbers in the list to keep
        track (while watching a box), and remove entries from the list that
        are no longer in the box. For persistent lists, we'd probably have to
        key on UIDs and wipe lists with non-matching UIDVALIDITY. No idea how
        to handle this for local or pop boxes. Probably easiest to not support
        that (everyone should run an IMAP server, even just to export their
        own mail! j/k, but only somewhat).

        Now, this actually breaks compatibility with heriloom-mailx, which
        interprets tokens after the `.

        For example, if we do 'f^$' (and message 6337 happens to be the last
        message), then the list becomes 1,6337.
        If we then do 'f`4', the list becomes 1,4,6337.

        However, if we then do 'f`test@example.com', it tells us there aren't
        any messages form test@example.com.
        But, then if we do 'f test@example.com', it will list the messages
        from test and set the new list. Odd.

        I'm hoping noone relies on this behavior in their workflow, but
        perhaps we should have a setting to use something resembling the old
        behavior. I doubt I could spec the actual old behavior outside of the
        actual program code that interpreted it.

        Another issue is that the mailx format precludes math, even though ed
        (supposedly the inspiration for the command line) /does/ allow math,
        and I've found myself wanting it several times in my daily mailx
        usage. For example, if I'm on message 6748, and I remembered seeing
        something in the previous message, I just type '-' and mailx shows me
        the previous message (6747), which is great. When I'm done, I want to
        look at the next message. However, I've already seen 6748, so I want
        to go to 6749. However, to get there I either have to iterate using
        '+' or '' (just pressing enter shows the next message) viewing an
        already viewed message (wasting network and terminal bandwidth, and my
        time), or type the whole message number (6749) which is both a lot of
        numbers and requires me to know the message number I was just at (but
        I've been moving relatively, so I don't know without doing a 'headers'
        command or paying attention to the message header when it printed a
        message to my pager).

        Ideally, I ought to be able to do something like '.+2', or really '.'
        plus or minus any number. However, mailx doesn't recognize the plus
        like that, and the minus is already used for ranges.

        Recognizing the plus shouldn't be hard, but differentiating between a
        ranging hyphen and subtraction is tougher. Our parser either needs to
        differentiate between starting with a dot vs a number (and thus
        disallow straight numerical math, which also might be useful to
        someone for selecting a message), or we need the option to use a
        different range character. IMAP uses a colon (':') for ranging, but
        that can get confusing with the shortcuts (e.g. ':u') without writing
        a smarter parser. Vim uses comma for ranging. This might work, because
        comma currently is used only on its own to refer to the current
        parent, never with another character (e.g. you can't spec the parent
        of a different message (say, message 1234) by doing '1234,' or
        ',1234', though oddly if you try either, mailx finds the comma and
        ignores the number).

        It might also be a bit more intuitive to today's users to use Git's parent
        notation (e.g. the parent of 123 would be 123^ or 123~1). This could
        be fairly convenient for a message that is in reply to multiple
        messages, though I don't know that MIME supports that, because the Git
        notation lets you select which parent (e.g. 123^2 picks the second
        parent). Of course, the caret/circumflex ('^') is already special,
        though tilde ('~') is available.

        Other annoying quirks of mailx:

            :d only works in the undelete command. You cannot list deleted
            messages by doing 'f :d' for example. You cannot display a deleted
            message by using :d or by giving the actual message number. You
            have to undelete it before it is accessible again.
        """

        s=set()
        # Second pass, read a few specials 
        if args == '.':
            return [self.C.currentMessage]
        if args == '+':
            # TODO: boundary check
            return [self.C.currentMessage + 1]
        if args == '-':
            # TODO: boundary check
            return [self.C.currentMessage - 1]
        if args == '`':
            # TODO: print "No previously marked messages" if the list is empty
            return self.C.lastList
        if args == '^':
            return [1]
        if args == '$':
            return [self.C.lastMessage]
        if args == ":u":
            data = self.C.connection.search("UTF-8", "unseen")
            if self.C.settings.debug.general:
                print(data)
            return map(int, data)
        if args == ":f":
            data = self.C.connection.search("UTF-8", "flagged")
            if self.C.settings.debug.general:
                print(data)
            return map(int, data)
        if args.startswith("(") and args.endswith(")"):
            # Support IMAP search by being a passthrough for IMAP SEARCH
            # command.
            data = self.C.connection.search("UTF-8", args)
            if self.C.settings.debug.general:
                print(data)
            return map(int, data)
        # First pass, lets just handle numbers.
        for r in args.split():
            # Note, we won't be able to keep the simple split once we include
            # quoting and parenthesis
            if '-' in r:
                r2 = r.split('-')
                if len(r2) != 2:
                    raise Exception("Invalid range '%s'" % r)
                s=s.union(range(int(r2[0]), int(r2[1]) + 1))
            else:
                s.add(int(r))

        return sorted(list(s))

    
    def default(self, args):
        c,a,l = self.parseline(args)
        #TODO Simulate the tokenizer of mailx a bit better. For example,
        # 'print5' will print message 5 instead of erroring that 'print5'
        # isn't a command.
        if not c:
            # Sometimes, parseline assumes there are aguments and no commands.
            # E.g. '-' and '$' result in c="", with a and l containing the
            # given string.
            c = l
            a = ''
        if c == 'h':
            #TODO: process shortcuts/aliases/whatever
            return self.do_headers(a)
        elif c[0] == 'h' and len(c) > 1 and not c[1].isalpha():
            return self.do_headers(c[1:] + ' ' + a)
        elif c == 'f':
            return self.do_from(a)
        elif c[0] == 'f' and len(c) > 1 and not c[1].isalpha():
            return self.do_from(c[1:] + ' ' + a)
        elif c == 'p':
            return self.do_print(a)
        elif c == 'P':
            return self.do_Print(a)
        elif c == 'q':
            return self.do_quit(a)
        elif c == 'vf':
            return self.do_virtfolder(a)
        elif c == 'x':
            return self.do_exit(a)
        elif c == 'EOF':
            # Exit or Quit? Maybe make it configurable? What does mailx do?
            print
            return self.do_exit(a)
        elif args.isdigit():
            # TODO: Should just try to do a parse of the line, independant of
            # if it looks like digits
            self.C.currentMessage = int(args)
            self.do_print("")
            self.C.lastcommand=""
        else:
            print("Unknown command", c)
    def emptyline(self):
        # repeat/continue last command
        if self.C.lastcommand=="search":
            self.C.lastsearchpos += 10
            self.do_search(self.C.lastsearch, offset=self.C.lastsearchpos)
        else:
            # Next message
            # TODO: mailx has a special case, which is when it picks the
            # message, e.g. when opening a box and it picks the first
            # flagged or unread message. In which case, the implicit
            # "next" command shows the message marked as current.
            #
            # Likewise, after a from command, the current message is used
            # instead of the next.
            #
            # Plan: Have both currentMessage and nextMessage. Typically
            # they are different, but can be the same.
            #
            # TODO: extension to mailx: Next could mean next in a list;
            # e.g. a saved list or results of f/search command or custom
            # list or whatever.
            # Ideally, we'd mention the active list in the prompt. Ideally
            # we'd also list what the implicit command is in the prompt
            # (e.g. next or search continuation)
            if (self.C.nextMessage > self.C.lastMessage):
                print("at EOF")
            else:
                self.C.currentMessage = self.C.nextMessage
                # print will update nextMessage for us
                self.do_print("")
    def getAddressCompleter(self):
        """Return a Completer class that will complete email addresses based on current preferences.

        It will be suitable for prompt_toolkit's completion."""
        class EmailCompleter(Completer):
            """This class currently requires code that creates objects of this to
            set an attribute "settings" that will have the searchcmd in it."""
            def get_completions(self, document, complete_event):
                before = document.current_line_before_cursor
                after = document.current_line_after_cursor
                # Simple first pass, use comma separation.
                # TODO: Actually parse emails or something.
                # NOTE: simple comma separation will break if a user's
                # displayed name has commas in it! (and some people prefer
                # "Surname, name" to "name Surname"
                thisstart = before.split(',')[-1]
                thisend = after.split(',')[0]
                this = thisstart + thisend
                prefix = " " if this.startswith(" ") else ""
                this = this.strip()
                s = subprocess.Popen(self.settings.addresssearchcmd.value.split() + [this], stdin=None, stdout=subprocess.PIPE)
                results=[]
                for i in range(10):
                    res = s.stdout.readline().strip()
                    #print(i,repr(res))
                    if res == "":
                        break
                    # Skip header line
                    if i == 0:
                        continue
                    results.append(res.split('\t'))
                s.stdout.close()
                res = s.wait()
                if res != 0 or len(results) == 0:
                    #print("res:",res,"len",len(results))
                    raise StopIteration()

                for res in results:
                    if len(res) > 1:
                        completion = "{} <{}>,".format(res[1], res[0])
                        if len(res) > 2:
                            meta = res[2]
                        else:
                            meta = None
                        yield Completion(prefix + completion, display=completion, start_position=-len(thisstart), display_meta=meta)
        compl = EmailCompleter()
        compl.settings = self.C.settings
        return compl

    @showExceptions
    def do_testq(self, text):
        print(processImapData(text), self.C.settings)

    @showExceptions
    def do_maildir(self, args):
        """Connect to the given maildir.

        You should use the folder command once that's working instead.

        This function should eventually dissappear."""
        C = self.C
        if C.connection:
            print("disconnecting")
            C.connection.close()
            C.connection.logout()
            C = None
        try:
            M = mailbox.Maildir(args, None, False)
        except Exception as ev:
            print("Error:", type(ev), ev)
            return
        C.connection = M
        C.currentMessage = 1
        C.nextMessage = 1
        C.lastMessage = len(M) - 1
        print("Opened maildir with %i messages." % len(M))

    @showExceptions
    def do_folder(self, args):
        """Connect to the given mailbox.

        If argument starts with a '+', the value of setting 'folder' is prepended to the target

        Currently supported protocols:
            imap://     - IMAP4r1 with STARTTLS
            imaps://    - IMAP4r1 over SSL
        """

        if args == "":
            # Just show information about the current connection, if any
            if not self.C.connection:
                print("No connection")
                return
            print("\"{}://{}@{}:{}/{}\": {} messages {} unread".format(
                self.C.connection.mailnexProto,
                self.C.connection.mailnexUser,
                self.C.connection.mailnexHost,
                self.C.connection.mailnexPort,
                self.C.connection.mailnexBox,
                self.C.lastMessage,
                len(self.C.connection.search("utf-8", "UNSEEN")),
                ))
            return


        C = self.C
        argss = args.split()
        user = None
        proto = None
        if len(argss) == 2:
            host = argss[0]
            port = int(argss[1])
        elif len(argss) == 1:
            if args.startswith("+"):
                args = self.C.settings.folder.value + args[1:]
            m = None
            if args.startswith("imap://"):
                m = re.match(r'([^@]*@)?([^/]*)(/.*)', args[7:])
                if not m:
                    print("failed to parse")
                    return
                port = 143
                proto = 'imap'
            elif args.startswith("imaps://"):
                m = re.match(r'([^@]*@)?([^/]*)(/.*)', args[8:])
                if not m:
                    print("failed to parse")
                    return
                port = 993
                proto = 'imaps'
            else:
                pass
            if not m:
                host = args
                port = None
                box = ""
            else:
                user, host, box = m.groups()
                if user:
                    # Remove '@' sign
                    user = user[:-1]
                if box:
                    # Remove single leading '/'
                    box = box[1:]
        else:
            raise Exception("Unknown connect format")
        if C.connection:
            if (
                    proto == C.connection.mailnexProto and
                    user == C.connection.mailnexUser and
                    host == C.connection.mailnexHost and
                    port == C.connection.mailnexPort
                    ):
                # We can reuse the existing connection
                c = self.C.connection
            else:
                print("disconnecting")
                C.connection.close()
                #C.connection.logout()
                C.connection = None
        if not C.connection:
            print("Connecting to '%s'" % args)
            c = imap4.imap4ClientConnection()
            c.debug = C.settings.debug.imap

            if "cacertsfile_{}".format(host) in self.C.settings:
                c.setCaCerts(getattr(self.C.settings, "cacertsfile_{}".format(host)).value)
            else:
                c.setCaCerts(C.settings.cacertsfile.value)
            # Set tracking info for detecting connection reusability
            c.mailnexProto = proto
            c.mailnexUser = user
            c.mailnexHost = host
            c.mailnexPort = port
            c.mailnexBox = box
            try:
                c.connect(host, port=port)
                if c.isTls():
                    print("Info: Connection already secure")
                else:
                    # TODO: if not c.caps, run capability command
                    if not c.caps or not 'STARTTLS' in c.caps:
                        print("Remote doesn't claim TLS support; trying anyway")
                    print("Info: Startting TLS negotiation")
                    c.starttls()
                    if c.isTls():
                        print("Info: Connection now secure")
                    else:
                        raise Exception("Failed to secure connection!")
                if not user:
                    user = getpass.getuser()
                try:
                    pass_ =  keyring.get_password("imap://%s" % host, user)
                except RuntimeError:
                    pass_ = None
                    print("Info: no password managers found; cannot save your password for automatic login")
                if not pass_:
                    pass_ = getpass.getpass()
                print("Info: Logging in")
                c.login(user, pass_)
                print("Info: Loggin complete")
            except KeyboardInterrupt:
                print("Aborting connection")
                self.C.connection = None
                return
            except imap4.ssl.SSLError as ev:
                print("Failed to establish a secure connection:", ev)
                print("Probably the certificate chain couldn't be verified. If you have\n"
                        "a trusted cert or authority for this host, try setting it in\n"
                        "cecertsfile_{}".format(host))
                self.C.connection = None
                return
        try:
            c.clearCB("exists")
            if box:
                c.select(box)
            else:
                c.select()
            print("Info: Mailbox opened")
            self.C.connection = c
            # By default, mailx marks the first unseen or flagged message as
            # the current message.
            # TODO: Actually, I think its the first new message, then flagged.
            if not hasattr(self.C.connection, 'unseen') or not self.C.connection.unseen:
                # IMAP server didn't give us the first unseen message on
                # connect; we'll have to ask for it. It could either be that
                # the server didn't feel like sending one, or there are no
                # messages that are unseen.
                unseen = map(int, self.C.connection.search("utf-8", "UNSEEN"))
            else:
                unseen = [self.C.connection.unseen]
            if len(unseen) != 0:
                self.C.currentMessage = sorted(unseen)[0]
            else:
                flagged = map(int, self.C.connection.search("utf-8", "flagged"))
                if len(flagged) != 0:
                    self.C.currentMessage = sorted(flagged)[0]
                else:
                    # Final fallback: start at beginning.
                    # TODO: There's probably a setting to start at the end
                    self.C.currentMessage = 1
            self.C.lastMessage = c.exists
            c.setCB("exists", self.newExist)
            c.setCB("expunge", self.newExpunge)
            if self.C.currentMessage > self.C.lastMessage:
                # This should only really happen when lastMessage is 0, but
                # range checking is probably good anyway.
                self.C.currentMessage = self.C.lastMessage
            self.C.nextMessage = C.currentMessage
            if self.C.settings.debug.general:
                # TODO: Maybe we should output this kind of info anyway...
                print("Current message: %s. Last message: %s" % (self.C.currentMessage, self.C.lastMessage))
            self.C.lastList = []
            self.C.virtfolder = None
            self.C.prevMessage = None

        except KeyboardInterrupt:
            print("Aborting")
            return
        # Finally, print stats about the connection
        # We already print this when called with no CLI arguments, so... just
        # call ourselves with empty CLI arguments to display it.
        self.do_folder("")
        # Finally finally, if 'headers' or 'headers_folder' is set, display
        # headers
        if self.C.settings.headers_folder if self.C.settings.headers_folder.value is not None else self.C.settings.headers:
            self.do_headers("")

    @showExceptions
    @needsConnection
    def do_folders(self, args):
        """List mailboxes in the default namespace.

        usage:
            folders
            folders base/path

        Doesn't currently support much for listing, but is hopefully somewhat
        helpful for the folder command.

        NOTE: Some older servers (e.g. uwimapd) will do a full recursive
        descent even when we only ask for one level of hierarchy. If you have
        a deep folder set on the remote side (e.g. a full unix home
        directory), this command can take a lot of time. Worse, if you have
        any symbolic loops, it can take a VERY long time (either forever or
        until the server gets some errors about exceeding path limits)

        See also lsub.
        """
        oldcb = None
        if "list" in self.C.connection.cbs:
            oldcb = self.C.connection.cbs["list"]
        folders = []
        def mycb(line):
            # TODO: More of this ought to be handled by the connection instead
            # fo us.
            if not line.startswith("* LIST "):
                raise Exception("Bad format from server")
            flags, delim, path =  processImapData(line[7:] + ' ', self.C.settings)
            # TODO: Is this supposed to be case insensitive?
            # TODO: How to handle servers without the CHILDREN extension? We
            # could probe or just never give hints about subfolders
            if 'CHILDREN' in self.C.connection.caps and '\\HasChildren' in flags:
                # NOTE: According to RFC3348, a server MAY have both
                # HasChildren and HasNoChildren if it isn't sure, and then we
                # shouldn't make assumptions, but on the next line says it is
                # an error if the server does this. I think we'll err on the
                # side of having children, and if the user cannot select it,
                # they'll get an error.
                print("{}{}".format(path,delim))
            else:
                print("{}".format(path))
        self.C.connection.cbs["list"] = mycb
        try:
            if len(args) == 0:
                self.C.connection.doSimpleCommand("LIST \"\" %")
            else:
                # TODO: Horrible. Should check formatting, and probably need to
                # handle escaping the string properly
                # TODO: obtain the 'correct' final separator somehow instead
                # of assuming slash
                self.C.connection.doSimpleCommand("LIST \"\" {}/%".format(args))
        except:
            if oldcb:
                self.C.connection.cbs["list"] = oldcb
            else:
                del self.C.connection.cbs["list"]
            raise

    def newExist(self, value):
        delta = value - self.C.lastMessage
        self.C.lastMessage = value
        if self.ttyBusy:
            # TODO: Collect messages for display once it isn't busy any more.
            return
        # TODO: We might get this on message expunge as well (e.g. delta would
        # be negative). Might also be the case where this doesn't change value
        # becuase something was added and something else was expunged.
        # Probably need to track untagged fetch results to see that.
        l = lambda: print("Info: %s: %i New messages" % (time.asctime(), delta))
        if self.cli._is_running:
            self.cli.run_in_terminal(l)
        else:
            l()
        if self.C.settings.headers_newmsg if self.C.settings.headers_newmsg.value is not None else self.C.settings.headers:
            ml = MessageList()
            ml.addRange(value - delta + 1, value)
            l = lambda: self.showHeadersNonVF(ml)
            def tcb(handle):
                if self.cli._is_running:
                    self.cli.run_in_terminal(l)
                else:
                    l()
            t = pyuv.Timer(self.ptkevloop.realloop)
            t.start(tcb,0.1,0)

    def newExpunge(self, value, msg):
        # TODO: let user know that message numbers have changed, but only do
        # it once between commands so as not to be a nuisance
        # Alternatively, use message UIDs behind the scenes so that we can
        # maintain the message numbers the user expects. Managing the
        # de-synchronization would probably be challenging, though
        self.C.lastMessage -= 1

    def bgcheck(self, event):
        # NOOP command does nothing, but it has the side effect of allowing
        # the server to send us untagged updates (e.g. new message
        # indications) as well as preventing inactivity timeout.
        # This should be done once every 29 minutes or so (servers are allowed
        # to make their timeout as small as 30 minutes). When not idling, this
        # can be set much quicker to find new mail in a reasonable amount of
        # time.
        if self.C.connection:
            try:
                self.C.connection.doSimpleCommand("noop")
            except:
                self.C.connection = None
                raise

    @showExceptions
    @needsConnection
    def do_lsub(self, args):
        """List mailbox subscriptions

        usage:
            lsub
            lsub base/path

        Doesn't currently support much for listing, but is hopefully somewhat
        helpful for the folder command.

        Entries that contain subscriptions but aren't subscribed are shown in parenthesis

        See also folders.
        """
        oldcb = None
        if "lsub" in self.C.connection.cbs:
            oldcb = self.C.connection.cbs["lsub"]
        folders = []
        def mycb(line):
            # TODO: More of this ought to be handled by the connection instead
            # fo us.
            if not line.startswith("* LSUB "):
                raise Exception("Bad format from server")
            flags, delim, path =  processImapData(line[7:] + ' ', self.C.settings)
            # TODO: Is this supposed to be case insensitive?
            if '\\Noselect' in flags:
                print("({}{})".format(path,delim))
            else:
                print("{}".format(path))
        self.C.connection.cbs["lsub"] = mycb
        try:
            if len(args) == 0:
                self.C.connection.doSimpleCommand("LSUB \"\" %")
            else:
                # TODO: Horrible. Should check formatting, and probably need to
                # handle escaping the string properly
                # TODO: obtain the 'correct' final separator somehow instead
                # of assuming slash
                self.C.connection.doSimpleCommand("LSUB \"\" {}/%".format(args))
        except:
            if oldcb:
                self.C.connection.cbs["lsub"] = oldcb
            else:
                del self.C.connection.cbs["lsub"]
            raise

    @showExceptions
    @optionalNeeds(haveXapian, "Needs python-xapian package installed")
    @needsConnection
    def do_index(self, args):
        C = self.C
        M = C.connection
        i = 1
        seen=0

        db = xapian.WritableDatabase(C.dbpath, xapian.DB_CREATE_OR_OPEN)
        termgenerator = xapian.TermGenerator()
        termgenerator.set_stemmer(xapian.Stem("en"))

        while True:
            if i > C.lastMessage:
                break
            try:
                data = M.fetch(i, '(UID BODYSTRUCTURE)')
                #print(typ)
                #print(data)
                # TODO: use BODYSTRUCTURE to find text/plain subsection and fetch that instead of guessing it will be '1'.
                data = M.fetch(i, '(BODY.PEEK[HEADER] BODY.PEEK[1])')
                #print(typ)
                #print(data)
                #print(data[0][1])
                #print("------------ Message %i -----------" % i)
                #print(data[1][1])

                data = processImapData(data[0][1], self.C.settings)

                headers = data[0][1]
                headers = processHeaders(headers)
                print("\r%i"%i, end='')
                sys.stdout.flush()
                doc = xapian.Document()
                termgenerator.set_document(doc)
                if 'subject' in headers:
                    termgenerator.index_text(headers['subject'][-1], 1, 'S')
                if 'from' in headers:
                    for h in headers['from']:
                        # Yes, a message *can* be from more than one person
                        termgenerator.index_text(h, 1, 'F')
                if 'to' in headers:
                    for h in headers['to']:
                        # Yes, a message *can* be from more than one person
                        termgenerator.index_text(h, 1, 'T')
                if 'cc' in headers:
                    for h in headers['cc']:
                        # Yes, a message *can* be from more than one person
                        termgenerator.index_text(h, 1, 'C')
                if 'thread-index' in headers:
                    termgenerator.index_text(headers['thread-index'][-1],1,'I')
                if 'references' in headers:
                    termgenerator.index_text(headers['references'][-1],1,'R')
                if 'in-reply-to' in headers:
                    termgenerator.index_text(headers['in-reply-to'][-1],1,'P')
                if 'message-id' in headers:
                    termgenerator.index_text(headers['message-id'][-1],1,'M')

                termgenerator.index_text(data[0][3])
                # Support full document retrieval but without reference info
                # (we'll have to fully rebuild the db to get new stuff. TODO:
                # store UID and such)
                doc.set_data(data[0][1])
                idterm = u"Q" + str(i)
                doc.add_boolean_term(idterm)
                db.replace_document(idterm, doc)
                i += 1
            finally:
                pass
        print()
        print("Done!")

    def getTextPlainParts(self, index, allParts=False):
        """Get the plain text parts of a message.

        Returns a list of tuples. Each list entry represents one part.
        Each tuple consists of the part number and unicode version of the text, already converted from specified or guessed charsets.
        Currently this includes the message headers (as the first section).

        If the optional parameter allParts is set to true, this will actually
        return everything instead of just text parts.
        """
        resparts = []
        data = self.C.connection.fetch(index, '(BODY.PEEK[HEADER] BODYSTRUCTURE)')
        parts = processImapData(data[0][1], self.C.settings)
        headers = getResultPart('BODY[HEADER]', parts[0])
        # TODO: Headers are required to be ASCII or encoded using a header
        # encoding that results in ASCII (lists charset and encodes as
        # quoted-printable or base64 with framing). We should decode headers
        # to unicode using ASCII and, if failing, replace with highlighted
        # error boxes or something (not question marks, ideally). Then, we
        # should look for the header encoding markers and handle those as
        # well.
        resparts.append((None, 'header', headers.decode('windows-1252')))
        # We look at the bodystructure to get the encoding since we already
        # have to fetch it to find the text/plain parts. The other options was
        # to explicitly fetch the MIME data of each sub-part.
        #
        # The spec is interesting here. In a MIME message, there are headers
        # for the message, and then headers for each part of a multipart. In
        # IMAP, the "HEADER" part specifier for fetch refers to the top-level
        # (overall) message, or to the headers of a message/rfc822 subpart.
        # The "MIME" part specifier refers to the headers for the various
        # parts, but cannot be applied to the overall message.
        # This is actually because the subpart has its own mime headers BEFORE
        # the encapsulated message's headers. (e.g the sub part has a content
        # type of message/rfc822, but *that* message has a header of
        # content-type text/plain or multipart/alternative or whatever.
        #
        structstr = getResultPart('BODYSTRUCTURE', parts[0])
        struct = unpackStruct(structstr, self.C.settings, tag=str(index))
        structureStrings = []
        fetchParts = []
        def pickparts(struct, allParts=False):
            """Pick the parts we are going to use to produce a regular view of the message.

            We'll build a visualization of the structure while we are at it (much of
            this code is duplicated from do_structure)"""
            # TODO: unify the code with do_structure? Make its own function?
            # We probably want that anyway since other parts would utilize
            # this, such as message reply/forwarding
            extra = ""
            skip = False
            if hasattr(struct, "disposition") and struct.disposition not in [None, "NIL"]:
                extra += " (%s)" % struct.disposition[0]
                if not allParts and struct.disposition[0].lower() == "attachment":
                    skip = True
                dispattrs = dictifyList(struct.disposition[1])
                if 'filename' in dispattrs:
                    extra += " (name: %s)" % dispattrs['filename']
            # TODO XXX: Preprocess control chars out of all strings before
            # display to terminal!
            structureStrings.append("%s   %s/%s%s" % (struct.tag, struct.type_, struct.subtype, extra))
            innerTag = ".".join(struct.tag.split('.')[1:])
            # First pass, we'll just grab all text/plain parts. Later we'll
            # want to check disposition, and later we'll want to deal with
            # multipart/alternative better (and multipart/related)
            if not allParts and struct.type_ == "text" and struct.subtype == "plain":
                # TODO: write the following a bit more efficiently. Like,
                # split only once, use second part of return only, perhaps?
                if not skip:
                    fetchParts.append((innerTag, struct))
            if allParts and not isinstance(struct, structureMessage) and not hasattr(struct, "subs"):
                # Probably useful to display, not a multipart itself, or a
                # message (which *should* have subparts itself?)
                # TODO: Properly handle attached messages
                fetchParts.append((innerTag, struct))
            if isinstance(struct, structureMessage):
                extra = ""
                # Switch to the inner for further processing
                struct = struct.subs[0]
                if hasattr(struct, "disposition") and struct.disposition not in [None, "NIL"]:
                    extra += " (%s)" % struct.disposition[0]

                structureStrings.append("%*s   `-> %s/%s%s" % (len(struct.tag), "", struct.type_, struct.subtype, extra))
                fetchParts.append(("%s.HEADER" % innerTag, struct))
            if hasattr(struct, "subs"):
                for i in struct.subs:
                    pickparts(i, allParts)
        pickparts(struct, allParts)
        structureString = u"\n".join(structureStrings)
        if len(fetchParts) == 0:
            return []
        elif len(fetchParts) == 1 and len(fetchParts[0][0]) == 0:
            # This message doesn't have parts, so fetch "part 1" to get the
            # body
            fparts = ["BODY.PEEK[1]"]
            parts = ["BODY[1]"]
        else:
            fparts = ["BODY.PEEK[%s]" % s[0] for s in fetchParts]
            parts = ["BODY[%s]" % s[0] for s in fetchParts]
        data = self.C.connection.fetch(index, '(%s)' % " ".join(fparts))
        resparts.append((None, None, structureString + '\r\n\r\n'))
        dpart = processImapData(data[0][1], self.C.settings)
        for p,o in zip(parts,fetchParts):
            dstr = getResultPart(p, dpart[0])
            if isinstance(o[1], structureMultipart):
                o[1].encoding = None
                o[1].attrs = None
            encoding = o[1].encoding
            # First, check for transfer encoding
            if encoding in [None, "", "NIL", '7bit', '8bit']:
                # Don't need to do anything
                pass
            elif encoding == "quoted-printable":
                dstr = dstr.decode("quopri")
            elif encoding == "base64":
                dstr = dstr.decode("base64")
            else:
                resparts.append((o[0],o[1],"Part %s: unknown encoding %s\r\n" % (o[0], encoding)))
                continue
            # Finally, check for character set encoding
            # and other layers, like format flowed
            if o[1].attrs and 'charset' in o[1].attrs:
                charset = o[1].attrs['charset']
                try:
                    # TODO: Is this possibly a security risk? Is there any
                    # value that causes the decode function to go awry?
                    d = dstr.decode(charset)
                    # Look for common control characters that likely mean a
                    # decode error. Most common is MS Outlook encoding text in
                    # CP1252 and then claiming it is iso-8859-1.
                    for c in map(unichr, range(0x80,0xa0)):
                        if c in d:
                            raise UnicodeDecodeError(str(charset), b"", 0, 1, b"control character detected")
                except UnicodeDecodeError:
                    if charset == 'iso-8859-1':
                        # MS Outlook lies about its charset, so we'll try what
                        # they mean instead of what they say. TODO: Should we
                        # complain about this? Not like the user can do much
                        # except encourage the sender to stop using outlook.
                        try:
                            d = dstr.decode('windows-1252')
                        except:
                            d = "Part %s: failed to decode as %s or windows-1252\r\n" % (o[0], charset)
                    else:
                        d = "Part %s: failed to decode as %s\r\n" % (o[0], charset)
            else:
                d = dstr
            if o[1].attrs and 'format' in o[1].attrs and o[1].attrs['format'].lower() == 'flowed':
                #TODO: Flowed format handling
                pass
            resparts.append((o[0], o[1], d))
        return resparts

    def partsToString(self, parts):
        body = u''
        for part in parts:
            if part[0] is None:
                # Headers or structure
                if part[1] == 'header':
                    msg = email.message_from_string(part[2])
                    for header in self.C.settings.ignoredheaders.value:
                        if header in msg:
                            del msg[header]
                    prefheaders = ""
                    for header in self.C.settings.headerorder.value:
                        if header in msg:
                            for val in msg.get_all(header):
                                prefheaders += "{}: {}\n".format(header, val)
                            del msg[header]
                    if self.C.settings.headerorderend:
                        body += msg.as_string().rstrip('\r\n')
                        body += '\r\n'
                        body += prefheaders
                        body += '\r\n'
                    else:
                        body += prefheaders
                        body += msg.as_string()
                else:
                    # must be structure
                    if not self.C.settings.showstructure:
                        continue
                    body += "\033[7mStructure:\033[0m\n"
                    body += part[2]
                continue
            if isinstance(part[1], structureMultipart):
                # Ideally, subparts like attatched messages should be
                # indented, perhaps with a colored bar
                if not self.C.settings.allpartlabels:
                    # Don't show a label here if we are going to show it
                    # anyway below.
                    body += "\033[7mPart %s:\033[0m\n" % (part[0] or '1')
            if self.C.settings.allpartlabels:
                body += "\033[7mPart %s:\033[0m\n" % (part[0] or '1')
            if self.C.settings.debug.general:
                body += "encoding: " + part[1].encoding + "\r\n"
                body += "struct: " + repr(part[1].__dict__) + "\r\n"
            body += part[2]
        return body

    @showExceptions
    @needsConnection
    def do_open(self, args):
        """Open a message part (e.g. attachment) in an external viewer.

        Takes a message sub-part notation. E.g. 670.1.2 is part 2 of part 1 of
        message 670.

        Doesn't update current message location or seen status.

        WARNING: No effort is made to ensure that opening any attachment is safe.
        Bugs in external viewer software can be exploited. Features too; for example,
        opening an HTML message part subjects you to javascript, external image fetching,
        and possibly plugin invocation. You have been warned!

        Also, no effort is made to fix up embedded images (at least not yet), so viewing
        an HTML part won't have its associated image files saved with it. Those currently
        must be opened separately.

        The user/system mailcap file is used for picking viewing software. See you system
        documentation for how to customize that (e.g. man mailcap).

        See also the 'structure' command."""
        args=args.split()
        if len(args) > 1:
            print("open takes single argument")
            return
        msgpart = args[0].split('.',1)
        if len(msgpart) == 1:
            # Use the first part if none given
            msgpart = (msgpart[0], "1")
        data = self.C.connection.fetch(msgpart[0], '(BODYSTRUCTURE)')
        parts = processImapData(data[0][1], self.C.settings)
        struct = getResultPart('BODYSTRUCTURE', parts[0])
        m = mailcap.getcaps()
        #print()
        #print(struct)
        struct = unpackStruct(struct, self.C.settings)
        #print()
        #print(struct)
        struct = flattenStruct(struct)
        #print()
        #print(struct)
        #print()
        key = '.' + msgpart[1]
        if not key in struct:
            print("Subpart not found in message. Try the 'structure' command.")
            return
        part = struct[key]
        #print(part)
        cmds = mailcap.findmatch(m, "{}/{}".format(part.type_, part.subtype))
        if not cmds[1]:
            print("Don't know how to display part. Maybe update your mailcap file,\n"
            "or specify a different message part? (use the 'structure' command\n"
            "to see a parts list)")
            return
        if not 'view' in cmds[1]:
            print("No view command found for part.")
            return
        #print("Would run",cmds[1]['view'])
        #print("Fetching attachment")
        data = self.C.connection.fetch(msgpart[0], '(BODY.PEEK[{}])'.format(msgpart[1]))
        #print("processing data")
        # TODO: This part is *very* slow
        parts = processImapData(data[0][1], self.C.settings)
        #print("getting part")
        data = getResultPart('BODY[{}]'.format(msgpart[1]), parts[0])
        #print(data)
        #print(part.encoding)
        if part.encoding in [None, "", "NIL", '7bit', '8bit']:
            # Don't need to do anything
            pass
        elif part.encoding == "quoted-printable":
            data = data.decode("quopri")
        elif part.encoding == "base64":
            data = data.decode("base64")
        else:
            print("unknown encoding %s; can't decode for display\r\n" % (encoding))
            return
        #print("Saving attachment to temporary file")
        with tempfile.NamedTemporaryFile() as outfile:
            outfile.write(data)
            outfile.flush()
            fullcmd = cmds[1]['view'] % outfile.name
            print("Launching viewer:", fullcmd)
            os.system(fullcmd)



    @shortcut("p")
    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_print(self, msglist):
        C = self.C
        M = C.connection
        lastMessage = len(self.C.virtfolder) if self.C.virtfolder else self.C.lastMessage
        if msglist is None:
            index = C.currentMessage
            if index == 0 or index > lastMessage:
                print("No applicable messages")
                return
            msgs = [index]
        else:
            msgs = msglist

        # Note: when we go to get the body structure, we should do a simple
        # search for text/plain. We *should* walk each level of the body
        # structure and interpret an action for it in constructing the
        # display.
        #
        # For each layer, we must *first* look for a disposition header to see
        # if it should be inline, attachment, or other. If other, assume
        # attachment. If not present, assume inline if it is something we
        # recognize, attachment otherwise.
        #
        # Then, based on what it is, we can continue parsing.
        #
        # NOTE: The main message might have a disposition header, set to
        # attachment. We would display no content, just prompt the user to
        # maybe save the file or explicitly open it. Opening might be tricky
        # for the actual top level, since we don't currently have a way to
        # differentiate the whole message from the primary contents. Maybe do
        # '.0' or something? Similar issue for the multipart containing a
        # message/rfc822 vs the header in the rfc882 itself.
        #
        # So, whenever we have a message/rfc822 (which is implicitly the top
        # layer), check the message headers. Otherwise check the MIME headers.
        # Make sure this is done in the order of hierarchy (example below)
        #
        # Things we know:
        #  * Multipart/mixed: process each sub-part in turn
        #  * Multipart/alternative: Scan for the best type we understand
        #  * Multipart/signed: check signature in second part against message
        #  in first part (unless told not to), then display sig status and
        #  then try to show the first part using above rules (recurse into it)
        #  * text/plain: easy, show the text after undoing transfer encoding
        #  and converting charset to the output device
        #
        # We might in the future know some others (we could probably implement
        # the RFC rich text email format to an extent, though I'm unaware of
        # any MUA that actually generates it. Or maybe we'll do some HTML
        # parsing, who knows?).
        #
        # Example 1:
        #   multipart/mixed
        #     multipart/alternative
        #       text/plain
        #       text/html
        #     text/plain
        #     image/png
        #
        # hit multipart/mixed, walk each child
        #   hit multipart/alternative, search child types for best
        #   presentation (will be text/plain as only one recognized)
        #       render text/plain
        #   hit (outer) text/plain. Render it
        #   hit image/png, show as attachment instead of rendering.
        #
        # If the (outer) text/plain or the alternative were marked as
        # attachment, they'd be not rendered as well. The contents of the
        # alternative would be odd to be marked, but we'd follow that too.
        #
        # If the png is explicitly inlined, we could either try to render it,
        # or mark is as unrenderable, able to be saved, but supposed to be
        # inlined (e.g. it'd act like an attachment, but we shouldn't call it
        # an attachment, since that wasn't the intent)
        #
        # For rendering, the img2txt program (from caca-utils) might be a good
        # option.

        # TODO: Support lists. For now, just handle the first in the list
        index = msgs[0]
        if self.C.virtfolder:
            if index > lastMessage:
                print("Message {} out of range".format(index))
                return
            index = self.C.virtfolder[index - 1]
        parts = self.getTextPlainParts(index)
        if len(parts) < 2:
            print("Message has no displayable parts")
            return
        body = self.partsToString(parts)

        # TODO: Use terminfo/termcap (or perhaps pygments or prompt_toolkit)
        # for styling
        content = b"\033[7mMessage %i:\033[0m\n" % index
        content += body.encode('utf-8')
        res = self.runAProgramWithInput(["less","-R"], content)
        if res == 0:
            # TODO: Allow asynchronous mode. That is, in mailx, we locally
            # keep track of the fact that the message was seen until the user
            # quits (not exits) or selects another folder.
            #
            # I'm pretty sure mailx did it that way because modifying mbox
            # files multiple times is expensive, so it was better for the
            # system to rewrite it once only when exiting. It also isn't
            # expected to have live synchronized access to the box.
            #
            # With IMAP and/or maildir, this isn't the case. Setting flags is
            # cheap (either the IMAP server handles it, or it is a file rename
            # in maildir) and doing it live ensures we don't lose data on a
            # crash *and* allows multiple clients to see updated information
            # as it happens from any client.
            #
            # However, some people probably like the mailx behavior better
            # because they are used to it, so we ought to support it.
            M.doSimpleCommand("STORE %s +FLAGS (\Seen)" % index)
            # Update message stuffs. Should probably update the 'lastList' as
            # well.
            C.nextMessage = C.currentMessage + 1

    @shortcut("P")
    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_Print(self, msglist):
        """Print all parts of a message."""
        C = self.C
        M = C.connection
        lastMessage = len(self.C.virtfolder) if self.C.virtfolder else self.C.lastMessage
        if msglist is None:
            index = C.currentMessage
            if index == 0 or index > lastMessage:
                print("No applicable messages")
                return
            msgs = [index]
        else:
            msgs = msglist

        # TODO: Support lists. For now, just handle the first in the list
        index = msgs[0]
        if self.C.virtfolder:
            if index > lastMessage:
                print("Message {} out of range".format(index))
                return
            index = self.C.virtfolder[index - 1]
        parts = self.getTextPlainParts(index, allParts=True)
        if len(parts) < 2:
            print("Message has no displayable parts")
            return
        body = self.partsToString(parts)

        # TODO: Use terminfo/termcap (or perhaps pygments or prompt_toolkit)
        # for styling
        content = b"\033[7mMessage %i:\033[0m\n" % index
        content += body.encode('utf-8')
        res = self.runAProgramWithInput(["less","-R"], content)
        if res == 0:
            # TODO: Allow asynchronous mode. See do_print for details.
            M.doSimpleCommand("STORE %s +FLAGS (\Seen)" % index)
            # Update message stuffs. Should probably update the 'lastList' as
            # well.
            C.nextMessage = C.currentMessage + 1

    @showExceptions
    @optionalNeeds(haveXapian, "Needs python-xapian package installed")
    @needsConnection
    def do_latest_threads(self, args):
        """Show the latest 10 threads. If given a number, show the thread containing *that* message.

        This is mostly a testing function.

        First pass: only do the thread bit.
        """

        if not args.isdigit():
            print("Sorry, don't support listing last 10 yet. Try giving a message ID instead")
            return
        M = self.C.connection
        index = int(args)
        data = M.fetch(index, '(BODY.PEEK[HEADER])')
        data = processImapData(data[0][1], self.C.settings)
        headers = processHeaders(data[0][1])
        term = None
        if 'thread-index' in headers:
            term = headers['thread-index'][-1]
        elif 'references' in headers:
            #TODO: find out how references is supposed to work
            # For now, guessing that they are in order, so the first entry is
            # the oldest
            term = headers['references'][-1].split(" ")[0]
        elif 'in-reply-to' in headers:
            term = headers['in-reply-to'][-1]
        elif 'message-id' in headers:
            term = headers['message-id'][-1]
        if term is None:
            print("singleton")
        else:
            def disp(data, matches):
                for i in range(len(data)):
                    headers = processHeaders(data[i])
                    #print('#%i id %s from %s to %s subject %s' % (matches[i].docid, headers['message-id'], headers['from'][-1], headers['to'][-1], headers['subject'][-1]))
                    if index == matches[i].docid:
                        marker = '>'
                    else:
                        marker = ' '
                    print('%s#%i from %-20s  subject %s' % (marker, matches[i].docid, headers['from'][-1][:20], headers['subject'][-1]))
            #find the first message
            term = term.strip('<>')
            #print("   Searching 'id:%s'" % term)
            disp(*self.search("id:%s" % term))
            #print("   Searching 'thread:%s'" % term)
            #disp(*self.search("thread:%s" % term))
            #print("   Searching 'ref:%s'" % term)
            #data, matches = self.search("ref:%s" % term)
            data, matches = self.search("ref:%s thread:%s" % (term, term))
            disp(data, matches)

            # TODO: Having found these matches, we ought to check the list of
            # results for additional references and in-reply-tos in case we
            # missed anything. Finally, we should sort by date or similar.
            #
            # The next step for providing somewhat usefull viewing of the
            # thread ought to be stripping off stuff the user's already seen.
            # In particular, we should try to detect pre- and post- quoted
            # text. A difficulty will be in-line responses. Almost impossible
            # will probably be people who inline response with color only
            # where their reply doesn't mark quoted lines (like Outlook)

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_show(self, msglist):
        """Show the raw, unprocessed message"""
        C = self.C
        M = C.connection
        if msglist is None:
            index = C.currentMessage
            if index == 0 or index > self.C.lastMessage:
                print("No applicable messages")
                return
            msgs = [index]
        else:
            msgs = msglist
        content = b""
        for index in msgs:
            data = M.fetch(index, '(BODY.PEEK[HEADER] BODY.PEEK[TEXT])')
            parts = processImapData(data[0][1], self.C.settings)
            headers = parts[0][1]
            body = parts[0][3]
            #content = headers.encode('utf-8') + body.encode('utf-8')
            content += b"Message {}:\n".format(index) + str(headers) + str(body)
        # TODO: Process content for control chars?
        res = self.runAProgramWithInput(["less"], content)

    @showExceptions
    def do_mail(self, args):
        """Compose and send a message"""
        # TODO: Completion of email addresses
        if args:
            to = args
        else:
            # TODO: Support completions from, e.g. Khard
            to = self.singleprompt("To: ")
        # Default is space separated:
        to = to.split()
        subject = self.singleprompt("Subject: ")
        newmsg = email.mime.text.MIMEText("")
        newmsg['To'] = ", ".join(to)
        newmsg['Subject'] = subject
        if self.C.settings.autobcc:
            newmsg['Bcc'] = self.C.settings.autobcc.value
        self.editMessage(newmsg)

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_reply(self, msglist):
        C = self.C
        M = C.connection
        lastMessage = len(self.C.virtfolder) if self.C.virtfolder else self.C.lastMessage
        if msglist is None:
            index = C.currentMessage
            if index == 0 or index > lastMessage:
                print("No applicable messages")
                return
            msgs = [index]
        else:
            msgs = msglist
        if len(msgs) > 1:
            print("Sorry, don't yet support replying to multiple messages at once")
            return
        index = msgs[0]
        if self.C.virtfolder:
            if index > lastMessage:
                print("Message {} out of range".format(index))
                return
            index = self.C.virtfolder[index - 1]
        parts = self.getTextPlainParts(index)
        hdrs = processHeaders(parts[0][2])
        # The spec doesn't say specifically how to handle replies, leaving it
        # up to individual implementations.
        # They give an example where the Reply-To or From is used for the new
        # To, and the old To and Cc are combined to form the new Cc.
        # Mailx copied To and Cc from the old and added the Reply-To or From
        # to the new To.
        # We'll follow mailx for now, though we'll assume the Reply-To or From
        # is more important than the old To and put them at the beginning of
        # the list.
        # TODO: Allow a user preference setting for how to do this. Maybe
        # allow a setting of 'ask' to prompt for each message.
        if 'to' in hdrs:
            # There can be multiple 'to' lines, theoretically. Lets merge them
            # and then split the components
            to = ",".join(hdrs['to']).split(',')
        else:
            to = ""
        if 'cc' in hdrs:
            cc = ",".join(hdrs['cc']).split(',')
        else:
            cc = ""
        # TODO: What if the message has BCC headers? Warn the user? Prompt?
        # Discard? Sheepishly discarding for now. Need to see what mailx does,
        # I guess.
        if 'subject' in hdrs:
            # Take the first subject we find.
            subj = hdrs['subject'][0]
            if not subj.lower().startswith("re: "):
                subject = "Re: " + subj
            else:
                subject = subj
        else:
            subject = "Re:"
        if 'from' in hdrs:
            from_ = hdrs['from'][0]
        else:
            #TODO: Print a warngin? An error? From is one of only 2 mandatory
            # fields. Not having it breaks many assumptions.
            from_ = "unkown"
        # Prepend the sender to the to list
        if 'reply-to' in hdrs:
            to[0:0] = hdrs['reply-to'][0]
        else:
            to[0:0] = [from_]
        body = ""
        for part in parts[2:]:
            # TODO: really need a better quoting algorithm here
            for line in part[2].split("\r\n"):
                if not line.startswith(">"):
                    # Add a space for padding
                    body += "> " + line + "\r\n"
                else:
                    # Don't add a space; line is already quoted
                    body += ">" + line + "\r\n"
        # Can't pass unicode to the constructor without having it encoded to a
        # charset. We'll prefer storing the payload as unicode and converting
        # it to a charset just before sending, so we don't have to keep
        # converting back-and-forth during editing.
        newmsg = email.mime.text.MIMEText("")
        if 'date' in hdrs:
            newmsg.set_payload("Quoth {} on {}:\r\n\r\n{}".format(from_, hdrs['date'][0], body))
        else:
            newmsg.set_payload("Quoth {}\r\n{}".format(from_, body))
        me = email.utils.getaddresses([self.C.settings['from'].value] + self.C.settings.altfrom.value)
        addrs=[]
        for addr in email.utils.getaddresses(to):
            for myaddr in me:
                if addr[1] == myaddr[1]:
                    break
            else:
                # Made it through for loop without any matches, so this
                # address isn't us; append it to the list
                addrs.append(addr)
        print(addrs)
        newmsg['To'] = ", ".join(map(email.utils.formataddr, addrs))
        addrs=[]
        for addr in email.utils.getaddresses(cc):
            for myaddr in me:
                if addr[1] == myaddr[1]:
                    break
            else:
                # Made it through for loop without any matches, so this
                # address isn't us; append it to the list
                addrs.append(addr)
        print(addrs)
        newmsg['Cc'] = ", ".join(map(email.utils.formataddr, addrs))
        newmsg['From'] = encodeEmail(self.C.settings['from'].value)
        newmsg['Subject'] = subject
        # On identifying fields, RFCs 2822 and 5322 say In-Reply-To should
        # exist if the parent(s) have message-ids, and should consist of those
        # id(s).
        # The References field should copy the parent's (if any) followed by
        # the parent's id. If no reference in parent's, but parent has
        # in-reply-to, copy that instead, then the parent's id. Updating
        # references is unspecified for multi-parent messages.
        # Apparently some implementations 'walk' the references list for the
        # purpose of threading, rather than building a heirarchy of
        # in-reply-to. Concatenating both parents' references headers would
        # potentially wreak havok on those clients. However, the specs don't
        # call out which client(s) do this, so who knows if they still exist
        # or how bad they break. Since it is unspecified, we can do whatever
        # we want.
        # For now, though, we only support single parent replies, so that
        # isn't a problem (yet).
        if 'message-id' in hdrs:
            newmsg['In-Reply-To'] = hdrs['message-id'][0]
        refs = []
        if 'references' in hdrs:
            refs.extend(hdrs['references'][0].split())
            if 'message-id' in hdrs:
                refs.append(hdrs['message-id'][0])
        elif 'in-reply-to' in hdrs:
            # TODO: What if multiple in-reply-to headers instead of a single
            # space-separated one?
            refs.extend(hdrs['in-reply-to'][0].split())
            if 'message-id' in hdrs:
                refs.append(hdrs['message-id'][0])
        elif 'message-id' in hdrs:
            refs.append(hdrs['message-id'][0])
        if len(refs):
            newmsg['references'] = " ".join(refs)
        if self.C.settings.autobcc:
            newmsg['Bcc'] = self.C.settings.autobcc.value
        print("Message to %s, replying to %s, subject %s" % (", ".join(to), from_, subject))
        sent = self.editMessage(newmsg)
        if sent:
            M.doSimpleCommand("STORE %s +FLAGS (\Answered)" % index)
            # Update message stuffs. Should probably update the 'lastList' as
            # well.
            C.nextMessage = C.currentMessage + 1


    def editMessage(self, message):
        attachlist = []
        # TODO: Allow a default setting for signing
        pgpsign = False
        while True:
            try:
                # TODO: allow tabs in the input
                line = self.singleprompt("")
                # TODO: Allow ctrl+c to abort the message, but not mailnex
                # (e.g. at this stage, two ctrl+c would be needed to exit
                # mailnex. The first to abort the message, the second to exit
                # mailnex)
            except EOFError:
                line = '.'
            if line.startswith("~~"):
                # User wants to start the line with a tidle
                message.set_payload(message.get_payload() + line[1:] + '\r\n')
            elif line.startswith("~?") or line == '~help':
                print("""  Help:
                    ~~ Text -> ~ Text   (enter a line starting with a single '~' into the message)
                    .          Send message
                    ~.         Send message
                    #@ file    Add file to the attachment list
                    ~h         Edit message headers (To, Cc, Bcc, Subject)
                    ~p         Print current message.
                    ~px        Print raw message escaping non-printing and line feed characters.
                    ~q         Quit composing. Don't send. Append message to ~/dead.letter if save is set, unless 'drafts' is set.
                    ~v         Edit message in external (visual) editor
                    ~x         Quit composing. Don't send. Discard current progress.
                    ~pgpsign   Sign the message with a PGP key (toggle)
                    """)
                # ~!command    = execute shell command
                # ~.           = same as end-of-file indicator (according to mailx)
                #                I feel like it ought to be to insert a literal dot. I can't find a way
                #                to do that in mailx. Maybe a setting to switch between the two operations?
                # ~<file       = same as ~r
                # ~<!command   = run command in shell, insert output into message
                # ~@           = edit attachment list
                # ~@ filename  = add filename to attachment list. Space separated list (according to mailx)
                # ~A           = insert string of the Sign variable (like '~i Sign)
                # ~a           = insert string of the sign variable (like '~i sign)
                # ~bname       = add names to bcc list (space separated)
                # ~cname       = add names to cc list (space separated)
                # ~d           = read ~/dead.letter into message
                # ~e           = edit current message in editor (default is ed?)
                # ~fmessage    = read messages (message ids) into message (or current message if none given). use format of print, but only include first part
                # ~Fmessage    = like ~f, but include all headers and mime parts
                # ~h           = edit headers (to, cc, bcc, subject)
                # ~H           = edit headers (from, reply-to, sender, organization). Once this command is used, ignore associated user settings
                # ~ivar        = insert value of variable into message.
                # ~mmessages   = read message like ~f, but include indentation.
                # ~Mmessages   = like ~m, but include all headers and MIME parts
                # ~p           = print message collected so far, prefaced by headers and postfixed by the attachment list. May be piped to pager.
                # ~q           = abort message, write to dead.letter IF 'save' is set.
                # ~rfile       = read file into message
                # ~sstring     = set subject to string
                # ~tname       = add names to To list (space separated) (direct recipient list)
                # ~v           = invoke alternate editor (VISUAL)
                # ~wfile       = write message to named file, appending if file exists TODO: Have a setting which fails if file exists (mailx doesn't write headers regardless of editheaders setting)
                # ~x           = like ~q, but don't save no matter what.
                # ~|command    = pipe message through shell command as a filter. Retain original if no output.
                # ~:command    = run our command (mailnex)
                # ~_command    = same as ~:
                # ~~string     = insert string prefixed by one '~'
                #
                #
            elif line == "." or line.startswith("~."):
                # Send message
                break
            elif line.startswith("~q"):
                if 'drafts' in self.C.settings:
                    print("Sorry, drafts setting is TBD")
                if 'save' in self.C.settings:
                    # TODO: Handle errors here. We want to try hard to not lose
                    # the user's message if at all possible.
                    ofile = open("%s/dead.letter" % os.environ['HOME'], "a")
                    # This is probably not the right format for dead.letter
                    ofile.write("From user@localhost\r\n%s\r\n" % (message.as_string()))
                return False
            elif line.startswith("~x"):
                print("Message abandoned")
                return False
            elif line.startswith("~h"):
                newto = self.singleprompt("To: ", default=message['To'] or '', completer=self.getAddressCompleter())
                newcc = self.singleprompt("Cc: ", default=message['Cc'] or '', completer=self.getAddressCompleter())
                newbcc = self.singleprompt("Bcc: ", default=message['Bcc'] or '', completer=self.getAddressCompleter())
                newsubject = self.singleprompt("Subject: ", default=message['Subject'] or '')
                if newto == "":
                    del message['To']
                elif 'To' in message:
                    message.replace_header('To', newto)
                else:
                    message.add_header('To', newto)

                if newcc == "":
                    del message['Cc']
                elif 'Cc' in message:
                    message.replace_header('Cc', newcc)
                else:
                    message.add_header('Cc', newcc)

                if newbcc == "":
                    del message['Bcc']
                elif 'Bcc' in message:
                    message.replace_header('Bcc', newbcc)
                else:
                    message.add_header('Bcc', newbcc)

                if newsubject == "":
                    del message['Subject']
                elif 'Subject' in message:
                    message.replace_header('Subject', newsubject)
                else:
                    message.add_header('Subject', newsubject)

            elif line == "~pgpsign":
                if not haveGpgme:
                    print("Cannot sign; python-gpgme package missing")
                else:
                    # Invert sign. Python doesn't like "sign = !sign"
                    pgpsign = pgpsign == False
                    if pgpsign:
                        print("Will sign the whole message with OpenPGP/MIME")
                    else:
                        print("Will NOT sign the whole message with OpenPGP/MIME")
            elif line.startswith("~px"):
                print(repr(message.get_payload()))
            elif line.startswith("~p"):
                # Well, we have to dance here to get the payload. Pretty sure
                # we must be doing this wrong.
                orig = message.get_payload()
                message.set_payload(orig.encode('utf-8'))
                print(message.as_string())
                message.set_payload(orig)
                #print("Message\nTo: %s\nSubject: %s\n\n%s" % (to, subject, messageText))
            elif line.startswith("~v"):
                f=tempfile.mkstemp()
                #TODO: If editHeaders is set, also save the headers
                os.write(f[0], message.get_payload().encode('utf-8'))

                # Would normally do cli.run_in_terminal, but that tries to
                # obtain the cursor position when done by asking the terminal
                # for it, but doesn't read it back in; it expects that the cli
                # will start running again and it'll pick up the response in
                # the normal loop. Unfortunately, we aren't returning to the
                # cli's loop here, we're invoking a different temporary one.
                #
                # Instead, we'll do just part of what run_in_terminal does,
                # that is, set the terminal up for "normal" use while we
                # invoke our terminal-using function
                with self.cli.input.cooked_mode():
                    # For whatever reason, vim complains the input isn't from
                    # the terminal unless we redirect it ourselves. I'm
                    # guessing prompt_toolkit changed python's stdin somehow
                    res = self.runAProgramStraight(["vim", f[1]])
                if res != 0:
                    print("Edit aborted; message unchanged")
                else:
                    os.lseek(f[0], 0, os.SEEK_SET)
                    fil = os.fdopen(f[0])
                    message.set_payload(fil.read().decode('utf-8'))
                    fil.close()
                    del fil
                    os.unlink(f[1])
                    #TODO: If editHeaders is set, retrieve those headers
            elif line.startswith("~@ "):
                filename = line[3:]
                # Can't read it. Is it because it doesn't exist?
                try:
                    st = os.stat(filename)
                except OSError as err:
                    if err.errno == errno.ENOENT:
                        print("WARNING: Given file doesn't currently exist. Adding to list anyway. We'll try reading it again when completing the message")
                    else:
                        print("WARNING: Couldn't get information about the file: %s" % err.strerror)
                        print("Adding to list anyway. We'll try reading it again when completing the message.")
                else:
                    if not os.access(filename, os.R_OK):
                        print("WARNING: Can't read existing file. Adding to list anyway. We'll try again when completing the message.")
                    else:
                        print("Attachment added to list. Raw size is currently %i bytes. Note: we'll actually read the data when completing the message" % st.st_size)
                        mtype = magic.from_file(filename, mime=True)
                        print("Mime type appears to be %s" % mtype)
                attachlist.append(filename)
            # TODO: The other ~* functions from mailx.
            # TODO: Extension commands. E.g. we might want "~save <path>" to
            # save a copy of the message to the given path, but keep editing.
            # We definitely want a way to edit an attachment (properties and
            # contents), and to add/edit arbitrary message parts. Should be
            # able to mark parts for signing, encryption, compression, etc.
            else:
                message.set_payload(message.get_payload() + line + '\r\n')

        message.set_payload(quopri.encodestring(message.get_payload().encode('utf-8')))
        message.set_charset("utf-8")
        del message['Content-transfer-encoding']
        message['Content-Transfer-Encoding'] = 'quoted-printable'
        # m is the outer message. message is the text part of the message.
        # Simple case, these are the same. Eventually, m is a
        # multipart/something that contains message somewhere within it. This is
        # the case for at least rich text, file attachments, and
        # signature/encryption messages. We'll start simple.
        m = message
        # Mandatory headers: From: and Date:
        # TODO: Allow this to be set by the user on a per-message basis
        if not 'From' in m:
            m['From'] = encodeEmail(self.C.settings['from'].value)
        if not 'Date' in m:
            m['Date'] = email.utils.formatdate(localtime=True) # Allow user to override local and possibly timezone?
        # Should headers: Message-Id
        if not 'Message-Id' in m:
            m['Message-Id'] = email.utils.make_msgid("mailnex")
        # Misc headers
        if not 'User-Agent' in m:
            # TODO: User-Agent isn't actually a mail header, it is a news
            # header. IANA doesn't currently recognize any header for email
            # MUA. mailx and Thunderbird us User-Agent. Eudora and Outlook use X-Mailer.
            m['User-Agent'] = 'mailnex 0.0' # TODO: Use global version string; allow user override
        # TODO: break out sending the message to a function. The function
        # should be invoked by the caller of this function instead of this
        # function. Rationale: perhaps we want to support editing a message
        # without sending it
        #
        # Broken out function should probably be replaceable or configurable.
        # For example, should support sending via sendmail or smtp.

        def addrs(data):
            return [a[1] for a in email.utils.getaddresses(data)]
        for attach in attachlist:
            try:
                with open(attach, "rb") as f:
                    data = f.read()
                    mtype = magic.from_buffer(data, mime=True)
            except KeyboardInterrupt:
                print("Aborting read of %s" % attach)
                # TODO: What do we do now? Ideall we'd go back to editing, but
                # we aren't achitected well for that. We'll dead letter it, I
                # guess
                ofile = open("%s/dead.letter" % os.environ['HOME'], "a")
                ofile.write("From user@localhost\r\n%s\r\n" % (m.as_string()))
                print("Message saved to dead.letter")
                return False
            except Exception as err:
                print("Error reading file %s for attachment" % attach)
                # TODO: What do we do now? Ideall we'd go back to editing, but
                # we aren't achitected well for that. We'll dead letter it, I
                # guess
                ofile = open("%s/dead.letter" % os.environ['HOME'], "a")
                ofile.write("From user@localhost\r\n%s\r\n" % (m.as_string()))
                print("Message saved to dead.letter")
                return False
            # TODO: Allow the user to override the detected mime type
            entity = email.mime.Base.MIMEBase(*mtype.split("/"))
            entity.set_payload(data)
            # TODO: Only use base64 if we have to. E.g. scan the file for bad
            # bytes. Alternatively, check if it is a type of text (e.g.
            # text/plain, text/html) and only do quoting if not.
            # TODO: Allow user to override this (e.g. force base64 or quopri)
            email.encoders.encode_base64(entity)
            entity.add_header('Content-Disposition', 'attachment', filename=attach.split(os.sep)[-1])
            if not isinstance(m, email.mime.Multipart.MIMEMultipart):
                # Convert into multipart/mixed
                n = email.mime.Multipart.MIMEMultipart()
                o = email.mime.Text.MIMEText("")
                o.set_payload(m.get_payload())
                for key in m.keys():
                    vals = m.get_all(key)
                    if key in ['Content-Type','Content-Transfer-Encoding']:
                        #print(" Migrating '%s' to new inner text part" % key)
                        if key in o:
                            o.replace_header(key, vals[0])
                            vals = vals[1:]
                        for val in vals:
                            o[key] = val
                    elif key in ['MIME-Version']:
                        #print(" Skipping '%s'" % key)
                        pass
                    else:
                        #print(" Migrating '%s' to new outer message" % key)
                        if key in n:
                            o.replace_header(key, vals[0])
                            vals = vals[1:]
                        for val in vals:
                            n[key] = val
                n.attach(o)
                m = n
            m.attach(entity)

        tos = addrs(m.get_all('To',[]))
        ccs = addrs(m.get_all('cc',[]))
        bccs = addrs(m.get_all('bcc',[]))
        recipients = list(set(tos + ccs + bccs))
        if len(recipients) == 0:
            ofile = open("%s/dead.letter" % os.environ['HOME'], "a")
            # This is probably not the right format for dead.letter
            ofile.write("From user@localhost\r\n%s\r\n" % (m.as_string()))
            print("Coudln't send message; no recipients were specified")
            print("Message saved to dead.letter")
            return False

        # TODO: Allow user to select behavior:
        # 1) message content excludes Bcc list
        # 2) message content includes Bcc list to users in the Bcc list,
        # excludes otherwise (Bcc persons get to see other bcc persons)
        # 3) each user listed in Bcc gets their own message with themselves
        # only in Bcc, others get no Bcc. (each Bcc person knows they were
        # supposed to be Bcc'd, but no one knows who else might be on Bcc)
        # 4) message has an empty Bcc field if there was a anyone listed in
        # Bcc. (Everyone knows that a Bcc happened, but not to whom)
        #
        # For now, we'll just do option 1, because that is easiest, since we
        # only have to form one copy of the message and only have to call
        # sendmail once. (Error handling might be more challenging if we had
        # to do more copies, e.g. what to do if the first sendmail succeeds
        # and the second fails?)
        if 'bcc' in m:
            del m['bcc']

        # Now that we have a recipient list and final on-the-wire headers, we
        # can deal with encryption.
        # We'll handle Signature and Encryption at the same point
        if pgpsign:
            ctx = gpgme.Context()
            keys = []
            # TODO: What about sender vs from, etc.
            if self.C.settings.pgpkey.value:
                keysearch = self.C.settings.pgpkey.value
            else:
                keysearch = m['from']
            for k in ctx.keylist(keysearch, True):
                keys.append(k)
            if len(keys) == 0:
                ofile = open("%s/dead.letter" % os.environ['HOME'], "a")
                # This is probably not the right format for dead.letter
                ofile.write("From user@localhost\r\n%s\r\n" % (m.as_string()))
                print("Coudln't send message; No keys found for %s" % keysearch)
                print("Message saved to dead.letter")
                return False
            elif len(keys) > 1:
                # TODO: Better key selection interface. E.g. should have a
                # header line, allow showing more details for a key
                index = 0
                for key in keys:
                    index += 1
                    print("{}: {}{}{}{}{}{}{}{}{} {} {}_{} {}".format(
                        index,
                        "C" if key.can_certify else " ",
                        "S" if key.can_sign else " ",
                        "E" if key.can_encrypt else " ",
                        "A" if key.can_authenticate else " ",
                        "D" if key.disabled else " ",
                        "X" if key.expired else " ",
                        "R" if key.revoked else " ",
                        "!" if key.invalid else " ",
                        "s" if key.secret else " ",
                        key.subkeys[0].length,
                        key.subkeys[0].fpr[-16:-8],
                        key.subkeys[0].fpr[-8:],
                        key.uids[0].uid,
                        ))
                keysel = self.singleprompt("Select key number (default 1): ", default="")
                if keysel == "":
                    keysel = '1'
                keys = [keys[int(keysel) - 1]]
            key = keys[0]
            ctx.signers = (key,)
            ctx.armor = True
            import io
            # Convert all lines to have the same line ending, else signing
            # will be bad. At the moment, on Ubuntu 16.04, the message will
            # consist of headers with unix (\n) line endings and a payload with
            # Windows/network line endings (\r\n).
            convlines = []
            # TODO: don't use as_string, use a flattener so we don't get
            # escaped 'From' lines
            for line in m.as_string().split(b'\n'):
                # So, RFC822 dictates that the lines should be network
                # terminated (\r\n), but doing so would result in quite a mix
                # here, since the rest of python's email package use native
                # line endings. As near as I can tell, these get converted
                # back and forth over the course of transmission and delivery,
                # with different levels of normalization. When I was debugging
                # signing issues, I couldn't tell what was wrong becase
                # everthing showed up as native in Thunderbird, for example
                #
                # We could do native to be consistent, but the PGPMime spec
                # requires that the hash be done with the network endings, and
                # while gpg2 can handle trying both, Thunderbird/Enigmail
                # doesn't (which is proper anyway)
                if line.endswith(b'\r'):
                    convlines.append(line[:-1])
                else:
                    convlines.append(line)
            indat = b"\r\n".join(convlines)
            indat = io.BytesIO(indat)
            outdat = io.BytesIO()
            # TODO: Handle case where signing fails (bad passphrase, cancelled
            # operation, etc).
            sigs = ctx.sign(indat, outdat, gpgme.SIG_MODE_DETACH)
            outdat = outdat.getvalue()
            if len(sigs) != 1:
                raise Exception("More than one sig found when only one requested!")
            sig = sigs[0]
            digests = {}
            for sym in dir(gpgme):
                if sym.startswith('MD_'):
                    digests[getattr(gpgme,sym)] = sym[3:]

            if not sig.hash_algo in digests:
                raise Exception("Unknown hashing algorithm used!")
            sigstr = "pgp-" + digests[sig.hash_algo].lower()

            # Create the signing wrapper
            newmsg = email.mime.Multipart.MIMEMultipart("signed", micalg=sigstr, protocol="application/pgp-signature")
            newmsg.attach(m)
            sigpart = email.mime.Base.MIMEBase("application","pgp-signature")
            sigpart.set_payload(outdat)
            newmsg.attach(sigpart)
            # Copy some headers from the signed message to the outer. We'll do
            # to, from, cc, bcc, and subject and a few others for now. Should
            # probably do others. We aren't going to move these; the receiver
            # should be able to use the ones in the signed version as the
            # official "untambered" version; these copies are just for clients
            # that display the outer headers in summaries (like this one right
            # now, or as IMAP servers do searches, etc)
            for key in m.keys():
                if key.lower() in ['to','from','cc','bcc','subject','date','message-id','user-agent']:
                    newmsg[key] = m[key]
            m = newmsg



        #print("Debug: Your message is:")
        #print(m.as_string())
        #return False

        s = subprocess.Popen([
            # TODO: Allow the user to override this somehow
            "sendmail", # Use sendmail to, well, send the mail
            "-bm", # postfix says this is "read mail from stdin and arrange for delivery". Hopefully this is standard across implementations
            "-i", # Don't use '.' as end of input. Hopefully this means we don't have to do dot stuffing.
            # TODO: Support delivery status notification settings? "-N" "failure, delay, success, never"
            ] + recipients,
            stdin=subprocess.PIPE)
        resstr = s.communicate(m.as_string())
        res = s.wait()
        if res == 0:
            return True
        else:
            # Try to save the message?
            ofile = open("%s/dead.letter" % os.environ['HOME'], "a")
            # This is probably not the right format for dead.letter
            ofile.write("From user@localhost\r\n%s\r\n" % (message.as_string()))
            print("Failed to send with error {}, messages {}".format(res, resstr))
            print("Message saved to dead.letter")
            return False


    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_mheader(self, msglist):
        C = self.C
        M = C.connection
        if msglist is None:
            index = C.currentMessage
            if index == 0 or index > self.C.lastMessage:
                print("No applicable messages")
                return
        elif len(msglist) == 0:
            print("No matches")
            return
        else:
            # For now, only support showing one set of headers. This is a
            # debugging command for now anyhow.
            index = msglist[0]
        data = M.fetch(args, '(BODY.PEEK[HEADER])')
        headers = processHeaders(processImapData(data[0][1])[0][1], self.C.settings)
        if "subject" in headers:
            print("Subject:", headers["subject"][-1])
        if "date" in headers:
            print("Date:", headers["date"][-1])
        if "from" in headers:
            print("From:", headers['from'][-1])

        print()
        for key,val in headers.iteritems():
            for i in range(len(val)):
                print("%s[%i]=%s" % (key, i, repr(val)))

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_structure(self, msglist):
        C = self.C
        M = C.connection
        if msglist is None:
            index = C.currentMessage
            if index == 0 or index > self.C.lastMessage:
                print("No applicable messages")
                return
        elif len(msglist) == 0:
            print("No matches")
            return
        else:
            # For now, only support showing one set of headers.
            index = msglist[0]
        data = M.fetch(index, '(BODYSTRUCTURE)')
        #print(data)
        for entry in data:
            #print(entry)
            try:
                # We should get a list of the form (ID, DATA)
                # where DATA is a list of the form ("BODYSTRUCTURE", struct)
                # and where struct is the actual structure
                d = processImapData(entry[1], self.C.settings)
                val = str(entry[0])
                d = d[0]
            except Exception as ev:
                print(ev)
                return
            if d[0] != "BODYSTRUCTURE":
                print("fail?")
                print(d)
                return
            res = unpackStruct(d[1], self.C.settings, tag=val)
            if C.settings.debug.struct:
                print("---")
            def disp(struct):
                extra = ""
                if hasattr(struct, "disposition") and struct.disposition not in [None, "NIL"]:
                    extra += " (%s)" % struct.disposition[0]
                    # TODO: Add filename if present
                # TODO XXX: Preprocess control chars out of all strings before
                # display to terminal!
                print("%s   %s/%s%s" % (struct.tag, struct.type_, struct.subtype, extra))
                if isinstance(struct, structureMessage):
                    extra = ""
                    # Switch to the inner for further processing
                    struct = struct.subs[0]
                    if hasattr(struct, "disposition") and struct.disposition not in [None, "NIL"]:
                        extra += " (%s)" % struct.disposition[0]
                    print("%*s   `-> %s/%s%s" % (len(struct.tag), "", struct.type_, struct.subtype, extra))
                if hasattr(struct, "subs"):
                    for i in struct.subs:
                        disp(i)
            disp(res)

    @shortcut("h")
    @showExceptions
    @needsConnection
    @argsToMessageList
    def do_headers(self, msglist):
        """List headers around the current message. (h for short)"""
        # heirloom-mailx says that it gives 18-message groups, but actually
        # shows about how many will fit on the active terminal.
        # It also says that a '+' argument shows the next 18 message group and
        # '-' shows the previous group. In practice, I've only seen '-' work,
        # and then only if the first message in the group is the active one
        # (otherwise '-' re-lists with the first message selected). I suspect
        # '+' will show the next group *if* the last message in the group is
        # the active one, but I haven't tested.
        C = self.C
        M = C.connection
        if msglist is not None:
            if len(msglist) == 0:
                print("No matches")
                return
            C.currentMessage = msglist[0]
            C.lastList = msglist
        rows = 25 # TODO get from terminal
        start = (C.currentMessage - 1) // rows * rows
        # ^- alternatively, start = C.currentMessage - (C.currentMessage % rows)
        start += 1 # IMAP is 1's based
        last = start + rows - 1
        if msglist:
            # mailx has this behaviour where specifying a location causes the
            # current message to become the first message in the list of
            # headings that contains the requested message. It is a bit
            # confusing, but it is expected by long-time (and medium time)
            # users.
            C.currentMessage = start
        if self.C.virtfolder:
            lastMessage = len(self.C.virtfolder)
        else:
            lastMessage = C.lastMessage
        if last > lastMessage:
            last = lastMessage
        if last == 0:
            print("No applicable messages")
            return
        if self.C.settings.headerstats:
            print("Page {current} of {last}, {rows} per page".format(
                current = (start - 1) // rows,
                last = (lastMessage - 1) // rows,
                rows = rows,
                ))
        ml = MessageList()
        ml.addRange(start, last)
        self.showHeaders(ml)

    def showHeaders(self, messageList):
        """Show headers. Takes virtual folders into consideration."""
        if self.C.virtfolder:
            # Map the message list onto the virtualFolder namespace
            messageList = MessageList([self.C.virtfolder[x-1] for x in messageList.iterate()])
        self.showHeadersNonVF(messageList)

    def showHeadersNonVF(self, messageList):
        """Show headers, given a global message list only"""
        msgset = messageList.imapListStr()
        args = "(ENVELOPE INTERNALDATE FLAGS)"
        if self.C.settings.debug.general:
            print("executing IMAP command FETCH {} {}".format(msgset, args))
        data = self.C.connection.fetch(msgset, args)
        #data = normalizeFetch(data)
        for d in data:
            try:
                d = (d[0], processImapData(d[1], self.C.settings))
            except Exception as ev:
                print("  %s  (error parsing envelope!)" % d[0], ev)
                continue
            envelope = getResultPart("ENVELOPE", d[1][0])
            internaldate = getResultPart("INTERNALDATE", d[1][0])
            flags = getResultPart("FLAGS", d[1][0])
            envelope = Envelope(*envelope)

            # Handle attrs. First pass, only do collapsed form.
            # TODO for second pass, define a class that is initialized with
            # the flags (and possibly other data) with a __format__  method
            # that allows the user to specify groups of characters with
            # priorities. E.g. first char is the NURO set, second is flagged,
            # third is answered, 4+ is whatever is left.
            # TODO: The priorities for flags are guessed by me based on what
            # I'd want to see. I'm guessing there's a POSIX standard for this.
            # Or I could look at mailx source code, but so far I've done
            # neither.
            uflags = map(lambda x: x.upper(), flags)
            if '\FLAGGED' in uflags:
                attr = self.C.settings.attrlist.value[ATTR_FLAGGED]
            elif '\DRAFT' in uflags:
                attr = self.C.settings.attrlist.value[ATTR_DRAFT]
            elif '\ANSWERED' in uflags:
                attr = self.C.settings.attrlist.value[ATTR_ANSWERED]
            elif '\RECENT' in uflags:
                if '\SEEN' in uflags:
                    attr = self.C.settings.attrlist.value[ATTR_NEWREAD]
                else:
                    attr = self.C.settings.attrlist.value[ATTR_NEW]
            else:
                if '\SEEN' in uflags:
                    attr = self.C.settings.attrlist.value[ATTR_OLD]
                else:
                    attr = self.C.settings.attrlist.value[ATTR_UNREAD]

            try:
                gnum = int(d[0])
                if self.C.virtfolder:
                    num = self.C.virtfolder.index(gnum) + 1 if gnum in self.C.virtfolder else ""
                else:
                    num = gnum
                datestr = envelope.date
                if datestr == 'NIL' or datestr == None:
                    datestr = internaldate
                if datestr == None or datestr == "NIL":
                    date = nodate()
                else:
                    try:
                        date = dateutil.parser.parse(datestr)
                    except ValueError:
                        print("Couldn't parse date string", datestr)
                        date = nodate()
                    if date.tzinfo is None:
                        date = date.replace(tzinfo = dateutil.tz.gettz(self.C.settings['defaultTZ'].value))
                    # TODO: Make setting for local or original timezone. Or
                    # perhaps better, make it part of the headline setting so if
                    # the user wants, they can see both.
                    date = date.astimezone(dateutil.tz.tzlocal())
                try:
                    subject = unicode(email.header.make_header(email.header.decode_header(envelope.subject)))
                except:
                    subject = envelope.subject
                this = True if (num == self.C.currentMessage) else False
                froms = [x[0] if not x[0] in [None, 'NIL'] else "%s@%s" % (x[2], x[3]) for x in envelope.from_]
                # Not great, but try to decode the froms fields
                newfroms = []
                for fr in froms:
                    try:
                        newfroms.append(unicode(email.header.make_header(email.header.decode_header(fr))))
                    except:
                        newfroms.append(fr)
                froms = newfroms
                if self.C.virtfolder and len(self.C.settings.headlinevf.value):
                    headline = self.C.settings.headlinevf.value
                else:
                    headline = self.C.settings.headline.value
                print(headline.format(**{
                        'attr': attr,
                        'this': '>' if this else ' ',
                        'num': num,
                        'gnum': gnum,
                        'date': date.strftime("%04Y-%02m-%02d %02H:%02M:%02S"),
                        'subject': subject,
                        'flags': " ".join(flags),
                        'from': froms[0],
                        't': self.C.t,
                    }))
            except Exception as ev:
                print("  %s  (error displaying because %s '%s'. Data follows)" % (d[0], type(ev), ev), repr(d))

    @shortcut("f")
    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_from(self, msglist):
        """List messages (like headers command) for given message list only."""
        # I originally thought 'f' was short for 'find' or something like
        # that. As near as I can guess, earlier implementations of mailx
        # (mail?) took only the straight email address method of selecting
        # message list. Later others were added, but the name was fixed.
        C = self.C
        M = C.connection
        if msglist is None:
            msglist = [self.C.currentMessage]
        elif len(msglist) == 0:
            print("No matches")
            return

        self.showHeaders(MessageList(msglist))
        C.nextMessage = C.currentMessage

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_delete(self, msglist):
        """Mark messages for deletion.

        A deleted message still exists until expunged.
        """
        if msglist is None:
            msglist = [self.C.currentMessage]
        elif len(msglist) == 0:
                print("No matches")
                return
        if self.C.virtfolder:
            msglist = [self.C.virtfolder[x - 1] for x in msglist]
        try:
            self.C.connection.doSimpleCommand("STORE %s +FLAGS (\Deleted)" % ",".join(map(str,msglist)))
            # TODO: either run once per flag, or collect errors to show at
            # end.
        except Exception as ev:
            print("Failed to flag: %s" % ev)

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_undelete(self, msglist):
        """Remove deletion mark for a message.

        Message must not yet be expunged.
        """
        if msglist is None:
            msglist = [self.C.currentMessage]
        elif len(msglist) == 0:
                print("No matches")
                return
        if self.C.virtfolder:
            msglist = [self.C.virtfolder[x - 1] for x in msglist]
        try:
            self.C.connection.doSimpleCommand("STORE %s -FLAGS (\Deleted)" % ",".join(map(str, msglist)))
            # TODO: either run once per flag, or collect errors to show at
            # end.
        except Exception as ev:
            print("Failed to unflag: %s" % ev)

    @showExceptions
    @needsConnection
    def do_expunge(self, args):
        """Flush deleted messages (actually remove them).
        """
        self.C.connection.doSimpleCommand("EXPUNGE")

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_read(self, msglist):
        """Mark messages as being seen (read).
        """
        if msglist is None:
            msglist = [self.C.currentMessage]
        elif len(msglist) == 0:
                print("No matches")
                return
        if self.C.virtfolder:
            msglist = [self.C.virtfolder[x - 1] for x in msglist]
        try:
            self.C.connection.doSimpleCommand("STORE %s +FLAGS (\Seen)" % ",".join(map(str,msglist)))
            # TODO: either run once per flag, or collect errors to show at
            # end.
        except Exception as ev:
            print("Failed to flag: %s" % ev)

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_unread(self, msglist):
        """Remove Seen flag from messages (make unread).
        """
        if msglist is None:
            msglist = [self.C.currentMessage]
        elif len(msglist) == 0:
                print("No matches")
                return
        if self.C.virtfolder:
            msglist = [self.C.virtfolder[x - 1] for x in msglist]
        try:
            self.C.connection.doSimpleCommand("STORE %s -FLAGS (\Seen)" % ",".join(map(str, msglist)))
            # TODO: either run once per flag, or collect errors to show at
            # end.
        except Exception as ev:
            print("Failed to unflag: %s" % ev)

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_flag(self, msglist):
        """Flag messages.

        Marks messages as 'flagged'. Flagged messages show up with the ":f" message specifier.
        This is similar to marking a message as a favorite or "starring" or "pinning" in other systems.
        There is no special meaning to flagged messages; it is just a marking."""
        if msglist is None:
            msglist = [self.C.currentMessage]
        elif len(msglist) == 0:
                print("No matches")
                return
        if self.C.virtfolder:
            msglist = [self.C.virtfolder[x - 1] for x in msglist]
        try:
            self.C.connection.doSimpleCommand("STORE %s +FLAGS (\Flagged)" % ",".join(map(str,msglist)))
            # TODO: either run once per flag, or collect errors to show at
            # end.
        except Exception as ev:
            print("Failed to flag: %s" % ev)

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd
    def do_unflag(self, msglist):
        """Remove Flag from messages.

        see flag command for information. This command undoes that one.
        """
        if msglist is None:
            msglist = [self.C.currentMessage]
        elif len(msglist) == 0:
                print("No matches")
                return
        if self.C.virtfolder:
            msglist = [self.C.virtfolder[x - 1] for x in msglist]
        try:
            self.C.connection.doSimpleCommand("STORE %s -FLAGS (\Flagged)" % ",".join(map(str, msglist)))
            # TODO: either run once per flag, or collect errors to show at
            # end.
        except Exception as ev:
            print("Failed to unflag: %s" % ev)

    @showExceptions
    @needsConnection
    def do_namespace(self, args):
        C = self.C
        M = C.connection
        res,data = M.namespace()
        #print(res)
        try:
            data = processImapData(data[0], self.C.settings)
        except Exception as ev:
            print(ev)
            return
        print("Personal namespaces:")
        for i in data[0]:
            print(i)
        print("Other user's namespaces:")
        for i in data[1]:
            print(i)
        print("Shared namespaces:")
        for i in data[2]:
            print(i)

    def search(self, terms, offset=0, pagesize=10):
        C = self.C
        dbpath = C.dbpath
        db = xapian.Database(dbpath)

        queryparser = xapian.QueryParser()
        queryparser.set_stemmer(xapian.Stem("en"))
        queryparser.set_stemming_strategy(queryparser.STEM_SOME)
        queryparser.add_prefix("subject", "S")
        queryparser.add_prefix("from", "F")
        queryparser.add_prefix("to", "T")
        queryparser.add_prefix("cc", "C")
        queryparser.add_prefix("thread", "I")
        queryparser.add_prefix("ref", "R")
        queryparser.add_prefix("prev", "P")
        queryparser.add_prefix("id", "M")
        queryparser.add_prefix("date", "D")
        queryparser.set_database(db)
        query = queryparser.parse_query(terms, queryparser.FLAG_BOOLEAN | queryparser.FLAG_WILDCARD)
        enquire = xapian.Enquire(db)
        enquire.set_query(query)
        matches = []
        data = []
        for match in enquire.get_mset(offset, pagesize):
            fname = match.document.get_data()
            data.append(fname)
            matches.append(match)

        #print(data[0])
        return data, matches

    @showExceptions
    @optionalNeeds(haveXapian, "Needs python-xapian package installed")
    def do_search(self, args, offset=0, pagesize=10):
        C = self.C
        C.lastsearch = args
        C.lastsearchpos = offset
        C.lastcommand="search"
        data, matches = self.search(args, offset, pagesize)
        for i in range(len(data)):
            fname = data[i]
            match = matches[i]
            fname = fname.split('\r\n')
            fname = filter(lambda x: x.lower().startswith("subject: "), fname)
            if len(fname) == 0:
                fname = "(no subject)"
            else:
                fname = fname[0]
            print(u"%(rank)i (%(perc)3s %(weight)s): #%(docid)3.3i %(title)s" % {
                    'rank': match.rank + 1,
                    'docid': match.docid,
                    'title': fname,
                    'perc': match.percent,
                    'weight': match.weight,
                    }
                    )

    @showExceptions
    def do_unset(self, args):
        """Unset an option.

        For program options, this restores the default value (same as 'set {option}&').
        For user options, removes the option from the system."""
        try:
            opt = self.C.settings[args]
            if isinstance(opt, settings.UserOption):
                self.C.settings.removeOption(args)
                # Remove user option
                pass
            else:
                # Restore default value
                opt.value = opt.default
        except KeyError:
            print("No setting named %s" % args)

    @showExceptions
    def do_set(self, args):
        """Set or get option values

        set                 show options that differ from their default value
        set all             show all options and their values
        set {option}?       show value of given option
        set {option}??      show default and current values of option with description
        set {option}&       reset option to default
        set {option}        assert a boolean option
        set no{option}      deassert a boolean option
        set inv{option}     toggle a boolean option
        set {option}!       equivalent to inv{option}
        set {option}={val}  set numeric, string, or flag list option to val.
                            Numbers are decimal, unles prefixed by 0x for hex
                            or 0 for octal.
        set {option}+={val} Append val to string or flag list. Increment
                            numeric option by val
        set {option}^={val} Prepend val to string or flag list. Multiply
                            numeric option by val
        set {option}-={val} Remove val from string or flag list. Subtract
                            numeric option by val"""
        if args == "" or args == "all":
            bools=[]
            numbers=[]
            strings=[]
            flags=[]
            user=[]
            unknown=[]
            for opt in self.C.settings:
                if isinstance(opt, settings.BoolOption):
                    bools.append(opt)
                elif isinstance(opt, settings.NumericOption):
                    numbers.append(opt)
                elif isinstance(opt, settings.StringOption):
                    strings.append(opt)
                elif isinstance(opt, settings.FlagsOption):
                    flags.append(opt)
                elif isinstance(opt, settings.UserOption):
                    user.append(opt)
                else:
                    unknown.append(opt)
            allsettings = (('boolean', bools),
                    ('numeric', numbers),
                    ('strings', strings),
                    ('flags', flags),
                    ('user', user),
                    ('unknown', unknown),
                    )
        if args == "":
            for name, optset in allsettings:
                if len(optset) == 0:
                    continue
                print("\n--- {} ---".format(name))
                for opt in optset:
                    if opt.value != opt.default:
                        print(unicode(opt))
        elif args == "all":
            for name, optset in allsettings:
                if len(optset) == 0:
                    continue
                print("\n--- {} ---".format(name))
                for opt in optset:
                    print(unicode(opt))
        else:
            sep = args.find('=')
            if sep == -1:
                # No equals, might be boolean or reset to default...
                if args[-2:] == '??':
                    # Print details
                    try:
                        opt = self.C.settings[args[:-2]]
                    except KeyError:
                        print("No setting named %s" % args[:-2])
                    print("Setting: %s" % args[:-2])
                    # TODO: Show type
                    print("Description:")
                    if not opt.doc:
                        print("  (none given)")
                    else:
                        for line in opt.doc.split("\n"):
                            print("   %s" % line)
                    oldval = opt.value
                    opt.value = opt.default
                    print("Default:", opt)
                    opt.value = oldval
                    print("current:", opt)
                elif args[-1] == '?':
                    # Print current value
                    try:
                        print(unicode(self.C.settings[args[:-1]]))
                    except KeyError:
                        print("No setting named %s" % args[:-1])
                elif args[-1] == '&':
                    # Reset to default value
                    try:
                        opt = self.C.settings[args[:-1]]
                    except KeyError:
                        print("No setting named %s" % args[:-1])
                    opt.value = opt.default
                elif args[-1].isalpha():
                    # Must be a boolean
                    print("nye")
                else:
                    print("invalid suffix")
                return
            else:
                if sep == 0:
                    print("invalid")
                    return
                mod = args[sep - 1]
                if mod == '+':
                    # Add/append
                    print("nye")
                    return
                elif mod == '^':
                    # multiply/prepend
                    print("nye")
                    return
                elif mod == '-':
                    # subtract/remove
                    print("nye")
                    return
                elif not mod.isalpha:
                    # Weird, perhaps an operator we'll have in the future?
                    print("invalid operator")
                    return
                else:
                    # Straight assignment
                    key,value = map(lambda x: x.strip(), args.split('=', 1))
                    try:
                        try:
                            self.C.settings[key] = value
                        except KeyError:
                            self.C.settings.addOption(settings.UserOption(key, None))
                            self.C.settings[key] = value
                    except ValueError:
                        print("Invalid value for setting")

    @shortcut("vf")
    @showExceptions
    @needsConnection
    @argsToMessageList
    def do_virtfolder(self, args):
        if args is not None and len(args) == 0:
            print("No Matches")
            args = None
        if args is None:
            if self.C.virtfolder:
                # We were in virtfolder mode, so restore selection
                (self.C.currentMessage, self.C.nextMessage, self.C.prevMessage, self.C.lastList) = self.C.virtfolderSavedSelection
            self.C.virtfolder = None
            self.setPrompt("mailnex> ")
        else:
            self.C.virtfolder = args
            self.setPrompt("mailnex (vf-{})> ".format(len(args)))
            self.C.virtfolderSavedSelection = (self.C.currentMessage, self.C.nextMessage, self.C.prevMessage, self.C.lastList)
            self.C.currentMessage = 1
            self.C.nextMessage = 1
            self.C.prevMessage = None
            self.C.lastList = []

    @showExceptions
    @needsConnection
    def do_z(self, args):
        """Scroll pages of headers

        On its own, go to next page.
        Given a +/-, go to next/previous page.
        Given a number, go to that page number.
        Given +/- and a number, go that many pages forward/back.
        The first page is page 0
        The last page is $
        """
        # TODO: Allow pages to be 1's based indexing, most of the rest of this
        # program is 1's based (the first message is message 1, not 0). Should
        # be an option.

        # First, find out where we are
        rows = 25
        if self.C.virtfolder:
            lastMessage = len(self.C.virtfolder)
        else:
            lastMessage = self.C.lastMessage
        if lastMessage == 0:
            print("No applicable messages")
            return
        start = (self.C.currentMessage - 1) // rows * rows
        # ^- alternatively, start = self.C.currentMessage - (self.C.currentMessage % rows)
        # Next, figure out where we are going
        if not args:
            args = '+1'
        elif args == '+':
            args = '+1'
        elif args == '-':
            args = '-1'
        if args[0] not in ['+', '-']:
            # Absolute page
            if args == '$':
                start = (lastMessage - 1) // rows * rows
            elif not args.isdigit():
                print("unrecognized scrolling command \"%s\"" % args)
                return
            else:
                start = int(args) * rows
        else:
            # Relative page
            if not args[1:].isdigit():
                print("unrecognized scrolling command \"%s\"" % args)
                return
            if args[0] == '+':
                start += int(args[1:]) * rows
            else:
                start -= int(args[1:]) * rows
        if start < 0:
            print("On first page of message")
            start = 0
        if start > lastMessage - 1:
            print("On last page of messages")
            start = (lastMessage - 1) // rows * rows
        start += 1 # IMAP is 1's based
        last = start + rows - 1
        if last > lastMessage:
            last = lastMessage
        self.C.prevMessage = self.C.currentMessage
        self.C.currentMessage = start
        self.C.nextMessage = start
        # NOTE: in mailx, this command does not 'mark any messages', which is
        # to say, doesn't update the lastList (whereas 'headers' with an
        # argument does)
        if self.C.settings.headerstats:
            print("Page {current} of {last}, {rows} per page".format(
                current = (start - 1) // rows,
                last = (lastMessage - 1) // rows,
                rows = rows,
                ))
        ml = MessageList()
        ml.addRange(start, last)
        self.showHeaders(ml)

    @showExceptions
    @needsConnection
    def do_Z(self, args):
        """Like z, but for interesting messages.

        Goes to page with flagged or new (not just unread) messages.

        Unlike z, doesn't take numbers, only nothing, +, or -
        """
        # TODO: Allow it to take numbers? Presumably, this would select the
        # page among the interesting pages, so the first thing to do is get a
        # list of interesting pages, then figure out where we are, then move
        # through the list. We have to do most of that anyway, so I don't know
        # why mailx doesn't do this. Maybe they do a linear search?
        rows = 25
        if self.C.virtfolder:
            lastMessage = len(self.C.virtfolder)
        else:
            lastMessage = self.C.lastMessage
        if lastMessage == 0:
            print("No applicable messages")
            return
        #TODO: Make 'interesting' criteria a user setting
        msgs = map(int,self.C.connection.search('utf-8', '(or FLAGGED NEW)'))
        if self.C.virtfolder:
            msgs = [self.C.virtfolder.index(x) for x in msgs if x in self.C.virtfolder]
        if self.C.settings.debug.general:
            print("{} msgs {}".format(len(msgs), msgs))
        # Observing mailx behavior, if there isn't anything interesting, go to
        # the last page. If we were on the last page, and '-' isn't specified
        # in args, display also the "On last page or messages" message.
        # Likewise, if the current page is after the last interesting page,
        # display the message and go to the last interesting page.
        # This should be able to be generalized by selecting the last message
        # for the target page and performing the normal actions.
        if len(msgs) == 0:
            msgs = [lastMessage]
        # These ignores feel dirty
        ignoreFirstPage = False
        ignoreLastPage = False
        # Current information.
        currentPage = (self.C.currentMessage - 1) // rows
        lastPage = (lastMessage - 1) // rows
        interestingPages = sorted(list(set((x - 1) // rows for x in msgs)))
        if self.C.settings.debug.general:
            print("Current {}\nLast {}\n{} Interesting {}\n".format(currentPage, lastPage, len(interestingPages), tuple((x, x * rows) for x in interestingPages)))
        if currentPage in interestingPages:
            i = interestingPages.index(currentPage)
            addedPage = False
            incDrop = False
        else:
            # insert current page into list so that we can get a list index
            # for it. There is surely a more effecient way to do this, but I'm
            # hoping no one has a huge list of interesting messages anyway.
            for i in range(len(interestingPages)):
                if interestingPages[i] > currentPage:
                    interestingPages.insert(i, currentPage)
                    if (i == 0):
                        ignoreFirstPage = True
                    break
            else:
                # Didn't find it yet, so we must be biggest, append to list
                interestingPages.append(currentPage)
                i += 1
                ignoreLastPage = True
            addedPage = True
            incDrop = True
        if self.C.settings.debug.general:
            print("page index {} of {}".format(i, interestingPages))
        # Now proceed like 'z', but on our list. Also, don't accept anything
        # other than '+' and '-' (for whatever reason). TODO: Allow it via an
        # option? Just allow it anyhow?
        if not args:
            args = "+"
        if args == "+":
            i += 1
            if i >= len(interestingPages) - int(ignoreLastPage):
                print("On last page of messages")
                i = len(interestingPages) - 1 - int(ignoreLastPage)
                incDrop = False
        elif args == "-":
            incDrop = False # don't drop the index for negative going movement
            i -= 1
            if i < 0 + int(ignoreFirstPage):
                print("On first page of messages")
                i = 0 + int(ignoreFirstPage)
                incDrop = addedPage
        else:
            print("Bad argument to Z")
            return
        page = interestingPages[i]
        start = page * rows + 1
        end = (page + 1) * rows
        if end > lastMessage:
            end = lastMessage
        self.C.prevMessage = self.C.currentMessage
        self.C.currentMessage = start
        self.C.nextMessage = start
        if self.C.settings.headerstats:
            print("Page {current} of {last}, Interesting Page {icurrent} of {ilast}, {rows} per page".format(
                current = (start - 1) // rows,
                last = (lastMessage - 1) // rows,
                icurrent = i - int(incDrop),
                ilast = len(interestingPages) - 1 - int(addedPage),
                rows = rows,
                ))
        ml = MessageList()
        ml.addRange(start, end)
        self.showHeaders(ml)

    @showExceptions
    def do_quit(self, args):
        # TODO: Support synchronizing if user setting asks for it. Something like:
        #print("Synchronizing events")
        #for i in self.C.pending:
            #print(" ",i)
            #self.commitaction(i)
        # TODO: Synchronize and quit
        return True

    @showExceptions
    def do_exit(self, args):
        # TODO: Disconnect but not synchronize and quit
        return True

    @showExceptions
    def do_python(self, args):
        if self.C.settings.debug.python:
            res = eval(args)
            if res is not None:
                print(repr(res))

def getOptionsSet():
    options = settings.Options()
    options.addOption(settings.StringOption("addresssearchcmd", "khard email -s", doc="""Command to use for searching addresses
    Used for address completion (e.g. in the ~h command when editing a message).
    Command output is expected to be the address, a tab, the name, and then
    optionally another tab and an identifier (e.g. name of address book or
    resource).
    
    Should work with at least khard and abook (khard email -s) (abook --mutt-query).
    abook currently doesn't work for unknown reasons."""))
    options.addOption(settings.BoolOption("allpartlabels", 0, doc="Show all part separation labels when displaying multi-part messages in print.\nWhen unset, only show separators for sub-messages."))
    options.addOption(settings.FlagsOption("altfrom", [], doc="""Alternative Addresses for user.

    There are 2 common use cases. In one, the user has multiple identities, and you'd like to
    have the From field of a new message match whatever identity received the original message.
    In the other, the user has multiple valid box names but only one identity. This covers the second
    case for now.

    For example, your company might have an alias "sales@example.com" that drops into several boxes,
    and the user is at "foo@example.com". User foo, on replying to a sales email, may want to not
    have sales end up in the to or cc list. Foo would have sales@example.com as an altfrom in this case.

    Case 1 is more challenging to implement (in particular, what to do if more than 1 valid identity
    is mentioned in the original email at the same time?) so it is currently not supported."""))
    # ^-- some notes on the above: Perhaps a more generic mechanism is a
    # mapping of email addresses to from fields. E.G. foo@example.com and
    # foo2@example.com map to "Foo User" <foo@example.com>, but
    # sales@example.com gets mapped to "Sales Enquiries" <sales@example.com>.
    # That's a tad more flexible in terms of identities, but still doesn't
    # answer how to deal with multiple identities. It also doesn't cover other
    # things a user might want to do, such as different autocc per identity.
    # Perhaps what we really need to do is something akin to mailx account
    # settings, but for identities. The combo of an identity's 'from' and
    # 'altfrom' settings cause identity selection, first selection wins
    # default (unless a priority list is given), and the user can change
    # identity during message composition. When changing, we can list detected
    # identities, but allow user to select any identity.
    #
    # This is more complicated, because we'd have to keep track of the
    # addresses that were culled from the to/cc lists from the default
    # identity to be able to put them back on an identity change.

    options.addOption(settings.StringOption("attrlist", "NUROSPMFATK+-J", doc=
        """Character mapping for attribute in headline.

        Characters represent: new, unread but old, new but read, read and old,
        saved, preserved, mboxed, flagged, answered, draft, killed, thread
        start, thread, and junk.

        Currently, we don't support saved, preserved, mboxed, killed, threads,
        or junk.

        Default mailx (presumably POSIX) is "NUROSPMFATK+-J".
        BSD style uses "NU  *HMFATK+-J.", which is read messages aren't
        marked, and saved/preserved get different letters (presumably 'Held'
        instead of 'Preserved')
        """
        ))
    options.addOption(settings.StringOption("autobcc", None, doc="""Automatically populate the Bcc field of new messages (new, reply, etc).
    This is useful to include a copy of sent mail to yourself. For example, a procmail or sieve filter can
    automatically mark messages from yourself as seen and/or put in a 'Sent' folder. Doing this instead of
    saving the message separately saves a transmission to the server.
    The downside to this method is that the message wouldn't include other Bcc for your records."""))
    options.addOption(settings.StringOption("cacertsfile", "/etc/ssl/certs/ca-certificates.crt", doc="""File containing trusted certificate authorities for validating SSL/TLS connections.

    For local imap servers, you can set this to the public cert file of the
    server, for example '/etc/dovecot/dovecot.pem'"""))
    options.addOption(settings.FlagsOption("debug", [], doc="""Enable various debug modes
        * exception - show detailed exceptions instead of short messages
        * general   - show general tidbits during runtime
        * imap      - debug output from imap handler (only applies on new imap
                      connections)
        * parse     - debug from parsers (e.g. IMAP data structures)
        * python    - enable the python command for mucking with program
                      internals live.
        * struct    - debug output from message structure parser
        """))
    options.addOption(settings.StringOption("defaultTZ", "UTC"))
    options.addOption(settings.StringOption("folder", "", doc="Replacement text for folder related commands that start with '+'"))
    # Use the username and hostname of this machine as a (hopefully reasonable)
    # guess for the from line.
    options.addOption(settings.StringOption("from", "%s@%s" % (getpass.getuser(), os.uname()[1]), doc="Value to use for From field of composed email messages"))
    options.addOption(settings.BoolOption("headers", 1, doc="""Show headers on events that update them.

    This includes things like connecting and receiving new messages. This setting covers the default.
    Individual events can be controlled by seting headers_EVENT where EVENT is the event's name.

    Current events:
        * folder    (changing folders)
        * newmsg
    """))
    options.addOption(settings.BoolOption("headers_folder", None))
    options.addOption(settings.BoolOption("headers_newmsg", None))
    options.addOption(settings.BoolOption("headerstats", 0, doc="Show information about the page of message headers displayed in headers and z/Z commands"))
    options.addOption(settings.StringOption("headline", "{this}{attr}{num:4} {date:19} {from:<15.15} {t.italic_blue}{subject:<30.30}{t.normal} |{flags}",
        doc="""Format string for headers lines (e.g. in headers and from commands)

        The format string follows the python format specification.
        Field replacement is done between braces.
        Literal braces must be doubled.

        For example: "this {{is}} the {subject}" where subject is
        "red ball" results in "this {is} the red ball"

        Field width and precision can be used to make the output columnar.
        E.g. {subject:<30.30} pads or truncates the subject to exactly 30
        characters. Instead of '<' you can use '>' to right align or '^' to
        center the subject.

        The following are currently supported fields (subject to change):

            this        - a '>' if the message is the current message, a
                          space otherwise (%> from mailx)
            attr        - Collapsed attribute from attrlist setting ('%a'
                          from mailx) when plain. Use format for more control.
            num         - the message number (%m from mailx)
            date        - the message date - TODO: custom date formatting
                          (roughly %d)
            subject     - the subject of the message (%s)
            flags       - space saparated list of imap flags
            from        - first from entry as name or address (TODO: setting to control that)
                          (%f from mailx)
            t           - terminal attributes. Use as 't.red' to make following
                          text red, or 't.bold' for bold. 't.normal' returns to
                          normal text. Attributes can be combined:
                          't.italic_blue_on_red' makes italic blue text on red
                          background.
        """))
    options.addOption(settings.StringOption("headlinevf", "{this}{attr}{num:2}(g{gnum:4}) {date:19} {from:<15.15} {t.italic_blue}{subject:<30.30}{t.normal} |{flags}", doc="Headline in virtual folder mode. If blank, use normal headline."))
    options.addOption(settings.FlagsOption('headerorder', [
        'Date',
        'Sender',
        'From',
        'To',
        'Cc',
        'Subject',
        ], doc="Prefered order of headers. Headers not mentioned in this list are displayed in the order of the message. Set this to empty in order to not re-order any headers for display."))
    options.addOption(settings.BoolOption('headerorderend', False, doc="Set to display the preferred headers (those mentioned in 'headerorder') at the end of the headers list. Clear to display preferred headers at the start."))
    options.addOption(settings.FlagsOption("ignoredheaders", [
        'content-transfer-encoding',
        'in-reply-to',
        'message-id',
        'mime-version',
        'received',
        'references',
        'content-type',
        ], doc="Setting form of the ignore command"))

    options.addOption(settings.StringOption("PAGER", "internal"))
    options.addOption(settings.StringOption("pgpkey", None, doc="PGP key search string. Can be an email address, UID, or fingerprint as recognized by gnupg. When unset, try to use the from field."))
    options.addOption(settings.BoolOption('showstructure', True, doc="Set to display the structure of the message between the headers and the body when printing."))
    return options

def interact(invokeOpts):
    cmd = Cmd(prompt="mailnex> ", histfile="mailnex_history")
    C = Context()
    C.dbpath = "./maildb1/" # TODO: get from config file or default to XDG data directory
    C.lastcommand=""
    cmd.C = C
    options = getOptionsSet()
    C.settings = options
    postConfFolder = None
    global confFile
    if invokeOpts.config:
        confFile = invokeOpts.config
    if confFile:
        # Walk through the config file
        with open(confFile) as conf:
            print("reading conf from", confFile)
            for lineno, line in enumerate(conf, 1):
                line = line.decode('utf-8')
                if line.strip() == "":
                    # Blank line
                    continue
                elif line.strip().startswith('#'):
                    #print("comment")
                    continue
                elif line.strip().startswith("set "):
                    #print("setting", line.strip()[4:])
                    m = re.match(r' *([^ =]+) *= *(.+)', line[4:])
                    if not m:
                        print("Failed to parse set command in line %i" % lineno)
                        continue
                    key, value = m.groups()
                    try:
                        C.settings[key] = value
                    except KeyError:
                        C.settings.addOption(settings.UserOption(key, None))
                        C.settings[key] = value

                elif line.strip().startswith("folder "):
                    postConfFolder = line.strip()[7:]
                else:
                    print("unknown command in line %i" % lineno)
    C.t = blessings.Terminal()
    if postConfFolder:
        cmd.do_folder(postConfFolder)
    t = pyuv.Timer(cmd.ptkevloop.realloop)
    t.start(cmd.bgcheck, 1, 5)
    try:
        cmd.cmdloop()
    except KeyboardInterrupt:
        cmd.do_exit("")
    except Exception as ev:
        if options.debug.exception:
            raise
        else:
            print("Bailing on exception",ev)

def main():
    import sys
    import argparse
    parser = argparse.ArgumentParser(description="command line mail user agent")
    parser.add_argument('--config', help='custom configuration file')
    args = parser.parse_args()
    interact(args)

if __name__ == "__main__":
    main()
