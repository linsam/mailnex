#import email.message
import email.mime.multipart
import email.mime.application
import email.mime.text
import tempfile
import os
import sys

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

