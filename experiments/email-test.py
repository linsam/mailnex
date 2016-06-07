#import email.message
import email.mime.multipart
import email.mime.application
import email.mime.text
import email.encoders
import tempfile
import os
import sys

# Some notes:
#
# RFC 822, then 2822, spec the message format.
# 2822 states that there are only 2 header fields that MUST be in every
# message:
#
#   From:
#   Date:
#
# The "Date" header indicates when the sender queues the message for delivery
# (when the message is in its final form). It is not when it actually enters
# transit (e.g. an offline user may type an email and hit send or enqueue or
# whatever, then connect to the network an hour later. The earlier time goes
# into the Date field)
#
# The 'From' header specifies the author or responsible entity for writing the
# message. Related fields include 'Sender', which specifies the entity
# responsible for transmitting the message (e.g. a secretary might 'send' a
# message that was 'written' by his boss). Sender should not be specified if
# identical to From. Another related field is 'Reply-To', which indicates that
# replies should not go to the mailbox specified by 'From'.
#
# Other interesting notes:
#   * Bcc header can be used in 4 ways:
#       * Stripped after composing a message prior to sending to recipients
#       * Stripped to non Bcc recipients but kept for Bcc recipients (2 actual
#         messages sent on-the-wire)
#       * Stripped prior to sending, but added to Bcc recipients with just
#         their name (1 regular message + n Bcc messages on-the-wire)
#       * Stripped of content before sending, indicating that one or more
#         unnamed persons received a copy. This method may actually be
#         combined with methods 2 or 3 for the primary receivers.
#     For composing mail, perhaps these should be presented as options.
#     For viewing mail, should show a flag or something for Bcc that is empty.
#     When replying, the replier may leak information if they were in the Bcc
#     list. 2822 notes that each of the above Bcc stripping methods has
#     varying security concerns. (see section 5)
#   * Replies should move "To" entities to the Cc header, except for the
#     person being replied to. The spec does mention that a application may
#     duplicate the destination addresses for the response, but says it
#     doesn't address the implications of doing this. We ought to allow either
#     way. In particular in some office contexts, the To list is used for
#     those active in the discussion, and the Cc list is for people who should
#     be aware of, but not participating in, the discussion (e.g. Engineers on
#     the To field and Managers in the Cc field, when dealing with a low-level
#     technical problem. The Managers don't need to make decisions (until they
#     do), but need to be aware that a problem is being worked upon).
#
# Beyond the required headers, there is a list of SHOULD headers:
#
#   * Message-ID
#   * In-Reply-To (where relevent)
#   * References (where relevant)
#
#

f=tempfile.mkstemp()
res = os.system("vim %s" % f[1])
if (res != 0):
    os.close(f[0])
    os.unlink(f[1])
    print "Edit aborted"
    sys.exit(1)
dat = file(f[1]).read()
if len(dat) == 0:
    os.close(f[0])
    os.unlink(f[1])
    print "No message"
    sys.exit(1)
if dat == "\n":
    os.close(f[0])
    os.unlink(f[1])
    print "No message (2)"
    sys.exit(1)
tpart = email.mime.text.MIMEText(dat)
tpart.set_charset("utf-8")
# Some fun facts:
#  * Sup and Thunderbird use the first Content-Transfer-Encoding they find.
#  * encode_quopri adds a Content-Transfer-Encoding header rather than modify
#    any previously existing one.
#
#  * encode_quopri makes *all* spaces into =20, not just the one or ones at
#    the end of a line. I'm pretty sure the rfc's only specify doing the end
#    of line that way, but I'm too tired to look up a reference.
#
#    Of course, sup and thunderbird are okay with =20 everywhere, since that
#    is fine and to spec. It is just quite wastefull (though typically not as
#    bad as base64).
#
del tpart['Content-transfer-encoding']
email.encoders.encode_quopri(tpart)
os.close(f[0])
m=open(f[1], "w")
m.write("\r\n".join(tpart.as_string().split('\n')))
m.write("\r\n") # Final EOL
m.close()

f2=tempfile.mkstemp()
os.close(f2[0])
res = os.system("gpg2 --clearsign <%s >%s" % (f[1], f2[1]))
os.unlink(f[1])
if res != 0:
    print "GPG didn't sign the message: %i" % res
    os.unlink(f2[1])
    sys.exit(1)
sdat = file(f2[1]).read()
os.unlink(f2[1])

osdat = []
insig = False
for line in sdat.split("\n"):
    if "-----BEGIN PGP SIGNATURE-----" in line:
        insig = True
    if not insig:
        continue
    osdat.append(line)
if len(osdat) == 0:
    print "Failed to find pgp sig"
    sys.exit(1)
sdat = "\r\n".join(osdat)

def nullEncoder(dat):
    return dat

spart = email.mime.application.MIMEApplication(sdat, 'pgp-signature', nullEncoder)

# TODO: How to *know* that GPG used SHA1?
#       It *looks* like the first line of the "SIGNED MESSAGE" (not the
#       "SIGNATURE" itself) gets a line like "Hash: SHA1"
m = email.mime.multipart.MIMEMultipart("signed", micalg="pgp-sha1", protocol="application/pgp-signature")
m['Subject'] = "Test Message"
m['From'] = "Test User <test@example.com>"
m['To'] = "Jane Doe <jdoe@example.com>"

m.attach(tpart)
m.attach(spart)
print "-----------------------"
print
print m.as_string()
out = open("out.eml", "wb")
out.write(m.as_string())

