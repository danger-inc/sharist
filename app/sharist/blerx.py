# Quick-Share-Empfang über Bluetooth LE – für Handys, die beim Teilen nur per BLE suchen (neuere Pixels).
# Aufbau nach open-quickshare / rquickshare-Forks (Nearby Connections, google/nearby):
#   1. Werbung unter Dienst 0xFEF3: volle Anzeige (erweiterte Werbung) + 15-Byte-Kopf (klassisch) mit Bloom-Filter.
#   2. GATT-Dienst 0xFEF3: „slot 0“ (lesbar: volle Anzeige) + Weave-Socket (0101 schreiben, 0102 benachrichtigen).
#   3. Über den Weave-Socket läuft der normale Quick-Share-Handshake (nearby.Inbound),
#      danach Wechsel aufs WLAN (Bandwidth-Upgrade auf unseren festen TCP-Port).
# Alles BlueZ-D-Bus-Zeug läuft im GLib-Hauptthread; die Inbound-Verbindung liest/schreibt aus ihrem Thread
# über WeaveSocket (wie ein normaler Socket).
import hashlib, os, socket, struct, sys, threading, time

FEF3 = "0000fef3-0000-1000-8000-00805f9b34fb"
SLOT0 = "00000000-0000-3000-8000-000000000000"
WEAVE_IN = "00000100-0004-1000-8000-001a11000101"     # Handy → wir (schreiben)
WEAVE_OUT = "00000100-0004-1000-8000-001a11000102"    # wir → Handy (benachrichtigen)
SVC_HASH = bytes([0xFC, 0x9F, 0x5E])
SERVICE_ID = b"NearbySharing"
M64 = (1 << 64) - 1


def log(msg):
    print(time.strftime("%X"), "sharist ble:", msg, file=sys.stderr, flush=True)


# ---------------- Bloom-Filter (MurmurHash3 x64_128, untere 64 Bit) ----------------
def _rotl(x, r):
    return ((x << r) | (x >> (64 - r))) & M64


def _fmix(k):
    k ^= k >> 33
    k = (k * 0xFF51AFD7ED558CCD) & M64
    k ^= k >> 33
    k = (k * 0xC4CEB9FE1A85EC53) & M64
    return k ^ (k >> 33)


def murmur3_low(data):
    c1, c2 = 0x87C37B91114253D5, 0x4CF5AD432745937F
    h1 = h2 = 0
    n = len(data) // 16
    for i in range(n):
        k1 = int.from_bytes(data[i * 16:i * 16 + 8], "little")
        k2 = int.from_bytes(data[i * 16 + 8:i * 16 + 16], "little")
        k1 = (_rotl((k1 * c1) & M64, 31) * c2) & M64
        h1 ^= k1
        h1 = (_rotl(h1, 27) + h2) & M64
        h1 = (h1 * 5 + 0x52DCE729) & M64
        k2 = (_rotl((k2 * c2) & M64, 33) * c1) & M64
        h2 ^= k2
        h2 = (_rotl(h2, 31) + h1) & M64
        h2 = (h2 * 5 + 0x38495AB5) & M64
    tail = data[n * 16:]
    if len(tail) > 8:
        k2 = int.from_bytes(tail[8:], "little")
        h2 ^= (_rotl((k2 * c2) & M64, 33) * c1) & M64
    if tail:
        k1 = int.from_bytes(tail[:8], "little")
        h1 ^= (_rotl((k1 * c1) & M64, 31) * c2) & M64
    h1 ^= len(data)
    h2 ^= len(data)
    h1 = (h1 + h2) & M64
    h2 = (h2 + h1) & M64
    h1, h2 = _fmix(h1), _fmix(h2)
    return (h1 + h2) & M64


def _i32(x):
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


