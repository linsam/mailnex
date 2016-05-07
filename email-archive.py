#!/usr/bin/env python

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
# IMAP4r1: rfc2060
# IDLE command: rfc2177
# Namespace: rfc2342
#

import xapian
import os
import sys
import imaplib
import getpass
imaplib._MAXLINE *= 10
import re
import keyring

class Context(object):
    def __init__(self):
        object.__init__(self)
        self.connection = None

def test():
    M = imaplib.IMAP4("localhost")
    #print dir(M)
    #print M.capabilities
    if "STARTTLS" in M.capabilities:
        if hasattr(M, "starttls"):
            res = M.starttls()
        else:
            print "Warning! Server supports TLS, but we don't!"
            print "Warning! You should upgrade your python-imaplib package to 3.2 or 3.4 or later"
    M.login(getpass.getuser(), getpass.getpass())
    res = M.list()
    print res
    res = M.lsub()
    for i in res[1]:
        print i
    res = M.namespace()
    print res
    M.select()
    M.close()
    M.logout()
    return
    i = 1
    while True:
        try:
            typ,data = M.fetch(i,'(FLAGS)')
        except:
            break
        print i, imaplib.ParseFlags(data[0])
        i += 1

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
    in quoted strings, it doesn't interpret leteral strings at all, and
    accepts quotes anywhere.
    """


    curlist=[]
    lset=[]
    lset.append(curlist)
    curtext=[]
    inquote = False
    inspace = True
    for c in text:
        #print "Processing char", repr(c)
        if c == ' ' or c == '\t':
            if inquote:
                #print " keep space, we are quoted"
                curtext.append(c)
                continue
            if not inspace:
                #print " End of token. Append completed word to list:", curtext
                inspace = True
                curlist.append("".join(curtext))
                curtext=[]
                continue
            continue
        inspace = False
        if c == '"': #TODO single quote too? -- no.
            if inquote:
                # TODO: Does ending a quote terminate an atom?
                #print " Leaving quote"
                inquote = False
            else:
                # TODO: Are we allowed to start a quote mid-atom?
                #print " Entering quote"
                inquote = True
            continue
        if c == '(':
            if inquote:
                #print " keep paren, we are quoted"
                curtext.append(c)
                continue
            if len(curtext):
                raise Exception("Need space before open paren?")
            #print " start new list"
            curlist=[]
            lset.append(curlist)
            inspace = True
            continue
        if c == ')':
            if inquote:
                #print " keep paren, we are quoted"
                curtext.append(c)
                continue
            if len(curtext):
                #print " finish atom before finishing list", curtext
                curlist.append("".join(curtext))
                curtext=[]
            t = curlist
            lset.pop()
            if len(lset) < 1:
                raise Exception("Malformed input. Unbalanced parenthesis: too many close parenthesis")
            curlist = lset[-1]
            #print " finish list", t
            curlist.append(t)
            inspace = True
            continue
        #print " normal character"
        curtext.append(c)
    if inquote:
        raise Exception("Malformed input. Reached end without a closing quote")
    if len(curtext):
        print "EOF, flush leftover text", curtext
        curlist.append("".join(curtext))
    if len(lset) > 1:
        raise Exception("Malformed input. Unbalanced parentheses: Not enough close parenthesis")
    #print "lset", lset
    #print "cur", curlist
    #print "leftover", curtext
    return curlist

def testq(C, text):
    try:
        print processAtoms(text)
    except Exception,ev:
        print ev


def connect(C, args):
    if C.connection:
        print "disconnecting"
        C.connection.close()
        C.connection.logout()
    print "Connecting to '%s'" % args
    M = imaplib.IMAP4(args)
    #print dir(M)
    print M.capabilities
    if "STARTTLS" in M.capabilities:
        if hasattr(M, "starttls"):
            res = M.starttls()
        else:
            print "Warning! Server supports TLS, but we don't!"
            print "Warning! You should upgrade your python-imaplib package to 3.2 or 3.4 or later"
    pass_ =  keyring.get_password("mailnex",getpass.getuser())
    if not pass_:
        pass_ = getpass.getpass()
    typ,data = M.login(getpass.getuser(), pass_)
    print typ, data
    C.connection = M
    typ,data = M.select()
    print typ, data

def indexInbox(C):
    #M = imaplib.IMAP4("localhost")
    #M.login("john", getpass.getpass())
    M = C.connection
    if not M:
        print "No connection :-("
        return
    M.select()
    i = 1
    seen=0

    db = xapian.WritableDatabase(C.dbpath, xapian.DB_CREATE_OR_OPEN)
    termgenerator = xapian.TermGenerator()
    termgenerator.set_stemmer(xapian.Stem("en"))

    while True:
        try:
            typ,data = M.fetch(i, '(UID BODYSTRUCTURE)')
            #print typ
            #print data
            typ,data = M.fetch(i, '(BODY.PEEK[HEADER] BODY.PEEK[1])')
            #print typ
            #print data
            #print data[0][1]
            #print "------------ Message %i -----------" % i
            #print data[1][1]

            headers = data[0][1]
            # TODO: Proper header parsing
            headers = headers.split("\r\n")
            origh = headers
            headers = filter(lambda x: "content-type:" in x.lower(), headers)
            #print headers
            #if len(headers) == 0:
            #    print data[1][1]
            print "\r%i"%i,
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
    print 
    print "Done!"
    M.close()
    M.logout()

def print_mail(C, index):
    M = C.connection
    # TODO: Decorate with a connection_check
    if not M:
        print "no connection"
        return
    M.select()
    ret,data = M.fetch(index, '(BODY.PEEK[HEADER] BODY.PEEK[1])')
    import subprocess
    s = subprocess.Popen("less", stdin=subprocess.PIPE)
    s.communicate(data[0][1] + data[1][1])

def unpackStruct(data, depth=1, value=""):
    if isinstance(data[0], list):
        # We are multipart
        for i in range(len(data)):
            if not isinstance(data[i], list):
                break
        print "%s%*s%s/%s" % (value, depth * 2, " ", "multipart", data[i])
        j = 1
        for dat in data[:i]:
            unpackStruct(dat, depth + 1, value + '.' + str(j))
            j += 1
    else:
        print "%s%*s%s/%s" % (value, depth * 2, " ", data[0], data[1])

def print_structure(C, index):
    M = C.connection
    # TODO: Decorate with a connection_check
    if not M:
        print "no connection"
        return
    res, data = M.fetch(index, '(BODYSTRUCTURE)')
    #print data
    for entry in data:
        #print entry
        try:
            # We should get a list of the form (ID, DATA)
            # where DATA is a list of the form ("BODYSTRUCTURE", struct)
            # and where struct is the actual structure
            d = processAtoms(entry)
            val = str(d[0])
            d = d[1]
        except Exception, ev:
            print ev
            return
        if d[0] != "BODYSTRUCTURE":
            print "fail?"
            print d
            return
        unpackStruct(d[1], value=val)


def headers(C):
    M = C.connection
    if not M:
        print "no connection"
        return
    print "TBD"

def namespace(C):
    M = C.connection
    res,data = M.namespace()
    #print res
    try:
        data = processAtoms(data[0])
    except Exception, ev:
        print ev
        return
    print "Personal namespaces:"
    for i in data[0]:
        print i
    print "Other user's namespaces:"
    for i in data[1]:
        print i
    print "Shared namespaces:"
    for i in data[2]:
        print i


def index(datapath, dbpath):
    db = xapian.WritableDatabase(dbpath, xapian.DB_CREATE_OR_OPEN)
    termgenerator = xapian.TermGenerator()
    termgenerator.set_stemmer(xapian.Stem("en"))
    for root, dirs, files in os.walk(datapath):
        files = filter(lambda x: x.endswith('.txt'), files)
        for f in files:
            if len(root) > 1 and root.endswith('/'):
                root = root[:-1]
            title = root+'/'+f
            print title,
            ident = hash(title) & 0xffffffff
            print ident
            # create and register document
            doc = xapian.Document()
            termgenerator.set_document(doc)
            # index fields
            termgenerator.index_text(title, 1, 'S')
            # index general search
            termgenerator.index_text(title)
            termgenerator.increase_termpos()
            termgenerator.index_text(open(title).read())
            # Set the data. Some setups put a JSON set of fields for display
            # purposes. We'll just store the filename for now, esp. since
            # that's just about all we're storing anyway.
            doc.set_data(title)
            # Not sure about this identifier stuff; copied from example
            idterm = u"Q" + str(ident)
            doc.add_boolean_term(idterm)
            db.replace_document(idterm, doc)

def search(C, querystring, offset=0, pagesize=10):
    dbpath = C.dbpath
    db = xapian.Database(dbpath)

    queryparser = xapian.QueryParser()
    queryparser.set_stemmer(xapian.Stem("en"))
    queryparser.set_stemming_strategy(queryparser.STEM_SOME)
    queryparser.add_prefix("file", "S")
    queryparser.set_database(db)
    query = queryparser.parse_query(querystring, queryparser.FLAG_BOOLEAN | queryparser.FLAG_WILDCARD)
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
        print u"%(rank)i (%(perc)3s %(weight)s): #%(docid)3.3i %(title)s" % {
                'rank': match.rank + 1,
                'docid': match.docid,
                'title': fname,
                'perc': match.percent,
                'weight': match.weight,
                }
        matches.append(match.docid)

    #print data[0]

#def compl(*args, **kwargs):
    #print
    #print "  Compl:"
    #print "    args:", args
    #print "    kwargs:", kwargs
    #print "    buffer:", repr(readline.get_line_buffer())
    #print "    type:", repr(readline.get_completion_type())
    #print "    range:", repr(readline.get_begidx(), readline.get_endidx())
    #print "    delims:", repr(readline.get_completer_delims())
    #readline.redisplay()
    #print >>sys.stderr, "wjat"
    #print "wjat"
    #print "wjat"
    #print "wjat"
def compl(text, state):
    #print type(state), state
    #print "    type:", repr(readline.get_completion_type())
    if state < 2:
        return "hello%i"% state
    return None

commands="help search quit exit reply print pipe".split()

def cmdcompl(text, state):
    #print
    #print "text",text
    #print "state",state
    #print "cmds",commands
    matches = [val for val in commands if val.startswith(text)]
    #print "match",matches
    if len(matches) > state:
        return matches[state]
    return None

def dispmatch(substitution, matches, longestlen):
    print "sub:",substitution
    i = 0
    for m in matches:
        print i, m
        i += 1

def interact():
    import readline
    try:
        readline.read_history_file("mailxhist")
    except IOError:
        print ("no hist file")
    readline.set_history_length(1000)
    readline.parse_and_bind("tab: complete")
    import atexit
    atexit.register(readline.write_history_file, "mailxhist")
    readline.set_completer(cmdcompl)
    #readline.set_completion_display_matches_hook(dispmatch)
    lastcommand=""
    C = Context()
    C.dbpath = "./maildb1/" # TODO: get from config file
    while True:
        try:
            line = raw_input("mail> ")
        except EOFError:
            print
            if C.connection:
                print "disconnecting"
                C.connection.close()
                C.connection.logout()
            break
        except KeyboardInterrupt:
            print
            if C.connection:
                print "disconnecting"
                C.connection.close()
                C.connection.logout()
            break
        #print "You typed:",repr(line)
        if line == "quit" or line == 'q' or line == 'exit' or line == 'x':
            # TODO: q commits, x aborts.
            break
        elif "search" in line:
            lastsearch = line[7:]
            lastsearchpos = 0
            search(C, lastsearch)
            lastcommand="search"
        elif line == "test":
            test()
        elif line == "index":
            indexInbox(C)
        elif line[:7] == "connect":
            connect(C, line[8:])
        elif line == "namespace":
            namespace(C)
        elif line[:5] == "testq":
            testq(C, line[6:])
        elif line[:9] == "structure":
            print_structure(C, line[10:])
        elif line.isdigit():
            print_mail(C, int(line))
        elif line == "":
            # Repeat/continue
            if lastcommand=="search":
                lastsearchpos += 10
                search(C, lastsearch, offset=lastsearchpos)
            else:
                print "TBD: next message"

        else:
            print "bad command"

if __name__ == "__main__":
    import sys
    #index("/home/john", "./db2/")
    #search("./db2/", " ".join(sys.argv[1:]))
    if len(sys.argv) < 2 or "-h" in sys.argv or "--help" in sys.argv or sys.argv[1] == "help":
        print "Usage:"
        print "   index"
        print "   search [terms]"
        print "   interact"
    elif sys.argv[1] == "index":
        indexInbox()
    elif sys.argv[1] == "search":
        search("./maildb1/", " ".join(sys.argv[2:]))
    elif sys.argv[1] == "interact":
        interact()
    else:
        print "Unknown command '%s'. Try help" % sys.argv[1]
