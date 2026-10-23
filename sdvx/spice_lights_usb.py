#!/usr/bin/env python3
import argparse
import json
import os
import socket
import sys
import time

try:
    import usb.core
    import usb.util
    import usb.backend.libusb1
except ImportError:
    sys.exit("pyusb not found. Try:\n"
             "  nix-shell -p 'python3.withPackages(ps: [ps.pyusb])' libusb1")

VENDOR_ID = 0x1CCF
PRODUCT_ID = 0x1014
INTERFACE = 1
ENDPOINT = None
TRANSPORT = "auto"
REPORT_ID = None
REPORT_LENGTH = 64
RESEND_INTERVAL = 0.25

HOST = "127.0.0.1"
PORT = 1337
PASSWORD = None
POLL_HZ = 120
RC4_PERSISTENT = False

LIGHTS = [
    "Upper Left Speaker Avg R",
    "Upper Left Speaker Avg G",
    "Upper Left Speaker Avg B",
    "Lower Left Speaker Avg R",
    "Lower Left Speaker Avg G",
    "Lower Left Speaker Avg B",
    "Lower Right Speaker Avg R",
    "Lower Right Speaker Avg G",
    "Lower Right Speaker Avg B",
    "Upper Right Speaker Avg R",
    "Upper Right Speaker Avg G",
    "Upper Right Speaker Avg B",
    "Left Wing Avg R",
    "Left Wing Avg G",
    "Left Wing Avg B",
    None, None, None,
    "Right Wing Avg R",
    "Right Wing Avg G",
    "Right Wing Avg B",
    None, None, None,
    "BT-A", "BT-B", "BT-C", "BT-D", "FX-L", "FX-R", "Start",
]

TAPE_SAMPLES = [
    (0, "Left Wing", 0.15),
    (3, "Left Wing", 0.70),
    (6, "Right Wing", 0.70),
    (9, "Right Wing", 0.15),
]
TAPE_WINDOW = 0.12
TAPE_INPUT_MAX = 95.0

HID_REQUEST_OUT = 0x21
HID_SET_REPORT = 0x09
HID_OUTPUT_REPORT = 0x02
ENDPOINT_TYPES = ("control", "isochronous", "bulk", "interrupt")


def get_backend():
    path = os.environ.get("PYUSB_LIBUSB_PATH")
    backend = None
    if path:
        backend = usb.backend.libusb1.get_backend(find_library=lambda _: path)
    if backend is None:
        backend = usb.backend.libusb1.get_backend()
    if backend is None:
        sys.exit("Could not load libusb. Add libusb1 to your shell, or set\n"
                 "  PYUSB_LIBUSB_PATH=/nix/store/.../lib/libusb-1.0.so")
    return backend


def rc4(key, data, state=None):
    if state is None or "S" not in state:
        S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + S[i] + key[i % len(key)]) & 0xFF
            S[i], S[j] = S[j], S[i]
        local = {"S": S, "i": 0, "j": 0}
    else:
        local = state

    S, i, j = local["S"], local["i"], local["j"]
    out = bytearray()
    for b in data:
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(b ^ S[(S[i] + S[j]) & 0xFF])
    local["i"], local["j"] = i, j
    if state is not None:
        state.update(local)
    return bytes(out)


def to_byte(value, full_scale=1.0):
    return max(0, min(255, round(float(value) * 255 / full_scale)))


def to_triples(value):
    if not isinstance(value, (list, tuple)) or not value:
        return []

    def chan(c):
        return to_byte(c, TAPE_INPUT_MAX)

    first = value[0]
    if isinstance(first, (list, tuple)):
        return [tuple(map(chan, led[:3])) for led in value if len(led) >= 3]
    if isinstance(first, dict):
        return [tuple(chan(led.get(k, led.get(k.upper(), 0))) for k in "rgb")
                for led in value]
    if isinstance(first, (int, float)):
        return [tuple(map(chan, value[i:i + 3]))
                for i in range(0, len(value) - 2, 3)]
    return []


def unwrap_tape(payload):
    if isinstance(payload, list):
        payload = next((i for i in payload if isinstance(i, dict)), None)
    return payload if isinstance(payload, dict) else {}


