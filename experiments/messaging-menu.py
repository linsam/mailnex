# From https://bugs.launchpad.net/ubuntu/+source/libindicate/+bug/1315384

from gi.repository import GLib, Gio, MessagingMenu

mmapp = MessagingMenu.App(desktop_id='mailnex.desktop')
mmapp.register()

ml = GLib.MainLoop()

def source_activated(mmapp, source_id):
    print('source {} activated'.format(source_id))
    mmapp.unregister()
    ml.quit()

mmapp.connect('activate-source', source_activated)

icon = Gio.ThemedIcon.new_with_default_fallbacks('empathy')
mmapp.append_source_with_count('inbox', icon, 'MyBox', 7)
mmapp.draw_attention('inbox')
ml.run()
