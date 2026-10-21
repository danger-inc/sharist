# Quick Share (ex Nearby Share) über WLAN – Protokoll nach NearDrop (github.com/grishka/NearDrop, PROTOCOL.md).
# Empfänger = Server: TCP-Port + mDNS-Dienst _FC9F5ED42C8A._tcp. Sender = Client: findet den Dienst, verbindet sich.
# Ablauf: ConnectionRequest → UKEY2 (P-256) → ConnectionResponse → ab da AES-256-CBC + HMAC (SecureMessage) →
# PairedKey-Rituale → Introduction (Dateiliste) → Zustimmung → Datei-Häppchen.
# Keine GTK-Abhängigkeit: Rückmeldungen gehen an ein Listener-Objekt (aus Threads – die UI muss selbst umleiten).
import base64, hashlib, hmac, os, queue, random, re, socket, string, struct, sys, threading, time

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "pb"))
import device_to_device_messages_pb2 as d2d  # noqa: E402
import offline_wire_formats_pb2 as off  # noqa: E402
import securegcm_pb2 as gcm  # noqa: E402
import securemessage_pb2 as smsg  # noqa: E402
import ukey_pb2 as ukey  # noqa: E402
import wire_format_pb2 as wire  # noqa: E402

SERVICE = "_FC9F5ED42C8A._tcp.local."
SERVICE_ID = bytes([0xFC, 0x9F, 0x5E])
MAX_FRAME = 5 * 1024 * 1024
CHUNK = 512 * 1024
D2D_SALT = hashlib.sha256(b"D2D").digest()
SMSG_SALT = hashlib.sha256(b"SecureMessage").digest()
DEVICE_TYPES = {0: "gerät", 1: "handy", 2: "tablet", 3: "laptop", 4: "auto", 5: "faltbar", 6: "xr"}
DEVICE_ICONS = {0: "󰾰", 1: "󰄜", 2: "󰓶", 3: "󰌢", 4: "󰄋", 5: "󰄜", 6: "󰢔"}


class ShareError(Exception):
    pass


# ---------------- kleine Helfer ----------------
def log(msg):
    print(time.strftime("%X"), "sharist:", msg, file=sys.stderr, flush=True)


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text):
    if isinstance(text, bytes):
        text = text.decode()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hkdf(ikm, salt, info, length=32):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def rand_id():
    return random.getrandbits(63) * random.choice((1, -1))


def new_endpoint_id():
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(4)).encode()


def endpoint_info(name, device_type=3, identity=None):
    """1 Byte Typ/Sichtbarkeit, 16 Zufallsbytes, Name mit Längenbyte."""
    raw = name.encode()[:255]
    return bytes([device_type << 1]) + (identity or os.urandom(16)) + bytes([len(raw)]) + raw


def parse_endpoint_info(data):
    """→ (name | None, gerätetyp, qr-daten | None)"""
    if len(data) < 17:
        return None, 0, None
    dtype = (data[0] >> 1) & 7
    has_name = not data[0] & 0x10
    name, off_ = None, 17
    if has_name and len(data) > 17:
        n = data[17]
        name = data[18:18 + n].decode("utf-8", "replace")
        off_ = 18 + n
    qr = None
    while len(data) - off_ > 2:
        t, n = data[off_], data[off_ + 1]
        off_ += 2
        if len(data) - off_ >= n:
            if t == 1:
                qr = data[off_:off_ + n]
            off_ += n
    return name, dtype, qr


def service_name(endpoint_id):
    return b64u(b"\x23" + endpoint_id + SERVICE_ID + b"\x00\x00")


def endpoint_from_service(name):
    try:
        raw = unb64u(name.split(".")[0])
    except Exception:
        return None
    if len(raw) < 10 or raw[0] != 0x23 or raw[5:8] != SERVICE_ID:
        return None
    return raw[1:5].decode("ascii", "replace")


def signed_bytes(n):
    """Zahl → Zweierkomplement-Bytes (wie Java BigInteger.toByteArray): führende 0, wenn das oberste Bit gesetzt ist."""
    b = n.to_bytes(32, "big")
    return b"\x00" + b if b[0] & 0x80 else b


def pin_from_auth(key):
    h, m = 0, 1
    for byte in key:
        b = byte - 256 if byte > 127 else byte
        h = (h + b * m) % 9973
        m = (m * 31) % 9973
    return "%04d" % abs(h)


def safe_name(name):
    name = re.sub(r'[/\\?%*:|"<>=\x00-\x1f]', "_", name or "").strip(". ") or "datei"
    return name[:200]


def unique_path(folder, name):
    path = os.path.join(folder, name)
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(os.path.join(folder, f"{base} ({i}){ext}")):
        i += 1
    return os.path.join(folder, f"{base} ({i}){ext}")


def mime_of(path):
    import mimetypes
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def file_type(mime, path):
    T = wire.FileMetadata
    if mime.startswith("image/"):
        return T.IMAGE
    if mime.startswith("video/"):
        return T.VIDEO
    if mime.startswith("audio/"):
        return T.AUDIO
    if path.lower().endswith(".apk"):
        return T.ANDROID_APP
    if mime.startswith("text/") or mime in ("application/pdf",) or "document" in mime:
        return T.DOCUMENT
    return T.UNKNOWN


