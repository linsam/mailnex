import os.path
from . import cmdprompt

def normalizePath(currentPath):
    if currentPath.startswith("~{}".format(os.path.sep)):
        if 'HOME' in os.environ:
            currentPath='{}{}{}'.format(os.environ['HOME'], os.path.sep, currentPath[2:])
    return currentPath

def pathCompleter(currentPath):
    # TODO: Cache the results from any given directory; it is very
    # inefficient to recalculate this for every character the user
    # types.
    # Alternatively, only complete one request (e.g. user hits
    # 'tab')
    currentPath = normalizePath(currentPath)
    dirname = os.path.dirname(currentPath)
    filename = os.path.basename(currentPath)
    try:
        paths = os.listdir(dirname) if dirname else os.listdir(".")
    except OSError:
        # Typically, file not found. Whatever the error, just
        # don't do completions.
        raise StopIteration
    # Remove paths that don't start with our query
    paths = filter(lambda x: x.startswith(filename), paths)
    paths.sort()
    for i in paths:
        extra=None
        try:
            if os.path.isdir(os.path.sep.join([dirname if dirname else '.',i])):
                extra = os.path.sep
        except OSError:
            # Ignore file-not-found and such. We shouldn't
            # actually get here typically (we got a list from
            # earlier), but things happen, like an entry being
            # removed after we got the list but before we checked
            # for it being a directory.
            # TODO: Since the error is usually that we cannot
            # check the entry (as in, it doesn't exist), perhaps
            # we should skip it in the listing?
            pass
        yield cmdprompt.prompt_toolkit.completion.Completion(i, start_position=-len(filename), display_meta=extra)


