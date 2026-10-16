#!/usr/bin/env python3
"""WinClip: Windows-style (Win+V) clipboard history for Wayland desktops."""
import base64, fcntl, hashlib, json, os, sqlite3, struct, sys
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

APP_ID = 'io.github.winclip.WinClip'
HERE = os.path.dirname(os.path.realpath(__file__))
LIMIT = 25  # Windows keeps the last 25 unpinned items


class History:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, kind TEXT, data BLOB,
                hash TEXT UNIQUE, pinned INTEGER DEFAULT 0, seq INTEGER);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);''')

    def new_boot(self, boot_id):
        """Like Windows, unpinned history does not survive a reboot."""
        row = self.db.execute("SELECT v FROM meta WHERE k='boot'").fetchone()
        if row and row[0] != boot_id:
            self.clear()
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('boot', ?)", (boot_id,))
        self.db.commit()

    def add(self, kind, data):
        raw = data.encode() if isinstance(data, str) else data
        digest = hashlib.sha256(kind.encode() + raw).hexdigest()
        self.db.execute('''INSERT INTO items (kind, data, hash, seq)
            VALUES (?, ?, ?, (SELECT IFNULL(MAX(seq), 0) + 1 FROM items))
            ON CONFLICT(hash) DO UPDATE SET seq = excluded.seq''', (kind, data, digest))
        self.db.execute('''DELETE FROM items WHERE pinned = 0 AND id NOT IN
            (SELECT id FROM items WHERE pinned = 0 ORDER BY seq DESC LIMIT ?)''', (LIMIT,))
        self.db.commit()

    def bump(self, item_id):
        self.db.execute('UPDATE items SET seq = (SELECT MAX(seq) + 1 FROM items) WHERE id = ?', (item_id,))
        self.db.commit()

    def pin(self, item_id, pinned):
        self.db.execute('UPDATE items SET pinned = ? WHERE id = ?', (int(pinned), item_id))
        self.db.commit()

    def delete(self, item_id):
        self.db.execute('DELETE FROM items WHERE id = ?', (item_id,))
        self.db.commit()

    def clear(self):
        self.db.execute('DELETE FROM items WHERE pinned = 0')
        self.db.commit()

    def items(self):
        return self.db.execute('SELECT id, kind, data, pinned FROM items ORDER BY seq DESC').fetchall()


class Keyboard:
    """Virtual keyboard on /dev/uinput, used to press Ctrl+V in the focused app."""
    def __init__(self):
        self.fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, 0x40045564, 1)  # UI_SET_EVBIT, EV_KEY
        for key in range(1, 249):  # all keys, so udev/libinput classify it as a full keyboard
            fcntl.ioctl(self.fd, 0x40045565, key)  # UI_SET_KEYBIT
        fcntl.ioctl(self.fd, 0x405c5503, struct.pack('4H80sI', 6, 1, 1, 1, b'WinClip virtual keyboard', 0))  # UI_DEV_SETUP
        fcntl.ioctl(self.fd, 0x5501)  # UI_DEV_CREATE

    def ctrl_v(self):
        # ponytail: always Ctrl+V; terminals want Ctrl+Shift+V, add per-app mapping if it matters
        for code, value in ((29, 1), (47, 1), (47, 0), (29, 0)):  # KEY_LEFTCTRL, KEY_V
            os.write(self.fd, struct.pack('llHHi', 0, 0, 1, code, value) + struct.pack('llHHi', 0, 0, 0, 0, 0))
        return False


TRAY_XML = '''<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
  <property name="IconName" type="s" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