# ---------------- QR-Code (Sender zeigt, Android scannt) ----------------
class QrKey:
    """Schlüssel für quickshare.google/qrcode#key=…: das Handy scannt, wirbt dann mit einem passenden Token,
    wir verbinden uns von selbst, und mit der Signatur entfällt dort die Nachfrage."""

    def __init__(self):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        # 2 Bytes Version + komprimierter Punkt (02/03 je nach y-Parität + x)
        self.data = b"\x00\x00" + self.priv.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)
        self.token = hkdf(self.data, b"", b"advertisingContext", 16)
        self.name_key = hkdf(self.data, b"", b"encryptionKey", 16)
        self.url = "https://quickshare.google/qrcode#key=" + b64u(self.data)

    def matches(self, qr):
        """→ (passt?, entschlüsselter Name | None)"""
        if not qr:
            return False, None
        if qr == self.token:
            return True, None
        if len(qr) > 28:
            try:
                name = AESGCM(self.name_key).decrypt(qr[:12], qr[12:], self.token)
                return True, name.decode("utf-8", "replace")
            except Exception:
                pass
        return False, None

    def sign(self, auth_key):
        r, s = decode_dss_signature(self.priv.sign(auth_key, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


# ---------------- Übertragungsobjekt (für die Oberfläche) ----------------
class Transfer:
    """Eine Übertragung (ein- oder ausgehend). Felder liest die UI, Änderungen meldet listener.changed(t)."""

    def __init__(self, conn, incoming):
        self.conn = conn
        self.incoming = incoming
        self.id = rand_id() & 0xFFFFFFFF
        self.peer = "?"
        self.peer_type = 0
        self.pin = None
        self.files = []        # [{name, size, path, done}]
        self.texts = []        # [{kind, title, size, value}]
        self.wifi = []         # [{ssid, password}]
        self.total = 0
        self.done = 0
        self.state = "verbinde"   # verbinde · frage · wartet · läuft · fertig · abgelehnt · abgebrochen · fehler
        self.error = None
        self.started = time.time()

    @property
    def finished(self):
        return self.state in ("fertig", "abgelehnt", "abgebrochen", "fehler")

    def accept(self):
        self.conn.accept()

    def reject(self):
        self.conn.reject()

    def cancel(self):
        self.conn.cancel()


# ---------------- Verbindung (gemeinsam) ----------------
class Connection(threading.Thread):
    SERVER = True

    def __init__(self, engine, sock):
        super().__init__(daemon=True)
        self.engine = engine
        self.sock = sock
        self.send_lock = threading.Lock()
        self.encrypted = False
        self.seq_out = 0
        self.seq_in = 0
        self.buffers = {}
        self.closed = False
        self.priv = None
        self.client_init_raw = self.server_init_raw = None
        self.auth_key = None
        self.t = Transfer(self, self.SERVER)
        self.last_rx = time.time()
        self.first = None        # schon gelesener erster Rahmen

    # --- Rahmen ---
    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("verbindung getrennt")
            buf += chunk
        return bytes(buf)

    def recv_frame(self):
        if self.first is not None:
            data, self.first = self.first, None
            return data
        (n,) = struct.unpack(">I", self._recv_exact(4))
        if n > MAX_FRAME:
            raise ShareError("rahmen zu groß")
        data = self._recv_exact(n)
        self.last_rx = time.time()
        return data

    def send_frame(self, data):
        with self.send_lock:
            if not self.closed:
                self.sock.sendall(struct.pack(">I", len(data)) + data)

    # --- Offline-Frames ---
    @staticmethod
    def offline(ftype, **fields):
        f = off.OfflineFrame(version=off.OfflineFrame.V1)
        f.v1.type = ftype
        for k, v in fields.items():
            getattr(f.v1, k).CopyFrom(v)
        return f

    def send_offline(self, frame):
        if self.encrypted:
            self.send_secure(frame.SerializeToString())
        else:
            self.send_frame(frame.SerializeToString())

    def connection_response(self):
        r = off.ConnectionResponseFrame(response=off.ConnectionResponseFrame.ACCEPT, status=0)
        r.os_info.type = off.OsInfo.LINUX
        return self.offline(off.V1Frame.CONNECTION_RESPONSE, connection_response=r)

    def send_keepalive(self, ack=False):
        try:
            self.send_offline(self.offline(off.V1Frame.KEEP_ALIVE, keep_alive=off.KeepAliveFrame(ack=ack)))
        except Exception:
            pass

    def send_disconnect(self):
        try:
            self.send_offline(self.offline(off.V1Frame.DISCONNECTION, disconnection=off.DisconnectionFrame()))
        except Exception:
            pass
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    # --- UKEY2 ---
    def make_key(self):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        n = self.priv.public_key().public_numbers()
        pk = smsg.GenericPublicKey(type=smsg.EC_P256)
        pk.ec_p256_public_key.x = signed_bytes(n.x)
        pk.ec_p256_public_key.y = signed_bytes(n.y)
        return pk.SerializeToString()

    def finish_key_exchange(self, peer_key_raw):
        pk = smsg.GenericPublicKey.FromString(peer_key_raw)
        if not pk.HasField("ec_p256_public_key"):
            raise ShareError("kein P-256-schlüssel")
        x = int.from_bytes(pk.ec_p256_public_key.x[-32:], "big")
        y = int.from_bytes(pk.ec_p256_public_key.y[-32:], "big")
        peer = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        secret = hashlib.sha256(self.priv.exchange(ec.ECDH(), peer)).digest()
        info = self.client_init_raw + self.server_init_raw
        self.auth_key = hkdf(secret, b"UKEY2 v1 auth", info)
        nxt = hkdf(secret, b"UKEY2 v1 next", info)
        self.t.pin = pin_from_auth(self.auth_key)
        ck = hkdf(nxt, D2D_SALT, b"client")
        sk = hkdf(nxt, D2D_SALT, b"server")
        c_enc, c_mac = hkdf(ck, SMSG_SALT, b"ENC:2"), hkdf(ck, SMSG_SALT, b"SIG:1")
        s_enc, s_mac = hkdf(sk, SMSG_SALT, b"ENC:2"), hkdf(sk, SMSG_SALT, b"SIG:1")
        if self.SERVER:
            self.dec_key, self.rx_mac, self.enc_key, self.tx_mac = c_enc, c_mac, s_enc, s_mac
        else:
            self.dec_key, self.rx_mac, self.enc_key, self.tx_mac = s_enc, s_mac, c_enc, c_mac

    def ukey_alert(self, kind):
        a = ukey.Ukey2Alert(type=kind)
        try:
            self.send_frame(ukey.Ukey2Message(message_type=ukey.Ukey2Message.ALERT,
                                              message_data=a.SerializeToString()).SerializeToString())
        except Exception:
            pass
        raise ShareError("ukey2-fehler")

    # --- SecureMessage ---
    def send_secure(self, payload):
        with self.send_lock:
            self.seq_out += 1
            m = d2d.DeviceToDeviceMessage(sequence_number=self.seq_out, message=payload).SerializeToString()
            iv = os.urandom(16)
            p = padding.PKCS7(128).padder()
            m = p.update(m) + p.finalize()
            enc = Cipher(algorithms.AES(self.enc_key), modes.CBC(iv)).encryptor()
            body = enc.update(m) + enc.finalize()
            hb = smsg.HeaderAndBody(body=body)
            hb.header.encryption_scheme = smsg.AES_256_CBC
            hb.header.signature_scheme = smsg.HMAC_SHA256
            hb.header.iv = iv
            hb.header.public_metadata = gcm.GcmMetadata(type=gcm.DEVICE_TO_DEVICE_MESSAGE, version=1).SerializeToString()
            hbr = hb.SerializeToString()
            sm = smsg.SecureMessage(header_and_body=hbr, signature=hmac.new(self.tx_mac, hbr, hashlib.sha256).digest())
            data = sm.SerializeToString()
            if not self.closed:
                self.sock.sendall(struct.pack(">I", len(data)) + data)

    def open_secure(self, raw):
        sm = smsg.SecureMessage.FromString(raw)
        if not hmac.compare_digest(hmac.new(self.rx_mac, sm.header_and_body, hashlib.sha256).digest(), sm.signature):
            raise ShareError("signatur falsch")
        hb = smsg.HeaderAndBody.FromString(sm.header_and_body)
        dec = Cipher(algorithms.AES(self.dec_key), modes.CBC(hb.header.iv)).decryptor()
        m = dec.update(hb.body) + dec.finalize()
        u = padding.PKCS7(128).unpadder()
        m = d2d.DeviceToDeviceMessage.FromString(u.update(m) + u.finalize())
        self.seq_in += 1
        if m.sequence_number != self.seq_in:
            raise ShareError("falsche reihenfolge")
        return off.OfflineFrame.FromString(m.message)

    # --- Payload-Ebene ---
    def send_bytes(self, data, pid=None):
        pid = rand_id() if pid is None else pid
        pt = off.PayloadTransferFrame(packet_type=off.PayloadTransferFrame.DATA)
        pt.payload_header.id = pid
        pt.payload_header.type = off.PayloadTransferFrame.PayloadHeader.BYTES
        pt.payload_header.total_size = len(data)
        pt.payload_header.is_sensitive = False
        pt.payload_chunk.offset = 0
        pt.payload_chunk.flags = 0
        pt.payload_chunk.body = data
        self.send_offline(self.offline(off.V1Frame.PAYLOAD_TRANSFER, payload_transfer=pt))
        pt.payload_chunk.flags = 1
        pt.payload_chunk.offset = len(data)
        pt.payload_chunk.ClearField("body")
        self.send_offline(self.offline(off.V1Frame.PAYLOAD_TRANSFER, payload_transfer=pt))

    def send_share(self, ftype, **fields):
        f = wire.Frame(version=wire.Frame.V1)
        f.v1.type = ftype
        for k, v in fields.items():
            getattr(f.v1, k).CopyFrom(v)
        self.send_bytes(f.SerializeToString())

    def paired_key_encryption(self, qr_sig=None):
        p = wire.PairedKeyEncryptionFrame(secret_id_hash=os.urandom(6), signed_data=os.urandom(72))
        if qr_sig:
            p.qr_code_handshake_data = qr_sig
        self.send_share(wire.V1Frame.PAIRED_KEY_ENCRYPTION, paired_key_encryption=p)

    def paired_key_result(self):
        self.send_share(wire.V1Frame.PAIRED_KEY_RESULT,
                        paired_key_result=wire.PairedKeyResultFrame(status=wire.PairedKeyResultFrame.UNABLE))

    def handle_secure(self, raw):
        self.dispatch(self.open_secure(raw))

    def dispatch(self, f):
        t = f.v1.type
        if t == off.V1Frame.PAYLOAD_TRANSFER:
            pt = f.v1.payload_transfer
            h, c = pt.payload_header, pt.payload_chunk
            if h.type == off.PayloadTransferFrame.PayloadHeader.BYTES:
                if h.total_size > MAX_FRAME:
                    raise ShareError("nutzlast zu groß")
                buf = self.buffers.setdefault(h.id, bytearray())
                if c.offset != len(buf):
                    raise ShareError("falscher versatz")
                buf += c.body
                if c.flags & 1:
                    data = bytes(self.buffers.pop(h.id))
                    if not self.on_bytes(h.id, data):
                        self.on_share(wire.Frame.FromString(data))
            elif h.type == off.PayloadTransferFrame.PayloadHeader.FILE:
                self.on_file_chunk(h, c)
        elif t == off.V1Frame.KEEP_ALIVE:
            if not f.v1.keep_alive.ack:
                self.send_keepalive(ack=True)
        elif t == off.V1Frame.DISCONNECTION:
            self.on_disconnect()
            self.close()
        elif t == off.V1Frame.BANDWIDTH_UPGRADE_NEGOTIATION:
            log(f"upgrade-rahmen außer der reihe: {f.v1.bandwidth_upgrade_negotiation.event_type}")

    def on_bytes(self, pid, data):
        return False

    def on_share(self, frame):
        pass

    def on_file_chunk(self, header, chunk):
        raise ShareError("unerwartete datei")

    def on_disconnect(self):
        pass

    # --- Lebenszyklus ---
    def changed(self):
        self.engine.emit("changed", self.t)

    def set_state(self, state, error=None):
        if self.t.finished and state != "fertig":
            return
        self.t.state = state
        if error:
            self.t.error = error
        self.changed()

    def keepalive_loop(self):
        while not self.closed:
            time.sleep(10)
            if self.encrypted and not self.closed:
                self.send_keepalive()

    def run(self):
        threading.Thread(target=self.keepalive_loop, daemon=True).start()
        try:
            self.main()
        except Exception as e:
            if not self.t.finished:
                msg = str(e) or e.__class__.__name__
                if isinstance(e, (ConnectionError, OSError)) and not isinstance(e, ShareError):
                    msg = "verbindung getrennt" if self.t.state != "verbinde" else f"keine verbindung ({e})"
                self.set_state("fehler", msg)
                print("sharist:", repr(e), file=sys.stderr, flush=True)
        finally:
            self.cleanup()
            self.close()
            if not self.t.finished:
                self.set_state("fehler", "verbindung getrennt")
            self.engine.emit("ended", self.t)

    def cleanup(self):
        pass


# ---------------- Empfangen ----------------
class Inbound(Connection):
    SERVER = True

    def __init__(self, engine, sock):
        super().__init__(engine, sock)
        self.stage = "request"
        self.payloads = {}       # payload_id → {"kind": file|text|wifi, …}
        self.decision = None

    def main(self):
        self.sock.settimeout(60)
        # 1) ConnectionRequest
        f = off.OfflineFrame.FromString(self.recv_frame())
        if f.v1.type != off.V1Frame.CONNECTION_REQUEST:
            raise ShareError("unerwarteter anfang")
        name, dtype, _ = parse_endpoint_info(f.v1.connection_request.endpoint_info)
        self.peer_eid = f.v1.connection_request.endpoint_id
        self.t.peer = name or f.v1.connection_request.endpoint_name or "unbekannt"
        log(f"eingehend von {self.t.peer} ({self.peer_eid}) über {self.t.address}")
        self.t.peer_type = dtype
        # 2) UKEY2 ClientInit → ServerInit
        raw = self.recv_frame()
        msg = ukey.Ukey2Message.FromString(raw)
        if msg.message_type != ukey.Ukey2Message.CLIENT_INIT:
            self.ukey_alert(ukey.Ukey2Alert.BAD_MESSAGE_TYPE)
        ci = ukey.Ukey2ClientInit.FromString(msg.message_data)
        if ci.version != 1 or len(ci.random) != 32 or ci.next_protocol != "AES_256_CBC-HMAC_SHA256":
            self.ukey_alert(ukey.Ukey2Alert.BAD_VERSION)
        commitment = next((c.commitment for c in ci.cipher_commitments if c.handshake_cipher == ukey.P256_SHA512), None)
        if commitment is None:
            self.ukey_alert(ukey.Ukey2Alert.BAD_HANDSHAKE_CIPHER)
        self.client_init_raw = raw
        si = ukey.Ukey2ServerInit(version=1, random=os.urandom(32), handshake_cipher=ukey.P256_SHA512,
                                  public_key=self.make_key())
        self.server_init_raw = ukey.Ukey2Message(message_type=ukey.Ukey2Message.SERVER_INIT,
                                                 message_data=si.SerializeToString()).SerializeToString()
        self.send_frame(self.server_init_raw)
        # 3) ClientFinish
        raw = self.recv_frame()
        if hashlib.sha512(raw).digest() != commitment:
            raise ShareError("ukey2: commitment passt nicht")
        msg = ukey.Ukey2Message.FromString(raw)
        if msg.message_type != ukey.Ukey2Message.CLIENT_FINISH:
            raise ShareError("ukey2: kein client finish")
        self.finish_key_exchange(ukey.Ukey2ClientFinished.FromString(msg.message_data).public_key)
        # 4) ConnectionResponse beider Seiten
        f = off.OfflineFrame.FromString(self.recv_frame())
        if f.v1.type != off.V1Frame.CONNECTION_RESPONSE:
            raise ShareError("keine verbindungsantwort")
        self.send_frame(self.connection_response().SerializeToString())
        self.encrypted = True
        self.paired_key_encryption()
        self.stage = "pke"
        self.changed()
        if getattr(self.sock, "is_ble", False):
            try:
                self.upgrade_wifi()
            except (ShareError, ConnectionError):
                raise
            except Exception as e:
                log(f"wlan-upgrade fehlgeschlagen, bleibe bei bluetooth: {e!r}")
        self.sock.settimeout(None)
        while not self.closed:
            self.handle_secure(self.recv_frame())

    # --- Bluetooth → WLAN (Bandwidth-Upgrade) ---
    @staticmethod
    def bwu(event, **fields):
        b = off.BandwidthUpgradeNegotiationFrame(event_type=event, **fields)
        return Connection.offline(off.V1Frame.BANDWIDTH_UPGRADE_NEGOTIATION, bandwidth_upgrade_negotiation=b)

    def upgrade_wifi(self):
        B = off.BandwidthUpgradeNegotiationFrame
        ips = self.engine.local_ips()
        port = self.engine.server.getsockname()[1] if self.engine.server else None
        if not ips or not port:
            log("kein wlan – übertragung bleibt bei bluetooth")
            return
        q = queue.Queue()
        self.engine.bwu_wait[self.peer_eid] = q
        try:
            info = B.UpgradePathInfo(medium=B.UpgradePathInfo.WIFI_LAN, supports_client_introduction_ack=True)
            info.wifi_lan_socket.ip_address = ips[0]
            info.wifi_lan_socket.wifi_port = port
            self.send_offline(self.bwu(B.UPGRADE_PATH_AVAILABLE, upgrade_path_info=info))
            log(f"biete wlan an: {socket.inet_ntoa(ips[0])}:{port}")
            end = time.time() + 30
            tcp = None
            while time.time() < end and not self.closed:
                try:
                    tcp, _first = q.get(timeout=0.2)
                    break
                except queue.Empty:
                    pass
                if self.sock.pending():
                    f = self.open_secure(self.recv_frame())
                    if f.v1.type == off.V1Frame.BANDWIDTH_UPGRADE_NEGOTIATION:
                        ev = f.v1.bandwidth_upgrade_negotiation.event_type
                        if ev == B.UPGRADE_FAILURE:
                            log("handy meldet upgrade-fehler – bleibe bei bluetooth")
                            return
                        log(f"upgrade-ereignis {ev} beim warten")
                    else:
                        self.dispatch(f)
        finally:
            self.engine.bwu_wait.pop(self.peer_eid, None)
        if not tcp:
            log("handy kam nicht übers wlan – bleibe bei bluetooth")
            return
        tcp.settimeout(10)
        ack = self.bwu(B.CLIENT_INTRODUCTION_ACK, client_introduction_ack=B.ClientIntroductionAck()).SerializeToString()
        tcp.sendall(struct.pack(">I", len(ack)) + ack)
        # alten Kanal leeren: LAST_WRITE ↔ SAFE_TO_CLOSE
        self.send_offline(self.bwu(B.LAST_WRITE_TO_PRIOR_CHANNEL))
        self.sock.settimeout(5)
        for _ in range(16):
            try:
                f = self.open_secure(self.recv_frame())
            except (socket.timeout, TimeoutError):
                break
            if f.v1.type != off.V1Frame.BANDWIDTH_UPGRADE_NEGOTIATION:
                self.dispatch(f)
                continue
            ev = f.v1.bandwidth_upgrade_negotiation.event_type
            if ev == B.LAST_WRITE_TO_PRIOR_CHANNEL:
                self.send_offline(self.bwu(B.SAFE_TO_CLOSE_PRIOR_CHANNEL))
            elif ev == B.SAFE_TO_CLOSE_PRIOR_CHANNEL:
                break
        # Klartext-DISCONNECTION auf Bluetooth, dann Socket tauschen (Schlüssel + Zähler bleiben)
        d = self.offline(off.V1Frame.DISCONNECTION, disconnection=off.DisconnectionFrame(
            request_safe_to_disconnect=False, ack_safe_to_disconnect=False))
        try:
            self.send_frame(d.SerializeToString())
        except Exception:
            pass
        time.sleep(0.2)
        with self.send_lock:
            old, self.sock = self.sock, tcp
        tcp.settimeout(None)
        tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.t.address = tcp.getpeername()[0]
        log(f"auf wlan gewechselt ({self.t.address})")
        old.kill()

    def on_share(self, frame):
        v = frame.v1
        if v.type == wire.V1Frame.CANCEL:
            self.set_state("abgebrochen", "vom sender abgebrochen")
            self.send_disconnect()
            return
        if self.stage == "pke" and v.type == wire.V1Frame.PAIRED_KEY_ENCRYPTION:
            self.paired_key_result()
            self.stage = "pkr"
        elif self.stage == "pkr" and v.type == wire.V1Frame.PAIRED_KEY_RESULT:
            self.stage = "intro"
        elif self.stage == "intro" and v.type == wire.V1Frame.INTRODUCTION:
            self.introduction(v.introduction)

    def introduction(self, intro):
        t = self.t
        for fm in intro.file_metadata:
            sub = "/".join(safe_name(p) for p in fm.parent_folder.split("/") if p) if fm.parent_folder else ""
            item = {"name": safe_name(fm.name), "size": fm.size, "sub": sub, "path": None, "done": 0, "fh": None,
                    "mime": fm.mime_type}
            t.files.append(item)
            self.payloads[fm.payload_id] = ("file", item)
            t.total += fm.size
        for tm in intro.text_metadata:
            kinds = {wire.TextMetadata.URL: "link", wire.TextMetadata.PHONE_NUMBER: "telefon",
                     wire.TextMetadata.ADDRESS: "adresse"}
            item = {"kind": kinds.get(tm.type, "text"), "title": tm.text_title, "size": tm.size, "value": None}
            t.texts.append(item)
            self.payloads[tm.payload_id] = ("text", item)
            t.total += tm.size
        for wm in intro.wifi_credentials_metadata:
            item = {"ssid": wm.ssid, "password": None}
            t.wifi.append(item)
            self.payloads[wm.payload_id] = ("wifi", item)
        if not self.payloads:
            self.respond(wire.ConnectionResponseFrame.UNSUPPORTED_ATTACHMENT_TYPE)
            self.set_state("fehler", "nichts dabei, was sharist kennt")
            self.send_disconnect()
            return
        self.set_state("frage")
        self.engine.emit("incoming", t)

    def respond(self, status):
        self.send_share(wire.V1Frame.RESPONSE, connection_response=wire.ConnectionResponseFrame(status=status))

    def accept(self):
        if self.t.state != "frage":
            return
        folder = self.engine.folder()
        try:
            for kind, item in self.payloads.values():
                if kind == "file":
                    d = os.path.join(folder, item["sub"]) if item["sub"] else folder
                    os.makedirs(d, exist_ok=True)
                    item["path"] = unique_path(d, item["name"])
                    item["fh"] = open(item["path"], "wb")
        except OSError as e:
            self.respond(wire.ConnectionResponseFrame.NOT_ENOUGH_SPACE)
            self.set_state("fehler", f"kann nicht speichern: {e}")
            self.send_disconnect()
            return
        self.set_state("läuft")
        self.respond(wire.ConnectionResponseFrame.ACCEPT)

    def reject(self):
        if self.t.state != "frage":
            return
        self.set_state("abgelehnt")
        try:
            self.respond(wire.ConnectionResponseFrame.REJECT)
        finally:
            self.send_disconnect()

    def cancel(self):
        if self.t.state == "frage":
            return self.reject()
        if self.t.finished:
            return
        self.set_state("abgebrochen", "abgebrochen")
        try:
            self.send_share(wire.V1Frame.CANCEL)
        except Exception:
            pass
        self.send_disconnect()

    def _progress(self):
        now = time.time()
        if now - getattr(self, "_last_emit", 0) > 0.15:
            self._last_emit = now
            self.changed()

    def on_file_chunk(self, h, c):
        kind, item = self.payloads.get(h.id, (None, None))
        if kind != "file" or not item.get("fh") or self.t.state != "läuft":
            raise ShareError("unbekannte datei")
        if c.offset != item["done"]:
            raise ShareError("falscher versatz in datei")
        if c.body:
            if item["done"] + len(c.body) > item["size"]:
                raise ShareError("datei größer als angekündigt")
            item["fh"].write(c.body)
            item["done"] += len(c.body)
            self.t.done += len(c.body)
            self._progress()
        if c.flags & 1:
            item["fh"].close()
            item["fh"] = None
            self.payloads.pop(h.id, None)
            self.check_done()

    def on_bytes(self, pid, data):
        kind, item = self.payloads.get(pid, (None, None))
        if kind == "text":
            item["value"] = data.decode("utf-8", "replace")
            self.t.done += len(data)
        elif kind == "wifi":
            try:
                item["password"] = wire.WifiCredentials.FromString(data).password
            except Exception:
                item["password"] = ""
        elif kind == "file" and item.get("fh"):   # winzige Dateien kommen manchmal als Bytes
            item["fh"].write(data)
            item["fh"].close()
            item["fh"] = None
            item["done"] += len(data)
            self.t.done += len(data)
        else:
            return False
        self.payloads.pop(pid, None)
        self.check_done()
        return True

    def check_done(self):
        if not self.payloads and self.t.state == "läuft":
            self.set_state("fertig")
            self.send_disconnect()
        else:
            self.changed()

    def on_disconnect(self):
        if self.t.state == "läuft" and self.payloads:
            self.set_state("abgebrochen", "sender hat getrennt")

    def cleanup(self):
        for item in self.t.files:          # angefangene Dateien nicht liegen lassen
            if item.get("fh"):
                item["fh"].close()
                item["fh"] = None
            if self.t.state != "fertig" and item.get("path") and item["done"] < item["size"]:
                try:
                    os.remove(item["path"])
                except OSError:
                    pass
                item["path"] = None


# ---------------- Senden ----------------
class Outbound(Connection):
    SERVER = False

    def __init__(self, engine, device, paths=(), text=None, qr=None):
        super().__init__(engine, None)
        self.device = device
        self.paths = list(paths)
        self.text = text
        self.qr = qr
        self.cancelled = False
        self.t.incoming = False
        self.t.peer = device.name
        self.t.peer_type = device.type
        for p in self.paths:
            sz = os.path.getsize(p)
            self.t.files.append({"name": os.path.basename(p), "size": sz, "path": p, "done": 0})
            self.t.total += sz
        if text:
            self.t.texts.append({"kind": "link" if re.match(r"^https?://\S+$", text.strip()) else "text",
                                 "title": text[:60], "size": len(text.encode()), "value": text})
            self.t.total += len(text.encode())

    def main(self):
        self.set_state("verbinde")
        log(f"verbinde mit {self.device.name} {self.device.address}:{self.device.port} qr={'ja' if self.qr else 'nein'}")
        self.sock = socket.create_connection((self.device.address, self.device.port), timeout=15)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.cancelled:
            return
        eng = self.engine
        # ConnectionRequest
        cr = off.ConnectionRequestFrame(endpoint_id=eng.endpoint_id.decode(), endpoint_name=eng.name(),
                                        endpoint_info=endpoint_info(eng.name()),
                                        mediums=[off.ConnectionRequestFrame.WIFI_LAN])
        self.send_frame(self.offline(off.V1Frame.CONNECTION_REQUEST, connection_request=cr).SerializeToString())
        # UKEY2 ClientInit (mit Commitment auf das spätere ClientFinish)
        fin = ukey.Ukey2Message(message_type=ukey.Ukey2Message.CLIENT_FINISH,
                                message_data=ukey.Ukey2ClientFinished(public_key=self.make_key()).SerializeToString())
        fin_raw = fin.SerializeToString()
        ci = ukey.Ukey2ClientInit(version=1, random=os.urandom(32), next_protocol="AES_256_CBC-HMAC_SHA256")
        ci.cipher_commitments.add(handshake_cipher=ukey.P256_SHA512, commitment=hashlib.sha512(fin_raw).digest())
        self.client_init_raw = ukey.Ukey2Message(message_type=ukey.Ukey2Message.CLIENT_INIT,
                                                 message_data=ci.SerializeToString()).SerializeToString()
        self.send_frame(self.client_init_raw)
        # ServerInit
        self.sock.settimeout(30)
        raw = self.recv_frame()
        msg = ukey.Ukey2Message.FromString(raw)
        if msg.message_type != ukey.Ukey2Message.SERVER_INIT:
            raise ShareError("gerät hat abgelehnt (ukey2)")
        si = ukey.Ukey2ServerInit.FromString(msg.message_data)
        if si.version != 1 or len(si.random) != 32 or si.handshake_cipher != ukey.P256_SHA512:
            self.ukey_alert(ukey.Ukey2Alert.BAD_HANDSHAKE_CIPHER)
        self.server_init_raw = raw
        self.finish_key_exchange(si.public_key)
        self.send_frame(fin_raw)
        self.send_frame(self.connection_response().SerializeToString())
        f = off.OfflineFrame.FromString(self.recv_frame())
        if f.v1.type != off.V1Frame.CONNECTION_RESPONSE or \
                f.v1.connection_response.response != off.ConnectionResponseFrame.ACCEPT:
            raise ShareError("verbindung abgelehnt")
        self.encrypted = True
        self.stage = "pke"
        self.paired_key_encryption(self.qr.sign(self.auth_key) if self.qr else None)
        self.set_state("wartet")
        self.sock.settimeout(None)
        while not self.closed:
            self.handle_secure(self.recv_frame())

    def on_share(self, frame):
        v = frame.v1
        if v.type == wire.V1Frame.CANCEL:
            self.set_state("abgebrochen", "vom empfänger abgebrochen")
            self.send_disconnect()
            return
        if self.stage == "pke" and v.type == wire.V1Frame.PAIRED_KEY_ENCRYPTION:
            self.paired_key_result()
            self.stage = "pkr"
        elif self.stage == "pkr" and v.type == wire.V1Frame.PAIRED_KEY_RESULT:
            self.send_introduction()
            self.stage = "intro"
        elif self.stage == "intro" and v.type == wire.V1Frame.RESPONSE:
            st = v.connection_response.status
            S = wire.ConnectionResponseFrame
            if st == S.ACCEPT:
                self.stage = "send"
                self.set_state("läuft")
                threading.Thread(target=self.send_payloads, daemon=True).start()
            else:
                why = {S.REJECT: None, S.NOT_ENOUGH_SPACE: "kein platz auf dem gerät",
                       S.UNSUPPORTED_ATTACHMENT_TYPE: "dateityp nicht unterstützt", S.TIMED_OUT: "zeit abgelaufen"}
                self.set_state("abgelehnt", why.get(st))
                self.send_disconnect()

    def send_introduction(self):
        intro = wire.IntroductionFrame()
        self.pids = []
        for item in self.t.files:
            mime = mime_of(item["path"])
            pid = rand_id()
            intro.file_metadata.add(name=safe_name(item["name"]), type=file_type(mime, item["path"]),
                                    payload_id=pid, size=item["size"], mime_type=mime, id=rand_id())
            self.pids.append(("file", pid, item))
        for item in self.t.texts:
            pid = rand_id()
            intro.text_metadata.add(text_title=item["title"], size=item["size"], payload_id=pid, id=rand_id(),
                                    type=wire.TextMetadata.URL if item["kind"] == "link" else wire.TextMetadata.TEXT)
            self.pids.append(("text", pid, item))
        self.send_share(wire.V1Frame.INTRODUCTION, introduction=intro)

    def send_payloads(self):
        try:
            for kind, pid, item in self.pids:
                if self.cancelled or self.closed:
                    return
                if kind == "text":
                    data = item["value"].encode()
                    self.send_bytes(data, pid)
                    self.t.done += len(data)
                    continue
                with open(item["path"], "rb") as fh:
                    offset = 0
                    while not self.cancelled:
                        buf = fh.read(CHUNK)
                        pt = off.PayloadTransferFrame(packet_type=off.PayloadTransferFrame.DATA)
                        pt.payload_header.id = pid
                        pt.payload_header.type = off.PayloadTransferFrame.PayloadHeader.FILE
                        pt.payload_header.total_size = item["size"]
                        pt.payload_header.is_sensitive = False
                        pt.payload_chunk.offset = offset
                        if buf:
                            pt.payload_chunk.flags = 0
                            pt.payload_chunk.body = buf
                        else:
                            pt.payload_chunk.flags = 1
                        self.send_offline(self.offline(off.V1Frame.PAYLOAD_TRANSFER, payload_transfer=pt))
                        if not buf:
                            break
                        offset += len(buf)
                        item["done"] = offset
                        self.t.done += len(buf)
                        self._progress()
            if not self.cancelled:
                self.set_state("fertig")
                # Empfänger trennt selbst, wenn alles da ist; sonst nach kurzer Zeit selbst
                time.sleep(3)
                self.send_disconnect()
        except Exception as e:
            if not self.t.finished:
                self.set_state("fehler", str(e) or "senden fehlgeschlagen")
            self.close()

    _progress = Inbound._progress

    def on_disconnect(self):
        if self.t.state in ("wartet", "verbinde"):
            self.set_state("abgelehnt")
        elif self.t.state == "läuft":
            self.set_state("abgebrochen", "empfänger hat getrennt")

    def accept(self):
        pass

    reject = accept

    def cancel(self):
        if self.t.finished:
            return
        self.cancelled = True
        self.set_state("abgebrochen", "abgebrochen")
        if self.encrypted:
            try:
                self.send_share(wire.V1Frame.CANCEL)
            except Exception:
                pass
        if self.sock:
            self.send_disconnect()


# ---------------- Geräte in der Nähe ----------------
class Device:
    def __init__(self, eid, name, dtype, address, port, qr=None):
        self.id, self.name, self.type, self.address, self.port, self.qr = eid, name, dtype, address, port, qr
        self.seen = time.time()

    @property
    def icon(self):
        return DEVICE_ICONS.get(self.type, "󰾰")


class BleNudge:
    """Android wirbt erst per mDNS, wenn es diese BLE-Anzeige sieht (Dienst fe2c, Daten fc128e0142…).
    Über BlueZ-D-Bus (LEAdvertisingManager1). Ohne Bluetooth läuft der Rest trotzdem."""
    PATH = "/de/invincible/sharist/adv0"
    XML = """<node><interface name="org.bluez.LEAdvertisement1">
      <method name="Release"/>
      <property name="Type" type="s" access="read"/>
      <property name="ServiceUUIDs" type="as" access="read"/>
      <property name="ServiceData" type="a{sv}" access="read"/>
    </interface></node>"""

    def __init__(self):
        self.reg_id = None
        self.adapter = None
        self.bus = None
        self.error = None

    def start(self):
        from gi.repository import Gio, GLib
        if self.reg_id:
            return True
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
            objs = self.bus.call_sync("org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                                      None, None, Gio.DBusCallFlags.NONE, 3000).unpack()[0]
            self.adapter = next((p for p, ifs in objs.items() if "org.bluez.LEAdvertisingManager1" in ifs
                                 and ifs.get("org.bluez.Adapter1", {}).get("Powered")), None)
            if not self.adapter:
                self.error = "bluetooth aus"
                return False
            data = bytes([0xFC, 0x12, 0x8E, 0x01, 0x42] + [0] * 9) + os.urandom(10)
            props = {"Type": GLib.Variant("s", "broadcast"),
                     "ServiceUUIDs": GLib.Variant("as", ["0000fe2c-0000-1000-8000-00805f9b34fb"]),
                     "ServiceData": GLib.Variant("a{sv}", {"0000fe2c-0000-1000-8000-00805f9b34fb":
                                                           GLib.Variant("ay", data)})}

            def method(conn, sender, path, iface, name, params, inv):
                inv.return_value(None)

            def getprop(conn, sender, path, iface, name):
                return props[name]

            info = Gio.DBusNodeInfo.new_for_xml(self.XML).interfaces[0]
            self.reg_id = self.bus.register_object(self.PATH, info, method, getprop, None)
            self.bus.call("org.bluez", self.adapter, "org.bluez.LEAdvertisingManager1", "RegisterAdvertisement",
                          GLib.Variant("(oa{sv})", (self.PATH, {})), None, Gio.DBusCallFlags.NONE, 5000, None,
                          self._registered)
            return True
        except Exception as e:
            self.error = str(e)
            self.stop()
            return False

    def _registered(self, bus, res):
        try:
            bus.call_finish(res)
            self.error = None
        except Exception as e:
            self.error = e.message if hasattr(e, "message") else str(e)
            print("sharist ble:", self.error, file=sys.stderr, flush=True)

    def stop(self):
        from gi.repository import Gio, GLib
        if self.reg_id and self.bus:
            try:
                self.bus.call_sync("org.bluez", self.adapter, "org.bluez.LEAdvertisingManager1",
                                   "UnregisterAdvertisement", GLib.Variant("(o)", (self.PATH,)), None,
                                   Gio.DBusCallFlags.NONE, 2000)
            except Exception:
                pass
            self.bus.unregister_object(self.reg_id)
        self.reg_id = None


# ---------------- Motor ----------------
class Engine:
    """Hält Server, mDNS und alle Verbindungen. emit(name, obj) ruft listener(name, obj) – aus beliebigen Threads."""

    def __init__(self, listener, name, folder, port=0):
        self.listener = listener
        self.name = name if callable(name) else (lambda: name)
        self.folder = folder if callable(folder) else (lambda: folder)
        self.port = port
        self.endpoint_id = new_endpoint_id()
        self.zc = None
        self.info = None
        self.server = None
        self.browser = None
        self.devices = {}
        self.dev_lock = threading.Lock()
        self.qr = None
        self.ble = BleNudge()
        self.transfers = []
        self.identity = os.urandom(16)     # gleich in mDNS und BLE, sonst zeigt das Handy uns doppelt
        self.blerx = None
        self.bwu_wait = {}                 # endpunkt-id des Handys → Queue für den WLAN-Socket

    def endpoint_info(self):
        return endpoint_info(self.name(), identity=self.identity)

    def emit(self, what, obj=None):
        try:
            self.listener(what, obj)
        except Exception as e:
            print("sharist listener:", repr(e), file=sys.stderr, flush=True)

    def _zc(self):
        if self.zc is None:
            from zeroconf import IPVersion, Zeroconf
            self.zc = Zeroconf(ip_version=IPVersion.V4Only)
        return self.zc

    # --- sichtbar werden (empfangen) ---
    def start_server(self):
        if self.server:
            return self.server.getsockname()[1]
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", self.port))
        except OSError:
            s.bind(("0.0.0.0", 0))
        s.listen(8)
        self.server = s
        threading.Thread(target=self._accept_loop, args=(s,), daemon=True).start()
        return s.getsockname()[1]

    def _accept_loop(self, s):
        while self.server is s:
            try:
                sock, addr = s.accept()
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._route, args=(sock, addr), daemon=True).start()

    def _route(self, sock, addr):
        """Ersten Rahmen lesen: WLAN-Teil eines Bluetooth-Upgrades → an die wartende Sitzung, sonst neu."""
        try:
            sock.settimeout(20)
            hdr = b""
            while len(hdr) < 4:
                c = sock.recv(4 - len(hdr))
                if not c:
                    raise ConnectionError("leer")
                hdr += c
            (n,) = struct.unpack(">I", hdr)
            if n > MAX_FRAME:
                raise ShareError("rahmen zu groß")
            raw = b""
            while len(raw) < n:
                c = sock.recv(n - len(raw))
                if not c:
                    raise ConnectionError("leer")
                raw += c
        except Exception as e:
            log(f"verbindung von {addr[0]} ohne anfang: {e!r}")
            sock.close()
            return
        try:
            f = off.OfflineFrame.FromString(raw)
            B = off.BandwidthUpgradeNegotiationFrame
            if f.v1.type == off.V1Frame.BANDWIDTH_UPGRADE_NEGOTIATION and \
                    f.v1.bandwidth_upgrade_negotiation.event_type == B.CLIENT_INTRODUCTION:
                eid = f.v1.bandwidth_upgrade_negotiation.client_introduction.endpoint_id
                q = self.bwu_wait.get(eid)
                log(f"wlan-upgrade von {eid} ({addr[0]}){'' if q else ' – keine wartende sitzung'}")
                if q:
                    q.put((sock, raw))
                else:
                    sock.close()
                return
        except Exception:
            pass
        sock.settimeout(None)
        c = Inbound(self, sock)
        c.first = raw
        c.t.address = addr[0]
        self.transfers.append(c.t)
        c.start()

    def inbound_ble(self, wsock):
        """Vom BLE-Empfänger: neue eingehende Verbindung über den Weave-Socket."""
        wsock.is_ble = True
        c = Inbound(self, wsock)
        c.t.address = "bluetooth"
        self.transfers.append(c.t)
        c.start()
        return c

    def local_ips(self):
        ips = set()
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.connect(("10.255.255.255", 1))
            ips.add(u.getsockname()[0])
            u.close()
        except OSError:
            pass
        return [socket.inet_aton(ip) for ip in ips if not ip.startswith("127.")]

    def set_visible(self, on):
        from zeroconf import ServiceInfo
        zc = self._zc()
        if self.info:
            try:
                zc.unregister_service(self.info)
            except Exception:
                pass
            self.info = None
        if self.blerx:
            self.blerx.stop()
        if not on:
            return None
        port = self.start_server()
        try:
            import blerx
            self.blerx = self.blerx or blerx.BleReceiver(self)
            self.blerx.start()
        except Exception as e:
            log(f"bluetooth-empfang nicht gestartet: {e!r}")
        self.info = ServiceInfo(SERVICE, f"{service_name(self.endpoint_id)}.{SERVICE}", port=port,
                                properties={"n": b64u(self.endpoint_info())},
                                addresses=self.local_ips() or None, server=f"sharist-{self.endpoint_id.decode()}.local.")
        zc.register_service(self.info, allow_name_change=True)
        return port

    # --- Geräte suchen (senden) ---
    def start_discovery(self, qr=False):
        from zeroconf import ServiceBrowser
        self.qr = QrKey() if qr else self.qr
        self.ble.start()
        if self.browser:
            return
        self.browser = ServiceBrowser(self._zc(), SERVICE, handlers=[self._on_service])
        from gi.repository import GLib   # BLE-Objekt lebt in der Hauptschleife → dort auch erneuern
        self._ble_timer = GLib.timeout_add_seconds(8, self._ble_refresh)

    def stop_discovery(self):
        from gi.repository import GLib
        if getattr(self, "_ble_timer", None):
            GLib.source_remove(self._ble_timer)
            self._ble_timer = None
        self.ble.stop()
        if self.browser:
            self.browser.cancel()
            self.browser = None
        with self.dev_lock:
            self.devices.clear()
        self.qr = None
        self.emit("devices")

    def _ble_refresh(self):
        """BLE-Anzeige regelmäßig mit neuen Zufallsbytes, wie Android selbst."""
        if not self.browser:
            self._ble_timer = None
            return False
        self.ble.stop()
        self.ble.start()
        return True

    def _on_service(self, zeroconf, service_type, name, state_change):
        from zeroconf import ServiceStateChange
        eid = endpoint_from_service(name)
        if not eid or eid == self.endpoint_id.decode():
            return
        if state_change == ServiceStateChange.Removed:
            with self.dev_lock:
                gone = self.devices.pop(eid, None)
            if gone:
                self.emit("devices")
            return
        threading.Thread(target=self._resolve, args=(zeroconf, service_type, name, eid), daemon=True).start()

    def _resolve(self, zc, stype, name, eid):
        info = zc.get_service_info(stype, name, timeout=3000)
        if not info or not info.addresses:
            return
        raw = (info.properties or {}).get(b"n")
        if not raw:
            return
        try:
            dname, dtype, qr = parse_endpoint_info(unb64u(raw))
        except Exception:
            return
        hit = False
        if self.qr:
            hit, qname = self.qr.matches(qr)
            dname = dname or qname
        log(f"gefunden {eid} {info.parsed_addresses()} name={dname!r} typ={dtype} qr={'ja' if qr else 'nein'}"
            f"{' passt' if hit else (' passt-nicht' if qr and self.qr else '')}")
        if not dname:
            return
        addr = socket.inet_ntoa(info.addresses[0])
        dev = Device(eid, dname, dtype, addr, info.port, qr=hit)
        with self.dev_lock:
            self.devices[eid] = dev
        self.emit("devices")
        if hit:
            self.emit("qr-match", dev)

    def device_list(self):
        with self.dev_lock:
            return sorted(self.devices.values(), key=lambda d: d.name.lower())

    def send(self, device, paths=(), text=None):
        c = Outbound(self, device, paths, text, qr=self.qr if device.qr else None)
        self.transfers.append(c.t)
        self.emit("changed", c.t)
        c.start()
        return c.t

    def shutdown(self):
        self.ble.stop()
        if self.blerx:
            self.blerx.stop()
        try:
            if self.browser:
                self.browser.cancel()
            if self.info:
                self.zc.unregister_service(self.info)
            if self.zc:
                self.zc.close()
        except Exception:
            pass
        if self.server:
            s, self.server = self.server, None
            s.close()
