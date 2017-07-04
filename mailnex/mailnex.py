#!/usr/bin/env python2
# -*- coding: utf-8 -*-

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
# ESEARCH: rfc 4731
# SEARCHRES: rfc 5182. requires ESEARCH capability. Adds "SAVE" to search
#     results option. Adds '$' as a search criteria (meaning the contents of
#     the last save list). Adds "NOTSAVED" response code to indicate that the
#     server refuses to save the results of a particular search.
#     Contains a contradiction: section 2.1 states that a search that returns
#     a BAD response or a search that returns NO but doesn't have a save
#     option MUST NOT change the search result variable, yet section 2.2.4
#     example 5 comments that the failure due to the bad charset in the
#     unsaving search that uses the saved search resets the saved search to
#     empty.
# WITHIN: rfc 5032. Adds capability 'WITHIN'. Adds "YOUNGER" and "OLDER"
#     search criteria, where the time is given as the number of seconds
#     relative to the server's concept of now (current time). Results are
#     based on a message's INTERNALDATE.
# MULTISEARCH: rfc 7377. Allows searches to span multiple mailboxes in a
#     single go. Not supported yet by any server I test against.
#     Adds ESEARCH command (even as ESEARCH capability doesn't), and a bunch
#     of other things.
# SORT and THREAD: rfcs 5256 and 5957. Adds server-side sorting and threading
#     for online clients (not so useful for offline clients). Adds
#     capabilities starting with "SORT" and "THREAD=". Requires I18NLEVEL=1
#     capability, desires I18NLEVEL=2 or better.
#     Adds "SORT" and "THREAD" commands
#     This RFC also includes procedures for calculating the threading of
#     messages based on subject line, and requires disconnected clients to use
#     (at least parts of) the same algorithm, so this is what we will use, at
#     least by default. It even has instructions on what to do when multiple
#     messages in a box have the same message-id (but not handling message-ids
#     of sub-messages, that is, attached emails aka forward-as-attachment).
#     I'm also not sure I agree with their rules for references threading
#     model. In particular, 'references' headers of earlier messages have
#     complete priority over later messages. The reasoning is that the header
#     may be truncated by a MUA (in practice, I've also seen it truncated by
#     MTAs trying to fix MUA bugs), however it feels like this provides an
#     oportunity for malicious thread info corruption. An attacker would
#     probably have to be lucky to guess message ids to corrupt the list, but
#     could also send a later message with the hope that the message order on
#     the server will be adjusted by the user moving messages out of INBOX
#     into other boxes out of order. Ideally, this is corrected in step B, so
#     this probably isn't a real issue. I should delete this comment. FIXME
#     Some of this is also called out in the Security Considerations section.
#
#     The threading algorithms also sort the messages the way most email
#     clients do that I find infuriating, which is by date of the thread
#     leader. I almost always want the sorting to be by the most recent child,
#     like KMail could do or gmail does. Otherwise, it can be difficult in
#     many situations to notice that an old thread has come alive again.
#
#     Standard sorting criteria (rfc5256):
#       * ARRIVAL
#       * CC
#       * DATE
#       * FROM
#       * SIZE
#       * SUBJECT
#       * TO
#     Extended sorting criteria
#       * DISPLAYFROM (rfc5957)
#       * DISPLAYTO (rfc5957)
#
#     Standard thread algos (rfc5256)
#       * ORDEREDSUBJECT
#       * REFERENCES
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
from . import printfStyle
# Date handler
import dateutil.parser
# Color and other terminal stuffs
import blessings
# Ability to launch external viewers
import mailcap
# Interpret mailcap command strings and other similar lines as shells do
# (quoting arguments and such)
import shlex
# Other
import tempfile
import pyuv
import time
from . import settings
import subprocess
import string
import shutil
from cStringIO import StringIO
import io
from io import BytesIO
try:
    import gpgme
    haveGpgme = True
except ImportError:
    haveGpgme = False
import magic
from prompt_toolkit.completion import Completer, Completion

confFile = xdg.BaseDirectory.load_first_config("linsam.homelinux.com","mailnex","mailnex.conf")
cacheDir = xdg.BaseDirectory.save_cache_path("linsam.homelinux.com","mailnex")
defDbFile = os.sep.join((cacheDir, "searchdb"))
histFile = os.sep.join((cacheDir, "histfile"))

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
ATTR_DELETED = 14

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
        # Extra info on messages in virtual folder. Used by things like thread
        # views. TODO: Ought to be collapsed into the virtfolder list
        # directly, but that will require some refactoring.
        self.virtfolderExtra = None
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
        # Message cache. Currently the key is the submessage identifier, and
        # the value is the text content. E.G. mime headers for message 123
        # part 4 would be key '123.4.MIME'
        self.cache = {}
        # Last IMAP criteria search. Used when specifying '()' as a message
        # list
        self.lastCriSearch = "()"
        # Some parts of the program might put other stuff in here. For
        # example, the exception trace wrapper.

class MyGenerator(email.generator.Generator):
    """Generator like the default email library generator, except without re-packing headers when signed.

    The default generator treats multipart/signed specially: it keeps the
    headers unmodified by disabling header wrapping when flattening.

    Unfortunately, we calculate the signature before packing into a
    multipart/signed, so the no-wrap doesn't occur; our signature is thus
    calculated on wrapped headers, and disabling wrapping to make things
    unmodified actually results in modifying them and, thus, invalidating our
    signatures.
    """
    def _handle_multipart_signed(self, msg):
        return self._handle_multipart(msg)

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

class threadMessage(object):
    __slots__ = ['mid', 'mseq', 'muid', 'children', 'parent', 'sortKey']
    def __init__(self, mid, mseq=-1, uid=-1):
        super(threadMessage, self).__init__()
        # Message-id. Unique per message version. A string
        self.mid = mid
        # Message sequence number. Message sequence numbers are contiguous
        # starting with 1, are per-mailbox, and can change during run-time, if
        # for example a message in the middle gets expunged.
        self.mseq = mseq
        # Message UID (unique identifier). UIDs are not contiguous, are
        # per-mailbox, and are stable across sessions, so long as the
        # UIDVALIDITY value doesn't change between sessions.
        self.muid = uid
        # Link to parent message. There is no current mechanism for a message
        # to unambiguously have more than one parent.
        self.parent = None
        # sortKey is assigned at the sorting phase, currently placing the
        # message globally against all other messages.
        self.sortKey = None
        # List of other threadMessages that consider this to be their parent
        self.children = []


def getResultPart(part, data):
    """Retrieve part's value from data.

    This is for flat arrays that are really key-value lists.
    e.g.
        [key1, val1, key2, val2, key3, val3...]

    Currently, this is a linear search, case insensitive.

    If values will be looked up often, using dictifyList with
    "preserveValue=True" may be more performant.
    """
    part = part.lower()
    for i in range(0,len(data),2):
        if data[i].lower() == part:
            return data[i + 1]
    # Raising an exception because, after an IMAP request, not having the key
    # you asked for is an exceptional case, and there isn't a good return
    # value that also couldn't be in the array itself without doing something
    # weird like returning a class or something.
    raise Exception("Part %s not found" % part)

def attachFile(attachList, filename, pos=None, replace=False):
    """Check a path and add it to the attachment list
    If pos is given and replace is False, insert attachment at given position.
    If pos is given and replace is True, replace the attachment at the given position.
    """
    if pos is not None:
        if pos < 1 or pos > len(attachList):
            print("Bad position. {} not between 1 and {}".format(pos, len(attachList)))
            return
        # Adjust from human position to index
        pos -= 1
    try:
        st = os.stat(filename)
    except OSError as err:
        import errno
        # Can't read it. Is it because it doesn't exist?
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
    if pos is None:
        attachList.append(filename)
    elif replace == False:
        attachList.insert(pos, filename)
    else:
        attachList[pos] = filename

def sanatize(data, condense=True, replace=False):
    """Remove control characters and (optionally) condense space.

    Most importantly, this removes escape and mode switching characters.

    This makes a string suitable for writing to a terminal, for example,
    without fear of inline codes corrupting the view.

    Condensing the space is also useful for preventing tabs and newlines from
    disrupting something expected to fit on a single line.
    """

    # TODO: these ought to be calculated just once during module load, not
    # every time this function is called...

    # ASCII control chars are 0-0x1f and 0x7f, called C0
    c0 = range(0, 0x20) + [0x7f]
    # ISO 6429 has additional, C1
    c1 = range(0x80,0xa0)
    # However, we'll leave gaps for whitespace generating chars, which will be
    # handled by the condense option
    # We will leave BS, Del, VT, and FF for regular removal here. VT and FF
    # could arguably go either way (strip or condense).
    c0.remove(0x9) # HTAB (horizontal tab)
    c0.remove(0xa) # LF (line feed or Unix EOL (end of line)
    c0.remove(0xd) # CR (carriage return. Part of Windows and Network new line (CR-LF))
    c1.remove(0x85) # NEL (next line)
    stripChars = map(unichr, c0 + c1)
    condenseChars = map(unichr, [0x9, 0xa, 0xd, 0x20, 0x85])

    res = []
    lastChar = None
    for i in data:
        if condense and i in condenseChars:
            if lastChar != ' ':
                res.append(' ')
                lastChar = ' '
            continue
        if i in stripChars:
            if replace:
                # Two common(ish) styles. We can do caret notation, which is
                # most common in terminal programs, or we can use Unicode
                # Control Pictures, which is more common in GUIS like in web
                # browsers. The latter assumes a unicode terminal and font
                # support, but that has gotten somewhat more common. Still, a
                # user toggle would probably be nice.
                # The Unicode Control Pictures block is 0x2400 to 0x243f. It
                # includes characters for 0x00 to 0x1f, 0x7f, and a couple of
                # C1 characters.
                res.append('^')
                # Note: in ASCII and Unicode, '@' comes just before 'A', so
                # calling it out separately is wasteful, unless we aren't
                # assuming character encodings, but we are assuming, otherwise
                # we wouldn't be hardcoding the range of control characters.
                if ord(i) == 0:
                    res.append('@')
                elif ord(i) < 0x20:
                    res.append(unichr(ord('A') + ord(i) - 1))
                else:
                    # Must be a high (C1) character. This display is
                    # non-standard. We'll use '^^A' to refer to the first.
                    res.append('^')
                    res.append(unichr(ord('A') + ord(i) - 0x80))
            laseChar = None
            continue
        res.append(i)
        lastChar = i
    return "".join(res)

def normalizePath(currentPath):
    if currentPath.startswith("~{}".format(os.path.sep)):
        if 'HOME' in os.environ:
            currentPath='{}{}{}'.format(os.environ['HOME'], os.path.sep, currentPath[2:])
    return currentPath

def pathCompleter(currentPath):
    # TODO: Cache the results from any given directory; it is very
    # inefficient to recalculate this for every character the user
    # types.
    # Alternatively, only complete one request (e.g. user hits
    # 'tab')
    currentPath = normalizePath(currentPath)
    dirname = os.path.dirname(currentPath)
    filename = os.path.basename(currentPath)
    try:
        paths = os.listdir(dirname) if dirname else os.listdir(".")
    except OSError:
        # Typically, file not found. Whatever the error, just
        # don't do completions.
        raise StopIteration
    # Remove paths that don't start with our query
    paths = filter(lambda x: x.startswith(filename), paths)
    paths.sort()
    for i in paths:
        extra=None
        try:
            if os.path.isdir(os.path.sep.join([dirname if dirname else '.',i])):
                extra = os.path.sep
        except OSError:
            # Ignore file-not-found and such. We shouldn't
            # actually get here typically (we got a list from
            # earlier), but things happen, like an entry being
            # removed after we got the list but before we checked
            # for it being a directory.
            # TODO: Since the error is usually that we cannot
            # check the entry (as in, it doesn't exist), perhaps
            # we should skip it in the listing?
            pass
        yield cmdprompt.prompt_toolkit.completion.Completion(i, start_position=-len(filename), display_meta=extra)

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
    def __nonzero__(self):
        return len(self.ranges) != 0
    # Python 3 compat
    __bool__ = __nonzero__
    def add(self, i):
        """Add a message ID to the message list"""
        # TODO: This could probably be more effecient. For example, we could
        # keep it sorted and avoid the post-sort call
        for index in range(len(self.ranges)):
            r = self.ranges[index]
            if r[0] <= i and i <= r[1]:
                return
            if i == r[0] - 1:
                self.ranges[index] = (i, r[1])
                return
            elif i == r[1] + 1:
                self.ranges[index] = (r[0], i)
                return
        self.ranges.append((i,i))
        self.ranges.sort()
        for index in range(len(self.ranges)-1):
            r1 = self.ranges[index]
            r2 = self.ranges[index + 1]
            if (r1[1] + 1 == r2[0]):
                # Adjacent ranges; merge them
                self.ranges[index] = (r1[0], r2[1])
                del self.ranges[index + 1]
                return
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
    __iter__ = iterate


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
    def __init__(self, tag, subtype, parameters, disposition=None, language=None, location=None, *newargs):
        """Create a multipart entry.

        @param tag name of this part (e.g. 1.5.3 for the third part of the fifth part of the first part)
        @param subtype variant of multipart (e.g. mixed, signed, alternative)
        (others as per IMAP spec)
        """
        # TODO: Log if we got newargs? The spec says future versions may have
        # more positional arguments, and we must handle but ignore them.
        structureRoot.__init__(self, tag, "multipart", subtype)
        # parameters
        if parameters and not isinstance(parameters, dict):
            self.parameters = dictifyList(parameters)
        else:
            self.parameters = parameters
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
        # default extra positional parts to be None, but fill if available
        self.md5, self.disposition, self.language, self.location = (args + (None,)*4)[:4]

class structureMessage(structureRoot):
    def __init__(self, tag, type_, subtype, attrs, bid, description, encoding, size, envelope, subStruct, lines, md5, disposition, language, location):
        structureRoot.__init__(self, tag, type_, subtype)
        if attrs:
            self.attrs = dictifyList(attrs)
        else:
            self.attrs = attrs
        self.bid = bid # Body ID
        self.description = description
        self.encoding = encoding
        self.size = size # octets of encoded message
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

def unpackStructM(data, options, depth=1, tag="", predesc=""):
    """Recursively unpack the structure of a message (by walking through a message)

    @param data email message object
    @depth starting depth (may be used for indenting output, or for debugging)
    @tag current identifier of parent. For the first call, this should be the message ID. It will be dot separated for sub parts.
    @predesc prefix description. Mostly used internally for when we hit a message/rfc822.
    @return array of parts, which may contain array of parts.
    """
    extra = ""
    this = None
    if data.is_multipart():
        # First pass, ignore most of the elements
        this = structureMultipart(tag, data.get_content_subtype(), dict(data.get_params()))
        r = data.get_payload()
        j = 1
        for submessage in r:
            this.addSub(unpackStructM(submessage, options, depth + 1, tag + '.' + str(j)))
            j += 1
    else:
        # TODO If we are message/rfc822, then we have further subdivision!
        # For now, just end it here.
        this = structureLeaf(tag, data.get_content_maintype(), data.get_content_subtype(), None, None, None, None, None, None)
        if 'cache' in options:
            headers = b"\r\n".join(map(lambda x: b"{}: {}".format(*x), data.items()))
            headers += b"\r\n\r\n"
            if '.' in tag:
                index,part = tag.split('.', 1)
            else:
                # TODO: should never reach here. Assert instead?
                index = tag
                part = ""
            options['cache']["{}.BODY[{}.MIME]".format(index,part)] = headers
            options['cache']["{}.BODY[{}]".format(index,part)] = data.get_payload()
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

def dictifyList(lst, preserveValue=False):
    """Convert a flat list of key-value into a dictionary.

    E.g
        [key1, val1, key2, val2, key3, val3...]
    becomes:
        {key1: val1, key2: val2, key3: val3...}

    If the array is small and a value is only pulled once or twice, using
    getResultPart may be more performant.
    """
    # convert to list of key,val pairs, then to dictionary
    # See http://stackoverflow.com/a/1625023/4504704 (answer to
    # http://stackoverflow.com/questions/1624883/alternative-way-to-split-a-list-into-groups-of-n)
    # Note: IMAP elements can be None, so a key-value list of no entries might
    # be an empty array or None (for example, the parameters list of the
    # disposition header from a FETCH BODYSTRUCTURE. The disposition might be
    # None, in which case this doesn't even get called, or it might have, say,
    # a disposition of 'inline', but no parameters. We'll get called for the
    # empty parameters, and the calling code may then look to see if there was
    # a 'filename' parameter.
    # TODO: Probably possible that there could be duplicate keys. Probably
    # need a custom dictionary to handle that. Might also be handy to store
    # the original case of the key and value.
    if lst is None:
        # We didn't have a list, so return an empty dictionary (no key/value
        # pairs)
        return {}
    if not preserveValue:
        return dict(zip(*(iter(map(lambda x: x.lower(),lst)),)*2))
    i = [1]
    def lowerother(val):
        i[0] += 1
        if i[0] % 2 == 0:
            return val.lower()
        return val
    return dict(zip(*(iter(map(lambda x: lowerother(x),lst)),)*2))


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

    This implementation is currently incomplete. It accepts quotes anywhere.
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
        if c == '\\' and inquote:
            # Backslash *should* only precede a doublequote or a backslash,
            # but we'll let it escape anything
            pos += 1
            curtext.append(text[pos])
            continue
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

#password = getPassword("smtp", user, host, port)
def getPassword(settings, protocol, user, host, port):
    """Attempt to lookup a password for plain/login authentication.

    Will walk through agent-shell-lookup settings, keyrings, and finally
    prompting.

    Returns a 3-tuple of (method, savable, password) where method is a string
    about how the password was obtained (agent, keyring, or interactive
    currently defined, others may exist later), savable is a bool indicating
    if the caller should offer to save the password into a keyring if logging
    in is successful (typically only True if the password was entered
    interactively, and at some point, based on the user not indicating that
    they don't want to be prompted), and password is the string of the actual
    password.
    """
    agentCmd = None
    # NOTE: When updating how passwords or other auth is looked
    # up, don't forget to update help_authentication()
    lookups = [
            "agent-shell-lookup-{}/{}@{}:{}".format(protocol, user, host, port),
            "agent-shell-lookup-{}/{}@{}".format(protocol, user, host),
            "agent-shell-lookup-{}@{}".format(user, host),
            "agent-shell-lookup-{}".format(host),
            "agent-shell-lookup",
            ]
    for l in lookups:
        if settings.debug.general:
            print("Checking for", l)
        if l in settings:
            agentCmd = getattr(settings, l).value
            if settings.debug.general:
                print(" Found it", agentCmd)
            break
    cantSave = False
    if agentCmd and agentCmd != "":
        cmdarr = ["/bin/sh", "-c", agentCmd]
        if settings.debug.general:
            print(" Running", cmdarr)
        s = subprocess.Popen(cmdarr, stdout=subprocess.PIPE)
        pass_ = s.stdout.read(4096)
        s.stdout.close()
        res = s.wait()
        if res != 0:
            print(" agent-shell-lookup for this account did not succeed.")
            pass_ = None
        elif len(pass_) > 4095:
            print(" Password command gave back 4k or more characters; assuming not valid and moving on")
            pass_ = None
        else:
            if pass_.endswith('\n'):
                # Strip off EOL, it isn't part of the password
                # itself. Should tell users if their passsword
                # actually ends with LF, to put an extra LF in the
                # output.
                pass_ = pass_[:-1]
            #print("Password",pass_)
            method = "agent"
    else:
        if not settings.usekeyring:
            cantSave = True
            pass_ = None
        else:
            try:
                pass_ =  keyring.get_password("%s://%s" % (protocol, host), user)
                method = "keyring"
            except RuntimeError:
                pass_ = None
                print("Info: no password managers found; cannot save your password for automatic login")
                cantSave = True
    prompt_to_save = False
    if not pass_:
        pass_ = getpass.getpass()
        if not cantSave:
            prompt_to_save = True
        method = "interactive"
    return method, prompt_to_save, pass_

