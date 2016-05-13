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

debug = 0

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
from functools import wraps
# xapian search engine
import xapian
# various email helpers
import imaplib
imaplib._MAXLINE *= 10
import email
import mailbox
# password prompter
import getpass
# Password manager
import keyring
# Configuration and other directory management
import xdg.BaseDirectory
# shell helper
import cmd

confFile = xdg.BaseDirectory.load_first_config("linsam.homelinux.com","mailnex","mailnex.conf")

class Context(object):
    def __init__(self):
        object.__init__(self)
        self.connection = None

# Some decorators
def needsConnection(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.C.connection:
            print("no connection")
        else:
            return func(self, *args, **kwargs)
    return wrapper

def unpackStruct(data, depth=1, value=""):
    if isinstance(data[0], list):
        # We are multipart
        for i in range(len(data)):
            if not isinstance(data[i], list):
                break
        print("%s   %s/%s" % (value, "multipart", data[i]))
        j = 1
        for dat in data[:i]:
            unpackStruct(dat, depth + 1, value + '.' + str(j))
            j += 1
    else:
        print("%s   %s/%s" % (value, data[0], data[1]))

def processAtoms(text):
    """Process a set of IMAP ATOMs

    ATOMs are roughly space separated text that can be quoted and can contain
    lists of other atoms by wrapping in parenthses

    According to the RFC:
        Data can be an atom, number, string, parenthesized list, or NIL.
        An atom consists of one or more non-special characters.
        A number consists of digits
        A string is either literal (has a length in braces followed by data of
        that length, followed by CRLF) or quoted (surrounded by double quotes)
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
    in quoted strings, it doesn't interpret literal strings at all, and
    accepts quotes anywhere.
    """


    curlist=[]
    lset=[]
    lset.append(curlist)
    curtext=[]
    inquote = False
    inspace = True
    for c in text:
        #print("Processing char", repr(c))
        if c == ' ' or c == '\t':
            if inquote:
                #print(" keep space, we are quoted")
                curtext.append(c)
                continue
            if not inspace:
                #print(" End of token. Append completed word to list:", curtext)
                inspace = True
                curlist.append("".join(curtext))
                curtext=[]
                continue
            continue
        inspace = False
        if c == '"': #TODO single quote too? -- no.
            if inquote:
                # TODO: Does ending a quote terminate an atom?
                #print(" Leaving quote")
                inquote = False
            else:
                # TODO: Are we allowed to start a quote mid-atom?
                #print(" Entering quote")
                inquote = True
            continue
        if c == '(':
            if inquote:
                #print(" keep paren, we are quoted")
                curtext.append(c)
                continue
            if len(curtext):
                raise Exception("Need space before open paren?")
            #print(" start new list")
            curlist=[]
            lset.append(curlist)
            inspace = True
            continue
        if c == ')':
            if inquote:
                #print(" keep paren, we are quoted")
                curtext.append(c)
                continue
            if len(curtext):
                #print(" finish atom before finishing list", curtext)
                curlist.append("".join(curtext))
                curtext=[]
            t = curlist
            lset.pop()
            if len(lset) < 1:
                raise Exception("Malformed input. Unbalanced parenthesis: too many close parenthesis")
            curlist = lset[-1]
            #print(" finish list", t)
            curlist.append(t)
            inspace = True
            continue
        #print(" normal character")
        curtext.append(c)
    if inquote:
        raise Exception("Malformed input. Reached end without a closing quote")
    if len(curtext):
        print("EOF, flush leftover text", curtext)
        curlist.append("".join(curtext))
    if len(lset) > 1:
        raise Exception("Malformed input. Unbalanced parentheses: Not enough close parenthesis")
    #print("lset", lset)
    #print("cur", curlist)
    #print("leftover", curtext)
    return curlist

class Cmd(cmd.Cmd):
    def help_hidden_commands(self):
        print("The following are hidden commands:")
        print()
        print("  h   -> headers")
        print("  p   -> print")
        print("  q   -> quit")
        print("  x   -> exit")
    def default(self, args):
        c,a,l = self.parseline(args)
        if c == 'h':
            return self.do_headers(a)
        elif c == 'p':
            return self.do_print(a)
        elif c == 'q':
            return self.do_quit(a)
        elif c == 'x':
            return self.do_exit(a)
        elif c == 'EOF':
            # Exit or Quit? Maybe make it configurable? What does mailx do?
            print
            return self.do_exit(a)
        elif args.isdigit():
            self.C.currentMessage = int(args)
            self.do_print("")
            self.C.lastcommand=""
        else:
            print("Unknown command", c)
    def emptyline(self):
        # repeat/continue last command
        if self.C.lastcommand=="search":
            self.C.lastsearchpos += 10
            self.do_search(lastsearch, offset=self.C.lastsearchpos)
        else:
            # Next message
            # TODO: mailx has a special case, which is when it picks the
            # message, e.g. when opening a box and it picks the first
            # flagged or unread message. In which case, the implicit
            # "next" command shows the message marked as current.
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
            if (self.C.currentMessage == self.C.lastMessage):
                print("at EOF")
            else:
                self.C.currentMessage += 1
                self.do_print("")

    def do_testq(self, text):
        try:
            print(processAtoms(text))
        except Exception as ev:
            print(ev)

    def do_connect(self, args):
        """Connect to the given imap host using local username.

        You should use the folder command once that's working instead.

        This function should eventually dissappear."""
        C = self.C
        if C.connection:
            print("disconnecting")
            C.connection.close()
            C.connection.logout()
        print("Connecting to '%s'" % args)
        M = imaplib.IMAP4(args)
        #print(dir(M))
        print(M.capabilities)
        if "STARTTLS" in M.capabilities:
            if hasattr(M, "starttls"):
                res = M.starttls()
            else:
                print("Warning! Server supports TLS, but we don't!")
                print("Warning! You should upgrade your python-imaplib package to 3.2 or 3.4 or later")
        pass_ =  keyring.get_password("mailnex",getpass.getuser())
        if not pass_:
            pass_ = getpass.getpass()
        typ,data = M.login(getpass.getuser(), pass_)
        print(typ, data)
        C.connection = M
        typ,data = M.select()
        print(typ, data)
        # Normally, you'd scan for the first flagged or new message and set that
        # (probably by issuing a SEARCH to the server). For now, we'll hard code
        # it to 1.
        C.currentMessage = 1
        C.lastMessage = int(data[0])

    @needsConnection
    def do_index(self, args):
        #M = imaplib.IMAP4("localhost")
        #M.login("john", getpass.getpass())
        C = self.C
        M = C.connection
        i = 1
        seen=0

        db = xapian.WritableDatabase(C.dbpath, xapian.DB_CREATE_OR_OPEN)
        termgenerator = xapian.TermGenerator()
        termgenerator.set_stemmer(xapian.Stem("en"))

        while True:
            try:
                typ,data = M.fetch(i, '(UID BODYSTRUCTURE)')
                #print(typ)
                #print(data)
                typ,data = M.fetch(i, '(BODY.PEEK[HEADER] BODY.PEEK[1])')
                #print(typ)
                #print(data)
                #print(data[0][1])
                #print("------------ Message %i -----------" % i)
                #print(data[1][1])

                headers = data[0][1]
                # TODO: Proper header parsing
                headers = headers.split("\r\n")
                origh = headers
                headers = filter(lambda x: "content-type:" in x.lower(), headers)
                #print(headers)
                #if len(headers) == 0:
                #    print(data[1][1])
                print("\r%i"%i,)
                sys.stdout.flush()
                doc = xapian.Document()
                termgenerator.set_document(doc)
                #TODO index subject, from, to, cc, etc.
                termgenerator.index_text(data[1][1])
                # Support full document retrieval but without reference info
                # (we'll have to fully rebuild the db to get new stuff. TODO:
                # store UID and such)
                doc.set_data(data[0][1])
                idterm = u"Q" + str(i)
                doc.add_boolean_term(idterm)
                db.replace_document(idterm, doc)
                i += 1
            except:
                break
        print()
        print("Done!")

    @needsConnection
    def do_print(self, args):
        C = self.C
        M = C.connection
        if args:
            try:
                index = int(args)
            except:
                print("bad arguments")
                return
        else:
            index = C.currentMessage
        ret,data = M.fetch(index, '(BODY.PEEK[HEADER] BODY.PEEK[1])')
        import subprocess
        s = subprocess.Popen("less", stdin=subprocess.PIPE)
        s.communicate(data[0][1] + data[1][1])

    @needsConnection
    def do_show(self, args):
        """Show the raw, unprocessed message"""
        C = self.C
        M = C.connection
        if args:
            try:
                index = int(args)
            except:
                print("bad arguments")
                return
        else:
            index = C.currentMessage
        ret,data = M.fetch(index, '(BODY.PEEK[HEADER] BODY.PEEK[TEXT])')
        import subprocess
        s = subprocess.Popen("less", stdin=subprocess.PIPE)
        s.communicate(data[0][1] + data[1][1])

    @needsConnection
    def do_structure(self, args):
        C = self.C
        M = C.connection
        if args:
            try:
                index = int(args)
            except:
                print("bad arguments")
                return
        else:
            index = C.currentMessage
        res, data = M.fetch(index, '(BODYSTRUCTURE)')
        #print(data)
        for entry in data:
            #print(entry)
            try:
                # We should get a list of the form (ID, DATA)
                # where DATA is a list of the form ("BODYSTRUCTURE", struct)
                # and where struct is the actual structure
                d = processAtoms(entry)
                val = str(d[0])
                d = d[1]
            except Exception as ev:
                print(ev)
                return
            if d[0] != "BODYSTRUCTURE":
                print("fail?")
                print(d)
                return
            unpackStruct(d[1], value=val)

# 254 area is interesting; actually uses literals :-/

    @needsConnection
    def do_headers(self, args):
        C = self.C
        M = C.connection
        rows = 25 # TODO get from terminal
        start = C.currentMessage / rows * rows
        # alternatively, start = C.currentMessage - (C.currentMessage % rows)
        start += 1 # IMAP is 1's based
        last = start + rows - 1
        if last > C.lastMessage:
            last = C.lastMessage
        typ, data = M.fetch("%i:%i" % (start, last), "(ENVELOPE)")
        # TODO: get rid of i and use d[0] instead?
        i = start
        for d in data:
            try:
                d = processAtoms(d)
            except:
                print("  %i  (error parsing envelope!)" % i)
                continue
            try:
                if i == C.currentMessage:
                    print("> %(num)s %(date)31s %(subject)s" % {
                            'num': d[0],
                            'date': d[1][1][0],
                            'subject': d[1][1][1],
                            }
                            )
                else:
                    print("  %(num)s %(date)31s %(subject)s" % {
                            'num': d[0],
                            'date': d[1][1][0],
                            'subject': d[1][1][1],
                            }
                            )
            except:
                print("  %i  (error displaying. Data follows)" % i, repr(d))
            i += 1

    @needsConnection
    def do_namespace(self, args):
        C = self.C
        M = C.connection
        res,data = M.namespace()
        #print(res)
        try:
            data = processAtoms(data[0])
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

    def do_search(self, args, offset=0, pagesize=10):
        C = self.C
        C.lastsearch = args
        C.lastsearchpos = offset
        C.lastcommand="search"
        dbpath = C.dbpath
        db = xapian.Database(dbpath)

        queryparser = xapian.QueryParser()
        queryparser.set_stemmer(xapian.Stem("en"))
        queryparser.set_stemming_strategy(queryparser.STEM_SOME)
        queryparser.add_prefix("file", "S")
        queryparser.set_database(db)
        query = queryparser.parse_query(args, queryparser.FLAG_BOOLEAN | queryparser.FLAG_WILDCARD)
        enquire = xapian.Enquire(db)
        enquire.set_query(query)
        matches = []
        data = []
        for match in enquire.get_mset(offset, pagesize):
            fname = match.document.get_data()
            data.append(fname)
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
            matches.append(match.docid)

        #print(data[0])

    def do_quit(self, args):
        # TODO: Synchronize and quit
        sys.exit(0)

    def do_exit(self, args):
        # TODO: Disconnect but not synchronize and quit
        sys.exit(0)

def interact():
    import readline
    try:
        readline.read_history_file("mailxhist")
    except IOError:
        print("no hist file")
    readline.set_history_length(1000)
    readline.parse_and_bind("tab: complete")
    import atexit
    atexit.register(readline.write_history_file, "mailxhist")
    cmd = Cmd()
    C = Context()
    C.dbpath = "./maildb1/" # TODO: get from config file
    C.lastcommand=""
    cmd.C = C
    cmd.prompt = "mail> "
    try:
        cmd.cmdloop()
    except KeyboardInterrupt:
        cmd.do_exit("")
    except Exception as ev:
        if debug:
            raise
        else:
            print("Bailing on exception",ev)

if __name__ == "__main__":
    import sys
    interact()

# 2357