</interface></node>'''
TRAY_PROPS = {'Category': GLib.Variant('s', 'ApplicationStatus'), 'Id': GLib.Variant('s', 'winclip'),
              'Title': GLib.Variant('s', 'Clipboard'), 'Status': GLib.Variant('s', 'Active'),
              'IconName': GLib.Variant('s', 'edit-paste-symbolic'), 'ItemIsMenu': GLib.Variant('b', False)}


class App(Adw.Application):
    def __init__(self, background):
        super().__init__(application_id=APP_ID)
        self.background = background

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.hold()  # keep running with no window, like the Windows clipboard service
        data_dir = os.path.join(GLib.get_user_data_dir(), 'winclip')
        os.makedirs(data_dir, exist_ok=True)
        self.history = History(os.path.join(data_dir, 'history.db'))
        with open('/proc/sys/kernel/random/boot_id') as f:
            self.history.new_boot(f.read().strip())
        try:
            self.keyboard = Keyboard()
        except OSError as e:
            self.keyboard = None
            print(f'winclip: auto-paste off, no access to /dev/uinput ({e})', file=sys.stderr)
        toggle = Gio.SimpleAction.new('toggle', None)
        toggle.connect('activate', lambda *_: self.toggle())
        self.add_action(toggle)
        self.build_window()
        self.spawn_watcher()
        self.start_tray()

    def do_activate(self):
        if self.background:
            self.background = False
        else:
            self.toggle()

    # clipboard bridge (Xwayland helper process): reads and writes the clipboard

    def spawn_watcher(self):
        proc = Gio.Subprocess.new([sys.executable, os.path.join(HERE, 'watcher.py')],
                                  Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDIN_PIPE)
        self.watcher_in = proc.get_stdin_pipe()
        self.read_line(Gio.DataInputStream.new(proc.get_stdout_pipe()))

    def read_line(self, stream):
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line)

    def on_line(self, stream, result):
        line, _ = stream.read_line_finish_utf8(result)
        if line is None:  # watcher died: restart it
            GLib.timeout_add_seconds(2, lambda: self.spawn_watcher() or False)
            return
        item = json.loads(line)
        if 'text' in item:
            self.history.add('text', item['text'])
        else:
            self.history.add('image', base64.b64decode(item['png']))
        if self.win.get_visible():
            self.refresh()
        self.read_line(stream)

    # tray icon (StatusNotifierItem; shown wherever the desktop has a tray host)

    def start_tray(self):
        # ponytail: no dbusmenu right-click menu, click toggles the window; add one if users ask
        conn = self.get_dbus_connection()
        info = Gio.DBusNodeInfo.new_for_xml(TRAY_XML).interfaces[0]
        conn.register_object_with_closures2('/StatusNotifierItem', info,
                             lambda c, s, p, i, method, params, inv: (self.toggle(), inv.return_value(None)),
                             lambda c, s, p, i, prop: TRAY_PROPS[prop], None)
        Gio.bus_watch_name_on_connection(conn, 'org.kde.StatusNotifierWatcher', Gio.BusNameWatcherFlags.NONE,
                                         lambda c, *_: c.call('org.kde.StatusNotifierWatcher', '/StatusNotifierWatcher',
                                             'org.kde.StatusNotifierWatcher', 'RegisterStatusNotifierItem',
                                             GLib.Variant('(s)', (c.get_unique_name(),)), None,
                                             Gio.DBusCallFlags.NONE, -1, None, None), None)

    # window

    def build_window(self):
        self.win = Adw.ApplicationWindow(application=self, title='Clipboard', default_width=380,
                                         default_height=520, resizable=False, hide_on_close=True)
        clear = Gtk.Button(label='Clear all')
        clear.connect('clicked', lambda _: (self.history.clear(), self.refresh()))
        header = Adw.HeaderBar(decoration_layout=':close')
        header.pack_start(clear)
        self.list = Gtk.ListBox(css_classes=['boxed-list-separate'], margin_start=12, margin_end=12,
                                margin_top=6, margin_bottom=12, valign=Gtk.Align.START)
        self.list.set_placeholder(Adw.StatusPage(icon_name='edit-paste-symbolic', title='Nothing here yet',
                                                 description="You'll see your clipboard history here once you've copied something."))
        self.list.connect('row-activated', lambda _, row: self.paste(row.item))
        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=self.list, hscrollbar_policy=Gtk.PolicyType.NEVER))
        view.add_top_bar(header)
        self.win.set_content(view)
        self.win.connect('notify::is-active', lambda w, _: w.is_active() or w.set_visible(False))
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.on_key)
        self.win.add_controller(keys)

    def on_key(self, _ctl, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.win.set_visible(False)
        elif keyval == Gdk.KEY_Delete and self.list.get_selected_row():
            self.history.delete(self.list.get_selected_row().item[0])
            self.refresh()
        else:
            return False
        return True

    def toggle(self):
        if self.win.get_visible():
            self.win.set_visible(False)
            return
        self.refresh()
        self.win.present()

    def refresh(self):
        self.list.remove_all()
        for item_id, kind, data, pinned in self.history.items():
            self.list.append(self.make_row(item_id, kind, data, pinned))
        first = self.list.get_row_at_index(0)
        if first:
            self.list.select_row(first)
            first.grab_focus()

    def make_row(self, item_id, kind, data, pinned):
        if kind == 'text':
            content = Gtk.Label(label=data[:500], xalign=0, wrap=True, wrap_mode=2, lines=4,
                                ellipsize=3, hexpand=True)  # wrap WORD_CHAR, ellipsize END
        else:
            content = Gtk.Picture(paintable=Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)), can_shrink=True,
                                  content_fit=Gtk.ContentFit.CONTAIN, height_request=96, hexpand=True)
        pin = Gtk.ToggleButton(icon_name='view-pin-symbolic', active=bool(pinned), tooltip_text='Pin',
                               css_classes=['flat'])
        pin.connect('toggled', lambda b: self.history.pin(item_id, b.get_active()))
        delete = Gtk.Button(icon_name='user-trash-symbolic', tooltip_text='Delete', css_classes=['flat'])
        delete.connect('clicked', lambda _: (self.history.delete(item_id), self.refresh()))
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.START)
        buttons.append(pin)
        buttons.append(delete)
        box = Gtk.Box(spacing=6, margin_start=12, margin_end=6, margin_top=6, margin_bottom=6)
        box.append(content)
        box.append(buttons)
        row = Gtk.ListBoxRow(child=box)
        row.item = (item_id, kind, data)
        return row

    def paste(self, item):
        item_id, kind, data = item
        # set via the X11 bridge: a Wayland client may only set the clipboard while focused
        msg = {'text': data} if kind == 'text' else {'png': base64.b64encode(data).decode()}
        self.watcher_in.write_all((json.dumps(msg) + '\n').encode(), None)
        self.history.bump(item_id)
        self.win.set_visible(False)
        if self.keyboard:  # give focus a moment to return to the previous app
            GLib.timeout_add(150, self.keyboard.ctrl_v)


if __name__ == '__main__':
    background = '--background' in sys.argv
    sys.exit(App(background).run([a for a in sys.argv if a != '--background']))
