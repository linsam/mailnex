import gpgme
import io
import time
import sys

if len(sys.argv) < 2:
    print "give us a key search"
    sys.exit(1)

# build up algo list
algos = {}
for sym in dir(gpgme):
    if sym.startswith('PK_'):
        algos[getattr(gpgme,sym)] = sym[3:]
# build up hash (message disgest) list
mds = {}
for sym in dir(gpgme):
    if sym.startswith('MD_'):
        mds[getattr(gpgme,sym)] = sym[3:]

ctx = gpgme.Context()
#ctx.keylist_mode |= gpgme.KEYLIST_MODE_SIGS
thekey = None
found = 0
for key in ctx.keylist(sys.argv[1], True):
    found += 1
    thekey = key
    print("{}{}{}{}{}{}{}{}{} {}".format(
        "C" if key.can_certify else " ",
        "S" if key.can_sign else " ",
        "E" if key.can_encrypt else " ",
        "A" if key.can_authenticate else " ",
        "D" if key.disabled else " ",
        "X" if key.expired else " ",
        "R" if key.revoked else " ",
        "!" if key.invalid else " ",
        "s" if key.secret else " ",
        key.uids[0].uid,
        ))
    for sub in key.subkeys:
        print("{}{}{}{}{}{}{}{}{} {:5} {} {} {} [{}]".format(
            "C" if sub.can_certify else " ",
            "S" if sub.can_sign else " ",
            "E" if sub.can_encrypt else " ",
            "A" if sub.can_authenticate else " ",
            "D" if sub.disabled else " ",
            "X" if sub.expired else " ",
            "R" if sub.revoked else " ",
            "!" if sub.invalid else " ",
            "s" if sub.secret else " ",
            algos[sub.pubkey_algo] if (sub.pubkey_algo in algos) else "??",
            sub.length,
            sub.fpr,
            time.ctime(sub.timestamp),
            time.ctime(sub.expires) if sub.expires else "none",
            ))
    print

if found != 1:
    print "Need a unique key"
    exit(1)
ctx.signers = (key,)
ctx.armor = True
sigs = ctx.sign(sys.stdin, sys.stdout, gpgme.SIG_MODE_DETACH)
for sig in sigs:
    print("{} {} {} {} {} {}".format(
        sig.fpr,
        algos[sig.pubkey_algo] if sig.pubkey_algo in algos else "??",
        mds[sig.hash_algo] if sig.hash_algo in mds else "??",
        sig.sig_class,
        sig.type,
        time.ctime(sig.timestamp),
        ))
