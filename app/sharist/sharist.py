#!/usr/bin/env python3
# sharist: Quick Share (Android ⇄ Linux) im Suite-Look (rice-apps/tui.py).
# Protokoll in nearby.py. Läuft im Hintergrund (--daemon), damit das Handy den Laptop jederzeit sieht;
# Super+D (--toggle) zeigt das Fenster.
import json, os, socket, sys, time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.expanduser("~/.local/share/rice-apps"))
import glass  # noqa: E402
import nearby  # noqa: E402
import tui  # noqa: E402

APP_ID = "de.invincible.Sharist"
CFG_DIR = os.path.expanduser("~/.config/sharist")
CFG_FILE = os.path.join(CFG_DIR, "config.json")
HIST_FILE = os.path.join(CFG_DIR, "verlauf.json")
PORT = 37521   # fest, damit die Firewall-Regel (ufw allow 37521/tcp) passt


def default_name():
    for p in ("/etc/hostname",):
        try:
            n = open(p).read().strip()
            if n:
                return n
        except OSError:
            pass
    return socket.gethostname() or "linux"


DEFAULTS = {"name": default_name(), "folder": os.path.expanduser("~/Downloads"), "visible": True,
            "copy_text": True}

CSS = """
window.tui .drop { border: 1px dashed alpha(@primary, 0.5); border-radius: 6px; padding: 18px 12px; }
window.tui .drop.hot { border-style: solid; border-color: @primary; background-color: alpha(@primary, 0.10); }
window.tui .pin { color: @on_primary; background-color: @primary; border-radius: 4px; padding: 0 7px; }
window.tui .ask { border: 1px solid @primary; border-radius: 6px; padding: 8px 10px; margin: 4px 0;
                  background-color: alpha(@primary, 0.08); }
window.tui .tline + .tline { border-top: 1px dashed alpha(@primary, 0.18); }
window.tui .tline { padding: 6px 2px; }
window.tui .chip { color: @secondary; }
window.tui button.dev { background: none; border: none; box-shadow: none; padding: 6px 6px; border-radius: 4px;
                        color: @foreground; min-height: 0; }
window.tui button.dev:hover, window.tui button.dev:focus-visible { background-color: alpha(@primary, 0.14); }
window.tui button.dev .go { color: @primary; }
window.tui button.dev:disabled { opacity: 0.45; }
window.tui .qr { background-color: #ffffff; border-radius: 6px; padding: 10px; }
"""


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def summary(t):
    parts = []
    if t.files:
        parts.append(t.files[0]["name"] if len(t.files) == 1 else f"{len(t.files)} dateien")
    for x in t.texts:
        parts.append({"link": "link", "telefon": "nummer", "adresse": "adresse"}.get(x["kind"], "text"))
    for w in t.wifi:
        parts.append(f"wlan „{w['ssid']}“")
    s = " · ".join(parts) or "–"
    if t.total and t.files:
        s += f" · {human(t.total)}"
    return s


class Past:
    """Empfangenes aus früheren Sitzungen (verlauf.json) – sieht für die Liste aus wie ein nearby.Transfer."""
    incoming, finished, pin, error = True, True, None, None

    def __init__(self, d):
        self.id = d.get("id", 0)
        self.peer, self.peer_type = d.get("peer", "?"), d.get("peer_type", 0)
        self.files, self.texts, self.wifi = d.get("files", []), d.get("texts", []), d.get("wifi", [])
        self.total = self.done = sum(f.get("size", 0) for f in self.files)
        self.state = "fertig"
        self.started = d.get("when", 0)

    def accept(self):
        pass

    reject = cancel = accept


def to_record(t):
    return {"id": t.id, "peer": t.peer, "peer_type": t.peer_type, "when": t.started,
            "files": [{"name": f["name"], "path": f["path"], "size": f["size"]} for f in t.files if f.get("path")],
            "texts": [{k: x[k] for k in ("kind", "title", "value")} for x in t.texts if x.get("value")],
            "wifi": [w for w in t.wifi if w.get("password") is not None]}


