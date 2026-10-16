#!/usr/bin/env python3
"""Clipboard bridge. Runs as a windowless Xwayland client (GDK_BACKEND=x11) because
GNOME/Mutter exposes no Wayland clipboard-monitoring protocol, but it does keep the
Xwayland clipboard in sync, and X11 needs no focus to own the clipboard.
stdout: one JSON line per new clipboard item. stdin: JSON lines to put on the clipboard."""
import base64, json, os, sys, time
os.environ['GDK_BACKEND'] = 'x11'
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf, GLib, Gtk, Gdk

MAX_BYTES = 4 * 1024 * 1024  # Windows' per-item limit
SKIP = {'x-kde-passwordManagerHint', 'x-special/gnome-copied-files'}  # secrets, file copies
last_set = 0


def emit(obj):
    print(json.dumps(obj), flush=True)


def on_change(cb, _ev):
    if time.monotonic() - last_set < 0.5:  # our own paste
        return
    ok, atoms = cb.wait_for_targets()
    if not ok or {a.name() for a in atoms} & SKIP:
        return
    if cb.wait_is_text_available():
        text = cb.wait_for_text()
        if text and text.strip() and len(text.encode()) <= MAX_BYTES:
            emit({'text': text})
    elif cb.wait_is_image_available():
        pix = cb.wait_for_image()
        if pix:
            ok, png = pix.save_to_bufferv('png', [], [])
            if ok and len(png) <= MAX_BYTES:
                emit({'png': base64.b64encode(png).decode()})


def on_command(_src, _cond):
    global last_set
    line = sys.stdin.readline()
    if not line:  # main app is gone
        Gtk.main_quit()
        return False
    item = json.loads(line)
    last_set = time.monotonic()
    if 'text' in item:
        cb.set_text(item['text'], -1)
    else:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(base64.b64decode(item['png']))
        loader.close()
        cb.set_image(loader.get_pixbuf())
    return True


cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
cb.connect('owner-change', on_change)
GLib.io_add_watch(sys.stdin, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP, on_command)
Gtk.main()