def normalizeSize(value, bi=False):
    """Given an integer value, normalize it to an SI prefix magnatude, and return as a float,string tuple

    e.g. normalizeSize(22867) -> (22.867, "k")
         normalizeSize(4096) -> (4.096, "k")
         normalizeSize(4096, bi=True) -> (4.0, "k")
    """
    mult = 1024 if bi else 1000
    pos = 0
    # Unit Prefixes
    # See http://physics.nist.gov/cuu/Units/prefixes.html and
    # http://physics.nist.gov/cuu/Units/binary.html
    # kilo, Mega, Giga, Tera, Peta, Exa, Zetta, Yotta
    # We'll skip 'deca' and 'hecto'; those are just about never seen when
    # discussing things like bytes.
    # Note: In the binary case, the IEC apparently didn't adopt deca and
    # hecto, nor did they adopt Zetta (zebi) nor Yotta (yobi). We'll support
    # these anyway; I'm sure it will be understood, and I doubt we'll see
    # sizes that big in the near future anyway.
    units = ('', 'k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y')
    value = float(value)
    res = value
    div = 1
    while pos + 1 < len(units) and res > mult:
        pos += 1
        div *= mult
        res = value / div
    unit = units[pos]
    if bi and unit:
        unit += 'i'
    return (res, unit)

def sigresToString(ctx, sig):
    if sig.summary & gpgme.SIGSUM_VALID:
        sigres = "\033[32mvalid\033[0m"
    else:
        sigres = "\033[31mbad\033[0m"
        sigsum = []
        if sig.summary & gpgme.SIGSUM_KEY_REVOKED:
            sigsum.append("key revoked")
        if sig.summary & gpgme.SIGSUM_KEY_EXPIRED:
            sigsum.append("key expired")
        if sig.summary & gpgme.SIGSUM_SIG_EXPIRED:
            sigsum.append("sig expired")
        if sig.summary & gpgme.SIGSUM_KEY_MISSING:
            sigsum.append("key missing")
        if sig.summary & gpgme.SIGSUM_CRL_MISSING:
            sigsum.append("crl missing")
        if sig.summary & gpgme.SIGSUM_CRL_TOO_OLD:
            sigsum.append("crl too old")
        if sig.summary & gpgme.SIGSUM_BAD_POLICY:
            sigsum.append("bad policy")
        if sig.summary & gpgme.SIGSUM_SYS_ERROR:
            sigsum.append("sys error")
        knownBits = gpgme.SIGSUM_VALID | gpgme.SIGSUM_GREEN | gpgme.SIGSUM_RED | gpgme.SIGSUM_KEY_REVOKED | gpgme.SIGSUM_KEY_EXPIRED | gpgme.SIGSUM_SIG_EXPIRED | gpgme.SIGSUM_KEY_MISSING | gpgme.SIGSUM_CRL_MISSING | gpgme.SIGSUM_CRL_TOO_OLD | gpgme.SIGSUM_BAD_POLICY | gpgme.SIGSUM_SYS_ERROR
        remainBits = sig.summary & ~knownBits
        if remainBits:
            sigsum.append("%x" % remainBits)
        if len(sigsum):
            sigres += "(%s)" % ", ".join(sigsum)
    if sig.validity == gpgme.VALIDITY_UNKNOWN:
        sigres += "(?)"
    if sig.validity == gpgme.VALIDITY_UNDEFINED:
        sigres += "(q)"
    if sig.validity == gpgme.VALIDITY_NEVER:
        sigres += "(\033[31mn\033[0m)"
    if sig.validity == gpgme.VALIDITY_MARGINAL:
        sigres += "(\033[33mm\033[0m)"
    if sig.validity == gpgme.VALIDITY_FULL:
        sigres += "(\033[32mf\033[0m)"
    if sig.validity == gpgme.VALIDITY_ULTIMATE:
        sigres += "(\033[34mu\033[0m)"
    keys = []
    for k in ctx.keylist(sig.fpr, True):
        keys.append(k)
    if len(keys) != 1:
        # TODO: What if we get multiple matches for
        # the FPR? For now, we'll show the FPR raw if
        # we can't find it or find it isn't unique
        sigres += " from %s" % sig.fpr
    else:
        key = keys[0]
        # TODO: Some kind of check between from and
        # the sig. Some notes:
        #   * The message isn't strictly bad if the
        #     sender or froms don't match, but it is
        #     possibly odd.
        #   * The header check /should/ be against the
        #     contained message. E.g., if this sig is
        #     in a forward-as-attachment message, then
        #     the sender is likely NOT the signer, but
        #     the original sender of the inner message
        #     SHOULD be the signer.
        #   * While we are at it, we should check
        #     signed headers against unsigned headers,
        #     or at least some of the relevant ones.
        #     For example, enigmail will copy the
        #     recipients and originators headers, as
        #     well as subject and date, into the
        #     signed portion to allow verification
        #     that those headers weren't tampered.
        sigres += " from %s %s" % (sig.fpr[-8:], key.uids[0].uid)
    return sigres

