import magic
import email.mime
import string
from . import cmdprompt
from .pathcompleter import *
try:
    import gpgme
    haveGpgme = True
except ImportError:
    haveGpgme = False

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
                    res = self.default(line)
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

def doAttachments(editor, m):
    """Given an editor instance and a message, apply the attachment list of the editor to the message.

    Returns a message consisting of the given message with added attachments.
    """
    for attach in editor.attachlist:
        try:
            with open(attach, "rb") as f:
                data = f.read()
                mtype = magic.from_buffer(data, mime=True)
        except KeyboardInterrupt:
            print("Aborting read of %s" % attach)
            raise Exception("read aborted")
        except Exception as err:
            print("Error reading file %s for attachment" % attach)
            raise err
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
    return m
