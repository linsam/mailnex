import gpgme
import sys

ctx=gpgme.Context()

# Check the sigs and output the text
sigs = ctx.verify(sys.stdin, None, sys.stdout)

for sig in sigs:
    try:
        key = ctx.get_key(sig.fpr)
    except gpgme.GpgmeError:
        key = None
    print "Signature on %s by %s" % (sig.timestamp, sig.fpr)
    print "   expire:", sig.exp_timestamp
    print "   status %s, summary %s, validity %s, vreason %s, wrong key %s" % (
            sig.status,
            sig.summary,
            sig.validity,
            sig.validity_reason,
            sig.wrong_key_usage
            )
    print "   notations:", sig.notations
    if not key:
        print "   UID: unknown"
    else:
        for uid in key.uids:
            print "   UID:", uid.uid
    if 0 and sig.status:
        print "st type", type(sig.status)
        #print "st dir ", dir(sig.status)
        print "st args", sig.status.args
        print "st code", sig.status.code
        print "st msg ", sig.status.message
        print "st src ", sig.status.source
        print "st stre", sig.status.strerror