def normalise_tape(payload):
    return {name: to_triples(v) for name, v in unwrap_tape(payload).items()}


def sample_tape(triples, pos, window=TAPE_WINDOW):
    if not triples:
        return 0, 0, 0
    n = len(triples)
    if n == 1:
        return triples[0]

    centre = pos * (n - 1)
    half = max(0.5, window * n / 2)
    lo = max(0, round(centre - half))
    hi = min(n - 1, round(centre + half))
    chunk = triples[lo:hi + 1] or [triples[min(n - 1, max(0, round(centre)))]]
    return tuple(sum(led[i] for led in chunk) // len(chunk) for i in range(3))


class SpiceAPI:
    def __init__(self, host, port, password=None):
        self.host = host
        self.port = port
        self.key = password.encode("utf-8") if password else None
        self.sock = None
        self.tx_state = None
        self.rx_state = None
        self.next_id = 0

    def connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.key and RC4_PERSISTENT:
            self.tx_state = {}
            self.rx_state = {}

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.tx_state = None
        self.rx_state = None

    def call(self, module, function, params=()):
        self.next_id = (self.next_id + 1) & 0xFFFFFFFF
        request = {"id": self.next_id, "module": module,
                   "function": function, "params": list(params)}
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
        if self.key:
            payload = rc4(self.key, payload, self.tx_state)
        self.sock.sendall(payload + b"\x00")

        buf = bytearray()
        while not buf.endswith(b"\x00"):
            chunk = self.sock.recv(8192)
            if not chunk:
                raise ConnectionError("spice closed the connection")
            buf += chunk

        body = bytes(buf[:-1])
        if self.key:
            body = rc4(self.key, body, self.rx_state)
        reply = json.loads(body.decode("utf-8"))
        if reply.get("errors"):
            raise RuntimeError("; ".join(str(e) for e in reply["errors"]))
        return reply.get("data", [])

    def read_lights(self):
        result = {}
        for entry in self.call("lights", "read"):
            if isinstance(entry, dict):
                result[entry.get("name")] = float(entry.get("state", 0.0))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                result[entry[0]] = float(entry[1])
        return result

    def read_tapeleds(self, names):
        return normalise_tape(self.call("sdvx", "tapeled_get", list(names)))


def is_out(ep):
    direction = usb.util.endpoint_direction(ep.bEndpointAddress)
    return direction == usb.util.ENDPOINT_OUT


class UsbLights:
    def __init__(self, vid, pid, interface, endpoint=None, transport="auto",
                 report_id=None):
        self.vid = vid
        self.pid = pid
        self.interface = interface
        self.endpoint_addr = endpoint
        self.transport = transport
        self.report_id = report_id
        self.dev = None
        self.ep = None
        self.detached = False
        self.last = None
        self.last_sent = 0.0

    def open(self):
        self.dev = usb.core.find(idVendor=self.vid, idProduct=self.pid,
                                 backend=get_backend())
        if self.dev is None:
            raise RuntimeError(f"{self.vid:04x}:{self.pid:04x} not found. "
                               "Plugged in, and is the udev rule applied?")

        try:
            if self.dev.is_kernel_driver_active(self.interface):
                self.dev.detach_kernel_driver(self.interface)
                self.detached = True
        except (NotImplementedError, usb.core.USBError):
            pass

        try:
            usb.util.claim_interface(self.dev, self.interface)
        except usb.core.USBError as exc:
            raise RuntimeError(
                f"Could not claim interface {self.interface}: {exc}\n"
                "Usually a permissions problem on the USB node.") from exc

        cfg = self.dev.get_active_configuration()
        intf = usb.util.find_descriptor(cfg, bInterfaceNumber=self.interface)
        if intf is None:
            raise RuntimeError(
                f"Interface {self.interface} not in active config")

        out_eps = [e for e in intf if is_out(e)]
        auto_control = self.transport == "auto" and not out_eps
        if self.transport == "control" or auto_control:
            self.ep = None
        elif self.endpoint_addr is not None:
            self.ep = usb.util.find_descriptor(
                intf, bEndpointAddress=self.endpoint_addr)
            if self.ep is None:
                raise RuntimeError(f"No endpoint 0x{self.endpoint_addr:02x} "
                                   f"on interface {self.interface}")
        elif out_eps:
            self.ep = out_eps[0]
        else:
            raise RuntimeError(f"Interface {self.interface} has no OUT "
                               "endpoint; use --transport control")

        how = (f"endpoint 0x{self.ep.bEndpointAddress:02x}" if self.ep
               else "control transfer (SET_REPORT)")
        print(f"Claimed interface {self.interface}, writing via {how}")

    def close(self):
        if self.dev is None:
            return
        try:
            usb.util.release_interface(self.dev, self.interface)
        except usb.core.USBError:
            pass
        if self.detached:
            try:
                self.dev.attach_kernel_driver(self.interface)
            except (NotImplementedError, usb.core.USBError):
                pass
        usb.util.dispose_resources(self.dev)
        self.dev = None

    def send(self, body):
        data = bytes(body)
        if self.report_id is not None:
            data = bytes([self.report_id]) + data
        if self.ep is not None:
            self.ep.write(data, timeout=200)
        else:
            value = (HID_OUTPUT_REPORT << 8) | (self.report_id or 0)
            self.dev.ctrl_transfer(HID_REQUEST_OUT, HID_SET_REPORT, value,
                                   self.interface, data, timeout=200)

    def write_states(self, states, tape=None):
        size = REPORT_LENGTH or len(LIGHTS)
        body = bytearray(size)
        for i, name in enumerate(LIGHTS[:size]):
            if name:
                body[i] = to_byte(states.get(name, 0.0))

        if tape:
            for i, tape_name, pos in TAPE_SAMPLES:
                if i + 2 >= size:
                    continue
                r, g, b = sample_tape(tape.get(tape_name, []), pos)
                body[i], body[i + 1], body[i + 2] = r, g, b

        body = bytes(body)
        now = time.monotonic()
        if body != self.last or now - self.last_sent >= RESEND_INTERVAL:
            self.send(body)
            self.last = body
            self.last_sent = now


def cmd_inspect(vid, pid):
    dev = usb.core.find(idVendor=vid, idProduct=pid, backend=get_backend())
    if dev is None:
        sys.exit(f"{vid:04x}:{pid:04x} not found")

    print(f"{vid:04x}:{pid:04x} on bus {dev.bus} address {dev.address}")
    for cfg in dev:
        print(f"\nConfiguration {cfg.bConfigurationValue}")
        for intf in cfg:
            try:
                claimed = dev.is_kernel_driver_active(intf.bInterfaceNumber)
            except (NotImplementedError, usb.core.USBError):
                claimed = "unknown"
            print(f"  Interface {intf.bInterfaceNumber} "
                  f"alt {intf.bAlternateSetting}  "
                  f"class {intf.bInterfaceClass:#04x}  "
                  f"kernel driver: {claimed}")
            endpoints = list(intf)
            if not endpoints:
                print("    (no endpoints)")
            for ep in endpoints:
                direction = "OUT" if is_out(ep) else "IN"
                kind = ENDPOINT_TYPES[usb.util.endpoint_type(ep.bmAttributes)]
                print(f"    Endpoint 0x{ep.bEndpointAddress:02x}  "
                      f"{direction:<3} {kind:<10} max {ep.wMaxPacketSize}")


def cmd_probe(lights, length, delay):
    lights.open()
    print(f"Cycling {length} body bytes. Watch for an LED. Ctrl-C to stop.")
    try:
        while True:
            for i in range(length):
                body = bytearray(length)
                body[i] = 0xFF
                lights.send(body)
                print(f"  index {i}", flush=True)
                time.sleep(delay)
    except KeyboardInterrupt:
        try:
            lights.send(bytes(length))
        except usb.core.USBError:
            pass
        print("\nCleared.")
    finally:
        lights.close()


def cmd_list(api):
    api.connect()
    found = api.read_lights()
    if not found:
        print("No lights reported. Is a game running, or spice in -apidebug?")
        return
    width = max(len(n or "") for n in found)
    for name, state in sorted(found.items()):
        print(f"{name:<{width}}  {state:.3f}")


def cmd_dump_tape(api, names):
    api.connect()
    raw = api.call("sdvx", "tapeled_get", list(names))
    print("--- raw ---")
    print(json.dumps(raw)[:3000])

    flat = []
    for value in unwrap_tape(raw).values():
        for item in value or []:
            if isinstance(item, (int, float)):
                flat.append(item)
            elif isinstance(item, (list, tuple)):
                flat.extend(c for c in item if isinstance(c, (int, float)))
            elif isinstance(item, dict):
                flat.extend(c for c in item.values()
                            if isinstance(c, (int, float)))
    if flat:
        print(f"\nchannel range in this sample: {min(flat)} to {max(flat)}"
              f"   (TAPE_INPUT_MAX = {TAPE_INPUT_MAX})")

    print("\n--- parsed ---")
    parsed = normalise_tape(raw)
    if not parsed:
        print("Nothing parsed. Check the raw dump above.")
        return
    for name, triples in sorted(parsed.items()):
        head = ", ".join(str(t) for t in triples[:3])
        print(f"{name}: {len(triples)} LEDs   {head}")


def cmd_run(api, lights, use_tape=True):
    lights.open()
    interval = 1.0 / POLL_HZ
    tape_names = sorted({name for _, name, _ in TAPE_SAMPLES})
    use_tape = use_tape and bool(tape_names)
    if use_tape:
        print(f"Sampling tape LEDs: {', '.join(tape_names)}")

    try:
        while True:
            try:
                if api.sock is None:
                    print("Connecting to spice...", flush=True)
                    api.connect()
                    print("Connected.", flush=True)

                next_tick = time.monotonic()
                while True:
                    tape = None
                    if use_tape:
                        try:
                            tape = api.read_tapeleds(tape_names)
                        except RuntimeError as exc:
                            print(f"Tape LEDs unavailable ({exc}); using the "
                                  "averaged zones instead", flush=True)
                            use_tape = False
                    lights.write_states(api.read_lights(), tape)
                    next_tick += interval
                    delay = next_tick - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        next_tick = time.monotonic()
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"Lost connection ({exc}); retrying in 2s", flush=True)
                api.close()
                lights.last = None
                time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            lights.write_states({})
        except usb.core.USBError:
            pass
        lights.close()
        api.close()
        print("\nStopped.")


