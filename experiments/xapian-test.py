#!/usr/bin/env python

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

import xapian
import os

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

def search(dbpath, querystring, offset=0, pagesize=20):
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
    for match in enquire.get_mset(offset, pagesize):
        fname = match.document.get_data()
        print u"%(rank)i (%(perc)3s %(weight)s): #%(docid)3.3i %(title)s" % {
                'rank': match.rank + 1,
                'docid': match.docid,
                'title': fname,
                'perc': match.percent,
                'weight': match.weight,
                }
        matches.append(match.docid)

if __name__ == "__main__":
    import sys
    #index("/home/john", "./db2/")
    search("./db2/", " ".join(sys.argv[1:]))
