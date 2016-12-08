# Settings module
#
# Provides for Vim-like settings and such.
#
# There are several types of options, and the types support different
# operations upon them.
#
# * Boolean. These are set or unset. Operations are to set (option), clear
#   (nooption), toggle (invoption or option!), and reset to default (option&)
# * Number. These consist of an integer (I'm unaware of any float options).
#   Operations are set to a value (=), add to a value (+=), subtract from a
#   value (-=), multiply a value (^=), and reset to default (&).
# * String. These consist of text. Operations are to set (=), prepend (^=),
#   append (+=), and reset (&). Possibly a form of subtraction? (-=)
# * List of flags. These consist of a comma separated list of text. Operations
#   are to set (=), prepend (^=), append (+=), remove (-=), and reset to
#   default (&)
#
# The set command is used for all operations, though mailx also has unset. I'm
# unsure right now if unset should be equivalent to "set nooption" or "set
# option&"
#

class Options(object):
    """Set of options for application"""
    def __init__(self):
        object.__init__(self)
        self.options = {}
    def addOption(self, opt):
        assert isinstance(opt, Option)
        assert opt.name not in self.options, "An option already exists by that name"
        self.options[opt.name] = opt
    def removeOption(self, name):
        if not name in self.options:
            raise KeyError("No option named %s" % key)
        del self.options[name]
    def __contains__(self, key):
        return self.options.__contains__(key)
    def __iter__(self):
        # First pass: hand off to underlying type
        return self.options.itervalues()
    def __getitem__(self, key):
        # Handle x[y] syntax
        return self.options[key]
    def __setitem__(self, key, value):
        if not key in self.options:
            raise KeyError("No option named %s" % key)
        self.options[key].setValue(value)
    def __getattr__(self, name):
        # handle x.y syntax
        if name in self.options:
            return self.options[name]
        raise AttributeError()


class Option(object):
    """Root class for all options.

    Subclasses must implement the following:

     * __str__ (or __unicode__): printable representation for the option.
       Should include the name of the option. This is primarily used for
       displaying in a list.
     * strValue: printable representation for the value of the option. This is
       primarily used for including the value of an option in an email as with
       the '~i' command.

    Subclasses should implement the following if applicable:

     * setValue: assign a value. If not defined, this class will do direct
       assignment (typically of a string). Subclasses are urged to do value
       checking.

    Subclasses may implement any other function to aid in the use of a setting
    by the software.
    """
    def __init__(self, name, default, doc=None):
        object.__init__(self)
        assert not name.startswith("no"), "Options must not start with 'no' or 'inv' to prevent clashes with set command syntax/semantics"
        assert not name.startswith("inv"), "Options must not start with 'no' or 'inv' to prevent clashes with set command syntax/semantics"
        self.name = name
        self.default = default
        self.value = default
        self.doc = doc
    def setValue(self, value):
        # Dumb default, overridable by subclasses
        self.value = value

class BoolOption(Option):
    def __str__(self):
        if self.value:
            return "  %s" % (self.name,)
        else:
            return "no%s" % (self.name,)
    def strValue(self):
        return str(self).strip()
    def setValue(self, value):
        if isinstance(value, (str,unicode)):
            if value.lower() in ["1", "true", self.name]:
                self.value = True
            elif value.lower() in ["0", "false", "no" + self.name]:
                self.value = False
            elif value.lower == "inv" + self.name:
                self.value = False if self.value else True
            else:
                raise ValueError()
        else:
            # Attempt to coerce it; let the conversions raise valueError
            # themselves.
            self.value=bool(int(value))
    def __bool__(self): # Python 3
        return self.value
    __nonzero__ = __bool__ # Python 2 compat

class NumericOption(Option):
    def __str__(self):
        return "%s=%i" % (self.name, self.value)
    def strValue(self):
        return str(self.value)
    def setValue(self, value):
        # Ensure value is an integer
        if not isinstance(value, (int, long)):
            # TODO: Wrap in a nicer try/except block?
            # TODO: Error if it was a float that got truncated?
            value = int(value, 0)
        self.value = value

class StringOption(Option):
    def __unicode__(self):
        return u"%s=%s" % (self.name, self.value)
    def strValue(self):
        return self.value
#    def __str__(self):
#        """Attempt to make a str from unicode by using utf-8. This is not a great way to do it, because it assumes a utf-8 capable system for display, and worse, will break when we move to python3
#
#        (python3 uses __bytes__ and __str__ rather than __str__ and __unicode__, so __str__ means different things between the two)"""
#        return unicode(self).encode('utf-8')
    def __len__(self):
        return len(self.value)

class FlagsOption(Option):
    # TODO: Should have a list of valid flags with descriptions?
    def __str__(self):
        return "%s=%s" % (self.name, ",".join(self.value))
    def strValue(self):
        return ",".join(self.value)
    def setValue(self, value):
        # Special case, if given an empty string, make an empty list
        if value == "" or value == None:
            self.value = []
            return
        # split into separate flags, make sure no dups
        # TODO: Do we really want to strip?
        self.value = map(lambda x: x.strip(),list(set(value.split(','))))
    def __getattr__(self, name):
        if name in self.value:
            return True
        return False

class UserOption(StringOption):
    """User option is a catch-all for when someone sets something we don't recognize.
    We'll treat it as a string. The reason we keep the type different is for sorting purposes
    (so that we can list "unknowns" separate from booleans, strings, numerics, and flag lists"""
    def __str__(self):
        return "%s=%s" % (self.name, repr(self.value))
    def strValue(self):
        return self.value