def main():
    ap = argparse.ArgumentParser(
        description="Drive USB HID lights from spice2x's SpiceAPI via libusb.")
    ap.add_argument("--vid", type=lambda s: int(s, 16), default=VENDOR_ID)
    ap.add_argument("--pid", type=lambda s: int(s, 16), default=PRODUCT_ID)
    ap.add_argument("--interface", type=int, default=INTERFACE)
    ap.add_argument("--endpoint", type=lambda s: int(s, 0), default=ENDPOINT)
    ap.add_argument("--transport", choices=("auto", "interrupt", "control"),
                    default=TRANSPORT)
    ap.add_argument("--report-id", type=int, default=REPORT_ID,
                    help="prepend a report ID byte (omitted by default)")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--password", default=PASSWORD)
    ap.add_argument("--inspect", action="store_true",
                    help="print interfaces and endpoints, then exit")
    ap.add_argument("--probe", action="store_true",
                    help="cycle each byte high to map LED order")
    ap.add_argument("--list", action="store_true",
                    help="print light names from the running game")
    ap.add_argument("--dump-tape", action="store_true",
                    help="print raw and parsed tape LED data, then exit")
    ap.add_argument("--no-tape", action="store_true",
                    help="ignore TAPE_SAMPLES and use the averaged zones")
    ap.add_argument("--length", type=int, default=8)
    ap.add_argument("--delay", type=float, default=0.6)
    args = ap.parse_args()

    if args.inspect:
        cmd_inspect(args.vid, args.pid)
        return

    api = SpiceAPI(args.host, args.port, args.password)
    if args.list:
        cmd_list(api)
    elif args.dump_tape:
        names = sorted({name for _, name, _ in TAPE_SAMPLES}) or ["Left Wing"]
        cmd_dump_tape(api, names)
    else:
        lights = UsbLights(args.vid, args.pid, args.interface, args.endpoint,
                           args.transport, args.report_id)
        if args.probe:
            cmd_probe(lights, args.length, args.delay)
        else:
            cmd_run(api, lights, use_tape=not args.no_tape)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
