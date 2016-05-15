import sys
import cmd

class mycmd(cmd.Cmd):
    def do_test(self, args):
        """A test function"""
        print "in test"

    def do_quit(self, args):
        print "exiting"
        sys.exit(0)

    def do_shell(self, args):
        print "Shell escape"


    def emptyline(self):
        print "Empty"

    def default(self, args):
        if args == "EOF":
            print
            sys.exit(0)

        else:
            print "Unknown command '%s'" % args

    def help_meh(self):
        print "blah"

c = mycmd()
c.intro = "Welcome!"
c.cmdloop()