class QrView(Gtk.DrawingArea):
    """QR-Code selbst gezeichnet (dunkle Module auf Weiß – das scannt jede Kamera)."""

    def __init__(self, text):
        super().__init__(halign=Gtk.Align.CENTER)
        import qrcode
        q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(text)
        q.make(fit=True)
        self.m = q.get_matrix()
        n = len(self.m)
        self.cell = max(4, 220 // n)
        self.set_content_width(n * self.cell)
        self.set_content_height(n * self.cell)
        self.set_draw_func(self._draw)

    def _draw(self, _a, cr, w, h):
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        cr.set_source_rgb(0.08, 0.08, 0.08)
        c = self.cell
        for y, row in enumerate(self.m):
            for x, on in enumerate(row):
                if on:
                    cr.rectangle(x * c, y * c, c, c)
        cr.fill()


class ShareWindow(tui.TuiWindow):
    APP_ID = APP_ID

    def on_show(self):
        self.get_application().window_shown()

    def on_hide(self):
        self.get_application().window_hidden()


class Sharist(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.win = None
        self.daemon = False
        self.cfg = {**DEFAULTS, **load_json(CFG_FILE, {})}
        self.history = [Past(d) for d in load_json(HIST_FILE, [])]
        self.live = []           # Transfers dieser Sitzung
        self.dismissed = set()
        self.selection = []      # Pfade zum Senden
        self.engine = None
        self.qr_on = False
        self.qr_dev = None      # per qr gefundenes handy, wartet auf auswahl
        self.port = None

    # ================= Start =================
    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.engine = nearby.Engine(self.on_engine, lambda: self.cfg["name"], lambda: self.cfg["folder"], PORT)
        self.apply_visible()
        for name, cb, ptype in (("show", lambda *_: self.show(), None), ("toggle", lambda *_: self.toggle(), None),
                                ("quit", lambda *_: self.quit(), None), ("accept", self.on_accept_action, "u"),
                                ("reject", self.on_reject_action, "u"), ("open-folder", lambda *_: self.open_folder(), None),
                                ("open-uri", lambda a, v: self.launch(v.get_string()), "s")):
            act = Gio.SimpleAction.new(name, GLib.VariantType.new(ptype) if ptype else None)
            act.connect("activate", cb)
            self.add_action(act)

    def do_shutdown(self):
        if self.engine:
            self.engine.shutdown()
        Gtk.Application.do_shutdown(self)

    def do_activate(self):
        if self.daemon:          # erster Start im Hintergrund: nur sichtbar sein, kein Fenster
            self.daemon = False
            self.hold()
            return
        self.show()

    def do_open(self, files, n, hint):
        self.show()
        self.add_paths([f.get_path() for f in files if f.get_path()])
        self.win.show_tab("send")

    def show(self):
        if not self.win:
            self.build()
        getattr(glass, "bring_here", lambda w: None)(self.win)
        self.win.present()

    def toggle(self):
        if self.win and self.win.get_visible() and self.win.is_active():
            self.win.close()
        else:
            self.show()

    def window_shown(self):
        self.update_discovery()

    def window_hidden(self):
        self.update_discovery()

    # ================= Aufbau =================
    def build(self):
        if CSS not in tui.CSS:
            tui.CSS += CSS
        tui.Theme.get().load()
        win = ShareWindow(self, "sharist", [("send", "senden", "󰒊"), ("recv", "empfangen", "󰇚")], size=(780, 620),
                          hints=(("strg+o", "dateien"), ("strg+v", "einfügen")))
        self.win = win   # Schließen versteckt nur (TuiWindow) – sharist bleibt fürs Handy sichtbar
        win.on_tab = lambda tid: self.update_discovery()
        for name, cb, keys in (("add", lambda *_: self.pick_files(), ["<Control>o"]),
                               ("paste", lambda *_: self.paste(), ["<Control>v"]),
                               ("close", lambda *_: win.close(), ["<Control>w"])):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            win.add_action(act)
            self.set_accels_for_action(f"win.{name}", keys)
        self.vis_chip = tui.label("", "dim")
        win.header_extra.append(self.vis_chip)
        win.add_changelog("sharist", HERE)
        win.add_page("send", self.build_send())
        win.add_page("recv", self.build_recv())
        self.refresh_selection()
        self.refresh_devices()
        self.refresh_transfers()
        self.refresh_visible()

    def build_send(self):
        page = tui.vbox(spacing=8)
        page.set_margin_top(6)
        page.set_margin_bottom(10)
        # Auswahl
        self.sel_panel = tui.Panel("was")
        self.drop = tui.vbox(spacing=6)
        self.drop.add_css_class("drop")
        self.sel_list = tui.vbox(spacing=2)
        self.drop_hint = tui.label("dateien hier ablegen  ·  strg+o  ·  strg+v", "dim", xalign=0.5)
        self.drop.append(self.drop_hint)
        self.drop.append(self.sel_list)
        self.sel_panel.body.append(self.drop)
        self.text_entry = tui.Entry("…oder text / link zum senden")
        self.text_entry.connect("changed", lambda *_: self.refresh_selection())
        self.sel_panel.body.append(tui.hbox(self.text_entry, tui.Button("󰈔 dateien", self.pick_files),
                                            self.clear_btn_make(), spacing=8))
        page.append(self.sel_panel)
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("drop", self.on_drop)
        target.connect("enter", lambda *_: (self.drop.add_css_class("hot"), Gdk.DragAction.COPY)[1])
        target.connect("leave", lambda *_: self.drop.remove_css_class("hot"))
        page.add_controller(target)
        # Geräte
        self.dev_panel = tui.Panel("geräte in der nähe")
        self.dev_list = tui.vbox(spacing=1)
        self.dev_empty = tui.label("", "empty", wrap=True)
        self.dev_panel.body.append(self.dev_list)
        self.dev_panel.body.append(self.dev_empty)
        self.qr_box = tui.vbox(spacing=6)
        self.qr_btn = tui.Button("󰐲 qr-code zeigen", self.toggle_qr,
                                 tooltip="Handy findet sharist nicht? Code mit der Kamera scannen – dann verbindet es sich selbst")
        self.dev_panel.body.append(tui.hbox(tui.label("", hexpand=True), self.qr_btn, spacing=8))
        self.dev_panel.body.append(self.qr_box)
        page.append(self.dev_panel)
        # ausgehende Übertragungen
        self.out_panel = tui.Panel("gesendet")
        self.out_list = tui.vbox(spacing=0)
        self.out_panel.body.append(self.out_list)
        page.append(self.out_panel)
        return page

    def clear_btn_make(self):
        self.clear_btn = tui.Button("✕", self.clear_selection, tooltip="Auswahl leeren")
        return self.clear_btn

    def build_recv(self):
        page = tui.vbox(spacing=8)
        page.set_margin_top(6)
        page.set_margin_bottom(10)
        self.ask_box = tui.vbox(spacing=4)
        page.append(self.ask_box)
        p = tui.Panel("sichtbarkeit")
        self.vis_check = tui.Check("sichtbar für handys in der nähe", self.cfg["visible"], self.set_visible)
        self.name_entry = tui.Entry("gerätename", text=self.cfg["name"], width=18, activate=self.save_name)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self.save_name())
        self.name_entry.add_controller(focus)
        p.body.append(tui.hbox(self.vis_check, tui.label("", hexpand=True), tui.label("name", "dim"),
                               self.name_entry, spacing=8))
        self.folder_lbl = tui.label("", "accent", ellipsize=True, hexpand=True)
        p.body.append(tui.hbox(tui.label("speichern in", "dim"), self.folder_lbl,
                               tui.Button("󰉋 öffnen", self.open_folder), tui.Button("ändern", self.pick_folder),
                               spacing=8))
        self.copy_check = tui.Check("empfangenen text gleich in die zwischenablage", self.cfg["copy_text"],
                                    lambda on: self.set_cfg("copy_text", on))
        p.body.append(self.copy_check)
        self.port_lbl = tui.label("", "dim", wrap=True)
        p.body.append(self.port_lbl)
        page.append(p)
        page.append(tui.theme_panel())
        self.in_panel = tui.Panel("empfangen")
        self.in_list = tui.vbox(spacing=0)
        self.in_empty = tui.label("noch nichts – am handy „teilen“ → quick share → dieses gerät", "empty")
        self.in_panel.body.append(self.in_list)
        self.in_panel.body.append(self.in_empty)
        page.append(self.in_panel)
        return page

    # ================= Einstellungen =================
    def set_cfg(self, key, value):
        self.cfg[key] = value
        save_json(CFG_FILE, self.cfg)

    def save_name(self):
        name = self.name_entry.get_text().strip()
        if name and name != self.cfg["name"]:
            self.set_cfg("name", name)
            self.apply_visible()
            self.refresh_visible()
            self.win.say(f"heißt jetzt „{name}“")

    def set_visible(self, on):
        self.set_cfg("visible", on)
        self.apply_visible()
        self.refresh_visible()

    def apply_visible(self):
        try:
            self.port = self.engine.set_visible(self.cfg["visible"])
        except Exception as e:
            self.port = None
            print("sharist: sichtbar werden fehlgeschlagen:", e, file=sys.stderr)
            if self.win:
                self.win.say(f"nicht sichtbar: {e}", error=True)

    def refresh_visible(self):
        if not self.win:
            return
        vis = self.cfg["visible"] and self.port
        self.vis_chip.set_label(f"󰐻 {self.cfg['name']}" if vis else "󰐼 unsichtbar")
        self.vis_chip.set_tooltip_text("für handys in der nähe sichtbar" if vis else "empfangen ist aus")
        self.folder_lbl.set_label(self.cfg["folder"].replace(os.path.expanduser("~"), "~", 1))
        if vis and self.port != PORT:
            self.port_lbl.set_label(f"port {PORT} belegt – läuft auf {self.port} (firewall-regel passt dann nicht)")
        elif vis:
            self.port_lbl.set_label(f"wlan · port {PORT}/tcp · mDNS  ·  handy im selben wlan, bluetooth an")
        else:
            self.port_lbl.set_label("")

    def pick_folder(self):
        dlg = Gtk.FileDialog(title="Empfangene Dateien speichern in", initial_folder=Gio.File.new_for_path(self.cfg["folder"]))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.set_cfg("folder", f.get_path())
                self.refresh_visible()
        dlg.select_folder(self.win, None, done)

    # ================= Auswahl =================
    def add_paths(self, paths):
        added = 0
        for p in paths:
            if os.path.isdir(p):
                for root, _dirs, files in os.walk(p):
                    for f in sorted(files):
                        fp = os.path.join(root, f)
                        if fp not in self.selection:
                            self.selection.append(fp)
                            added += 1
            elif os.path.isfile(p) and p not in self.selection:
                self.selection.append(p)
                added += 1
        if self.win:
            self.refresh_selection()
            if added and getattr(self, "qr_dev", None):
                self.qr_send(self.qr_dev)
        return added

    def clear_selection(self):
        self.selection.clear()
        self.text_entry.set_text("")
        self.refresh_selection()

    def pick_files(self):
        self.win.show_tab("send")
        dlg = Gtk.FileDialog(title="Dateien senden")

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.add_paths([files.get_item(i).get_path() for i in range(files.get_n_items())])
        dlg.open_multiple(self.win, None, done)

    def paste(self):
        if isinstance(self.win.get_focus(), Gtk.Text):    # im Textfeld normal einfügen
            self.win.get_focus().emit("paste-clipboard")
            return
        cb = self.win.get_clipboard()
        if cb.get_formats().contain_gtype(Gdk.FileList):
            def got(c, res):
                try:
                    fl = c.read_value_finish(res)
                    self.add_paths([f.get_path() for f in fl.get_files() if f.get_path()])
                except GLib.Error:
                    pass
            cb.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, got)
        else:
            def got_text(c, res):
                try:
                    text = c.read_text_finish(res)
                except GLib.Error:
                    text = None
                if text:
                    paths = [p[7:] if p.startswith("file://") else p for p in text.strip().splitlines()]
                    if all(os.path.exists(p) for p in paths):
                        self.add_paths(paths)
                    else:
                        self.text_entry.set_text(text.strip())
            cb.read_text_async(None, got_text)
        self.win.show_tab("send")

    def on_drop(self, _t, value, x, y):
        self.drop.remove_css_class("hot")
        self.add_paths([f.get_path() for f in value.get_files() if f.get_path()])
        return True

    def has_selection(self):
        return bool(self.selection or self.text_entry.get_text().strip())

    def refresh_selection(self):
        tui.clear(self.sel_list)
        for p in self.selection[:8]:
            rm = Gtk.Button(label="✕", valign=Gtk.Align.CENTER, tooltip_text="weglassen")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, p=p: (self.selection.remove(p), self.refresh_selection()))
            self.sel_list.append(tui.hbox(tui.label("󰈔", "accent"), tui.label(os.path.basename(p), ellipsize=True, hexpand=True),
                                          tui.label(human(os.path.getsize(p)) if os.path.exists(p) else "?", "dim"),
                                          rm, spacing=8))
        if len(self.selection) > 8:
            self.sel_list.append(tui.label(f"… und {len(self.selection) - 8} weitere", "dim"))
        total = sum(os.path.getsize(p) for p in self.selection if os.path.exists(p))
        self.drop_hint.set_visible(not self.selection)
        self.clear_btn.set_sensitive(self.has_selection())
        n = len(self.selection)
        self.sel_panel.set_title(f"was · {n} {'datei' if n == 1 else 'dateien'} · {human(total)}" if n else "was")
        self.refresh_devices()

    # ================= Geräte =================
    def update_discovery(self):
        want = bool(self.win and self.win.get_mapped() and self.win.current_tab() == "send")
        if want and not self.engine.browser:
            self.engine.start_discovery(qr=self.qr_on)
        elif not want and self.engine.browser:
            self.engine.stop_discovery()
            self.qr_dev = None
            if self.qr_on:
                self.qr_on = False
                self.refresh_qr()
        if self.win:
            self.refresh_devices()

    def qr_send(self, dev):
        self.qr_dev = None
        self.send_to(dev)
        self.qr_on = False
        self.engine.qr = None
        self.refresh_qr()

    def toggle_qr(self):
        self.qr_dev = None
        self.qr_on = not self.qr_on
        if self.qr_on:
            self.engine.qr = nearby.QrKey()
        else:
            self.engine.qr = None
        self.refresh_qr()

    def refresh_qr(self):
        tui.clear(self.qr_box)
        self.qr_btn.set_label("󰐲 qr-code ausblenden" if self.qr_on else "󰐲 qr-code zeigen")
        if self.qr_on and self.engine.qr:
            frame = Gtk.Box(halign=Gtk.Align.CENTER)
            frame.add_css_class("qr")
            frame.append(QrView(self.engine.qr.url))
            self.qr_box.append(frame)
            self.qr_box.append(tui.label("mit der handy-kamera scannen – danach verbindet sich sharist von selbst",
                                         "dim", xalign=0.5))

    def refresh_devices(self):
        if not self.win:
            return
        devs = self.engine.device_list()
        can = self.has_selection()
        tui.sync_rows(self.dev_list, devs, lambda d: d.id, self.make_dev_row, lambda r, d: self.update_dev_row(r, d, can))
        searching = bool(self.engine.browser)
        ble = self.engine.ble.error
        self.dev_empty.set_visible(not devs)
        if not searching:
            self.dev_empty.set_label("suche pausiert")
        else:
            self.dev_empty.set_label("suche …  am handy: quick share → sichtbarkeit „alle“, gleiches wlan"
                                     + (f"  ·  ({ble}: android wacht evtl. nicht auf – qr-code hilft)" if ble else ""))
        self.dev_panel.set_title(f"geräte in der nähe · {len(devs)}" if devs else "geräte in der nähe")

    def make_dev_row(self, d):
        b = Gtk.Button()
        b.add_css_class("dev")
        b.icon, b.name, b.kind, b.go = tui.label("", "glyph"), tui.label("", hexpand=True, ellipsize=True), \
            tui.label("", "dim"), tui.label("senden ›", "go")
        b.set_child(tui.hbox(b.icon, b.name, b.kind, b.go, spacing=8))
        b.connect("clicked", lambda *_: self.send_to(b.dev))
        return b

    def update_dev_row(self, b, d, can):
        b.dev = d
        b.icon.set_label(d.icon)
        b.name.set_label(d.name)
        b.kind.set_label(("qr · " if d.qr else "") + nearby.DEVICE_TYPES.get(d.type, "gerät") + "  ·")
        b.set_sensitive(can)
        b.set_tooltip_text(f"{d.address}:{d.port}" if can else "erst dateien oder text wählen")

    def send_to(self, dev):
        text = self.text_entry.get_text().strip()
        if not self.selection and not text:
            self.win.say("erst dateien oder text wählen")
            return
        paths = [p for p in self.selection if os.path.isfile(p)]
        self.track(self.engine.send(dev, paths, text if not paths else None))
        if paths and text:      # Quick Share mischt Text und Dateien nicht – Text als eigene Übertragung hinterher
            self.pending_text = (dev, text)
        self.selection = []
        self.text_entry.set_text("")
        self.refresh_selection()
        self.refresh_transfers()
        self.win.say(f"verbinde mit {dev.name} …")

    # ================= Rückmeldungen vom Protokoll =================
    def on_engine(self, what, obj):
        GLib.idle_add(self._engine_event, what, obj)

    def _engine_event(self, what, obj):
        if what == "devices":
            self.refresh_devices()
        elif what == "qr-match":
            nearby.log(f"qr-treffer {obj.name}: fenster={'ja' if self.win else 'nein'} auswahl={self.selection} "
                       f"text={bool(self.win and self.text_entry.get_text().strip())}")
            if self.win and self.has_selection():
                self.qr_send(obj)
            elif self.win:      # noch nichts gewählt: Handy merken, Senden startet beim Auswählen
                self.qr_dev = obj
                self.win.say(f"qr erkannt: {obj.name} – jetzt datei wählen, dann geht es los")
        elif what == "incoming":
            self.incoming(obj)
        elif what in ("changed", "ended"):
            self.track(obj)
            if what == "ended" or obj.finished:
                self.finished(obj)
            self.refresh_transfers()
        return False

    def incoming(self, t):
        n = Gio.Notification.new(f"{t.peer} möchte teilen")
        n.set_body(f"{summary(t)}  ·  pin {t.pin}")
        n.set_priority(Gio.NotificationPriority.URGENT)
        n.set_icon(Gio.ThemedIcon.new("sharist"))
        n.add_button_with_target("annehmen", "app.accept", GLib.Variant("u", t.id))
        n.add_button_with_target("ablehnen", "app.reject", GLib.Variant("u", t.id))
        n.set_default_action("app.show")
        self.send_notification(f"ask-{t.id}", n)
        if self.win and self.win.get_mapped():
            self.win.show_tab("recv")

    def track(self, t):
        if t not in self.live:
            self.live.append(t)

    def find(self, tid):
        return next((t for t in self.live if t.id == tid), None)

    def on_accept_action(self, _a, v):
        t = self.find(v.get_uint32())
        if t:
            t.accept()

    def on_reject_action(self, _a, v):
        t = self.find(v.get_uint32())
        if t:
            t.reject()

    def finished(self, t):
        if getattr(t, "_reported", False):
            return
        if t.state not in ("fertig", "abgelehnt", "abgebrochen", "fehler"):
            return
        t._reported = True
        self.withdraw_notification(f"ask-{t.id}")
        if t.incoming and t.state == "fertig":
            self.history.insert(0, Past(to_record(t)))
            self.history = self.history[:60]
            save_json(HIST_FILE, [to_record(h) for h in self.history if isinstance(h, Past)])
            texts = [x for x in t.texts if x.get("value")]
            if texts and self.cfg["copy_text"]:
                self.copy(texts[0]["value"])
            n = Gio.Notification.new(f"von {t.peer}: {summary(t)}")
            if t.files:
                n.set_body("gespeichert in " + self.cfg["folder"].replace(os.path.expanduser("~"), "~", 1))
                n.set_default_action("app.open-folder")
            elif texts:
                n.set_body(texts[0]["value"][:200] + ("  (kopiert)" if self.cfg["copy_text"] else ""))
                if texts[0]["kind"] == "link":
                    n.add_button_with_target("öffnen", "app.open-uri", GLib.Variant("s", texts[0]["value"]))
                n.set_default_action("app.show")
            n.set_icon(Gio.ThemedIcon.new("sharist"))
            self.send_notification(f"done-{t.id}", n)
        elif not t.incoming and t.state == "fertig":
            if self.win:
                self.win.say(f"an {t.peer} gesendet: {summary(t)}")
            pt = getattr(self, "pending_text", None)
            if pt:
                self.pending_text = None
                self.track(self.engine.send(pt[0], (), pt[1]))
        elif t.state in ("fehler", "abgelehnt") and self.win and not (t.incoming and t.state == "abgelehnt"):
            what = "abgelehnt" if t.state == "abgelehnt" else "fehler"
            self.win.say(f"{t.peer}: {what}" + (f" – {t.error}" if t.error else ""), error=t.state == "fehler")
        if t.incoming and t.state == "abgelehnt" and not t.error:
            self.dismissed.add(t.id)       # selbst abgelehnt → nicht in die liste
        if t.incoming and t.state in ("abgebrochen", "fehler") and not t.files and not t.texts and not t.wifi:
            self.dismissed.add(t.id)       # Verbindungsversuche ohne Inhalt nicht in die Liste

    # ================= Übertragungen anzeigen =================
    def refresh_transfers(self):
        if not self.win:
            return
        live_in = [t for t in self.live if t.incoming and t.id not in self.dismissed and (t.files or t.texts or t.wifi)]
        asks = [t for t in live_in if t.state == "frage"]
        rec = [t for t in live_in if t.state != "frage" and not (t.state == "fertig")] + \
              [h for h in self.history if h.id not in self.dismissed]
        rec = [t for t in rec if t.id not in {a.id for a in asks}]
        tui.sync_rows(self.ask_box, asks, lambda t: t.id, self.make_ask, self.update_ask)
        tui.sync_rows(self.in_list, rec, lambda t: ("h", t.id) if isinstance(t, Past) else t.id,
                      self.make_line, self.update_line)
        self.in_empty.set_visible(not rec)
        out = [t for t in self.live if not t.incoming and t.id not in self.dismissed]
        out.reverse()
        tui.sync_rows(self.out_list, out, lambda t: t.id, self.make_line, self.update_line)
        self.out_panel.set_visible(bool(out))
        running = any(t.state == "läuft" for t in self.live)
        if running and not getattr(self, "_tick", None):
            self._tick = GLib.timeout_add(250, self._tick_cb)

    def _tick_cb(self):
        self.refresh_transfers()
        if not any(t.state == "läuft" for t in self.live):
            self._tick = None
            return False
        return True

    def make_ask(self, t):
        box = tui.vbox(spacing=6)
        box.add_css_class("ask")
        box.title = tui.label("", "big", ellipsize=True, hexpand=True)
        box.pin = tui.label("", "pin")
        box.files = tui.vbox(spacing=1)
        box.append(tui.hbox(tui.label(nearby.DEVICE_ICONS.get(t.peer_type, "󰾰"), "glyph"), box.title,
                            tui.label("pin", "dim"), box.pin, spacing=8))
        box.append(box.files)
        box.append(tui.hbox(tui.label("stimmt die pin mit dem handy überein?", "dim", hexpand=True),
                            tui.Button("󰄬 annehmen", lambda: t.accept()), tui.Button("✕ ablehnen", lambda: t.reject(),
                                                                                     danger=True), spacing=8))
        return box

    def update_ask(self, box, t):
        box.title.set_label(f"{t.peer} möchte {summary(t)} senden")
        box.pin.set_label(t.pin or "····")
        tui.clear(box.files)
        for f in t.files[:6]:
            box.files.append(tui.hbox(tui.label("󰈔", "accent"), tui.label(f["name"], ellipsize=True, hexpand=True),
                                      tui.label(human(f["size"]), "dim"), spacing=8))
        if len(t.files) > 6:
            box.files.append(tui.label(f"… und {len(t.files) - 6} weitere", "dim"))
        for x in t.texts:
            box.files.append(tui.hbox(tui.label("󰦨", "accent"), tui.label(x["title"] or x["kind"], ellipsize=True), spacing=8))
        for w in t.wifi:
            box.files.append(tui.hbox(tui.label("󰖩", "accent"), tui.label(f"wlan „{w['ssid']}“"), spacing=8))

    def make_line(self, t):
        box = tui.vbox(spacing=4)
        box.add_css_class("tline")
        box.icon = tui.label("", "glyph")
        box.title = tui.label("", ellipsize=True, hexpand=True)
        box.state = tui.label("", "dim")
        box.btns = tui.hbox(spacing=6)
        box.meter = tui.Meter(maximum=1000, interactive=False, small=True, mark=False)
        box.append(tui.hbox(box.icon, box.title, box.state, box.btns, spacing=8))
        box.append(box.meter)
        box.key = None
        return box

    def update_line(self, box, t):
        box.icon.set_label(("󰇚" if t.incoming else "󰒊"))
        peer = f"{'von' if t.incoming else 'an'} {t.peer}"
        box.title.set_markup(f"{tui.esc(peer)}  <span alpha='70%'>{tui.esc(summary(t))}</span>")
        st = t.state
        if st == "läuft" and t.total:
            pct = t.done / t.total
            box.state.set_label(f"{pct * 100:.0f} %  {human(t.done)} / {human(t.total)}")
        elif st == "wartet":
            box.state.set_label(f"wartet auf zustimmung · pin {t.pin}")
        elif st == "fertig" and isinstance(t, Past):
            box.state.set_label(time.strftime("%d.%m. %H:%M", time.localtime(t.started)))
        else:
            box.state.set_label(st + (f" · {t.error}" if t.error and st != "fertig" else ""))
        (box.state.add_css_class if st in ("fehler", "abgelehnt", "abgebrochen") else box.state.remove_css_class)("warn")
        box.meter.set_visible(st == "läuft")
        if st == "läuft" and t.total:
            box.meter.set_value(1000 * t.done / t.total)
        key = (st, isinstance(t, Past))
        if key != box.key:            # Knöpfe nur bei Zustandswechsel neu bauen
            box.key = key
            tui.clear(box.btns)
            for b in self.line_buttons(t):
                box.btns.append(b)

    def line_buttons(self, t):
        out = []
        if not t.finished:
            if t.state in ("verbinde", "wartet", "läuft"):
                out.append(tui.Button("abbrechen", lambda: t.cancel(), danger=True))
            return out
        if t.incoming and t.state == "fertig":
            files = [f for f in t.files if f.get("path")]
            if len(files) == 1:
                out.append(tui.Button("öffnen", lambda: self.launch(Gio.File.new_for_path(files[0]["path"]).get_uri())))
            if files:
                out.append(tui.Button("󰉋", lambda: self.show_file(files[0]["path"]), tooltip="im ordner zeigen"))
            for x in t.texts:
                if x.get("value"):
                    out.append(tui.Button("kopieren", lambda v=x["value"]: (self.copy(v), self.win.say("kopiert"))))
                    if x["kind"] == "link":
                        out.append(tui.Button("öffnen", lambda v=x["value"]: self.launch(v)))
            for w in t.wifi:
                if w.get("password") is not None:
                    out.append(tui.Button(f"wlan-passwort kopieren", lambda v=w["password"]: (self.copy(v), self.win.say(
                        f"passwort für „{w['ssid']}“ kopiert"))))
        rm = Gtk.Button(label="✕", valign=Gtk.Align.CENTER, tooltip_text="aus der liste")
        rm.add_css_class("flat")
        rm.add_css_class("off")
        rm.connect("clicked", lambda *_: self.dismiss(t))
        out.append(rm)
        return out

    def dismiss(self, t):
        self.dismissed.add(t.id)
        if isinstance(t, Past):
            self.history = [h for h in self.history if h is not t]
            save_json(HIST_FILE, [to_record(h) for h in self.history])
        self.refresh_transfers()

    # ================= Helfer =================
    def copy(self, text):
        Gdk.Display.get_default().get_clipboard().set(text)

    def launch(self, uri):
        Gtk.UriLauncher(uri=uri).launch(self.win, None, None)

    def open_folder(self):
        os.makedirs(self.cfg["folder"], exist_ok=True)
        self.launch(Gio.File.new_for_path(self.cfg["folder"]).get_uri())

    def show_file(self, path):
        try:
            Gtk.FileLauncher(file=Gio.File.new_for_path(path)).open_containing_folder(self.win, None, None)
        except Exception:
            self.open_folder()


def main(argv):
    app = Sharist()
    args = [a for a in argv if a not in ("--daemon", "--toggle")]
    if "--daemon" in argv:
        app.daemon = True
    if "--toggle" in argv:
        app.register(None)
        if app.get_is_remote():
            app.activate_action("toggle", None)
            return 0
    return app.run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