def bloom(elements, nbytes=10):
    nbits, out = nbytes * 8, bytearray(nbytes)
    for el in elements:
        low = murmur3_low(el)
        a, b = _i32(low), _i32(low >> 32)
        for i in range(1, 6):
            c = _i32(a + i * b)
            if c < 0:
                c = ~c
            p = c % nbits
            out[p // 8] |= 1 << (p % 8)
    return bytes(out)


# ---------------- Werbedaten ----------------
def full_advert(endpoint_id, einfo, token):
    """Volle Empfänger-Anzeige (Dienstdaten 0xFEF3), ~ wie ein Android in „Alle“-Sichtbarkeit."""
    data = b"\x23" + SVC_HASH + endpoint_id + bytes([len(einfo)]) + einfo + bytes(8)   # MAC 0 → kein RFCOMM
    return b"\x48" + SVC_HASH + struct.pack(">I", len(data)) + data + token + b"\x00"   # 0x00: keine PSM


def advert_header(full):
    """15-Byte-Kopf (passt in klassische Werbung): Version 2, „voll auch erweitert“, 1 Slot, Bloom, Hash."""
    dummy = os.urandom(128)
    h = hashlib.sha256(dummy).digest()[:4]
    h = hashlib.sha256(h + full).digest()[:4]
    return bytes([(2 << 5) | 0x10 | 1]) + bloom([dummy, SERVICE_ID]) + h


# ---------------- WeaveSocket: socket-artig für nearby.Connection ----------------
class WeaveSocket:
    def __init__(self, session):
        self.session = session
        self.buf = bytearray()
        self.cond = threading.Condition()
        self.dead = False
        self.timeout = None

    # vom Hauptthread
    def feed(self, data):
        with self.cond:
            self.buf += data
            self.cond.notify_all()

    def kill(self):
        with self.cond:
            self.dead = True
            self.cond.notify_all()

    # von der Verbindung
    def settimeout(self, t):
        self.timeout = t

    def pending(self):
        with self.cond:
            return len(self.buf)

    def wait_data(self, t):
        with self.cond:
            if not self.buf and not self.dead:
                self.cond.wait(t)
            return bool(self.buf) or self.dead

    def recv(self, n):
        end = None if self.timeout is None else time.time() + self.timeout
        with self.cond:
            while not self.buf and not self.dead:
                left = None if end is None else end - time.time()
                if left is not None and left <= 0:
                    raise socket.timeout("ble: zeit abgelaufen")
                self.cond.wait(left)
            if not self.buf:
                return b""
            out = bytes(self.buf[:n])
            del self.buf[:n]
            return out

    def sendall(self, data):
        if self.dead:
            raise ConnectionError("ble getrennt")
        self.session.send_message(SVC_HASH + data)

    def shutdown(self, how):
        pass

    def close(self):
        self.kill()

    def getpeername(self):
        return ("bluetooth", 0)


# ---------------- Weave-Sitzung (ein Handy) ----------------
class WeaveSession:
    def __init__(self, server):
        self.server = server
        self.connected = False
        self.max_payload = 19
        self.counter = 0
        self.reasm = bytearray()
        self.sock = WeaveSocket(self)
        self.lock = threading.Lock()
        self.started = time.time()
        self.conn = None

    def on_packet(self, pkt):                     # Hauptthread
        if not pkt:
            return
        hdr = pkt[0]
        if hdr & 0x80:                            # Steuerpaket
            cmd = hdr & 0x0F
            if cmd == 0 and not self.connected:   # CONNECTION_REQUEST
                size = int.from_bytes(pkt[5:7], "big") if len(pkt) >= 7 else 100
                size = max(20, min(509, size))
                self.max_payload = size - 1
                self.notify(bytes([0x81, 0x00, 0x01]) + size.to_bytes(2, "big"))   # CONFIRM, Zähler 0
                self.counter = 1
                self.connected = True
                log(f"weave verbunden (paketgröße {size})")
                # eigene INTRODUCTION auf Socket-Ebene, sonst wartet das Handy ~4 s
                self.send_message(b"\x00\x00\x00\x08\x01\x12\x07\x0a\x03" + SVC_HASH + b"\x10\x02")
                self.server.start_inbound(self)
            elif cmd == 2:
                log("weave: fehler vom handy")
                self.end()
            return
        if not self.connected:
            return
        self.reasm += pkt[1:]
        if not hdr & 0x04:                        # noch nicht das letzte Stück
            return
        msg, self.reasm = bytes(self.reasm), bytearray()
        if len(msg) < 3:
            return
        if msg[:3] == b"\x00\x00\x00":            # SocketControlFrame
            kind = msg[4] if len(msg) >= 5 and msg[3] == 0x08 else 0
            if kind == 2:
                log("weave: handy trennt")
                self.end()
            return
        self.sock.feed(msg[3:])

    def send_message(self, msg):                  # beliebiger Thread
        with self.lock:
            off_ = 0
            while off_ < len(msg) or off_ == 0:
                part = msg[off_:off_ + self.max_payload]
                h = (self.counter & 7) << 4
                if off_ == 0:
                    h |= 0x08
                off_ += len(part)
                if off_ >= len(msg):
                    h |= 0x04
                self.counter += 1
                self.notify(bytes([h]) + part)
                if not part:
                    break

    def notify(self, value):
        self.server.notify(value)

    def end(self):
        self.sock.kill()
        self.server.session_ended(self)


# ---------------- GATT-Server + Werbung über BlueZ ----------------
class BleReceiver:
    ROOT = "/de/invincible/sharist/gatt"
    ADV = "/de/invincible/sharist/rxadv"
    IFACES = """<node>
    <interface name="org.freedesktop.DBus.ObjectManager">
      <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
    </interface>
    <interface name="org.bluez.GattService1">
      <property name="UUID" type="s" access="read"/><property name="Primary" type="b" access="read"/>
    </interface>
    <interface name="org.bluez.GattCharacteristic1">
      <method name="ReadValue"><arg type="a{sv}" direction="in"/><arg type="ay" direction="out"/></method>
      <method name="WriteValue"><arg type="ay" direction="in"/><arg type="a{sv}" direction="in"/></method>
      <method name="StartNotify"/><method name="StopNotify"/>
      <property name="UUID" type="s" access="read"/><property name="Service" type="o" access="read"/>
      <property name="Flags" type="as" access="read"/><property name="Value" type="ay" access="read"/>
      <property name="Notifying" type="b" access="read"/>
    </interface>
    <interface name="org.bluez.LEAdvertisement1">
      <method name="Release"/>
      <property name="Type" type="s" access="read"/><property name="ServiceUUIDs" type="as" access="read"/>
      <property name="ServiceData" type="a{sv}" access="read"/><property name="Discoverable" type="b" access="read"/>
      <property name="MinInterval" type="u" access="read"/><property name="MaxInterval" type="u" access="read"/>
    </interface></node>"""

    def __init__(self, engine):
        self.engine = engine
        self.bus = self.adapter = None
        self.regs = []
        self.adv_paths = []
        self.app_ok = False
        self.session = None
        self.notifying = False
        self.full = self.header = b""
        self.timer = None
        self.fast = True
        self.error = None

    # --- Lebenszyklus ---
    def start(self):
        from gi.repository import Gio, GLib
        if self.regs:
            return True
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
            objs = self.bus.call_sync("org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                                      None, None, Gio.DBusCallFlags.NONE, 3000).unpack()[0]
            self.adapter = next((p for p, i in objs.items() if "org.bluez.GattManager1" in i
                                 and "org.bluez.LEAdvertisingManager1" in i
                                 and i.get("org.bluez.Adapter1", {}).get("Powered")), None)
            if not self.adapter:
                self.error = "bluetooth aus"
                log(self.error)
                return False
            eng = self.engine
            self.full = full_advert(eng.endpoint_id, eng.endpoint_info(), os.urandom(2))
            self.header = advert_header(self.full)
            node = Gio.DBusNodeInfo.new_for_xml(self.IFACES)
            ifs = {i.name: i for i in node.interfaces}
            svc = self.ROOT + "/service0"
            self.objects = {
                svc: ("org.bluez.GattService1", {"UUID": GLib.Variant("s", FEF3), "Primary": GLib.Variant("b", True)}),
                svc + "/slot0": ("org.bluez.GattCharacteristic1", self._char(SLOT0, svc, ["read"])),
                svc + "/weavein": ("org.bluez.GattCharacteristic1", self._char(WEAVE_IN, svc, ["write"])),
                svc + "/weaveout": ("org.bluez.GattCharacteristic1",
                                    self._char(WEAVE_OUT, svc, ["notify", "indicate"])),
            }
            self.out_path = svc + "/weaveout"
            self.regs.append(self.bus.register_object(self.ROOT, ifs["org.freedesktop.DBus.ObjectManager"],
                                                      self._method, None, None))
            for path, (iface, _props) in self.objects.items():
                self.regs.append(self.bus.register_object(path, ifs[iface], self._method, self._getprop, None))
            self.adv_iface = ifs["org.bluez.LEAdvertisement1"]
            self.bus.call("org.bluez", self.adapter, "org.bluez.GattManager1", "RegisterApplication",
                          GLib.Variant("(oa{sv})", (self.ROOT, {})), None, Gio.DBusCallFlags.NONE, 10000, None,
                          self._app_done)
            self._advertise()
            self.timer = GLib.timeout_add_seconds(180, self._watch)
            return True
        except Exception as e:
            self.error = str(e)
            log(f"start: {e}")
            self.stop()
            return False

    def _char(self, uuid, svc, flags):
        from gi.repository import GLib
        return {"UUID": GLib.Variant("s", uuid), "Service": GLib.Variant("o", svc),
                "Flags": GLib.Variant("as", flags)}

    def _app_done(self, bus, res):
        try:
            bus.call_finish(res)
            self.app_ok = True
            log(f"gatt-dienst bereit (anzeige {len(self.full)} byte, kopf {len(self.header)} byte)")
        except Exception as e:
            self.error = getattr(e, "message", str(e))
            log(f"gatt: {self.error}")

    def stop(self):
        from gi.repository import Gio, GLib
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        if self.session:
            self.session.sock.kill()
            self.session = None
        self._unadvertise()
        if self.bus and self.adapter and self.app_ok:
            try:
                self.bus.call_sync("org.bluez", self.adapter, "org.bluez.GattManager1", "UnregisterApplication",
                                   GLib.Variant("(o)", (self.ROOT,)), None, Gio.DBusCallFlags.NONE, 2000)
            except Exception:
                pass
        self.app_ok = False
        for r in self.regs:
            try:
                self.bus.unregister_object(r)
            except Exception:
                pass
        self.regs = []

    # --- Werbung ---
    def _advertise(self):
        from gi.repository import Gio, GLib
        self._unadvertise()
        for i, data in enumerate((self.full, self.header)):
            path = f"{self.ADV}{i}"
            props = {"Type": GLib.Variant("s", "peripheral"),
                     "ServiceUUIDs": GLib.Variant("as", [FEF3]),
                     "ServiceData": GLib.Variant("a{sv}", {FEF3: GLib.Variant("ay", data)}),
                     "Discoverable": GLib.Variant("b", True)}
            if self.fast:
                props["MinInterval"] = GLib.Variant("u", 100)
                props["MaxInterval"] = GLib.Variant("u", 150)
            reg = self.bus.register_object(path, self.adv_iface, lambda *a: a[-1].return_value(None),
                                           lambda c, s, p, i, n, props=props: props.get(n), None)
            self.adv_paths.append((path, reg))
            self.bus.call("org.bluez", self.adapter, "org.bluez.LEAdvertisingManager1", "RegisterAdvertisement",
                          GLib.Variant("(oa{sv})", (path, {})), None, Gio.DBusCallFlags.NONE, 10000, None,
                          self._adv_done, (i, path))

    def _adv_done(self, bus, res, data):
        i, path = data
        try:
            bus.call_finish(res)
        except Exception as e:
            msg = getattr(e, "message", str(e))
            log(f"werbung {'voll' if i == 0 else 'kopf'}: {msg}")
            if self.fast and ("Interval" in msg or "Invalid" in msg):
                self.fast = False         # MinInterval/MaxInterval sind in BlueZ experimentell
                from gi.repository import GLib
                GLib.timeout_add(500, lambda: (self._advertise(), False)[1])

    def _unadvertise(self):
        from gi.repository import Gio, GLib
        for path, reg in self.adv_paths:
            try:
                self.bus.call_sync("org.bluez", self.adapter, "org.bluez.LEAdvertisingManager1",
                                   "UnregisterAdvertisement", GLib.Variant("(o)", (path,)), None,
                                   Gio.DBusCallFlags.NONE, 2000)
            except Exception:
                pass
            self.bus.unregister_object(reg)
        self.adv_paths = []

    def _watch(self):
        """Verbindbare Werbung wird von einer Verbindung „verbraucht“ – ohne Sitzung regelmäßig erneuern."""
        if not self.session:
            self._advertise()
        return True

    # --- D-Bus-Aufrufe ---
    def _getprop(self, conn, sender, path, iface, name):
        from gi.repository import GLib
        if path == self.out_path and name == "Notifying":
            return GLib.Variant("b", self.notifying)
        if name == "Value":
            return GLib.Variant("ay", b"")
        return self.objects.get(path, (None, {}))[1].get(name)

    def _method(self, conn, sender, path, iface, name, params, inv):
        from gi.repository import GLib
        try:
            if name == "GetManagedObjects":
                out = {p: {i: props} for p, (i, props) in self.objects.items()}
                inv.return_value(GLib.Variant("(a{oa{sa{sv}}})", (out,)))
            elif name == "ReadValue":
                if path.endswith("/slot0"):
                    log("handy liest anzeige (slot 0)")
                    off_ = params.unpack()[0].get("offset", 0)
                    inv.return_value(GLib.Variant("(ay)", (self.full[off_:],)))
                else:
                    inv.return_value(GLib.Variant("(ay)", (b"",)))
            elif name == "WriteValue":
                value = bytes(params.unpack()[0])
                inv.return_value(None)
                if path.endswith("/weavein"):
                    if value and value[0] & 0x8F == 0x80:     # CONNECTION_REQUEST → neue Sitzung
                        self._new_session()
                    if self.session:
                        self.session.on_packet(value)
            elif name == "StartNotify":
                self.notifying = True
                inv.return_value(None)
                log("handy hat weave-benachrichtigung an")
            elif name == "StopNotify":
                self.notifying = False
                inv.return_value(None)
                if self.session:
                    self.session.end()
            else:
                inv.return_value(None)
        except Exception as e:
            log(f"{name}: {e!r}")
            try:
                inv.return_dbus_error("org.bluez.Error.Failed", str(e))
            except Exception:
                pass

    def _new_session(self):
        if self.session:
            self.session.sock.kill()
        self.session = WeaveSession(self)

    def notify(self, value):
        from gi.repository import GLib
        self.bus.emit_signal(None, self.out_path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                             GLib.Variant("(sa{sv}as)", ("org.bluez.GattCharacteristic1",
                                                         {"Value": GLib.Variant("ay", value)}, [])))

    def start_inbound(self, session):
        session.conn = self.engine.inbound_ble(session.sock)

    def session_ended(self, session):
        if self.session is session:
            self.session = None
            from gi.repository import GLib
            GLib.timeout_add(1500, lambda: (self._advertise() if not self.session and self.regs else None, False)[1])
