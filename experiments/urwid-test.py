

import urwid

def show_or_exit(key):
    if key in ('q', 'Q', 'esc'):
        raise urwid.ExitMainLoop()
    return
    if key == 'i':
        txt.set_text(('banner', repr(dir(loop.event_loop))))
        return
    txt.set_text(('banner',key))

palette = [
        ('banner', 'light cyan', 'dark blue'),
        ('streak', 'black', 'dark red'),
        ('bg', 'black', 'dark magenta'),
        ]

txt = urwid.Text(('banner', open("/etc/passwd").read()), align='left')
view = urwid.ListBox([txt])
#fill = urwid.Filler(view, 'top')
map2 = urwid.AttrMap(view, 'bg')
top = urwid.LineBox(map2)
loop = urwid.MainLoop(top, palette,  unhandled_input=show_or_exit)
loop.run()