def enumerateKeys(keys):
    """Display a list of keys as for user selection."""
    for i, key in enumerate(keys, 1):
        print("{}: {}{}{}{}{}{}{}{}{} {} {}_{} {}".format(
            i,
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

class Cmd(cmdprompt.CmdPrompt):
    def __init__(self, *args, **kwargs):
        cmdprompt.CmdPrompt.__init__(self, *args, **kwargs)
        self.__dict__[b'do_='] = self.equals
        # Allow the equals symbol to show up in commands, which allows a
        # command to be named '='. Alternatively, we could handle it in the
        # default handler.
        self.identchars += "="
    def help_hidden_commands(self):
        return """The following are hidden commands:

            -> print next message (usually 'print +' except the first time)
        h   -> headers
        p   -> print
        q   -> quit
        x   -> exit
        """
    def help_optional_packages(self):
        # TODO: format to the user's terminal
        return """Some commands require optional packages to be installed to enable
        functionality. For example, indexing and searching the index require
        that Xapian be installed with python bindings. Similarly,
        cryptographically signing email requires python bindings for gpgme.

        These are often unavailable via pip install, and must therefore either
        be installed by hand or come from your system's package manager. As
        such, if running mailnex from a virtual-env, the virtual-env needs
        to be set to have access to the system packages (using the
        '--system-site-packages' flag of the virtualenv tool). See the file
        'INSTALL' that came with this program for more details.
        """
    def help_authentication(self):
        # TODO: format to the user's terminal
        return """mailnex looks for passwords from the following sources
        in the following order:
         * option 'agent-shell-lookup-PROTOCOL/USER@HOST:PORT'
         * option 'agent-shell-lookup-PROTOCOL/USER@HOST'
         * option 'agent-shell-lookup-USER@HOST'
         * option 'agent-shell-lookup-HOST'
         * option 'agent-shell-lookup'
         * user keyring (e.g. seahorse or kwallet. see 'pydoc keyring.backends' for a list)
         * prompt for input

        Note that, in all cases, the password is kept in RAM only
        long enough to send to the server. If the connection is
        lost, a new password will have to be sourced to reconnect.

        Note that mailnex tries to prevent the password from traversing
        the network in the clear by requiring a TLS connection. Note that
        this doesn't prevent the remote side from subsequently abusing the
        password, nor does it protect against local retrieval (e.g. memory
        dumping this program while the raw data is still in memory)
        """
    def help_message_specifiers(self):
        return """mailnex supports most of the standard mailx message list specifiers with some enhancements.

        Plain number        message with that number.       e.g.  5
        Range               messages in range inclusive.    e.g.  5-10
        :u                  unread messages
        :f                  flagged messages
        +                   next message(*)
        +N                  Nth next message                e.g.  +3
        -                   previous message
        -N                  Nth previous message
        `                   last message list
        ^                   first message
        $                   last message
        (CRI)               IMAP search critera CRI         e.g. (to bob)
                e.g. (unseen since 5-jan-2017 to bob subject "meeting day")

        (*) This is different than the 'next' command; the '+' always selects
        the message following the current message.
        """

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

            :n      All new messages (TODO)
            :o      All old messages (not read or new) (TODO)
            :u      All unread messages
            :d      All deleted messages (used in undelete command) (TODO)
            :r      All read messages (TODO)
            :f      All flagged messages
            :a      All answered messages (replied) (TODO)
            :t      All messages marked as draft (TODO)
            :k      All killed messages (TODO)
            :j      All junk messages (TODO)
            .       The current message
            ;       The previously current message (using ; over and over
                    bounces between the last 2 messages) (TODO)
            ,       The parent of the current message (looking for the message
                    with the Message-Id matching the current In-Reply-To or
                    last References entry (TODO)
            -       (hyphen) The next previous undeleted message for regular
                    commands or the next previus deleted message for undelete.
                    (Currently, selects previous message, deleted or not; this is different from mailx)
            +       The next undeleted message, or the next deleted message
                    for undelete.
                    (Currently, selects next message, deleted or not; this is different from mailx)
            ^       The first undeleted message, or first deleted for the
                    undelete command.
                    (Currently, selects first message, deleted or not; this is different from mailx)
            $       The last message
            &x      The message 'x' and all messages from the thread that
                    begins at it (in thread mode only). X defaults to '.' if
                    not given. (TODO)
            *       (asterisk) All messages. (TODO)
            `       (back tick) All messages listed in the previous command
            /str    All messages with 'str' in the subject field, case
                    insensitive (ASCII). If empty, use last search "of that
                    type", whatever that means (TODO)
            addr    Messages from address 'addr', normally case sensitive for
                    complete email address. Some variables change the
                    processing. (TODO)
            (cri)   Messages matching an IMAP-style SEARCH criterion.
                    Performed locally if necesary, even when not on IMAP
                    connections.  if 'cri' is empty, reuse last search.

        (cri) is a complicated beasty, see the full documentation for details (from mailx until we have our own).
        As a simplification, we might just pass these literally to the IMAP server.

        We'll do the following extensions:

            gx      The 'x'th message by sequence number. This equivalent to
                    just giving 'x' in a normal view, but gives you the real
                    mailbox sequence numbered message when in a virtual folder
                    view. The 'g' stands for 'global', meaning outside of any
                    view of the mailbox. (TODO)
            ux      The message with 'x' as a UID. Probably not very useful (TODO)
            px      The 'x'th message on this page. Caution: the association
                    of a px number to a message can change when the terminal
                    resizes. (TODO)
            $x      the 'x'th to the last message. For example, if there are
                    500 messages in a box, '$10' would refer to message 490.
                    Can be used as part of a range. For example, '$10-$' gives
                    the last 11 messages (490 to 500 inclusive in the above
                    example).
            n--x    The result of 'n' subtracted by 'x'. 'x' must resolve to a
                    single number. 'n' may be a message list, in which case
                    the result is a message list where each member of 'n' was
                    subtracted by 'x'. 'n' defaults to the current message (if
                    not given).
                    e.g. '$--10' is equivalent to '-10'. '.--1' is the
                    previous message (equivalent to '-'). '.--2' is the
                    message before the previous message. (TODO)
            n++x    The result of 'x' added to 'n'. 'x' must resolve to a
                    single number. 'n' may be a message list. (TODO)


        Notes and brainstorming:

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
        # Tokenize and walk through the args
        args = args.strip().split()
        cri = []
        messages = MessageList()
        # TODO: Will probably need to make this a formal grammer and write a
        # proper parser for it
        incri = False
        def doCriSearch(messages, cri):
            # Support IMAP search by being a passthrough for IMAP SEARCH
            # command.
            if cri == "()":
                cri = self.C.lastCriSearch
            if self.C.virtfolder:
                r = MessageList(self.C.virtfolder).imapListStr()
                # Create a sub criteria search that is limited by the message
                # list
                subcri = '({} {})'.format(r, cri[1:-1])
            else:
                # Use original criteria
                subcri = cri
            data = self.C.connection.search("UTF-8", subcri)
            # Store original criteria for future recall
            self.C.lastCriSearch = cri
            if self.C.settings.debug.general:
                print(data)
            data = map(int, data)
            if self.C.virtfolder:
                # Convert back to virtual indices
                data = map(lambda x: self.C.virtfolder.index(x) + 1, data)
            map(messages.add, data)
        for i in args:
            if i.startswith('('):
                cri = [i]
                incri = True
                if i.endswith(')'):
                    # Whole cri search in one token
                    doCriSearch(messages, " ".join(cri))
                    incri = False
                continue
            if i.endswith(')'):
                if incri:
                    cri.append(i)
                    doCriSearch(messages, " ".join(cri))
                    incri = False
                else:
                    print("Parse error: close parenthesis without open")
                    return []
                continue
            if incri:
                cri.append(i)
                continue
            def parseLow(i):
                messages = MessageList()
                if i.startswith(":"):
                    i = i[1:]
                    if i == 'u':
                        if self.C.virtfolder:
                            r = MessageList(self.C.virtfolder).imapListStr() + " "
                        else:
                            r = ""
                        data = self.C.connection.search("UTF-8", "{}unseen".format(r))
                        if self.C.settings.debug.general:
                            print(data)
                        data = map(int, data)
                        if self.C.virtfolder:
                            # Convert back to virtual indices
                            data = map(lambda x: self.C.virtfolder.index(x) + 1, data)
                        map(messages.add, data)
                    elif i == 'f':
                        if self.C.virtfolder:
                            r = MessageList(self.C.virtfolder).imapListStr() + " "
                        else:
                            r = ""
                        data = self.C.connection.search("UTF-8", "{}flagged".format(r))
                        if self.C.settings.debug.general:
                            print(data)
                        data = map(int, data)
                        if self.C.virtfolder:
                            # Convert back to virtual indices
                            data = map(lambda x: self.C.virtfolder.index(x) + 1, data)
                        map(messages.add, data)
                    else:
                        print("Error: Unrecognized message class :{}".format(i))
                        return []
                elif i.isdigit():
                    messages.add(int(i))
                elif i == '.':
                    messages.add(self.C.currentMessage)
                elif i == '`':
                    # TODO: print "No previously marked messages" if the list is empty
                    map(messages.add, self.C.lastList)
                elif i == '^':
                    messages.add(1)
                elif i == '$':
                    messages.add(self.C.lastMessage)
                elif i[0] == '$' and i[1:].isdigit():
                    messages.add(self.C.lastMessage - int(i[1:]))
                else:
                    print("Error: didn't understand '{}'".format(i))
                    return []
                return messages
            def parseRange(i):
                # Not a good place to test for this
                if i[0] == '+':
                    # Relative number to current place
                    r = list(parseLow(i[1:]))
                    if len(r) != 1:
                        print("Error: '{}' refers to {} messages instead of 1".format(i[1:], len(r)))
                        return []
                    return [self.C.currentMessage + r[0]]
                if '-' in i:
                    r = i.split('-')
                    if len(r) != 2:
                        print("Too many '-' in '{}'".format(i))
                        return []
                    if r[0] == "":
                        low = None
                    else:
                        low = list(parseLow(r[0]))
                    high = list(parseLow(r[1]))
                    if len(high) != 1:
                        print("Error: Bad range. '{}' refers to {} messages instead of 1".format(r[1], len(high)))
                        return []
                    if low is None:
                        # This is actually a relative motion to the current
                        # message
                        return [self.C.currentMessage - high[0]]
                    if len(low) != 1:
                        print("Error: Bad range. '{}' refers to {} messages instead of 1".format(r[0], len(low)))
                        return []
                    return list(range(low[0], high[0] + 1))

                return parseLow(i)
            def parseMath(i):
                if '--' in i:
                    i = i.split('--')
                    if i[0] == "":
                        i[0] = "."
                    # We'll allow multiple subtractions; no good reason not to,
                    # apart from no obvious use case right now. Very first element
                    # may be a list, every other element must be a single number
                    # (or list of length 1, containint a number).
                    # Might be confusing, since we also allow addition, but cannot
                    # subtract and add in the same entry.
                    res = list(parseRange(i[0]))
                    for sub in i[1:]:
                        subres = list(parseRange(sub))
                        if len(subres) > 1:
                            print("Error: '{}' is more than one number".format(sub))
                            return []
                        if len(subres) == 0:
                            print("Error: '{}' is empty".format(sub))
                            return []
                        res = map(lambda x: x-subres[0], res)
                    return res
                if '++' in i:
                    i = i.split('++')
                    if i[0] == "":
                        i[0] = "."
                    # We'll allow multiple additions; no good reason not to,
                    # apart from no obvious use case right now. Very first element
                    # may be a list, every other element must be a single number
                    # (or list of length 1, containint a number).
                    # Might be confusing, since we also allow subtraction, but cannot
                    # subtract and add in the same entry.
                    res = list(parseRange(i[0]))
                    for sub in i[1:]:
                        subres = list(parseRange(sub))
                        if len(subres) > 1:
                            print("Error: '{}' is more than one number".format(sub))
                            return []
                        if len(subres) == 0:
                            print("Error: '{}' is empty".format(sub))
                            return []
                        res = map(lambda x: x+subres[0], res)
                    return res
            # TODO
            # What kind of precedance to we want to have? If I allow a list to
            # be subtracted from, indicating that you could do a range, like:
            #   3-6++10  would give 13 14 15 16
            # but I also wanted to be able to use the result of math for one
            # end of the range. like:
            #    $--4-$   to show the last 5 messages  ($-4, $-3, $-2, $-1, $)
            # which would make 3-6++10 be 3 4 5 6 7 8 9 10 11 12 13 14 15 16
            #
            # We could pick one and have parenthesis force order, except
            # parenthesis are used for IMAP CRI search.
            #
            # Another note: to me, '-' looks like subtraction and '--' looks
            # like range (must be from using LaTeX), so visually the above
            # feels backwards.
            #
            # At this point, I feel the weight of mailx compatibility might be
            # too heavy. I think this must be what Vim felt like trying to be
            # compatible with vi. Maybe we do like them, have a compatibility
            # flag to parse (sortof) like mailx, or use a better syntax
            map(messages.add, parseRange(i))
        return list(messages)

    def precmd(self, line):
        # We set lastcommand in some cases to repeat the last command instead
        # of the default implicit 'next'. When running commands that aren't
        # special, we should return to the default behavior. So, as long as we
        # see any command, we'll reset here.
        if line.strip():
            self.C.lastcommand = ''
        return line
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
        elif c == 'n':
            return self.do_next(a)
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
            self.search2(self.C.lastsearch, offset=self.C.lastsearchpos)
        else:
            # Next message
            # Note: mailx has a special case, which is when it picks the
            # message, e.g. when opening a box and it picks the first
            # flagged or unread message. In which case, the implicit
            # "next" command shows the message marked as current.
            #
            # Likewise, after a from command, the current message is used
            # instead of the next.
            #
            # We handle this by having both currentMessage and nextMessage,
            # and upate them appropriately.
            #
            # TODO: extension to mailx: Next could mean next in a list;
            # e.g. a saved list or results of f/search command or custom
            # list or whatever.
            # Ideally, we'd mention the active list in the prompt. Ideally
            # we'd also list what the implicit command is in the prompt
            # (e.g. next or search continuation)
            self.do_next("")
    def processConfig(self, fileName, startlineno, lines):
        """Process configuration lines.

        Called when processing a configuration file or a part of one (e.g. an
        account specification)

        fileName and startlineno are used for printing diagnostics. Normally
        startlineno would be 1.
        lines is an iterable; it could be a file object or a list, etc.
        """
        inaccount = None
        accounts = self.C.accounts
        postConfFolder = None
        for lineno, line in enumerate(lines, startlineno):
            try:
                line = line.decode('utf-8')
            except UnicodeEncodeError as err:
                print("Error processing line {} of {}: {}".format(lineno, fileName, err))
                continue
            if inaccount:
                if line.strip() == "}":
                    inaccount = None
                elif line.strip().startswith("account"):
                    print("Error in config line {}. Accounts cannot be nested".format(lineno))
                else:
                    accounts[inaccount].append((confFile,lineno,line.rstrip('\r\n').encode('utf-8')))
            elif line.strip().startswith("account"):
                m = re.match(r' *account *([^ ]*) *\{ *', line)
                if not m:
                    m = re.match(r'account *([^ ]*)', line.strip())
                    if not m:
                        print("Failed to parse account command in line {}".format(lineno))
                        continue
                    name = m.groups()[0]
                    if not name in accounts:
                        print("Failed to run account commands: no account {} yet.".format(repr(name)))
                        continue
                    ac = accounts[name]
                    if len(ac):
                        firstLine = ac[0]
                        filename = firstLine[0]
                        startline = firstLine[1]
                        res = self.processConfig(filename, startline, map(lambda x: x[2], ac))
                        if res:
                            postConfFolder = res
                    continue

                name = m.groups()[0]
                print("New account found: {}".format(name))
                accounts[name] = []
                inaccount = name
            elif line.strip() == "":
                # Blank line
                continue
            elif line.strip().startswith('#'):
                #print("comment")
                continue
            elif line.strip().startswith("set "):
                #print("setting", line.strip()[4:])
                m = re.match(r' *set *([^ =]+) *= *(.+)', line)
                if not m:
                    print("Failed to parse set command in line %i" % lineno)
                    continue
                key, value = m.groups()
                try:
                    self.C.settings[key] = value
                except KeyError:
                    self.C.settings.addOption(settings.UserOption(key, None))
                    self.C.settings[key] = value

            elif line.strip().startswith("folder "):
                postConfFolder = line.strip()[7:]
            else:
                print("unknown command %s in line %s:%i" % (repr(line),fileName,lineno))
        return postConfFolder

    def cacheFetch(self, msgset, args):
        """Retrieve parts from cache. If not in cache, retrieve from IMAP
        first, then populate cache, then retrieve from cache.

        msgset can be a MessageList, a list, or an integer (for a single message).

        Returns an array of message data sets, even when only a single message
        is requested.
        """
        # For now, we'll always fetch FLAGS from IMAP, as they aren't
        # permenant. TODO: Have flag update statuses write to the cache when
        # we get them, then we can just use the cache fully
        # First pass, except for FLAGS, we'll not split up the parts. If any
        # part is missing from the cache, we'll re-fetch all parts to populate
        # the cache
        assert args.startswith('(')
        assert args.endswith(')')
        if isinstance(msgset,int) or isinstance(msgset, long):
            # Convert to list
            msgset = [msgset]
        argsList = args[1:-1].split()
        origArgsList = list(argsList)
        # Always re-cache flags
        if 'FLAGS' in argsList:
            argsList.remove('FLAGS')
            if self.C.settings.debug.general:
                print("executing IMAP command FETCH {} {}".format(msgset.imapListStr(), '(FLAGS)'))
            data = self.C.connection.fetch(msgset.imapListStr(), '(FLAGS)')
            for d in data:
                r = processImapData(d[1], self.C.settings)[0]
                self.C.cache["{}.{}".format(d[0], 'FLAGS')] = getResultPart('FLAGS', r)

        # Build a fetch list
        flist = MessageList()
        for i in msgset:
            for a in argsList:
                if a.upper().startswith("BODY.PEEK"):
                    a = "BODY" + a[9:]
                if not '{}.{}'.format(i,a) in self.C.cache:
                    flist.add(i)
                    break
        # Fetch and cache
        if flist:
            args = '({})'.format(" ".join(argsList))
            if self.C.settings.debug.general:
                print("executing IMAP command FETCH {} {}".format(flist.imapListStr(), args))
            data = self.C.connection.fetch(flist.imapListStr(), args)
            for d in data:
                r = processImapData(d[1], self.C.settings)[0]
                for arg in argsList:
                    if arg.upper().startswith("BODY.PEEK"):
                        arg = "BODY" + arg[9:]
                    part = getResultPart(arg, r)
                    self.C.cache["{}.{}".format(d[0], arg)] = part
        data = []
        for i in msgset:
            d = []
            for a in origArgsList:
                if a.upper().startswith("BODY.PEEK"):
                    a = "BODY" + a[9:]
                d.append(a)
                d.append(self.C.cache['{}.{}'.format(i, a)])
            data.append((i, d))
        return data


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
    def equals(self, args):
        """Show current message number (.)

        With 'all', show the various states."""
        if args != "all":
            print(self.C.currentMessage)
            return
        print("last list:", self.C.lastList)
        #print("last search:", self.C.lastsearch)
        print("last command:", self.C.lastcommand)
        print("last message:", self.C.lastMessage)
        print("current message:", self.C.currentMessage)
        print("next message:", self.C.nextMessage)

    @showExceptions
    def do_account(self, args):
        """List defined accounts, or run commands for an account

        account             list accounts
        account -v          list accounts and show account settings
        account NAME        invoke the account named NAME
        """
        stripArgs = args.strip()
        if len(stripArgs) == 0 or stripArgs == '-v':
            # Display accounts list
            for i in self.C.accounts.keys():
                print(" {}".format(i))
                if stripArgs == "-v":
                    for line in self.C.accounts[i]:
                        print("  {}:{}: {}".format(line[0], line[1], line[2].decode('utf-8')))
        else:
            #Select an account
            if ' ' in stripArgs:
                print("Specify only one account")
                return
            name = stripArgs
            if not name in self.C.accounts:
                print("No account named {}.".format(repr(name)))
                return
            ac = self.C.accounts[name]
            if len(ac):
                firstLine = ac[0]
                filename = firstLine[0]
                startline = firstLine[1]
                postFolder = self.processConfig(filename, startline, map(lambda x: x[2], ac))
                if postFolder:
                    self.do_folder(postFolder)

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
    def do_cache(self, args):
        """Manage live cache.

        cache clear         clear whole cache
        cache cleardec      clear decrypted data from cache
        cache info          show information about the cache
        """
        args = args.strip()
        if args == 'clear':
            del self.C.cache
            self.C.cache = {}
        elif args == 'cleardec':
            for i in self.C.cache.keys():
                if '.d.' in i or 'BODY[d.' in i:
                    del self.C.cache[i]
        elif args == 'info':
            # This approximates the size of the cache data.
            s = sys.getsizeof(self.C.cache)
            print("Cache structure: %7.3f %sB" % normalizeSize(s, bi=True))
            c = 0
            for k,v in self.C.cache.iteritems():
                c += sys.getsizeof(k)
                c += sys.getsizeof(v)
            print("Cache contents:  %7.3f %sB" % normalizeSize(c, bi=True))
            print("Cache size:      %7.3f %sB" % normalizeSize(s + c, bi=True))
        else:
            print("Please select clear or cleardec")
        return
    @showExceptions
    def do_folder(self, args):
        """Connect to the given mailbox, or show info about the current connection.

        With no arguments, gives an overview of the current connection
        (location, number of messages, etc).

        With an argument, close any current connection and connect to the
        specified location. Then show info. If 'headers' or 'headers_folder'
        is set, also run the headers command.

        If argument starts with a '+', the value of setting 'folder' is
        prepended to the target

        Currently supported protocols:
            imap://       - IMAP4r1 with STARTTLS
            imaps://      - IMAP4r1 over SSL
            imap+plain:// - IMAP4r1 plain clear text. No protection of data.
                            NOTE: This should only be used for testing, and
                            only as a last resort. Your emails and username
                            and password will be visible on the network, and
                            there is no protection to ensure you connect to
                            the server you specify (server is spoofable)!

        Examples:
            folder imap://john.smith@mail.example.com/Sent-Mail
            folder +
            folder +Sent-Mail
        """

        if args == "":
            # Just show information about the current connection, if any
            if not self.C.connection:
                print("No connection. Give a location to this command to establish a connection.\nSee 'help folder' for more info.")
                return
            unseen = len(self.C.connection.search("utf-8", "UNSEEN"))
            print("\"{}://{}@{}:{}/{}\": {} messages {} unread".format(
                self.C.connection.mailnexProto,
                self.C.connection.mailnexUser,
                self.C.connection.mailnexHost,
                self.C.connection.mailnexPort,
                self.C.connection.mailnexBox,
                self.C.lastMessage,
                unseen,
                ))
            self.status['unread'] = unseen
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
                m = re.match(r'([^@]*@)?([^/]*)(/.*)?', args[7:])
                if not m:
                    print("failed to parse")
                    return
                port = 143
                proto = 'imap'
            elif args.startswith("imaps://"):
                m = re.match(r'([^@]*@)?([^/]*)(/.*)?', args[8:])
                if not m:
                    print("failed to parse")
                    return
                port = 993
                proto = 'imaps'
            elif args.startswith("imap+plain://"):
                m = re.match(r'([^@]*@)?([^/]*)(/.*)?', args[13:])
                if not m:
                    print("failed to parse")
                    return
                port = 143
                proto = 'imap+plain'
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
                    box = ""
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
                if box != C.connection.mailnexBox:
                    # Changed box (probably; this doesn't skip things like
                    # going from '' to 'INBOX' to 'inbox', for example), so
                    # cached message information is (probably) wrong, so wipe
                    # the cache
                    del self.C.cache
                    self.C.cache = {}
                    c.mailnexBox = box
            else:
                print("disconnecting")
                C.connection.close()
                #C.connection.logout()
                C.connection = None
                # Since we closed the connection, the message cache is no
                # longer valid. Wipe it.
                del self.C.cache
                self.C.cache = {}
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
                elif proto == "imap+plain":
                        print(self.C.t.red("Warning: Connection is NOT secure! Login credentials are not protected!"))
                else:
                    # TODO: if not c.caps, run capability command
                    if not c.caps or not 'STARTTLS' in c.caps:
                        print("Remote doesn't claim TLS support; trying anyway")
                    print("Info: Startting TLS negotiation")
                    c.starttls()
                    if c.isTls():
                        print("Info: Connection now secure")
                    else:
                        #TODO: Allow user to override (at their own peril) should be per-host. Should possibly not be global.
                        raise Exception("Failed to secure connection!")
                if not user:
                    user = getpass.getuser()
                if proto == "imap+plain":
                    # When the user explicitly requests unsafe passage, don't
                    # automatically get the password; require it to be typed.
                    # This gives the user another chance to prevent their
                    # credentials from being sent in-the-clear.
                    prompt_to_save = False
                    pass_ = getpass.getpass()
                else:
                    _, prompt_to_save, pass_ = getPassword(self.C.settings, proto, user, host, port)
                print("Info: Logging in")
                # TODO: Retry N times? Or at least, prompt for password entry
                # if we got the password from an agent or keyring that didn't
                # work. Ideally, if it came from the keyring, didn't work, and
                # the user entered a new one that does, we'd offer saving it
                # over the old keyring entry.
                # TODO: Display where we got the password, if it wasn't
                # entered by prompt (e.g. by agent (and which agent?) or by
                # keyring (and which keyring?))
                c.login(user, pass_)
                if prompt_to_save:
                    # TODO: Allow user to prevent this prompt. Perhaps by
                    # disabling keyring in general, or by disabling it for
                    # particular accounts (or enabling for particular accounts
                    # only).
                    while True:
                        line = self.singleprompt("Save password to keyring (yes/no)? ").lower().strip()
                        if line == 'y' or line == 'yes':
                            print(" Saving...")
                            try:
                                keyring.set_password("imap://%s" % host, user, pass_)
                            except RuntimeError:
                                print("Error: couldn't save password to keyring")
                            break
                        elif line == 'n' or line == 'no':
                            break
                del pass_
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
            except imap4.imap4Exception as ev:
                print("Failed to login")
                if 'imap_string' in dir(ev):
                    print("  Server said:", repr(ev.imap_string))
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
            c.setCB("fetch", self.fetchMonitor)
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
        # Assume new messages must be unseen. Assume we'll get a fetch
        # notification if it subsequently becomes seen. TODO: Verify these
        # assumptions!
        if self.C.settings.debug.general:
            print("Notified of new message(s) (newExist = {}, so delta is {})".format(value, delta))
        for i in range(self.C.lastMessage + 1, value + 1):
            p = '{}.FLAGS'.format(i)
            # Don't set \Seen flag
            self.C.cache[p]=[]
            if self.C.settings.debug.general:
                print(" Faking cache of {}".format(p))
        self.status['unread'] += delta
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
        # TODO: Fix up the cache, either clear all entries after the lowest
        # changed number, or somehow fixup all the keys to be the right
        # numbers again. (OR maybe store the UID instead of the ID of the
        # message in the cache; then we can simply remove that UID from the
        # cache and be done, maybe)
        self.C.lastMessage -= 1
        # was the message unseen? If so, decrement self.status['unread']
        p = '{}.FLAGS'.format(msg)
        if p in self.C.cache:
            if '\\Seen' not in self.C.cache[p]:
                self.status['unread'] -= 1
            else:
                # Wasn't part of unread count. Nothing to do.
                pass
        else:
            # We don't know if it was seen or not.
            # TODO: Schedule an unseen count in a second or something to
            # update the status
            pass

    def fetchMonitor(self, msg, data):
        data = processImapData(data, self.C.settings)[0]
        l = lambda: print("fetch received:", msg, data)
        if self.cli._is_running and self.C.settings.debug.general:
            self.cli.run_in_terminal(l)
        # NOTE: data is often raw, can't always be made unicode, so trying to
        # search for a (unicode) string in it can cause conversion errors to
        # do the comparison. Better to search for a bytestring instead.
        if b'FLAGS' in data:
            flags = getResultPart('FLAGS', data)
            p = '{}.FLAGS'.format(msg)
            if p in self.C.cache:
                oldflags = self.C.cache[p]
                if '\\Seen' in oldflags and not '\\Seen' in flags:
                    self.status['unread'] += 1
                if not '\\Seen' in oldflags and '\\Seen' in flags:
                    self.status['unread'] -= 1
            else:
                # We don't know what the flags were, so we don't know if the
                # unread (unseen) count changed. We'll have to ask for a new
                # count. We don't want to do this for *every* fetch result we
                # get; if a client marked a whole bunch of messages as read,
                # we'll get called for each back-to-back, and re-searching the
                # unseen count might be inefficient on the server (and
                # certainly is a waste of network)
                oldflags = None
                # TODO: schedule refresh for a second or so later, unless such
                # a refresh is already pending (once per second shouldn't be
                # bad if a lot of operations are continuously happening in the
                # background. If we were instead to delayuntil a second after
                # things stop can make us look very unresponsive.
                #unseen = len(self.C.connection.search("utf-8", "UNSEEN"))
                #self.status['unread'] = unseen

                pass
            self.C.cache[p] = flags
            l = lambda: print("New flags:", flags, "old flags:", oldflags)
            if self.cli._is_running and self.C.settings.debug.general:
                self.cli.run_in_terminal(l)
            if self.cli._is_running:
                self.cli.invalidate()

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
    @argsToMessageList
    def do_findrefs(self, msglist):
        """Experimental command. Will eventually become the threaded view command.

        Takes a message list. To do the full mailbox, give '1-$' as the
        message list.

        Flags are currently hardcoded to 'el'.
        Currently used flags are:

        modes. Pick one:
         'e' - expanded view. All messages are shown. Currently the default.
         'c' - collapsed view. Only message leaders are shown. Rest of
               conversation is hidden and inaccessible.
        Additional. Pick zero or more:
         'd' - perform extra debugging (can be very slow; lots of IMAP traffic)
         'l' - sort message leaders by the highest member sequence number.
               Mostly equivalent to sorting by most recent receive date. This
               keeps threads with recent activity towards the end of the
               message list, just like recent unthreaded messages appear at
               the end.
        """
        #print(dir(self.C.connection))
        # If we cache threading information, we'll need to save off the
        # uidvalidity to know if the cache is valid.
        #print(self.C.connection.uidvalidity)
        t1=time.time()
        messageLeaders={}
        messages={}
        #
        #
        # RFC5256 procedures:
        #
        #
        #   Base-Subject (used by ORDEREDSUBJECT and REFERENCES):
        #     1: Convert subject to UTF-8, tabs and continuations to space,
        #        and collapse multiple spaces into a single space.
        #     2: Repeatedly remove postfix <subj-trailer> until none remain.
        #        subj-trailer = "(fwd)" / WSP
        #        WSP = SP / HTAB
        #     3: Remove prefix <subj-leader> (says "all prefix text...that
        #        matches", but doesn't say "repeat")
        #        subj-leader = (*subj-blob subj-refwd) / WSP
        #        subj-blob = "[" *BLOBCHAR "]" *WSP
        #        subj-refwd = ("re" / ("fw" ["d"])) *WSP [subj-blob] ":"
        #        BLOBCHAR = ;any char8 except '[' and ']'
        #     4: if prefix matching <subj-blob>, remove it, unless that leaves a
        #        empty <subj-base>.
        #        subj-blob = "[" *BLOBCHAR "]" *WSP
        #        BLOBCHAR = ;any char8 except '[' and ']', utf-8 encoded
        #        subj-base = NONSWP *(*WSP NONWSP)
        #             ; can match <subj-blob>
        #        WSP = SP / HTAB
        #        NONWSP = ; any char other than SP or HTAB
        #     5: repeat 3 and 4 until no matches remain
        #     6: if prefix matches <subj-fwd-hdr> and postfix matches
        #        <subj-fwd-trl>, remove both and goto 2.
        #        subj-fwd-hdr = "[fwd:"
        #        subj-fwd-trl = "]"
        #
        #     re: [list] [list2] re [bob]: fwd: [list] [fwd: my subject] (fwd)
        #                                                                2---2
        #     3-3
        #        ?
        #   5     3----------------------3
        #                                 ?
        #   5                              3--3
        #                                      ?
        #   5                                   4----4
        #                                              6---6           6
        #
        #
        #   Sent Date:
        #     1: If fully valid, convert to UTC.
        #     2: If invalid TZ, assume it is UTC already (optional)
        #     3: If invalid time, assume 00:00:00 (optional)
        #     4: if invalid date, assume 00:00:00 on MININT (earliest possible
        #        date) (optional)
        #     5: If Sent Date can't be had (bad parse or missing header), use
        #        INTERNALDATE
        #     6: When doing ordering, matching dates fall back to message
        #        sequence ordering.
        #
        #    I feel like rules 4 and 5 would be exclusive, but maybe there is
        #    a case where you can successfully parse an invalid date or
        #    something. Since 4 is optional ("SHOULD" in the spec), we can
        #    just jump to step 5 if we want, I think, and still comply
        #
        #
        #   ORDEREDSUBJECT:
        #     1: sort by base subject, then sent date
        #     2: separate into threads using base subject
        #        first message is thread leader. All others in thread are
        #        direct children of the leader
        #     3: sort threads by sent date of thread leader
        #
        #   REFERENCES:
        #     1: Parse and normalize message-ids (headers message-id,
        #        in-reply-to, and references).
        #     2: Extract valid messade-ids from references header. If none
        #        (missingg header or no valid ids), extract first message-id
        #        from in-reply-to if any valid. Else, references is NIL
        #     3: if subject basification removes certain texts, consider this
        #        message a reply/forward (subj-refwd, subj-trailer,
        #        subj-fwd-hdr/trl).
        #     4: Link the references list into parent child relationships,
        #        unless a message already has a parent. The spec says to
        #        create a unique message ID to messages without valid IDs, or
        #        two messages with duplicate IDs (other than the first). This
        #        can't be saved back to the IMAP server and the IMAP server
        #        isn't allowed to change the header data it gives the client
        #        (I don't think), so I don't know why they specify this; seems
        #        like an opaque implementation detail.
        #        If a reference ID doesn't (yet) exist, create a dummy message
        #        for it and create the links.
        #        Also, don't add a link if a loop would form.
        #     5: Make the last reference parent to the current message,
        #        replacing any existing parent reference (this cleans up
        #        truncated reference headers found in other messages).
        #        Except, don't do this if a loop would be formed. (I'd expect
        #        the loop should be broken elsewhere to enforce this message's
        #        parent, but I'm sure they have their reasons. We might be
        #        able to do this in our own vendor-algorithm, just not the
        #        standards compliant one)
        #        Leave parentless if there was no reference list.
        #     6: Repeat 1 through 5 for all messages, retaining a database of
        #        the results.
        #     7: Create a dummy 'root' message. Make it the parent of all
        #        messages that do not yet have a parent.
        #     8: Remove dummy messages, except for the root. If a dummy
        #        message has children, reparent the children to the current
        #        message's parent, unless that parent is the root, in which
        #        case skip this step (unless there is only one child, in which
        #        case do it anyway). Repeat recursively down the tree.
        #     9: Sort the dummy root's children by sent date. When the child
        #        is a dummy, use that dummy's first child. This is not
        #        recursive; there can be no further dummies at this point.
        #    10: Create a subject table mapping base-subjects to message
        #        trees. Iterate through the dummy root's children. Get the
        #        base-subject for each (either directly, or if a dummy, its
        #        first child); skip if empty. Add this message to the subject
        #        table if the subject doesn't exist yet. Else, if message in
        #        table is not a dummy and (current is dummy OR message in
        #        table is reply/forward and current isn't) then replace the
        #        message in subject table with this one. (make it the most
        #        leaderly it can be)
        #    11: Merge threads with same subject. Re-iterate through the
        #        dummy-root's children; for each regain the thread subject.
        #        Skip if empty. Lookup the subject in the table. Skip if
        #        message is current. Else, merge current in (if both dummies,
        #        append current children to other's children and delete
        #        current dummy. Else if table entry is dummy and current
        #        isn't, make current a child of dummy. else if current is a
        #        reply/forward and table entry is not, make the current a
        #        child of the table entry. Else, create a new dummy message,
        #        reparent both the table entry and current message to the new
        #        dummy, and replace the table entry in the table with the new
        #        dummy.
        #    12: Sort all messages under the dummy root again using sent-date,
        #        this time recursively, starting with the deepest part of the
        #        tree first, then moving up. (sort grandchildren before
        #        children). If a dummy (only possible for root's children),
        #        use first child instead.
        #
        #    Note: This sorting is restrictive; we will extend it to support
        #          other orderings. Also note that sent-date sorting requires
        #          knowing the "Date: " header and the INTERNALDATE if the
        #          "Date: " header doesn't exist (or possibly if it is
        #          imparsible)
        #
        #          I expect we'll have sort orderings for thread-leaders
        #          (children of dummy-root), and within-thread. Likely, we'll
        #          have separate aggregation and thread arrangement (e.g. use
        #          REFERENCES for aggregation, but then allow flat list or
        #          hierarchy, with sorting for either.
        #    Note: Step 8 can leave dummy thread leaders (dummy root children)
        #          that don't get cleaned up later (step 12 even calls this
        #          out as something to be aware of when sorting). The end of
        #          section 4 has an example of this: "((3)(5))" showing that
        #          messages 3 and 5 are siblings of a parent that doesn't
        #          exist in the result (e.g. not in box, or didn't match
        #          search criteria), but are the same thread. They are not
        #          direct children of the dummy root, but a dummy child or
        #          root.
        def processMessage(i, uid, headers):
            if not 'message-id' in headers:
                print("Fail (no id):", i, uid)
                return
            mid = headers['message-id']
            if len(mid) != 1:
                print("Fail: multiple ids", i, uid)
                return
            mid = mid[0]
            refs = []
            # MS Outlook (unknown version(s)) includes a blank 'in-reply-to'
            # header sometimes, but a valid 'references' header.
            # So, I suppose we'll parse references first, for now.
            # really, we probbably need to look at both and make a heuristic
            # decision.
            # Update: RFC 5256 says to use references first.
            #
            # Also, I've seen Outlook include both 'in-reply-to' and
            # 'references' that are both blank when someone replies to their
            # own message. Can only guess that it is a reply by looking at the
            # subject (has a 'RE:' leadstring, matches a leader without it),
            # and possibly by comparing message contents to see what parts are
            # in other messages (very costly if not doin subject comparison to
            # limit the search, and difficult to get right given the
            # inconsistent methods of including quoted text).
            if 'references' in headers:
                refs = headers['references']
                #print("references something", i, uid, refs)
                # Ok, so the reference list needs to be actually parsed. This
                # quick-split method isn't valid, though it usually works.
                # TODO FIXME
                if len(refs) != 1:
                    # There should (must?) be a single of this header, which
                    # contains multiple message ids in a list
                    #print("Fail: multiple references lines")
                    return
                refs = refs[0].split()
                # First is supposed to be leader, last is direct reply, rest
                # is (supposed to be) part of email chain in-between. Note
                # that nothing stops a message from listing a reference
                # out-of-chain, and parsing a reply-to-multiple-messages
                # explicitly isn't possible, though implicitly this is a reply
                # to all messages listed.
            if len(refs) == 0 and 'in-reply-to' in headers:
                replies = headers['in-reply-to']
                # This is supposed to contain all of the messages this one
                # replies to. In practice, apparently, this has rarely or
                # never actually referred to more than one message, and has
                # often enough contained the reply MID /and/ a reply email
                # address, making hard or impossible to use reliably (see RFC
                # 5256 for more info). As per the RFC, we'll use the first, if
                # any.
                # TODO FIXME proper mid parsing here as well
                refs = replies[0].split()
                del refs[1:]
            # TODO: RFCs say to remove missing messages for display,
            # though that is in the scope of listing search results over
            # IMAP. Clients like "sup" show missing messages, which allows
            # one to see approximately how much of a conversation is
            # missing. We could do either or both.
            c = None
            def lookupOrCreate(mid):
                if mid in messages:
                    return messages[mid]
                else:
                    msg = threadMessage(mid)
                    messages[mid] = msg
                    return msg
            if len(refs):
                c = lookupOrCreate(refs[0])
            for ref in refs[1:]:
                p = c
                c = lookupOrCreate(ref)
                if not c.parent:
                    c.parent = p
                    p.children.append(c)
                else:
                    if c.parent is not p and self.C.settings.debug.general:
                        print("At message %i, Would have set %i as parent to %i, but already had parent %i" % (i, p.mseq, c.mseq, c.parent.mseq))
            # TODO NEXT: change this so that we look for current message
            # in list. If it already exists, we need to update mseq/muid
            # (2 cases: this was a stub from a previously encountered
            # reference list so we need to fill it out, or this is a
            # duplicate mid to a previously seen message, in which case we
            # are supposed to give it a new unique mid, but I don't think
            # that is neccessary; we just need to keep it separate
            # somehow, likee the mid could have a list of mseq/muids. If
            # we support multibox at some point (like sup), we could have
            # a list of box/muid) and, if it was a stub, break existing
            # parent/child and form a new one.
            # TODO: What if we had a parent listed, but this message lacks
            # a stated parent (e.g. references got stripped)? RFC might
            # have guidance.
            p = c # The parent of this message is the last child dealt with above, if any.
            if mid in messages:
                this = messages[mid]
                if self.C.settings.debug.general:
                    print("update mid",mid)
                if this.mseq == -1:
                    # found a placeholder. Update its info
                    this.mseq = i
                    this.muid = uid
                    if this.parent:
                        # had a parent. Replace it with this message's
                        # parent
                        if not this.parent.mid == p.mid:
                            if self.C.settings.debug.general:
                                print("Reparenting %i from %i to %i" % (this.mseq, this.parent.mseq, p.mseq))
                            this.parent.children.remove(this)
                            this.parent = p
                            # TODO: Could we already be listed as a child?
                            # might want to not be in the list multiple
                            # times
                            p.children.append(this)
                else:
                    # found a duplicate mid.
                    print("Duplicate mid!", mid, "at", this.mseq, "and", i)
                    # TODO: handle it?
            else:
                #print("new mid", mid)
                this = threadMessage(mid, i, uid)
                messages[mid] = this
                if p:
                    this.parent = p
                    p.children.append(this)
        args = 'el'
        if 0:
            t2 = None
            # slow path, but interruptable
            for i in range(1,self.C.lastMessage + 1):
                res = self.C.connection.fetch(i, '(UID BODY.PEEK[HEADER.FIELDS (references in-reply-to message-id)])')
                data = processImapData(res[0][1], self.C.settings)
                headertext = getResultPart('body[header.FIELDS (references in-reply-to message-id)]', data[0])
                headers = processHeaders(headertext)
                uid = getResultPart('uid', data[0])
                processMessage(i, uid, headers)
        else:
            print("list:",msglist)
            m = MessageList(msglist)
            res = self.C.connection.fetch(m.imapListStr(), '(UID BODY.PEEK[HEADER.FIELDS (references in-reply-to message-id)])')
            t2=time.time()
            for i,data in res:
                i = int(i)
                # TODO: Since we didn't go through our caching fetch, we
                # should perhaps try to add some relevant data to the cache?
                # Actually, we should probably try to read relevant data from
                # the cache as well
                # TODO: The server MAY send us unsolicited fetch data while we
                # were asking for specific data. We could check the mseq ('i'
                # in this case) of the message to ensure it was in the set we
                # asked for, but the server may send the message twice. E.g.
                # from dovecot:
                #   c: A3 FETCH 123:125 (body)
                #   s: * 123 FETCH (BODY...)
                #   s: * 124 FETCH (BODY...)
                #   s: * 125 FETCH (BODY...)
                #   s: * 125 FETCH (FLAGS (\Seen))
                #   s: A3 OK Fetch completed
                # Therefore, we really need to check that the response was for
                # the data we requested. Ideally, this should be handled in
                # the imap4 library, not by us (even though both are part of
                # this project).
                data = processImapData(data, self.C.settings)
                # FIXME: when we ask for HEADER.FIELDS we get back the same
                # thing, but we only group on parenthesis, so we end up with
                # something like:
                #  [
                #    'UID',
                #    number,
                #    'BODY.PEEK[HEADER.FIELDS',
                #    [
                #      'REFERENCES',
                #      'IN-REPLY-TO',
                #      'MESSAGE-ID',
                #    ],
                #    ']',
                #    headers_text,
                #  ]
                #
                # Which is a wrong interpretation of the results. However,
                # since we know we only have one set of that, we'll use the
                # ']' as the key.
                headertext = getResultPart(']', data[0])
                headers = processHeaders(headertext)
                uid = getResultPart('uid', data[0])
                # TODO: Not part of the RFC, but a useful extension would be
                # to also scan for attached messages (e.g. message/rfc822
                # parts) and process those as well.
                # TODO: What about encrypted attachments? We don't want to
                # have to prompt for decryption a bunch of times while
                # threading. At some point, we'll be caching threads to disk;
                # should we cache the relationship of encrypted messages to
                # disk as well?
                processMessage(i, uid, headers)
        # step 7, make dummy root, parent of all parentless messages
        for m in messages.values():
            if not m.parent:
                messageLeaders[m.mid] = m
        t3=time.time()
        if self.C.settings.debug.general:
            print("Done")
            print("duration:", t3-t1)
            if t2:
                # Note: most of the fetch duration is parsing the IMAP response
                # data
                # E.G.
                # duration: 4.20123004913
                #   fetch duration: 2.41206598282
                #   calc duration: 1.78916406631
                #
                # Of the fetch duration, less than 0.5 was getting the data over a
                # slow-ish link where the server already had the data cached.
                print("  fetch duration:", t2-t1)
                print("  calc duration:", t3-t2)
        if 0:
            # More detailed stats
            for m,d in messageLeaders.iteritems():
                if len(d.children) == 0:
                    print("singleton", d.mseq, d.muid)
                elif len(d.children) == 1:
                    print("repl", d.mseq, d.muid)
                else:
                    print("Multibeast", d.mseq, d.muid)
                    for i in d.children:
                        print("  ", i.mseq, i.muid)
        def countAllChildren(leader):
            count = 1 # myself
            for i in leader.children:
                count += countAllChildren(i)
            return count
        # Build virtfolder with thread ordering
        msgleaderlist = []
        for m,d in messageLeaders.iteritems():
            msgleaderlist.append((m,d))
        if 'l' in args:
            # Last child sort order
            def findlast(m):
                last = [0]
                def iter(m):
                    if m.mseq > last[0]:
                        last[0] = m.mseq
                    for i in m.children:
                        iter(i)
                iter(m)
                return last[0]
            for _,d in messageLeaders.iteritems():
                d.sortKey = findlast(d)
        else:
            # Default, first child sort order
            for _,d in messageLeaders.iteritems():
                # TODO: actually iterate children for first valid msq (message
                # sequence number).
                d.sortKey = d.mseq
        msgleaderlist.sort(cmp=lambda x,y: cmp(x[1].sortKey,y[1].sortKey))
        msglist = []
        msglistextra = []
        def expand(m, leader=False):
            if m.mseq > 0:
                msglist.append(m.mseq)
                if leader and not m.children:
                    leader = 2
                msglistextra.append((leader, None, None))
            #print(m)
            for i in m.children:
                expand(i)
        def collapse(m, _):
            if m.mseq > 0:
                msglist.append(m.mseq)
                mod = 0
            else:
                # This was a dummy leader. Use the first child for display
                # and prep to remove the dummy leader from the thread count
                msglist.append(m.children[0].mseq)
                mod = -1
            l = None
            if not m.children:
                l = 2
            msglistextra.append((l, countAllChildren(m) + mod, m))
        for i in msgleaderlist:
            #print("Adding leader", i[0],i[1][0])
            if 'e' in args:
                expand(i[1], True)
            elif 'c' in args:
                collapse(i[1], True)
            else:
                # Do some default.
                expand(i[1], True)
        # Clear existing virt folder if any
        self.do_virtfolder("")
        self.C.virtfolderSavedSelection = (self.C.currentMessage, self.C.nextMessage, self.C.prevMessage, self.C.lastList)
        self.C.currentMessage = 1
        self.C.nextMessage = 1
        self.C.prevMessage = None
        self.C.lastList = []
        self.C.virtfolder=msglist
        self.C.virtfolderExtra = msglistextra
        self.setPrompt("mailnex (vf-threads)> ")
        if not 'd' in args:
            return

        # Do some diagnostics and stuff
        maxdepth = 0
        maxmsg = None
        maxChildren = 0
        maxChildrenMsg = None
        for m,d in messageLeaders.iteritems():
            chldcnt = countAllChildren(d)
            if chldcnt > maxChildren:
                maxChildren=chldcnt
                maxChildrenMsg = m
            if len(d.children) == 0:
                continue
            depth = 0
            while True:
                #print(depth, d[0], d[2])
                if d.children:
                    d = d.children[0]
                    depth += 1
                else:
                    break
            if depth > maxdepth:
                maxdepth = depth
                maxmsg = m
        def showReplList(leader, file=sys.stdout):
            mlist = MessageList()
            def info(m,depth, markers):
                if m.mseq > 0:
                    mlist.add(m.mseq)
                #print("%s %s %s %s" % (" "*depth, m[0], m[1], repr(m)))
                mstr=" "
                mi = 0
                # for box drawing characters, see
                # https://en.wikipedia.org/wiki/Box-drawing_character
                for i in markers:
                    mstr += " "*(i-mi - 1) + "│"
                    mi = i
                mstr += " "*(depth - mi - 1)
                if depth != mi:
                    #mstr += "╰"
                    mstr += "└"
                elif depth != 0: # Don't do this for leaders
                    mstr = mstr[:-1] + "├"
                #print("%s %s %s   %s" % (" "*depth, m[0], m[1], markers), file=file)
                if m.mseq != -1:
                    print("%s%s %s   %s,%i" % (mstr, m.mseq, m.muid, markers, depth), file=file)
                else:
                    print("%sMissing message %s   %s,%i" % (mstr, m.mid, markers, depth), file=file)
                for i in m.children[:-1]:
                    info(i, depth+1, markers+[depth+1])
                for i in m.children[-1:]:
                    info(i, depth+1, markers)
            info(leader, 0, [])
            if not mlist:
                print("[no real messages?]", file=file)
                return
            self.showHeadersNonVF(mlist, file=file)
        print("----------- Max depth ----------")
        print(maxdepth, maxmsg)
        if maxmsg:
            showReplList(messageLeaders[maxmsg])
        print("----------- Max count ----------")
        print(maxChildren, maxChildrenMsg)
        if maxChildrenMsg:
            showReplList(messageLeaders[maxChildrenMsg])
        #return
        import codecs
        # NOTE: python3 has an encoding parameter directly in open, no need
        # for codecs.open.
        # See
        # http://stackoverflow.com/questions/10971033/backporting-python-3-openencoding-utf-8-to-python-2
        with codecs.open("/tmp/list.txt","w", encoding='utf-8') as f:
            print("----------- all non 0 ----------", file=f)
            for m,d in messageLeaders.iteritems():
                if len(d.children) == 0:
                    continue
                showReplList(d, file=f)
                print("--", file=f)




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
        uv = None
        lastu = None

        # TODO: We are assuming that a user+host combo is sufficient to
        # identify a mail account (set of mail boxes/folders). This breaks if
        # using a custom port number or a different protocol connects to a
        # different set of boxes (e.g. imap vs imaps, or port 143 vs 12345)
        # OTOH, keeping separate databases for the same boxes just because the
        # user connected slightly differently would also be a nuisance. Other
        # clients seem to deal with this by requiring such things be managed
        # by configuring accounts before doing anything, which we don't do.
        boxid = "{}@{}.{}".format(
                self.C.connection.mailnexUser,
                self.C.connection.mailnexHost,
                self.C.connection.mailnexBox.replace('/','.'),
                )
        dbpath="{}.{}".format(C.dbpath, boxid)

        # TODO: store based on location (connection and mbox)
        lastMessageFile = os.sep.join((dbpath, "lastMessage"))
        print("Indexing box {} in {}@{}".format(
                repr(self.C.connection.mailnexBox),
                self.C.connection.mailnexUser,
                self.C.connection.mailnexHost,
                ))
        try:
            with open(lastMessageFile) as f:
                uv, lastu = map(int,f.read().split())
        except:
            pass

        if uv and lastu:
            if self.C.connection.uidvalidity != uv:
                # FIXME: Clear the database of this box; all history is no
                # longer valid!
                i = 1
            else:
                #search("uid %i" % lastu)
                data = self.C.connection.search("UTF-8", "uid %i" % lastu)
                # TODO: What if the last message we indexed was expunged? The
                # search will be empty! We could try again with "uid %i:*". If
                # that is also empty, then the last message we indexed is
                # expunged, and there aren't any messages after that to index,
                # so we are done.
                # TODO: What to do about expunged messages in general? We
                # don't want them contributing to search results. We should
                # try to delete them from the search DB whenever we get an
                # expunged message and during the initial connection to the
                # box, if it is a box we are caching.
                if len(data) == 0:
                    return
                i = map(int, data)[0]

        # TODO: Use location + UIDVALIDITY and UIDs in messages, else things
        # will go awry when messages are deleted or someone tries to search in
        # a different folder or connection.
        # TODO: Should we have one large combined database, or a separate
        # database per indexed location?
        # Having a single database means we can search across all indexed
        # messages no matter where they are. If we store the location as a
        # key, we should even be able to filter so that we can also search
        # just one location.
        # However, having separate dabases means we can store more messages
        # (xapian has a max record limit).
        db = xapian.WritableDatabase(dbpath, xapian.DB_CREATE_OR_OPEN)
        termgenerator = xapian.TermGenerator()
        termgenerator.set_stemmer(xapian.Stem("en"))

        lastuid = 1 # Safe first-message UID
        # If starting with id 1, throw a dummy document into the DB. This makes the
        # xapian docids not equivalent to the message IDs from the get-go, and
        # should prevent accidental reliance on the equivalence
        print("Id is", i)
        if i == 1:
            print("creating dummy initial")
            doc = xapian.Document()
            termgenerator.set_document(doc)
            doc.set_data("dummy data")
            db.replace_document("Q-1", doc)

        while True:
            # TODO: This would be a bit faster (or a lot faster, depending on
            # the connection) if we fetched multiple messages at once.
            if i > C.lastMessage:
                break
            try:
                #data = M.fetch(i, '(UID BODYSTRUCTURE)')
                #print(typ)
                #print(data)
                # TODO: use BODYSTRUCTURE to find text/plain subsection and fetch that instead of guessing it will be '1'.
                # TODO: Could also store the structure for quicker reference
                # when reading search data, for things like attachements or
                # whatever
                # TODO: Add attachment file names (when available) to search
                # data for method, so that they can be found.
                data = M.fetch(i, '(BODY.PEEK[HEADER] BODY.PEEK[1] UID)')
                #print(typ)
                #print(data)
                #print(data[0][1])
                #print("------------ Message %i -----------" % i)
                #print(data[1][1])

                data = processImapData(data[0][1], self.C.settings)

                headertext = getResultPart('body[header]', data[0])
                uid = int(getResultPart('UID', data[0]))
                headers = processHeaders(headertext)
                print("\r%i (%i)"% (i, uid), end='')
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
                # TODO: Decompose the message dates (sent and received, that
                # is, message header "Date:" and IMAP's INTERNALDATE) and
                # store as values to allow for ranged searches

                termgenerator.index_text(getResultPart('body[1]', data[0]))
                # Support full document retrieval but without reference info
                # (we'll have to fully rebuild the db to get new stuff. TODO:
                # store UID and such)
                doc.set_data("x-mailnex-uid: {}\r\nx-mailnex-location: {}@{}\r\nx-mailnex-box: {}\r\n{}".format(
                    uid,
                    self.C.connection.mailnexUser,
                    self.C.connection.mailnexHost,
                    self.C.connection.mailnexBox,
                    headertext,
                    ))
                # We will use the message UID (formerly we were using the
                # MSeq) as the identifier. This will allow us to obtain this
                # record via UID for updating or deletion, and doesn't hit
                # xapian limits (UIDs can be 64bit I think, xapian document
                # ids are limited to 32bit). However, this is stored as a
                # term; retreiving a term from a search result requires
                # iterating through all of the terms in a document, and Q (the
                # recommended prefix for external ids) is pretty late in the
                # sort order (though not terribly). As such, we'll ALSO store
                # the UID in the document data (above). Note that
                # http://getting-started-with-xapian.readthedocs.io/en/latest/concepts/indexing/values.html
                # discusses storing values as well as terms and data. It
                # specifically recommends against storing data needed to
                # display a document in values. Since we need the UID to
                # display our messages, we won't use values.
                idterm = u"Q" + str(uid)
                doc.add_boolean_term(idterm)
                db.replace_document(idterm, doc)
                i += 1
                lastuid = uid
            except KeyboardInterrupt:
                # TODO: There is usually an outstanding fetch at this point
                # that would be good to consume somehow; otherwise we later
                # get a warning about receiving data for an unknown request.
                print("\n\nCanceled")
                try:
                    with open(lastMessageFile, "w") as f:
                        # Store the previous index, in case we didn't actually
                        # write the current one TODO: Verify this logic
                        f.write(b"%i %i" % (self.C.connection.uidvalidity, lastuid))
                except Exception as ev:
                    print("Failed to store lastMessage", ev)
                return
            finally:
                pass
        print()
        with open(lastMessageFile, "w") as f:
            # Store the previous index, in case we didn't actually
            # write the current one TODO: Verify this logic
            f.write(b"%i %i" % (self.C.connection.uidvalidity, uid))
        print("Done!")

    def getTextPlainParts(self, index, allParts=False):
        """Get the plain text parts of a message and all headers.

        Returns a list of tuples. Each list entry represents one part.
        Each tuple consists of the part number and unicode version of the text, already converted from specified or guessed charsets.
        Currently this includes the message headers (as the first section).

        If the optional parameter allParts is set to true, this will actually
        return everything instead of just text parts.
        """
        resparts = []
        parts = self.cacheFetch(index, '(BODY.PEEK[HEADER] BODYSTRUCTURE)')[0]
        headers = getResultPart('BODY[HEADER]', parts[1])
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
        structstr = getResultPart('BODYSTRUCTURE', parts[1])
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
            sigres = None
            secondaryStruct = None
            # TODO: What are protected-headers="v1"?
            if struct.type_ == "multipart" and struct.subtype == 'encrypted':
                if '{}.d.SUBSTRUCTURE'.format(struct.tag) in self.C.cache:
                    # Already decoded this message
                    secondaryStruct = self.C.cache['{}.d.SUBSTRUCTURE'.format(struct.tag)]
                else:
                    p = struct.parameters
                    if p and 'protocol' in p and p['protocol'].lower() == 'application/pgp-encrypted':
                        # TODO: What if the message doesn't have the protocol
                        # parameter but otherwise follows the protocol?
                        if haveGpgme:
                            inner = struct.tag.split('.')[1:]
                            encpart = ".".join(inner + ['2'])
                            data = self.cacheFetch(index, '(BODY.PEEK[%s])' % (encpart))[0]
                            message = getResultPart("BODY[{}]".format(encpart), data[1])
                            ctx = gpgme.Context()
                            msgdat = io.BytesIO(message)
                            result = io.BytesIO()
                            try:
                                ret = ctx.decrypt_verify(msgdat, result)
                            except gpgme.GpgmeError:
                                pass
                            else:
                                for sig in ret:
                                    # TODO: Handle displaying multiple signatures
                                    sigres = sigresToString(ctx, sig)
                                m = email.message_from_string(result.getvalue())
                                secondaryStruct = unpackStructM(m, {"cache": self.C.cache}, 1, struct.tag + ".d")
                                self.C.cache["{}.d.SUBSTRUCTURE".format(struct.tag)] = secondaryStruct

            if struct.type_ == "multipart" and struct.subtype == "signed":
                p = struct.parameters
                if p and 'protocol' in p and p['protocol'].lower() == 'application/pgp-signature':
                    # TODO: What if the message doesn't have the protocol
                    # parameter, but otherwise follows the protocol? (That is,
                    # has 2 parts, the second part being
                    # applciation/pgp-signature) Should we try handling that
                    # anyway, or parse as if unsigned. On the one hand, the
                    # internet mantra is to be forgiving with what you
                    # receive, but on the other hand, if it doesn't specify
                    # the protocol, it could be some other spec than we know
                    # how to handle, and we probably shouldn't give the user
                    # misleading information.
                    if haveGpgme:
                        inner = struct.tag.split('.')[1:]
                        messageTag = ".".join(inner + ['1'])
                        signatureTag = ".".join(inner + ['2'])
                        data = self.cacheFetch(index, '(BODY.PEEK[{}.MIME] BODY.PEEK[{}] BODY.PEEK[{}])'.format(messageTag, messageTag, signatureTag))[0]
                        messageData = getResultPart('BODY[{}.MIME]'.format(messageTag), data[1]) + getResultPart('BODY[{}]'.format(messageTag), data[1])
                        sigData = getResultPart('BODY[{}]'.format(signatureTag), data[1])

                        ctx = gpgme.Context()
                        msgdat = io.BytesIO(messageData)
                        sigdat = io.BytesIO(sigData)
                        ret = ctx.verify(sigdat, msgdat, None)
                        for sig in ret:
                            # TODO: Handle displaying multiple signatures
                            sigres = sigresToString(ctx, sig)
            if hasattr(struct, "disposition") and struct.disposition not in [None, "NIL"]:
                extra += " (%s)" % struct.disposition[0]
# mailx shows attachments inline if they are text or message type. We
# shouldn't ignore attachments unless a user set option says to.
#                if not allParts and struct.disposition[0].lower() == "attachment":
#                    skip = True
                dispattrs = dictifyList(struct.disposition[1])
                if 'filename' in dispattrs:
                    extra += " (name: %s)" % dispattrs['filename']
            # TODO XXX: Preprocess control chars out of all strings before
            # display to terminal!
            structureStrings.append("%s   %s/%s%s %s" % (
                struct.tag,
                struct.type_,
                struct.subtype,
                extra,
                sigres if sigres else "",
                ))
            innerTag = ".".join(struct.tag.split('.')[1:])
            # First pass, we'll just grab all text/plain parts. Later we'll
            # want to check disposition, and later we'll want to deal with
            # multipart/alternative better (and multipart/related)
            if (allParts and struct.type_ == "text") or (not allParts and struct.type_ == "text" and struct.subtype == "plain"):
                # TODO: write the following a bit more efficiently. Like,
                # split only once, use second part of return only, perhaps?
                if not skip:
                    # Default: Show each section's headers too
                    if innerTag != "":
                        # If this is the outermost part (e.g. this isn't a
                        # multipart message), then there isn't a MIME header.
                        # Only get the section MIME headers for lower levels.
                        fetchParts.append(("%s.MIME" % innerTag, None))
                    fetchParts.append((innerTag, struct))
#            if allParts and not isinstance(struct, structureMessage) and not hasattr(struct, "subs"):
#                # Probably useful to display, not a multipart itself, or a
#                # message (which *should* have subparts itself?)
#                # TODO: Properly handle attached messages
#                fetchParts.append((innerTag, struct))
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
            if secondaryStruct:
                pickparts(secondaryStruct, allParts)
        pickparts(struct, allParts)
        structureString = u"\n".join(structureStrings)
        resparts.append((None, None, structureString + '\r\n\r\n'))
        if len(fetchParts) == 0:
            resparts.append(('info', None, "No displayable parts"))
            return resparts
        elif len(fetchParts) == 1 and len(fetchParts[0][0]) == 0:
            # This message doesn't have parts, so fetch "part 1" to get the
            # body
            fparts = ["BODY.PEEK[1]"]
            fetchParts[0] = (u'1', fetchParts[0][1])
        fparts = ["BODY.PEEK[%s]" % s[0] for s in fetchParts]
        if fparts:
            data = self.cacheFetch(index, '(%s)' % " ".join(fparts))[0]
        for o in fetchParts:
            dstr = getResultPart("BODY[%s]" % (o[0],), data[1])
            if o[1] is None and isinstance(o[1], structureMultipart):
                o[1].encoding = None
                o[1].attrs = None
            if o[1]:
                encoding = o[1].encoding if hasattr(o[1], "encoding") else None
            else:
                encoding = None
            # First, check for transfer encoding
            dstr = self.transferDecode(dstr, encoding)
            if dstr == None:
                resparts.append((o[0],o[1],"Part %s: unknown encoding %s\r\n" % (o[0], encoding)))
                continue
            # Finally, check for character set encoding
            # and other layers, like format flowed
            if o[1] and hasattr(o[1], "attrs") and o[1].attrs and 'charset' in o[1].attrs:
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
                            if self.C.settings.debug.general:
                                print("Found control characters!")
                            raise UnicodeDecodeError(str(charset), b"", 0, 1, b"control character detected")
                except UnicodeDecodeError as err:
                    if charset == 'iso-8859-1':
                        # MS Outlook lies about its charset, so we'll try what
                        # they mean instead of what they say. TODO: Should we
                        # complain about this? Not like the user can do much
                        # except encourage the sender to stop using outlook.
                        try:
                            d = dstr.decode('windows-1252')
                            if self.C.settings.debug.general:
                                print("decoded as cp-1252 instead of iso-8859-1")
                            realcharset = 'windows-1252'
                        except:
                            d = "Part %s: failed to decode as %s or windows-1252\r\n" % (o[0], charset)
                    else:
                        if self.C.settings.debug.general:
                            d = "Part %s: failed to decode as %s (%s)\r\n%s" % (o[0], charset, err, repr(dstr))
                        else:
                            d = "Part %s: failed to decode as %s" % (o[0], charset)
                        realcharset = None
                else:
                    if self.C.settings.debug.general:
                        print("Successfully decoded as", charset)
                    realcharset = charset
            else:
                d = dstr
                realcharset = None
            if o[1] and hasattr(o[1], 'attrs') and o[1].attrs and 'format' in o[1].attrs and o[1].attrs['format'].lower() == 'flowed':
                #TODO: Flowed format handling
                pass
            if o[1]:
                o[1].realcharset = realcharset
            if o[1] is None:
                # MIME header
                o = (None, 'mime')
            resparts.append((o[0], o[1], d))
        return resparts

    def lex_help(self, text, rest, res):
        # TODO: we aren't highlighting if there is more than one space before
        # the help topic
        if "do_{}".format(rest) in dir(self):
            res.append((cmdprompt.Generic.Heading, rest))
        elif "help_{}".format(rest) in dir(self):
            res.append((cmdprompt.Generic.Heading, rest))
        else:
            res.append((cmdprompt.Text, rest))
    def compl_help(self, document, complete_event):
        topics = []
        this_word = document.get_word_before_cursor()
        for i in dir(self):
            cmdstr = "do_{}".format(this_word)
            helpstr = "help_{}".format(this_word)
            if i.startswith(cmdstr):
                topics.append(i[3:])
            elif i.startswith(helpstr):
                topics.append(i[5:])
        # TODO: Should we really do icase sort? We aren't matching
        # insensitively. This feels inconsistent.
        topics.sort(cmp=lambda x,y: cmp(x.lower(), y.lower()))
        for i in topics:
            yield cmdprompt.prompt_toolkit.completion.Completion(i, start_position=-len(this_word))

    def do_help(self, args):
        """List available commands/topics or details about a specific command or topic

        help        list commands and topics
        help CMD    show help on command CMD
        """
        # This overrides the one from cmd.Cmd. That one assumed terminal width
        # was 80 and didn't support calling out to a pager.
        if args:
            # Show specific command or topic if available
            if hasattr(self, "help_{}".format(args)):
                # Either a topic or dedicated function version of help for a
                # command. Call the function and use its string
                helpdata = getattr(self, "help_{}".format(args))()
                if helpdata is None:
                    # TODO: functiona probably output its own data. Maybe we
                    # should flag that?
                    helpdata = ""
            elif hasattr(self, "do_{}".format(args)):
                helpdata = None
                if hasattr(getattr(self, "do_{}".format(args)), "__doc__"):
                    helpdata = getattr(getattr(self, "do_{}".format(args)), "__doc__")
                if not helpdata:
                    helpdata = "No documentation for command '{}'".format(args)
            else:
                helpdata = "Unknown command/topic: '{}'".format(args)
            # TODO: Reflow help text (to terminal width), and fixup indentation.
            if not isinstance(helpdata, (list, tuple)):
                helpdata = helpdata.split('\n')
        else:
            cmdstr = "do_"
            helpstr = "help_"
            doc = []
            undoc = []
            topic = []
            for i in dir(self):
                if i.startswith(cmdstr):
                    command = i[len(cmdstr):]
                    if command in topic:
                        # We found a help_{} before the do_{}. Move it
                        topic.remove(command)
                        doc.append(command)
                    else:
                        docstr = ""
                        if hasattr(getattr(self, i), "__doc__"):
                            docstr = getattr(getattr(self, i), "__doc__")
                        if docstr:
                            doc.append(command)
                        else:
                            undoc.append(command)
                elif i.startswith(helpstr):
                    topicstr = i[len(helpstr):]
                    if not topicstr in doc:
                        # We haven't already found a command this is
                        # documenting. Put it in the topics list. If we find a
                        # command later, it will be moved; no need to search
                        # for a matching command here.
                        topic.append(topicstr)
            width = self.C.t.width
            outlines = []
            # cmd has a nice organize-as-columns implementation, but it
            # directly writes to stdout. We want to hold it for possible
            # sending to a pager. Rather than re-implement it ourselves, we'll
            # just replace stdout temporarily
            oldout = self.stdout
            myio = StringIO()
            self.stdout = myio
            indent = "    "
            def icasecmp(a, b):
                return cmp(a.lower(), b.lower())
            doc.sort(icasecmp)
            topic.sort(icasecmp)
            undoc.sort(icasecmp)
            if doc:
                outlines.append("Documented commands:")
                self.columnize(doc, width - len(indent))
                myio.reset()
                for line in myio:
                    outlines.append(indent + line[:-1])
                myio.reset()
                myio.truncate()
                outlines.append("")
            if topic:
                outlines.append("Topics:")
                self.columnize(topic, width - len(indent))
                myio.reset()
                for line in myio:
                    outlines.append(indent + line[:-1])
                myio.reset()
                myio.truncate()
                outlines.append("")
            if undoc:
                outlines.append("Undocumented commands:")
                self.columnize(undoc, width - len(indent))
                myio.reset()
                for line in myio:
                    outlines.append(indent + line[:-1])
                myio.reset()
                myio.truncate()
                outlines.append("")
            self.stdout = oldout
            helpdata = outlines
        # TODO: Use crt_help or crt.
        if len(helpdata) > self.C.t.height - self.ui_lines:
            self.runAProgramWithInput(["less", "-R"], "\n".join(helpdata).encode("utf-8"))
        else:
            print("\n".join(helpdata))

    def filterHeaders(self, headers, ignore, headerOrder, allHeaders):
        headerstr = u''
        if allHeaders:
            # First pass, dump them directly and move on.
            # TODO: Perhaps apply the headerorder setting even to
            # Print command?
            headerstr += headers
            return headerstr
        msg = email.message_from_string(headers)
        for header in ignore:
            if header in msg:
                del msg[header]
        prefheaders = ""
        otherheaders = ""
        for header in headerOrder:
            if header in msg:
                for val in msg.get_all(header):
                    enc = unicode(email.header.make_header(email.header.decode_header(val)))
                    tmpl = "format_header_{}".format(header.lower())
                    if tmpl in self.C.settings:
                        prefheaders += self.C.settings[tmpl].value.format(t=self.C.t,header=header,value=enc) + '\n'
                    elif "format_header_PREF" in self.C.settings:
                        prefheaders += self.C.settings["format_header_PREF"].value.format(t=self.C.t,header=header,value=enc) + '\n'
                    elif "format_header" in self.C.settings:
                        prefheaders += self.C.settings["format_header"].value.format(t=self.C.t,header=header,value=enc) + '\n'
                    else:
                        prefheaders += "{}: {}\n".format(header, enc)
                del msg[header]
        for header in msg.items():
            key, val = header
            enc = unicode(email.header.make_header(email.header.decode_header(val)))
            tmpl = "format_header_{}".format(key.lower())
            if tmpl in self.C.settings:
                otherheaders += self.C.settings[tmpl].value.format(t=self.C.t,header=key,value=enc) + '\n'
            elif "format_header" in self.C.settings:
                prefheaders += self.C.settings["format_header"].value.format(t=self.C.t,header=key,value=enc) + '\n'
            else:
                otherheaders += "{}: {}\n".format(key, enc)

        #TODO: Should headerorderend apply to both mime and message headers?
        if self.C.settings.headerorderend:
            headerstr += otherheaders
            headerstr += prefheaders
        else:
            headerstr += prefheaders
            headerstr += otherheaders
        headerstr += '\r\n'
        return headerstr

    def partsToString(self, parts, allHeaders=False):
        body = u''
        headerstr = u''
        for part in parts:
            if part[0] is None:
                # Headers or structure
                if part[1] == 'header':
                    body += self.filterHeaders(part[2], self.C.settings.ignoredheaders.value, self.C.settings.headerorder.value, allHeaders)
                elif part[1] == 'mime':
                    headerstr += self.filterHeaders(part[2], self.C.settings.ignoredmimeheaders.value, self.C.settings.mimeheaderorder.value, allHeaders)
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
                if hasattr(part[1], 'encoding') and part[1].encoding:
                    body += "encoding: " + part[1].encoding + "\r\n"
                if part[1]:
                    body += "struct: " + repr(part[1].__dict__) + "\r\n"
            if headerstr != "":
                body += headerstr
                headerstr = u''
            if part[0].endswith(".HEADER"):
                body += self.filterHeaders(part[2], self.C.settings.ignoredheaders.value, self.C.settings.headerorder.value, allHeaders)
            elif not part[1]:
                body += part[2]
            else:
                t = part[1].type_
                s = part[1].subtype
                cmd = None
                settingsearch = [
                    'pipe-{}/{}'.format(t, s),
                    'pipe-{}'.format(t, s),
                    'pipe'.format(t, s),
                    ]
                for setting in settingsearch:
                    if setting in self.C.settings and self.C.settings[setting].value:
                        cmd = self.C.settings[setting].value
                        break
                if cmd:
                    body += "filtered with cmd: {}\n\n".format(cmd)
                    ienc = None
                    settingsearch = [
                        'pipe-ienc-{}/{}'.format(t, s),
                        'pipe-ienc-{}'.format(t, s),
                        'pipe-ienc'.format(t, s),
                        ]
                    for setting in settingsearch:
                        if setting in self.C.settings and self.C.settings[setting].value:
                            ienc = self.C.settings[setting].value
                            break
                    if not ienc:
                        if hasattr(part[1], 'realcharset'):
                            ienc = part[1].realcharset
                        else:
                            ienc = 'utf-8'
                    if ienc == 'same' and hasattr(part[1], 'realcharset'):
                        ienc = part[1].realcharset
                    # TODO: Maybe allow chaining encoders. E.g. encode to
                    # utf-8, then to base64.
                    try:
                        encdata = part[2].encode(ienc,'xmlcharrefreplace')
                    except AssertionError:
                        # Some encoders demmand strict error handling, so
                        # if we couldn't do charref, we'll try strict
                        # instead
                        encdata = part[2].encode(ienc)
                    if printfStyle.check(cmd, ['f']):
                        with tempfile.NamedTemporaryFile() as outfile:
                            outfile.write(encdata)
                            outfile.flush()
                            cmd = printfStyle.replace(cmd, {'s': s, 't': t, 'f': outfile.name})
                            status, data = self.runAProgramAsFilter(['/bin/sh', '-c', cmd], b"")
                    else:
                        cmd = printfStyle.replace(cmd, {'s': s, 't': t})
                        status, data = self.runAProgramAsFilter(['/bin/sh', '-c', cmd], encdata)
                    if status == 0:
                        oenc = None
                        settingsearch = [
                        'pipe-oenc-{}/{}'.format(t, s),
                        'pipe-oenc-{}'.format(t, s),
                        'pipe-oenc'.format(t, s),
                        ]
                        for setting in settingsearch:
                            if setting in self.C.settings and self.C.settings[setting].value:
                                oenc = self.C.settings[setting].value
                                break
                        if oenc is None:
                            oenc = 'utf-8'
                        elif oenc == 'same':
                            oenc = ienc
                        part = (part[0], part[1], data.decode(oenc))
                    else:
                        print("Error running:", status)

                body += part[2]
            if not part[2].endswith('\r\n\r\n'):
                body += "\r\n"
        return body

    def transferDecode(self, data, encoding):
        if encoding:
            # Some mailers do weird casing, so we'll normalize it
            encoding = encoding.lower()
        if encoding in [None, "", "nil", '7bit', '8bit', '7-bit', '8-bit']:
            # Don't need to do anything
            return data
        elif encoding == "quoted-printable":
            return data.decode("quopri")
        elif encoding == "base64":
            return data.decode("base64")
        print("unknown encoding %s; can't decode for display\r\n" % (encoding))
        # TODO: raise an exception instead?
        return None

    def fetchAndDecode(self, msgpart, part):
        """Fetch a message part and decode the contents.

        Takes a message number and part as a list (e.g. [1234,1,3] for part "1234.1.3")

        Returns transfer decoded subpart data, or None if it couldn't decode

        Note: doesn't decode characterset data into unicode"""
        #print("Fetching attachment")
        data = self.C.connection.fetch(msgpart[0], '(BODY.PEEK[{}])'.format(msgpart[1]))
        #print("processing data")
        parts = processImapData(data[0][1], self.C.settings)
        #print("getting part")
        data = getResultPart('BODY[{}]'.format(msgpart[1]), parts[0])
        #print(data)
        #print(part.encoding)
        data = self.transferDecode(data, part.encoding)
        return data

    @showExceptions
    @needsConnection
    def do_save(self, args):
        """Save an entire message to a file.

        e.g.:
            save 6531 /tmp/mymessage.eml

        Doesn't update current message location or seen status.

        If the file already exists, the message will be appended.

        Note that there is no faked 'From ' line, so the resulting file cannot
        be treated as an mbox file.

        See also the 'write' command.
        """
        args=args.split(' ', 1)
        if len(args) != 2:
            print("Need a message and a filename")
            return
        filename=args[1]
        msg = args[0]
        data = self.C.connection.fetch(msg, '(BODY.PEEK[])')
        parts = processImapData(data[0][1], self.C.settings)
        data = parts[0][1]
        with open(filename, "wa")  as outfile:
            outfile.write(data)
            outfile.flush()

    def getStructure(self, index):
        res = self.cacheFetch(index, '(BODYSTRUCTURE)')[0]
        struct = getResultPart('BODYSTRUCTURE', res[1])
        struct = unpackStruct(struct, self.C.settings)
        struct = flattenStruct(struct)
        return struct

    def lex_write(self, text, rest, res):
        def checkMsg(tok):
            try:
                msg = tok.split('.')
                msg = map(int,msg)
            except:
                res.append((cmdprompt.Generic.Error, tok))
            else:
                if msg[0] < 0 or msg[0] > self.C.lastMessage:
                    res.append((cmdprompt.Generic.Error, tok))
                else:
                    res.append((cmdprompt.Generic.Heading, tok))
        tok = rest.split()
        if len(tok) == 1:
            checkMsg(tok[0])
            rest = rest[len(tok[0]):]
            if len(rest):
                # Catch spaces typed by user
                res.append((cmdprompt.Generic.Normal, rest))
            return
        elif len(tok) == 2:
            checkMsg(tok[0])
            # TODO: Ideally, validate path or look for pipe
            res.append((cmdprompt.Generic.Normal, rest[len(tok[0]):]))
            return
        else:
            # TODO: Look for pipe ('|') and such
            res.append((cmdprompt.Generic.Normal, rest))
        return
    def compl_write(self, document, complete_event):
        topics = []
        this_word = document.get_word_before_cursor()
        before = document.current_line_before_cursor
        after = document.current_line_after_cursor
        line = before + after
        # TODO: Find out which word this is. Look for pipe to abort completion
        if '|' in line:
            raise StopIteration()
        count = len(before.split())
        if count != 3:
            raise StopIteration()
        compl = pathCompleter(before.split()[-1])
        while True:
            yield compl.next()
    @showExceptions
    @needsConnection
    def do_write(self, args):
        """Write a message part (e.g. attachment) to a file.

        e.g.:
            write 6531.2 /tmp/attach1.txt

        Takes a message sub-part notation. E.g. 670.1.2 is part 2 of part 1 of
        message 670.

        Doesn't update current message location or seen status.

        If the first character of the file name is a pipe ("|"), then instead of
        saving a file, the contents of the message part are given as standard
        input to the named program.

        See also the 'save' command (for writing the entire message (headers and all parts)
        See also the 'structure' command."""
        args=args.split(' ', 1)
        if len(args) != 2:
            print("Need a message part and a filename")
            return
        filename=args[1]
        msgpart = args[0].split('.',1)
        if len(msgpart) == 1:
            # Use the first part if none given
            msgpart = (msgpart[0], "1")
        struct = self.getStructure(int(msgpart[0]))
        key = '.' + msgpart[1]
        if not key in struct:
            if u'' in struct:
                key = u''
            else:
                print("Subpart not found in message. Try the 'structure' command.")
                return
        part = struct[key]
        data = self.fetchAndDecode(msgpart, part)
        if data is None:
            # Already displayed error message in fetchAndDecode
            return
        if filename[0] == '|':
            # TODO: Support opening in the background (maybe by checking for
            # an ampersand at the end of the program?)
            res = self.runAProgramWithInput(['/bin/sh', '-c', filename[1:]], data)
            return
        filename = normalizePath(filename)
        with open(filename, "w")  as outfile:
            outfile.write(data)
            outfile.flush()

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
        struct = self.getStructure(int(msgpart[0]))
        m = mailcap.getcaps()
        key = '.' + msgpart[1]
        if not key in struct:
            if u'' in struct:
                key = u''
            else:
                print("Subpart not found in message. Try the 'structure' command.")
                return
        part = struct[key]
        # This part is kindof tough, trade-off wise.
        # One the one hand, we don't want to bother downloading the whole file
        # if we have no way to view it (since that would just output that we
        # don't support it after a possibly long file transfer, then delete
        # the data we just transferred).
        #
        # On the other hand, mailcap is setup such that the handlers can
        # perform tests on the actual data to see if they apply. While I'm not
        # aware of any that actually do in distribution mailcap files, custom
        # user files may certainly make use of this feature.
        #
        # Maybe download always, then offer to save the file if we cannot view
        # it?
        #
        # Or, make it an option, so the user can decide if they need
        # content-testable mailcap entries.
        #
        # Or, we can get a list of matching untested entries from mailcap,
        # look if any entries actually need this (by looking for '%s' in the
        # test fields), and then only download the file early if so. Perhaps a
        # user option would be on/off/auto for testable attachment rules,
        # where auto does the two-step matching.
        #
        # Since doing such tests seems uncommon, we can default to 'off' or
        # 'auto'
        with tempfile.NamedTemporaryFile() as outfile:
            # Mailcap does replacements for us in cmd, if given a filename.
            # Unfortunately, it doesn't support filetemplate, so hopefully no
            # commands actually need that. It also doesn't let us filter on
            # anything (can't request copiousoutput entries, for example)
            #
            # TODO: Commands might want the parameter list ('plist=' arg to findmatch)
            fullcmd, entry = mailcap.findmatch(m, "{}/{}".format(part.type_, part.subtype), filename=outfile.name)
            if not fullcmd:
                print("Don't know how to display part. Maybe update your mailcap file,\n"
                "or specify a different message part? (use the 'structure' command\n"
                "to see a parts list)")
                return
            #print("Would run", fullcmd)
            data = self.fetchAndDecode(msgpart, part)
            if data is None:
                # Already displayed error message in fetchAndDecode
                return
            #print("Saving attachment to temporary file")
            #  TODO: Handle 'textualnewlines' if specified?
            #  TODO: handle nametemplate field? Probably requires replacing
            #  parts of python mailcap lib functions
            #  TODO: Check 'needsterminal'. Means interactive program that
            #  needs a terminal (e.g. not an X program)
            #  TODO: Check 'copiousoutput'. Means non-interactive and probably
            #  requires a pager. Mutt uses this solely to mean that it is a
            #  non-interactive program and can thus be used for in-line
            #  viewing of an attachment with Mutt's pager/viewer/whatever.
            outfile.write(data)
            outfile.flush()
            # TODO: make asking a parameter.
            res = self.singleprompt("Launch viewer %r? [y/N] " % fullcmd).lower()
            if res != 'y' and res != 'yes':
                return
            # TODO: Support opening in the background (should check cap for
            # non-terminal status of program first). Note that background
            # launching complicates automatic removal of the temporary file.

            # Pass the string to the shell as via the system(3) call. Note
            # 'system' runs '/bin/sh -c command', blocks SIGCHLD and ignores
            # SIGINT and SIGQUIT until done. It appears to expect to fork,exec
            # and call wait on the forked process.
            #
            # Hopefully this is close enough
            #
            # Note that we must use a shell. The Unix semantics as per RFC1524
            # allow things like shell pipelines and dictate bourne shell
            # compatible processing, and we really don't want to implement
            # that much of a shell.
            self.runAProgramStraight(['/bin/sh', '-c', fullcmd])


    @shortcut("n")
    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd(UMSAE_RETURNS_CURRENT)
    def do_next(self, msglist):
        """Display the next message in the given list.

        If no list given, displays the next message in the mailbox.

        Starts with the message after the current one and looks for a match in
        the list. If none found, picks the first message in the list.

        Usefull to just to the next unread message like so:

            next :u

        Or iterate through the last message list by repeatedly running

            next `
        """
        index = None
        if msglist is None:
            index = self.C.nextMessage
            if index > self.C.lastMessage:
                print("at EOF")
                return self.C.currentMessage
        else:
            for i in msglist:
                if i > self.C.currentMessage:
                    index = i
                    break
            if not index:
                index = msglist[0]

        vindex = index
        if self.C.virtfolder:
            index = self.C.virtfolder[index - 1]
        parts = self.getTextPlainParts(index)
        # TODO: This code copied from do_print.
        # Should be made common. See also TODOs from there.
        body = self.partsToString(parts)
        content = b"\033[7mMessage %i:\033[0m\n" % index
        content += body.encode('utf-8')
        res = self.runAProgramWithInput(["less","-R"], content)
        if res == 0:
            self.C.connection.doSimpleCommand("STORE %s +FLAGS (\Seen)" % index)
        return vindex

    @shortcut("p")
    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
        #  * Multipart/encrypted:: for pgp, ignore first part, decrypt second
        #  part.
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
        # TODO: Raise exception if not successful?
        # Pros: Explicitly lets the user know something went wrong (no output
        # is probably a bad thing
        # Cons: The output can be pretty verbose (may be mitigated by raising
        # a special exception type that a higher level can catch and treat)
        # Pro/Con: Not raising an exception causes the message numbers to
        # update (see decorators.updateMessageSelectionAtEnd). I don't know if
        # it is better to update or not. As a point of reference: s-nail
        # 14.8.6 appears to show nothing and move on. It also marks messages
        # as read, even though the PAGER fails.

    @shortcut("P")
    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
    def do_Print(self, msglist):
        """Print all text parts of a message.

        This differs from 'print' in that ignored headers and all parts of
        multipart/alternative messages are displayed.
        """
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
        body = self.partsToString(parts, True)

        # TODO: Use terminfo/termcap (or perhaps pygments or prompt_toolkit)
        # for styling
        content = b"\033[7mMessage %i:\033[0m\n" % index
        content += body.encode('utf-8')
        res = self.runAProgramWithInput(["less","-R"], content)
        if res == 0:
            # TODO: Allow asynchronous mode. See do_print for details.
            M.doSimpleCommand("STORE %s +FLAGS (\Seen)" % index)

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
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
            parts = processImapData(data[0][1], self.C.settings)[0]
            headers = getResultPart('BODY[HEADER]', parts)
            body = getResultPart('BODY[TEXT]', parts)
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
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
            to = []
        if 'cc' in hdrs:
            cc = ",".join(hdrs['cc']).split(',')
        else:
            cc = []
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
            # RFC2822 only allows 0 or 1 'reply-to' header in a message. If
            # there are actually more than 1 present, we have to decide which
            # to pick. Viable options are to pick the first, or pick the last,
            # or merge them all together. For now, we'll pick the first as a
            # probably reasonable interpretation of the spec.
            to[0:0] = [hdrs['reply-to'][0]]
        else:
            to[0:0] = [from_]
        # TODO: Notify the user if something looks a tad fishy here. For
        # example, if there was a Reply-to that wasn't a subset of the From
        # header, the user might be in for a surprise.
        # TODO: mailx only quotes the first part of the message and ignores
        # the rest. This could be good or bad.
        body = ""
        # parts should look roughly like this:
        #   [
        #       (None, 'header', "the header data as text"),
        #       (None, None, "the message structure as text"),
        #       (None, 'mime', "the mime header as text, if applicable"),
        #       ("part number", structure_data, "part payload text"),
        #       # Repeat mime and part info
        #   ]
        for part in parts[2:]:
            if part[1] == "mime":
                # Don't put the headers in the reply
                continue
            if hasattr(part[1],'disposition') and part[1].disposition and part[1].disposition[0] == 'attachment':
                # Don't include attachments in the reply
                continue
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

    def editMessage(self, message):

        self.C.printInfo("Type your message. End with single '.' on a line, or EOF.\nUse '~?' on a line for help.")
        def noarg(func):
            """Marks a function as needing 0 arguments

            Actually, we'll just passthrough right now. This will just document intent for now.

            Eventually, we can add implementation without going through all the functions again to add it."""
            return func
        def needarg(func):
            """Marks a function as needing 1 argument

            Actually, we'll just passthrough right now. This will just document intent for now.

            Eventually, we can add implementation without going through all the functions again to add it."""
            return func
        class editorCompleter(cmdprompt.prompt_toolkit.completion.Completer):
            """CLI Completer for the message editor"""
            def get_completions(self, document, complete_event):
                line = document.current_line_before_cursor
                if not line.startswith("~@ "):
                    # Only support completing filenames for now
                    raise StopIteration()
                # TODO: Break out filename completer into separate function,
                # to be reused by the attachment editor and possibly other
                # places.
                currentPath = line[3:]
                compl = pathCompleter(currentPath)
                while True:
                    yield compl.next()
        class editorCmds(object):
            """Similar to the regular command class, but we only run commands if the line starts with a '~'"""
            def __init__(self, context, message, prompt, cli, addrCmpl, runner, cmdCmpl):
                object.__init__(self)
                self.attachlist = []
                # TODO: Allow a default setting for signing
                self.pgpsign = False
                self.pgpencrypt = False
                self.C = context
                self.message = message
                self.singleprompt = prompt
                self.getAddressCompleter = addrCmpl
                self.cli = cli
                self.runAProgramStraight = runner
                self.cmdCmpl = cmdCmpl
                # tmpdir is used for holding edited or created attachments. It
                # should be automatically cleaned out when the message is sent
                # or aborted.
                self.tmpdir = None
                # Python doesn't allow certain symbols in function names that
                # we intend to use as commands. Since we lookup the functions
                # ourselves anyhow, we can just dump them into our dictionary (in
                # theory)
                self.__dict__['do_~'] = self.tilde
                self.__dict__['do_@'] = self.at
                self.__dict__['do_?'] = self.do_help
            def tilde(self, line):
                """Add a line that starts with a '~' character"""
                # User wants to start the line with a tidle
                self.message.set_payload(self.message.get_payload() + line[1:] + '\r\n')
            def run(self):
                while True:
                    try:
                        # TODO: allow tabs in the input
                        line = self.singleprompt("", completer=self.cmdCmpl)
                        # TODO: Allow ctrl+c to abort the message, but not mailnex
                        # (e.g. at this stage, two ctrl+c would be needed to exit
                        # mailnex. The first to abort the message, the second to exit
                        # mailnex)
                    except EOFError:
                        line = '.'
                    except KeyboardInterrupt:
                        return self.do_x("")

                    if line == "." or line == "~.":
                        # Send message
                        break
                    # NOTE: mailx only looks to see if most commands are at the start
                    # of the line. E.G. a line like '~vwhatever I want' launches an
                    # editor just like '~v' does. I'm breaking compatibility here,
                    # because being compatible prevents more expressive commands,
                    # though it does mean some of the old commands now require a space
                    # (e.g. ~@)
                    elif line.startswith('~'):
                        # Look for command to call in editorCmds. If none obviously
                        # found, try the default cmd. If the called command returns
                        # 'False', then return False as well (message is over, do not
                        # send)
                        if ' ' in line:
                            func, args = line[1:].split(None, 1)
                        else:
                            func = line[1:]
                            args = None

                        func = 'do_{}'.format(func)
                        if func in dir(self):
                            res = getattr(self, func)(line)
                            # TODO: Check if the function is supposed to allow args
                        else:
                            res = editor.default(line)
                        if res == False:
                            return False
                    else:
                        self.message.set_payload(self.message.get_payload() + line + '\r\n')
            def default(self, line):
                self.C.printError("Unrecognized operation. Try '~?' for help")
            @noarg
            def do_a(self, line):
                """Insert the 'sign' variable (as if '~i sign')"""
                if not 'sign' in self.C.settings:
                    self.C.printError("No signature set. Try setting 'sign'")
                    return
                return self.do_i('~i sign')
            @noarg
            def do_A(self, line):
                """Insert the 'Sign' variable (alternate signature) (as if '~i Sign')"""
                if not 'Sign' in self.C.settings:
                    self.C.printError("No alternate signature set. Try setting 'Sign'")
                    return
                return self.do_i('~i Sign')
            def do_help(self, line=None):
                """Display summary help, list of commands, or help on a specific command"""
                if line:
                    parts = line.split(None,1)
                    if len(parts) != 1:
                        topic = parts[1]
                        if topic == 'all':
                            funcs = filter(lambda x: x.startswith('do_'), dir(self))
                            maxlen = max(map(lambda x: len(x), funcs))
                            i = 0
                            outstr=['Commands:']
                            linestr=""
                            for funcname in sorted(funcs):
                                cmdname = '~{}'.format(funcname[3:])
                                # TODO: Use screen width instead of 80?
                                if len(linestr) + len(cmdname) > 80:
                                    outstr.append(linestr)
                                    linestr=""
                                linestr += cmdname
                                #TODO only append space if a command would follow (cleaner terminal output)
                                linestr += " " * (maxlen - len(cmdname))
                            if linestr:
                                outstr.append(linestr)
                            self.C.printInfo("\n".join(outstr))
                        else:
                            if topic.startswith("~") and not topic == '~':
                                topic = topic[1:]
                            func = 'do_{}'.format(topic)
                            if not func in dir(self):
                                self.C.printError("No command ~{}".format(topic))
                            else:
                                func = getattr(self,func)
                                if hasattr(func, '__doc__') and func.__doc__:
                                    self.C.printInfo(func.__doc__)
                                else:
                                    self.C.printInfo("No help for command ~{}".format(topic))
                        return
                self.C.printInfo("Summary of commands (not all shown; use '~? all' to list all):\n"
                    #1       10        20        30        40        50       60       70        80
                    #|       |         |         |         |         |        |        |         |
                    "  ~~ Text -> ~ Text   (enter a line starting with a single '~' into the\n"
                    "                       message)\n"
                    "  .          Send message\n"
                    "  ~.         Send message\n"
                    "  ~?         Summary help\n"
                    "  ~? NAME    show help for NAME. Use 'all' to list all commands\n"
                    "  ~@ FILE    Add FILE to the attachment list\n"
                    "  ~@         Edit attachment list\n"
                    "  ~h         Edit message headers (To, Cc, Bcc, Subject)\n"
                    "  ~i VAR     Insert the value of variable 'var' into message\n"
                    "  ~p         Print current message.\n"
                    "  ~q         Quit composing. Don't send. Append message to ~/dead.letter if\n"
                    "              save is set, unless 'drafts' is set.\n"
                    "  ~v         Edit message in external (visual) editor\n"
                    "  ~x         Quit composing. Don't send. Discard current progress.\n"
                    "  ~pgpsign   Sign the message with a PGP key (toggle)\n"
                    "  ~pgpenc    Encrypt the message with PGP (toggle)\n"
                    )
                #     Commands from heirloom-mailx:
                # ~!command    = execute shell command
                # ~.           = same as end-of-file indicator (according to mailx)
                #                I feel like it ought to be to insert a literal dot. I can't find a way
                #                to do that in mailx. Maybe a setting to switch between the two operations?
                # ~<file       = same as ~r
                # ~<!command   = run command in shell, insert output into message
                # ~@           = edit attachment list
                # ~@ filename  = add filename to attachment list. Space separated list (according to mailx)
                # ~@ #msgnum   = add message msgnum to the attachment list.
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
            @noarg
            def do_q(self, line):
                if 'drafts' in self.C.settings:
                    self.C.printError("Sorry, drafts setting is TBD")
                if 'save' in self.C.settings:
                    # TODO: Handle errors here. We want to try hard to not lose
                    # the user's message if at all possible.
                    ofile = open("%s/dead.letter" % os.environ['HOME'], "a")
                    # This is probably not the right format for dead.letter
                    ofile.write("From user@localhost\r\n%s\r\n" % (self.message.as_string()))
                return False
            @noarg
            def do_x(self, line):
                self.C.printInfo("Message abandoned")
                return False
            @noarg
            def do_h(self, line):
                newto = self.singleprompt("To: ", default=self.message['To'] or '', completer=self.getAddressCompleter())
                newcc = self.singleprompt("Cc: ", default=self.message['Cc'] or '', completer=self.getAddressCompleter())
                newbcc = self.singleprompt("Bcc: ", default=self.message['Bcc'] or '', completer=self.getAddressCompleter())
                newsubject = self.singleprompt("Subject: ", default=self.message['Subject'] or '')
                if newto == "":
                    del self.message['To']
                elif 'To' in self.message:
                    self.message.replace_header('To', newto)
                else:
                    self.message.add_header('To', newto)

                if newcc == "":
                    del self.message['Cc']
                elif 'Cc' in self.message:
                    self.message.replace_header('Cc', newcc)
                else:
                    self.message.add_header('Cc', newcc)

                if newbcc == "":
                    del self.message['Bcc']
                elif 'Bcc' in self.message:
                    self.message.replace_header('Bcc', newbcc)
                else:
                    self.message.add_header('Bcc', newbcc)

                if newsubject == "":
                    del self.message['Subject']
                elif 'Subject' in self.message:
                    self.message.replace_header('Subject', newsubject)
                else:
                    self.message.add_header('Subject', newsubject)
            @needarg
            def do_i(self, line):
                # NOTE: shouldn't match unless line starts with '~i ', that
                # is, it needs the 'i' command AND a space. Maybe we can
                # decorate
                assert len(line) > 3
                var = line[3:]
                if not var in self.C.settings:
                    self.C.printError("Var {} is not set, message unchanged".format(var))
                else:
                    # TODO: Better processing of the text
                    value = self.C.settings[var].strValue()
                    if value == "":
                        self.C.printWarning("Var {} was empty; message unchanged.".format(var))
                    else:
                        # For now, interpret a literal '\' followd by 'n' in the
                        # setting as a newline in our message. This allows for the
                        # primary use-case of including a signature. Mailx also
                        # interprets \t, which seems bad, since tab-stops
                        # aren't universal. Perhaps we could expand \t into
                        # some appropriate number of spaces, but that would
                        # require better parsing.
                        value = '\r\n'.join(value.split('\\n'))
                        self.message.set_payload(self.message.get_payload() + value + '\r\n')

            @noarg
            def do_pgpsign(self, line):
                if not haveGpgme:
                    self.C.printError("Cannot sign; python-gpgme package missing")
                else:
                    # Invert sign. Python doesn't like "sign = !sign"
                    self.pgpsign = self.pgpsign == False
                    if self.pgpsign:
                        self.C.printInfo("Will sign the whole message with OpenPGP/MIME")
                    else:
                        self.C.printInfo("Will NOT sign the whole message with OpenPGP/MIME")
            @noarg
            def do_pgpenc(self, line):
                if not haveGpgme:
                    self.C.printError("Cannot sign; python-gpgme package missing")
                else:
                    # Invert sign. Python doesn't like "sign = !sign"
                    self.pgpencrypt = self.pgpencrypt == False
                    if self.pgpencrypt:
                        self.C.printInfo("Will encrypt the whole message with OpenPGP/MIME")
                    else:
                        self.C.printInfo("Will NOT encrypt the whole message with OpenPGP/MIME")
            @noarg
            def do_px(self, line):
                """Print raw message, escaping non-printing and lf characters."""
                print(repr(self.message.get_payload()))
            @noarg
            def do_p(self, line):
                # Well, we have to dance here to get the payload. Pretty sure
                # we must be doing this wrong.
                orig = self.message.get_payload()
                self.message.set_payload(orig.encode('utf-8'))
                print(self.message.as_string())
                self.message.set_payload(orig)
                #print("Message\nTo: %s\nSubject: %s\n\n%s" % (to, subject, self.messageText))
            @noarg
            def do_v(self, line):
                f=tempfile.mkstemp()
                #TODO: If editHeaders is set, also save the headers
                os.write(f[0], self.message.get_payload().encode('utf-8'))

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
                    editor = None
                    if hasattr(self.C.settings, "VISUAL") and self.C.settings.VISUAL.value:
                        editor = self.C.settings.VISUAL.value
                    elif hasattr(self.C.settings, "EDITOR") and self.C.settings.EDITOR.value:
                        editor = self.C.settings.EDITOR.value
                    elif os.environ.get('VISUAL'):
                        editor = os.environ.get('VISUAL')
                    elif os.environ.get('EDITOR'):
                        editor = os.environ.get('EDITOR')
                    else:
                        editor = "vim"
                    # For whatever reason, vim complains the input isn't from
                    # the terminal unless we redirect it ourselves. I'm
                    # guessing prompt_toolkit changed python's stdin somehow
                    res = self.runAProgramStraight(["/bin/sh","-c", editor + " " + f[1]])
                if res != 0:
                    self.C.printWarning("Edit aborted; message unchanged")
                else:
                    os.lseek(f[0], 0, os.SEEK_SET)
                    fil = os.fdopen(f[0])
                    self.message.set_payload(fil.read().decode('utf-8'))
                    fil.close()
                    del fil
                    os.unlink(f[1])
                    #TODO: If editHeaders is set, retrieve those headers

            def at(self, line):
                parts = line.split(None, 1)
                if len(parts) == 2:
                    filename = parts[1]
                    if not filename.startswith('#'):
                        attachFile(self.attachlist, normalizePath(filename))
                    else:
                        if self.tmpdir is None:
                            self.tmpdir = tempfile.mkdtemp()
                        data = self.C.connection.fetch(filename[1:], '(BODY.PEEK[])')
                        parts = processImapData(data[0][1], self.C.settings)
                        data = parts[0][1]
                        f = tempfile.NamedTemporaryFile(dir=self.tmpdir, delete=False)
                        f.write(data)
                        f.close()
                        attachFile(self.attachlist, f.name)
                    return
                if len(self.attachlist):
                    self.C.printInfo("Current attachments:")
                    for att in range(len(self.attachlist)):
                        self.C.printInfo("%i: %s" % (att + 1, self.attachlist[att]))
                else:
                    self.C.printInfo("No attachments yet.")
                while True:
                    try:
                        line = self.singleprompt("attachment> ")
                    except EOFError:
                        line = 'q'
                    except KeyboardInterrupt:
                        line = 'q'
                    if line.strip() == '':
                        # Do nothing
                        continue
                    elif line.strip() == 'q':
                        self.C.printInfo("Resume composing your message")
                        break
                    elif line.strip() == 'help' or line.strip() == 'h':
                        self.C.printInfo("q                leave attachment edit mode")
                        self.C.printInfo("add FILE         add an attachment")
                        self.C.printInfo("insert POS FILE  Insert an attachment at position POS, pushing other attachments back")
                        self.C.printInfo("remove POS       remove attachment at position POS")
                        self.C.printInfo("list             list attachments")
                        #self.C.printInfo("edit POS         edit attachment (TBD)")
                        self.C.printInfo("file POS FILE    change file to attach")
                    elif line.strip() == "list":
                        if len(self.attachlist):
                            self.C.printInfo("Current attachments:")
                            for att in range(len(self.attachlist)):
                                self.C.printInfo("%i: %s" % (att + 1, self.attachlist[att]))
                        else:
                            self.C.printInfo("No attachments yet.")
                    elif line.startswith("add "):
                        attachFile(self.attachlist, line[4:])
                    elif line.startswith("insert"):
                        try:
                            cmd, pos, filename = line.split(None, 3)
                        except ValueError:
                            self.C.printError("Need position and filename")
                            continue
                        try:
                            pos = int(pos)
                        except ValueError:
                            self.C.printError("Position should be an integer")
                            continue
                        attachFile(self.attachlist, filename, pos)
                    elif line.startswith("remove "):
                        try:
                            pos = int(line[7:])
                        except ValueError:
                            self.C.printError("Position should be an integer")
                            continue
                        try:
                            del self.attachlist[pos - 1]
                        except IndexError:
                            self.C.printError("No attachment at that position")
                    elif line.startswith("file "):
                        try:
                            cmd, pos, filename = line.split(None, 3)
                        except ValueError:
                            self.C.printError("Need position and filename")
                            continue
                        try:
                            pos = int(pos)
                        except ValueError:
                            self.C.printError("Position should be an integer")
                            continue
                        attachFile(self.attachlist, filename, pos, replace=True)
                    else:
                        self.C.printError("unknown command")

            # TODO: The other ~* functions from mailx.
            # TODO: Extension commands. E.g. we might want "~save <path>" to
            # save a copy of the message to the given path, but keep editing.
            # We definitely want a way to edit an attachment (properties and
            # contents), and to add/edit arbitrary message parts. Should be
            # able to mark parts for signing, encryption, compression, etc.
        editor = editorCmds(self.C, message, self.singleprompt, self.cli, self.getAddressCompleter, self.runAProgramStraight, editorCompleter())
        if editor.run() == False:
            return False

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
        for attach in editor.attachlist:
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

            # Note: string.printable would be better, but it includes vertical
            # tab and form-feed, which I'm not certain should be included in
            # emails, so we'll construct our own without it for now.
            printable = string.digits + string.letters + string.punctuation + ' \t\r\n'
            if not all(c in printable for c in data):
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
        if editor.tmpdir:
            shutil.rmtree(editor.tmpdir)

        tos = addrs(m.get_all('To',[]))
        ccs = addrs(m.get_all('cc',[]))
        bccs = addrs(m.get_all('bcc',[]))
        recipients = list(set(tos + ccs + bccs))
        # Get rid of empty addresses, which can occur from double comma, or
        # trailing comma
        # TODO: Should also clean up the headers?
        recipients = filter(None, recipients)
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
        # Clean up other headers. If there is no value, probably shouldn't
        # send the header. Note that 'From:' and 'Date:' are required. The
        # addressing fields (from, to, cc, bcc, sender, etc) must have at
        # least 1 address if the header is present (ignoring obsolete syntax)
        if len(tos) == 0:
            del m['to']
        if len(ccs) == 0:
            del m['cc']
        if 'subject' in m and len(m['subject']) == 0:
            # Subject, unlike addressing fields, may be present and empty. Not
            # sure if we should prefer empty subject or no subject, or how to
            # let the user decide.
            del m['subject']

        if editor.pgpencrypt and not editor.pgpsign:
            print("Don't yet support ecryption without also signing. Enabling signature.")
            editor.pgpsign = True
        # Now that we have a recipient list and final on-the-wire headers, we
        # can deal with encryption.
        # We'll handle Signature and Encryption at the same point
        if editor.pgpsign:
            ctx = gpgme.Context()
            keys = []
            # TODO: What about sender vs from, etc.
            if self.C.settings.pgpkey:
                keysearch = self.C.settings.pgpkey.value
            else:
                keysearch = m['from']
            for k in ctx.keylist(keysearch, True):
                keys.append(k)
            # TODO: Filter on keys that can sign. Possibly filter out
            # expired/revoked keys.
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
                enumerateKeys(keys)
                keysel = self.singleprompt("Select key number (default 1): ", default="")
                if keysel == "":
                    keysel = '1'
                keys = [keys[int(keysel) - 1]]
            key = keys[0]
            ctx.signers = (key,)
            ctx.armor = True
            # Convert all lines to have the same line ending, else signing
            # will be bad. At the moment, on Ubuntu 16.04, the message will
            # consist of headers with unix (\n) line endings and a payload with
            # Windows/network line endings (\r\n).
            convlines = []
            fp = StringIO()
            gen = MyGenerator(fp)
            gen.flatten(m)
            for line in fp.getvalue().split(b'\n'):
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
            if editor.pgpencrypt:
                rkeys = []
                for r in recipients:
                    kl = list(ctx.keylist(r))
                    if len(kl) == 0:
                        print("No key found for {}!".format(r))
                        # TODO: Allow selection, re-edit, or at least save to
                        # DEAD.LETTER
                        return False
                    if len(kl) > 1:
                        print("Multiple keys ({}) found for {}!".format(len(kl), r))
                        enumerateKeys(kl)
                        keysel = self.singleprompt("Select key number (default 1): ", default="")
                        if keysel == "":
                            keysel = '1'
                        kl = [kl[int(keysel) - 1]]
                    print("Found key {} {} for {}".format(kl[0].subkeys[0].keyid, kl[0].uids[0].uid, r))
                    rkeys.append(kl[0])
                # TODO: If sender isn't also a recipient, but we are storing,
                # should encrypt for sender as well. Should we encrypt to
                # sender always anyway? Maybe make it an option.
                res = ctx.encrypt_sign(rkeys, 0, indat, outdat)
                outdat = outdat.getvalue()
                print("res:", res)
                newmsg = email.mime.Multipart.MIMEMultipart("encrypted", protocol="application/pgp-encrypted")
                pgppart = email.mime.Base.MIMEBase("application", "pgp-encrypted")
                pgppart.set_payload("Version: 1\r\n")
                datpart = email.mime.Base.MIMEBase("application", "octet-stream", name="encrypted.asc")
                datpart.add_header('Content-Disposition', 'inline', filename="encrypted.asc")
                datpart.set_payload(outdat)
                newmsg.attach(pgppart)
                newmsg.attach(datpart)
                # Copy some headers from the encrypted message to the outer. We'll do
                # to, from, cc, bcc, and subject and a few others for now. Should
                # possibly do others.
                # TODO: What if the user doesn't want these headers divulged
                # in-the-clear? The To, CC, and Bcc can be figured out via the
                # PGP data stream, since the current version of pygpgme
                # doesn't let us request hiding the the recipients. Date and
                # From are required, but could be faked (at least from
                # probably can). The rest could be left out to hide the
                # information.
                for key in m.keys():
                    if key.lower() in ['to','from','cc','bcc','subject','date','message-id','user-agent','references','in-reply-to']:
                        newmsg[key] = m[key]
                m = newmsg
            else:
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
                sigpart = email.mime.Base.MIMEBase("application","pgp-signature", name="signature.asc")
                # Not sure if inline or attachment is best. Some versions of
                # Eudora try to save the signature as a file in the
                # attachments directory if we don't have a disposition or make
                # it attachment. OTOH, marking it inline might result in some
                # MUAs trying to show the sig, which isn't useful without the
                # partially processed message to go with it. Adding the header
                # so that Eudora doesn't save the file based on the subject of
                # the outer message.
                sigpart.add_header('Content-Disposition', 'inline', filename="signature.asc")
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
                    if key.lower() in ['to','from','cc','bcc','subject','date','message-id','user-agent','references','in-reply-to']:
                        newmsg[key] = m[key]
                m = newmsg

        fp = StringIO()
        gen = MyGenerator(fp)
        gen.flatten(m)

        #print("Debug: Your message is:")
        #print(fp.getvalue())
        #return False

        if('smtp' in self.C.settings and self.C.settings.smtp):
            # Use SMTP
            # Note: port 25 is plain SMTP, 465 is TLS wrapped plain SMTP, and
            # 587 is SUBMIT (SUBMISSION). Both 25 and 587 are plain text until
            # STARTTLS is used. If no protocol is given, we should probably
            # use SUBMISSION by default.
            constr = self.C.settings.smtp.value
            # TODO: handle SMTP URIs more formally. See at least RFC3986
            # (URI), RFC5092 (IMAP-URI) and draft-melnikov-smime-msa-to-mda-04
            # or draft-earhart-url-smtp for details of proposed URL schemes
            # and their basis
            rem = re.match(r'([^:]*)://([^@]*@)?([^:/]*)(:[^/]*)?/?', constr)
            if not rem:
                raise Exception("failed to match")
            scheme, user, host, port = rem.groups()
            ssl = False
            if scheme == "smtps":
                defport = 465
                ssl = True
            elif style == "smtp+plain":
                defport = 25
            elif style == "submission":
                defport = 587
            else:
                raise Exception("Uknown protocol: {}".format(style))
            print("user: {}\nhost: {}\nport: {}".format(user,host,port))
            if user:
                # Strip off @
                user = user[:-1]
                if ':' in user:
                    raise Exception("Don't put passwords into the URL")
                if ';' in user:
                    raise Exception("We don't yet support modifiers")
            if port:
                port = int(port[1:])
            else:
                port = defport
            import smtplib
            secure = False
            # TODO: Allow overriding CA somehow
            # TODO: Allow user certificates
            if ssl:
                # TODO: Verify host cert!
                s=smtplib.SMTP_SSL(host, port)
                secure = True
            else:
                s=smtplib.SMTP(host, port)
                if not scheme == "smtp+plain":
                    # TODO: Verify host cert!
                    # Like with imap, this appears not to be default behavior.
                    # We might have to wrap the socket ourselves, and override
                    # smtplib.
                    try:
                        s.starttls()
                    except smtplib.SMTPResponseException as ev:
                        print(self.C.t.red("Error: Failed to establish secure link"))
                        print(ev.smtp_error)
                        # TODO: Save message to dead.letter?
                        return False
                    secure = True
            if user:
                if not secure:
                    print(self.C.t.red("Error: Insecure link. Don't send your password lightly!"))
                    # XXX TODO: Force prompt for password if insecure. DO NOT
                    # auto fetch and auto send it
                # TODO: Allow saving password to keyring
                # TODO: Always smtps, or "smtp{}".format(style) ?
                _, _, password = getPassword(self.C.settings, "smtps", user, host, port)
                s.login(user, password)
            # TODO: What if multiple from? Should use sender. Or, should we
            # allow explicitly setting the smtp from value?
            res = s.sendmail(m['from'], recipients, fp.getvalue())
            for addr in res.keys():
                # This could be done better. Also use error reporting
                print("Error: Sending to {} failed".format(addr))
            s.quit()
            return True
        s = subprocess.Popen([
            # TODO: Allow the user to override this somehow
            "sendmail", # Use sendmail to, well, send the mail
            "-bm", # postfix says this is "read mail from stdin and arrange for delivery". Hopefully this is standard across implementations
            "-i", # Don't use '.' as end of input. Hopefully this means we don't have to do dot stuffing.
            # TODO: Support delivery status notification settings? "-N" "failure, delay, success, never"
            ] + recipients,
            stdin=subprocess.PIPE)
        resstr = s.communicate(fp.getvalue())
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
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
        parts = self.getStructure(index)
        for part in sorted(parts.keys()):
            p = parts[part]
            if p.disposition:
                disp = " ({})".format(p.disposition[0])
                # Display attachment filename if available
                # p.disposition looks like ["attachment", ["filename", "file1.txt"]]
                if p.disposition[1]:
                    try:
                        d = getResultPart('filename', p.disposition[1])
                        # TODO: Interpret name (may be a quopri or b64 encoded
                        # header)
                        disp += " (name: {})".format(d)
                    except:
                        # Probably didn't have a 'filename' parameter
                        pass
            else:
                disp = ""
            print("{}{}   {}/{}{}".format(index, part, p.type_, p.subtype, disp))

    def getRows(self, adjust=0):
        """Gets the number of headline rows based on user preference.
        Additionally, if the user preference is to base on the terminal,
        get the current terminal size and subtract our overhead, adjusted
        by the adjust parameter, possibly limited by a user specified
        maximum.

        adjust is not used when the user specifies a specific row count.
        """
        # Internal adjust is the number of rows the UI occupies.
        # At the moment, that is 1 line for the prompt, and 7 lines for
        # completion popup.
        internalAdjust = self.ui_lines
        # Must show at least 1 row.
        minrows = 1
        try:
            rows = int(self.C.settings.headlinerows.value)
            return rows if rows > minrows else minrows
        except ValueError:
            if self.C.settings.headlinerows.value.lower() in ["term", "terminal"]:
                rows = self.C.t.height - internalAdjust + adjust
                return rows if rows > minrows else minrows
            elif self.C.settings.headlinerows.value.lower().startswith("terminal<"):
                _,maxval = self.C.settings.headlinerows.value.split("<")
                maxval=int(maxval)
                rows = self.C.t.height - internalAdjust + adjust
                if rows > maxval:
                    return maxval
                return rows if rows > minrows else minrows
            else:
                # TODO: Error? Assume they meant terminal?
                # Ideally, we'd catch this during the 'set' operation and not
                # here!
                raise

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
        rows = self.getRows(adjust=-1)
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

    def showHeadersNonVF(self, messageList, file=sys.stdout):
        """Show headers, given a global message list only"""
        msgset = messageList.imapListStr()
        args = "(ENVELOPE INTERNALDATE FLAGS)"
        if self.C.settings.debug.general:
            print("FETCH {} {}".format(messageList.imapListStr(), args))
        data = self.cacheFetch(messageList, args)
        #data = normalizeFetch(data)
        resset = []
        for d in data:
            envelope = getResultPart("ENVELOPE", d[1])
            internaldate = getResultPart("INTERNALDATE", d[1])
            flags = getResultPart("FLAGS", d[1])
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
            if '\RECENT' in uflags:
                if '\SEEN' in uflags:
                    nuro = self.C.settings.attrlist.value[ATTR_NEWREAD]
                else:
                    nuro = self.C.settings.attrlist.value[ATTR_NEW]
            else:
                if '\SEEN' in uflags:
                    nuro = self.C.settings.attrlist.value[ATTR_OLD]
                else:
                    nuro = self.C.settings.attrlist.value[ATTR_UNREAD]
            attr=None
            deleted = flagged = draft = answered = False
            if '\DELETED' in uflags:
                if attr is None:
                    try:
                        attr = self.C.settings.attrlist.value[ATTR_DELETED]
                    except:
                        # Originally, we didn't require this value to be in
                        # the set; assume 'D' for backwards compatibility
                        # TODO: Make a general getter? Allow fallback of any
                        # of these to the default setting's value maybe
                        attr = 'D'
                deleted = True
            if '\FLAGGED' in uflags:
                if attr is None:
                    attr = self.C.settings.attrlist.value[ATTR_FLAGGED]
                flagged = True
            if '\DRAFT' in uflags:
                if attr is None:
                    attr = self.C.settings.attrlist.value[ATTR_DRAFT]
                draft = True
            if '\ANSWERED' in uflags:
                if attr is None:
                    attr = self.C.settings.attrlist.value[ATTR_ANSWERED]
                answered = True
            if attr is None:
                attr = nuro

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
                if self.C.virtfolderExtra:
                    extra = self.C.virtfolderExtra[num - 1]
                    if extra[0] is None:
                        leader = '+'
                    elif extra[0] is False:
                        leader = ' '
                    elif extra[0] is True:
                        leader = '+'
                    elif extra[0] == 2:
                        leader = '-'
                    else:
                        leader = '?'
                    tcount = 1 if extra[1] is None else extra[1]
                    if tcount is False:
                        tcount = 1
                    # TODO: What is probably more useful is, how many messages in
                    # the thread are unread and/or flagged.
                else:
                    leader = False
                    tcount = 1
                if self.C.virtfolder and len(self.C.settings.headlinevf.value):
                    headline = self.C.settings.headlinevf.value
                else:
                    headline = self.C.settings.headline.value
                # Sanitize strings for display
                subject = sanatize(subject)
                froms = map(sanatize, froms)

                attrlist = self.C.settings.attrlist.value

                resset.append((num, headline.format(**{
                        'attr': attr,
                        'this': '>' if this else ' ',
                        'num': num,
                        'gnum': gnum,
                        'date': date.strftime("%04Y-%02m-%02d %02H:%02M:%02S"),
                        'subject': subject,
                        'flags': " ".join(flags),
                        'from': froms[0],
                        'leader': leader,
                        'tcount': tcount,
                        'flagged': attrlist[ATTR_FLAGGED] if flagged else ' ',
                        'answered': attrlist[ATTR_ANSWERED] if answered else  ' ',
                        'draft': attrlist[ATTR_DRAFT] if draft else ' ',
                        'deleted': attrlist[ATTR_DELETED] if deleted and len(attrlist) > ATTR_DELETED else ' ',
                        'nuro': nuro,
                        't': self.C.t,
                    })))
            except Exception as ev:
                if self.C.settings.debug.exception:
                    print("  %s  (error displaying because %s '%s'. Data follows)" % (d[0], type(ev), ev), repr(d), file=file)
                    import traceback
                    traceback.print_exc(file=file)
                elif self.C.settings.debug.general:
                    print("  %s  (error displaying because %s '%s'. Data follows)" % (d[0], type(ev), ev), repr(d), file=file)
                else:
                    print("  %s  (error displaying because %s '%s')" % (d[0], type(ev), ev), file=file)
        resset.sort()
        for n,s in resset:
            print(s, file=file)

    @shortcut("f")
    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd(UMSAE_NEXT_IS_CURRENT)
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

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
            print("Failed to delete: %s" % ev)

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
            print("Failed to undelete: %s" % ev)

    @showExceptions
    @needsConnection
    def do_expunge(self, args):
        """Flush deleted messages (actually remove them).
        """
        self.C.connection.doSimpleCommand("EXPUNGE")

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
            print("Failed to mark as read: %s" % ev)

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
            print("Failed to unmark as read: %s" % ev)

    @showExceptions
    @needsConnection
    @argsToMessageList
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
    @updateMessageSelectionAtEnd(UMSAE_DEFAULT)
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
        boxid = "{}@{}.{}".format(
                self.C.connection.mailnexUser,
                self.C.connection.mailnexHost,
                self.C.connection.mailnexBox.replace('/','.'),
                )
        try:
            db = xapian.Database("{}.{}".format(C.dbpath, boxid))
        except:
            print("Error opening database. Try running 'index' first.")
            return [],[]

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
    def do_search(self, args):
        """Search emails for given query

        With no query, extend last search (load 10 more results)

        This command creates a virtual folder consisting of the (so far)
        loaded results of the search, in order of search relevance.

        Run the 'virtfolder' ('vf') without arguments to exit the view
        and return to the folder view.
        """
        if args.strip():
            self.search2(args)
        else:
            if not hasattr(self.C, 'lastsearch') or self.C.lastsearch is None:
                print("no previous search") # or "no match"? No match would look more like trying to resume an exhausted search.
                return
            self.C.lastsearchpos += 10
            self.search2(self.C.lastsearch, offset=self.C.lastsearchpos)
    def search2(self, args, offset=0, pagesize=10):
        # This is a separate function from do_search as it is called from more
        # than one place, so we can't wrap it as a base command.
        C = self.C
        C.lastsearch = args
        C.lastsearchpos = offset
        C.lastcommand="search"
        data, matches = self.search(args, offset, pagesize)
        res = []
        for i in range(len(data)):
            headers = data[i]
            match = matches[i]
            headers = headers.split('\r\n')
            subject = filter(lambda x: x.lower().startswith("subject: "), headers)
            if len(subject) == 0:
                subject = "(no subject)"
            else:
                subject = subject[0]
            uid = filter(lambda x: x.lower().startswith("x-mailnex-uid: "), headers)
            if len(uid) != 1:
                # This should only happen if mailnex has been updated from a
                # version that wasn't using UIDs, or the DB is somehow
                # corrupted but working.
                # TODO: Automatically update DB?
                raise Exception("Database is bad. Please re-index it")
            uid = int(uid[0].split(":")[1])
            print(u"%(rank)i (%(perc)3s %(weight)s): #%(docid)3.3i ##%(uid)i %(title)s" % {
                    'rank': match.rank + 1,
                    'docid': match.docid,
                    'title': subject,
                    'perc': match.percent,
                    'weight': match.weight,
                    'uid': uid,
                    }
                    )

            # TODO: Should build up the list of UIDs, then search for all 10
            # at once. Otherwise, the round-tripping is terrible on latent
            # connections. NOTE: our list is in order of search relevance. The
            # server prefers UID order requests, and can return results in any
            # order, and most will return in MSeq/UID order, so we'll have to
            # re-order the results when we are done. Of course, doing 1 at a
            # time doesn't incur this (but is slower)
            # TODO: A better alternative would be to store the UIDs to the
            # virtfolder list directly. The global MSeq can be shown in the
            # headers as a result of the fetch there. The user doesn't really
            # get to see the MSeq until then, anyway.
            fetch = self.C.connection.uidfetch(uid, "(UID)")
            # example: fetch == [('81', '(UID 74997)')]
            if len(fetch) == 0:
                print("  ^ Message no longer exists")
            mseq = int(fetch[0][0])
            res.append(mseq)
        if len(res) == 0:
            print("No match") # TODO: Better message
        else:
            if offset == 0:
                # This is a new search, so clear any existing virtfolder and
                # start fresh. This prevents appending to a previous search or
                # previous manually set virtfolder
                self.do_virtfolder("")
            # TODO: How to handle when the virtfolder is exited, but the user asks for
            # more results (currently, creates a new virtfolder starting with
            # the next results; the first results are forgotten entirely).
            #
            # Should we just document this as expected behavior, or try to
            # handle it in some intelligent manner?
            if not self.C.virtfolder:
                self.C.virtfolderSavedSelection = (self.C.currentMessage, self.C.nextMessage, self.C.prevMessage, self.C.lastList)
                self.C.currentMessage = 1
                self.C.nextMessage = 1
                self.C.prevMessage = None
                self.C.lastList = []
                self.C.virtfolder=[]
                self.setPrompt("mailnex (vf-search)> ")
            self.C.virtfolder.extend(res)


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

    def compl_set(self, document, complete_event):
        topics = []
        this_word = document.get_word_before_cursor()
        for opt in self.C.settings:
            if opt.name.startswith(this_word):
                topics.append(opt.name)
        topics.sort(cmp=lambda x,y: cmp(x.lower(), y.lower()))
        for i in topics:
            yield cmdprompt.prompt_toolkit.completion.Completion(i, start_position=-len(this_word))
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
                    print("Default:", opt.value)
                    opt.value = oldval
                    print("current:", opt.value)
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
            self.C.virtfolderExtra = None
            self.setPrompt("mailnex> ")
        else:
            if self.C.virtfolder:
                # Args consists of virtfolder numbers. So, create a new list
                # based on the old list
                newvf = []
                for i in args:
                    newvf.append(self.C.virtfolder[i - 1])
                args = newvf
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
        rows = self.getRows(adjust=-1)
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
        rows = self.getRows(adjust=-1)
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
    options.addOption(settings.StringOption("addresssearchcmd", "khard email", doc="""Command to use for searching addresses
    Used for address completion (e.g. in the ~h command when editing a message).
    Command output is expected to be the address, a tab, the name, and then
    optionally another tab and an identifier (e.g. name of address book or
    resource).
    
    Should work with at least khard and abook (khard email) (abook --mutt-query).
    abook currently doesn't work for unknown reasons."""))
    options.addOption(settings.StringOption("agent-shell-lookup", None, doc="""Command to use for obtaining an account password.
    If set, will be used when searching for account password credentials.
    Useful for rolling your own method, such as decrypting a credential using GPG to decrypt a file.
    You may also set 'agent-shell-lookup-HOST' for any acount on host HOST,
    'agent-shell-lookup-USER@HOST' for account USER on host HOST
    'agent-shell-lookup-PROTO/USER@HOST' for account USER on host HOST using
    protocol PROTO (e.g. imaps), or 'agent-shell-lookup-PROTO/USER@HOST:PORT'
    for account USER on host HOST using protocol PROTO (e.g. imaps) on port
    PORT.
    Each section is optional. The most specific descriptor will be used.
    See 'help authentication' for more information.
    """))
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

    options.addOption(settings.StringOption("attrlist", "NUROSPMFATK+-JD", doc=
        """Character mapping for attribute in headline.

        Characters represent: new, unread but old, new but read, read and old,
        saved, preserved, mboxed, flagged, answered, draft, killed, thread
        start, thread, junk, and deleted. (deleted is a mailnex extension)

        Currently, we don't support saved, preserved, mboxed, killed, threads,
        or junk.

        Default mailx (presumably POSIX) is "NUROSPMFATK+-J".
        BSD style uses "NU  *HMFATK+-J.", which is read messages aren't
        marked, and saved/preserved get different letters (presumably 'Held'
        instead of 'Preserved'). Neither POSIX nor BSD style represent deleted
        messages in the headlines.
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
    options.addOption(settings.StringOption("format_header","{header}: {value}", doc="""Format string for email headers, like headline.

    The format string follows the python format specification.
    Fields are 'header' and 'value'. Also 't' is supported for terminal settings.

    Ex: "{header}: {t.bold}{value}{t.normal}"

    Individual headers can be formatted differently by naming them in lower
    case after an underscore. For example, so set the subject line in bold
    blue:

        format_header_subject={t.bold_blue}{header}: {value}{t.normal}

    To format preferred headers (those listed in 'headerorder'), use 'format_header_PREF'.

    The format used will be the first of a header name match, header_PREF, and
    lastly, 'format_header' itself.
    """))


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
    options.addOption(settings.StringOption("headlinerows", "terminal<25",
        doc="""Number of rows to display for h, z, and Z commands.

        May be a positive integer, or the special value "terminal",
        which will use the terminal rows count reduced by screen
        overhead (e.g. a 25 row terminal screen might show 16 rows)

        May also be "terminal<NN" where NN is the maximum number of
        rows to display.

        No matter what, at least 1 row is always shown, even if it
        would scroll off the top of the terminal.
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
    options.addOption(settings.FlagsOption("ignoredmimeheaders", [
        'content-transfer-encoding',
        'mime-version',
        ], doc="Mime Headers to ignore (as opposed to message headers). See also 'ignoredheaders'."))
    options.addOption(settings.FlagsOption("mimeheaderorder", [
        #TODO: a default ordering?
        ], doc="Prefered order of MIME headers. See also 'headerorder'."))

    options.addOption(settings.StringOption("PAGER", "internal"))
    options.addOption(settings.StringOption("pipe", None, doc="""Filter content prior to display using command.

        The given command is interpreted by the system shell. If the command
        contains '%f', the data to be filtered will be written to a temporary
        file and the file name will replace the '%f'. Otherwise, the data is
        given to the filter's stdin. The filter's stdout is then used to
        replace the content for display.

        The following escapes are interpreted:

            %t      mime type of message part
            %s      mime subtype of message part

        This is intended for content matching, and the following search order
        is performed based on the part's mime type:

            pipe-type/subtype
            pipe-type
            pipe

        Example types are 'text' or 'application'. Example subtypes are 'html' or 'octect-stream'

        Example use:

            set pipe-text/html=links2 -dump %f

        See also 'pipe-ienc' and 'pipe-oenc'
        """))
    options.addOption(settings.StringOption("pipe-ienc", None, doc="""Set input encoding for filter command.

    This encodes the data stream using the given encoding before sending the
    data to the command.

    mailnex internally converts all data to unicode when reading. This data
    must be encoded into a valid character stream for a program to interpret
    it. This setting indicates what encoding the command needs for its input.

    If unspecified, or if using the special value "same", mailnex will attempt
    to use the charset specified for the message part to be filtered. If no
    charset is specified in the message, mailnex assumes utf-8 (which should
    be safe, since no charset on the input should mean ASCII, a subset of
    utf-8)

    The search order is:

        pipe-ienc-TYPE/SUBTYPE
        pipe-ienc-TYPE
        pipe-ienc

    where TYPE and SUBTYPE are the mime content-type.

    NOTE: some message parts have in-band charset information in addition to
    the mime specified charset. In this case, using a value other than 'same'
    is likely to confuse a filter that interprets the inband specifier. For
    example, some text/html messages do this.

    NOTE: Any encoding that python understands can be used here. You should,
    however, only specify charset encodings. For example, specifying base64
    won't work, because it expects a byte stream, not a unicode stream.

    See also 'pipe' and 'pipe-oenc'
    """))
    options.addOption(settings.StringOption("pipe-oenc", None, doc="""Set output encoding for filter command.

    This decodes the data stream using the given encoding after reading the
    data from the command.

    mailnex internally converts all data to unicode when reading. This data
    must be encoded into a valid character stream for a program to interpret
    it. This setting indicates what encoding the command uses for its output.

    If unspecified, mailnex will assume utf-8 encoding. If the special value
    "same" is given, mailnex will use the same encoding as was used for the
    input of the filter (which might be different from the charset of the
    original message part, unless ienc is also set to "same").

    The search order is:

        pipe-ienc-TYPE/SUBTYPE
        pipe-ienc-TYPE
        pipe-ienc

    where TYPE and SUBTYPE are the mime content-type.

    See also 'pipe' and 'pipe-ienc'
    """))
    options.addOption(settings.StringOption("pgpkey", None, doc="PGP key search string. Can be an email address, UID, or fingerprint as recognized by gnupg. When unset, try to use the from field."))
    options.addOption(settings.BoolOption('showstructure', True, doc="Set to display the structure of the message between the headers and the body when printing."))
    options.addOption(settings.StringOption('smtp', None, doc="""Set to an smtp/submission URI to send messages via SMTP instead of local sendmail agent.

        Supported URL schemes will be "smtp://", "smtps://", and "submission://".
        Of the three, 'submission' is recommended.

        For now, only smtps:// and smtp+plain:// are implemented.

        The rest is similar to imap scheme URLs used in the folder setting and
        command. If a username isn't specified here, an attempt will be made
        to send the message without a login on the server. If a username is
        specified, password retrieval will ocur like with imap, but using
        smtp, smtps, or submission as the protocol part.

        NOTE: This feature is in progress. Only 'smtps' is actually supported so far.
        """))
    options.addOption(settings.BoolOption('usekeyring', True, doc="Set to attempt to use system keyrings for password storage"))
    return options

def instancemethod(func, obj, cls):
    """Make function an instance method bound to an object.

    This is the complement to the builtin staticmethod and classmethod.

    This is somewhat black magic.
    See http://users.rcn.com/python/download/Descriptor.htm
    See https://stackoverflow.com/a/1015405/4504704  (from https://stackoverflow.com/questions/1015307/python-bind-an-unbound-method)
    """
    return func.__get__(obj, cls)

def interact(invokeOpts):
    cmd = Cmd(prompt="mailnex> ", histfile=histFile)
    C = Context()
    C.dbpath = defDbFile # TODO: allow get from config file
    C.lastcommand=""
    # Setup some functions for outputting info. Ideally these would be
    # configurable by our settings; e.g. should usage/error messages from
    # mailnex have color? What color does the user want to use?
    # These could also write to a log or something...
    # Currently, these assume we have a blessings instance in C.t
    def printInfo(self, string):
        print(self.t.cyan(string))
    def printWarning(self, string):
        print(self.t.yellow(string))
    def printError(self, string):
        print(self.t.red(string))
    C.printInfo = instancemethod(printInfo, C, Context)
    C.printWarning = instancemethod(printWarning, C, Context)
    C.printError = instancemethod(printError, C, Context)
    cmd.C = C
    options = getOptionsSet()
    C.settings = options
    postConfFolder = None
    global confFile
    cmd.C.accounts = {}
    if invokeOpts.config:
        confFile = invokeOpts.config
    if confFile:
        # Walk through the config file
        with open(confFile) as conf:
            print("reading conf from", confFile)
            postConfFolder = cmd.processConfig(confFile, 1, conf)
    if invokeOpts.account:
        res = cmd.processConfig("cmdline account", 1, ['account {}'.format(invokeOpts.account)])
        if res:
            postConfFolder = res
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
    parser.add_argument('--account','-A', help='run account command after config file is read')
    args = parser.parse_args()
    interact(args)

if __name__ == "__main__":
    main()
