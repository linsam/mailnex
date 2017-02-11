def check(data, specifiers):
    """Parse data, looking for specifiers. Return True if any match.

    Specifiers should be a list of single characters

    This is useful to see if any of the specifiers are actually used.

    For example, when processing a command line with replacements, if a
    filename represented by %f isn't in the string, there would be no need to
    bother creating a file for it. This function performs the test without doing
    any actual replacements
    """
    inescape = False
    for char in data:
        if char == '%':
            if inescape:
                inescape = False
                continue
            inescape = True
            continue
        if inescape:
            inescape = False
            if char in specifiers:
                return True
    return False

def replace(data, specifiers):
    """Parse data, replacing specifiers.

    Specifiers should be a dict, with single character keys.

    E.g. replace("Hello %s, how are you?", {'s': "World"})
     -> "Hello World, how are you?"
    """
    res = []
    inescape = False
    for char in data:
        if char == '%':
            if inescape:
                res.append(char)
                inescape = False
                continue
            inescape = True
            continue
        if inescape:
            inescape = False
            if char in specifiers:
                res.append(specifiers[char])
            continue
        res.append(char)
    # Use the same type as data, that way we keep str/bytes or unicode
    return data.__class__("").join(res)
