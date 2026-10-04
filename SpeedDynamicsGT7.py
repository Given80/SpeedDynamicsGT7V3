import json, os, socket, struct, sys, threading, time
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import tkinter as tk
from tkinter import ttk, messagebox
from Crypto.Cipher import Salsa20

APP = "Speed Dynamics GT7"
SEND_PORT = 33739
RECV_PORT = 33740
WEB_PORT = 8080
KEY = b"Simulator Interface Packet GT7 ver. 0.0"[:32]
MAGIC = 0x47375330

state = {
    "connected": False, "mode": "", "packets": 0, "valid": 0,
    "bytes": 0, "packet_size": 0, "speed": 0.0, "rpm": 0,
    "gear": 0, "throttle": 0, "brake": 0, "fuel": 0.0,
    "fuel_capacity": 0.0, "lap": 0, "total_laps": 0,
    "best_lap_ms": -1, "last_lap_ms": -1, "current_lap_ms": -1,
    "position": 0, "cars": 0, "delta_ms": None,
    "reference_ms": None, "reference_ready": False,
    "fuel_laps": None, "fuel_lap_consumption": None,
    "rpm_limit": None, "rpm_learning": True
}
lock = threading.Lock()

def base_path():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def decrypt_packet(packet, mode):
    if len(packet) not in (296, 368):
        return None
    iv1 = int.from_bytes(packet[0x40:0x44], "little")
    constants = [0xDEADBEEF, 0x55FABB4F, 0xDEADBEAF] if mode == "C" else [0xDEADBEAF]
    for c in constants:
        iv2 = iv1 ^ c
        nonce = iv2.to_bytes(4, "little") + iv1.to_bytes(4, "little")
        try:
            plain = Salsa20.new(key=KEY, nonce=nonce).decrypt(packet)
            if int.from_bytes(plain[:4], "little") == MAGIC:
                return plain
        except Exception:
            pass
    return None

def f32(d, o):
    return struct.unpack_from("<f", d, o)[0]

def i16(d, o):
    return struct.unpack_from("<h", d, o)[0]

def i32(d, o):
    return struct.unpack_from("<i", d, o)[0]

def parse_common(d):
    return {
        "speed": max(0.0, f32(d, 0x4C) * 3.6),
        "rpm": max(0, round(f32(d, 0x3C))),
        "fuel": max(0.0, f32(d, 0x44)),
        "fuel_capacity": max(0.0, f32(d, 0x48)),
        "lap": max(0, i16(d, 0x74)),
        "total_laps": max(0, i16(d, 0x76)),
        "best_lap_ms": i32(d, 0x78),
        "last_lap_ms": i32(d, 0x7C),
        "position": max(0, i16(d, 0x84)),
        "cars": max(0, i16(d, 0x86)),
        "gear": d[0x90] & 0x0F,
        "throttle": round(d[0x91] / 255 * 100),
        "brake": round(d[0x92] / 255 * 100),
    }

def parse_packet(plain):
    p = parse_common(plain[:296])
    if len(plain) >= 368:
        p["current_lap_ms"] = i32(plain, 0x140)
        p["packet"] = "C"
    else:
        p["current_lap_ms"] = -1
        p["packet"] = "A"
    return p


def median(values):
    vals = sorted(values)
    if not vals:
        return None
    n = len(vals)
    m = n // 2
    return vals[m] if n % 2 else (vals[m-1] + vals[m]) / 2.0


# --- Continuous live delta reference trace ----------------------------------
LIVE_REFERENCE_FILE = Path.home() / "SpeedDynamicsGT7_live_reference.json"

def load_live_reference_trace():
    try:
        if LIVE_REFERENCE_FILE.exists():
            d = json.loads(LIVE_REFERENCE_FILE.read_text(encoding="utf-8"))
            pts = [(float(x), int(y)) for x, y in d.get("points", [])]
            if len(pts) >= 20:
                pts.sort(key=lambda p: p[0])
                return {"points": pts, "lap_ms": int(d.get("lap_ms", pts[-1][1]))}
    except Exception:
        pass
    return None

def save_live_reference_trace(ref):
    try:
        tmp = LIVE_REFERENCE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(ref, separators=(",", ":")), encoding="utf-8")
        tmp.replace(LIVE_REFERENCE_FILE)
    except Exception:
        pass

def trace_time_at_distance(points, distance_m):
    if not points:
        return None
    xs = [p[0] for p in points]
    i = bisect.bisect_left(xs, distance_m)
    if i <= 0:
        return points[0][1]
    if i >= len(points):
        return points[-1][1]
    x0, y0 = points[i-1]
    x1, y1 = points[i]
    if x1 <= x0:
        return y1
    f = (distance_m-x0)/(x1-x0)
    return int(y0 + (y1-y0)*f)

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api"):
            with lock:
                payload = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            super().do_GET()
    def log_message(self, *args):
        pass

def start_web():
    os.chdir(base_path() / "web")
    ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler).serve_forever()

class Bridge:
    def __init__(self, ip, status):
        self.ip = ip
        self.status = status
        self.running = False
        self.mode = "C"
        self.reference = None
        self.reference_source = None
        self.lap_start = None
        self.last_lap = None
        self.fuel_lap_start = None
        self.fuel_samples = []
        self.rpm_max_seen = 0
        self.rpm_limit = None
        self.rpm_near_limit_count = 0
        self.distance_m = 0.0
        self.trace = []
        self.reference_trace = load_live_reference_trace()
        self.prev_sample_time = None
        self.reference_lap_ms = self.reference_trace["lap_ms"] if self.reference_trace else None

    def start(self):
        self.running = True
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self.running = False

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", RECV_PORT))
            sock.settimeout(0.5)
        except Exception as e:
            self.status("UDP 33740 FEHLER: " + str(e))
            return

        self.status("UDP 33740 bereit · Packet C wird angefordert")
        started = time.monotonic()
        last_request = 0

        while self.running:
            now = time.monotonic()
            if now - last_request >= 1:
                try:
                    sock.sendto(self.mode.encode(), (self.ip, SEND_PORT))
                except Exception as e:
                    self.status("UDP-Sende-Fehler: " + str(e))
                last_request = now

            try:
                raw, _ = sock.recvfrom(4096)
                with lock:
                    state["packets"] += 1
                    state["bytes"] += len(raw)
                    state["packet_size"] = len(raw)

                plain = decrypt_packet(raw, self.mode)

                # If Packet C is unavailable after a short test period,
                # automatically use the proven V0.5 Packet-A path.
                if plain is None and self.mode == "C" and now - started > 4:
                    self.mode = "A"
                    self.status("Packet C nicht erkannt · V0.5 Packet A wird verwendet")
                    continue
                if plain is None:
                    continue

                p = parse_packet(plain)

                # Continuous track-distance integration from live speed.
                # GT7 packets arrive frequently; using monotonic time keeps
                # the trace independent of packet jitter.
                sample_now = time.monotonic()
                if self.prev_sample_time is None:
                    dt = 0.0
                else:
                    dt = max(0.0, min(0.25, sample_now - self.prev_sample_time))
                self.prev_sample_time = sample_now
                self.distance_m += (float(p.get("speed", 0.0) or 0.0) / 3.6) * dt
                if p.get("current_lap_ms", -1) >= 0:
                    self.trace.append((self.distance_m, int(p["current_lap_ms"])))
                    if len(self.trace) > 12000:
                        self.trace = self.trace[-12000:]

                # --- Vehicle RPM end / redline learning --------------------
                # GT7's current parsed telemetry does not expose a dedicated
                # redline field. Learn the actual limit from the live RPM
                # plateau of the current car. Once a stable limiter plateau is
                # seen, use that observed value as the bar's 100% point.
                current_rpm = int(p.get("rpm", 0) or 0)
                if current_rpm > self.rpm_max_seen:
                    self.rpm_max_seen = current_rpm
                    # Keep a small headroom while learning; once the limiter
                    # is reached repeatedly, the observed maximum becomes
                    # the vehicle-specific end RPM.
                if current_rpm >= max(1000, self.rpm_max_seen - 60) and p.get("throttle", 0) >= 85:
                    self.rpm_near_limit_count += 1
                else:
                    self.rpm_near_limit_count = max(0, self.rpm_near_limit_count - 1)
                if self.rpm_near_limit_count >= 8 and self.rpm_max_seen >= 3000:
                    self.rpm_limit = self.rpm_max_seen
                elif self.rpm_limit is None and self.rpm_max_seen >= 3000:
                    # While learning, expose the highest observed RPM so the
                    # bar follows the current car instead of a fixed 9000 RPM.
                    self.rpm_limit = self.rpm_max_seen

                # --- Track-dependent fuel-per-lap estimate ------------------
                # Consumption is measured from actual fuel at lap boundaries,
                # so the estimate is specific to the current track, car and
                # driving style. Refuelling/teleports are ignored.
                if self.fuel_lap_start is None and p.get("fuel", 0) > 0:
                    self.fuel_lap_start = float(p["fuel"])

                lap_changed = self.last_lap is not None and p.get("lap") != self.last_lap
                if lap_changed:
                    end_fuel = float(p.get("fuel", 0) or 0)
                    if self.fuel_lap_start is not None:
                        used = self.fuel_lap_start - end_fuel
                        if 0.05 <= used <= 30.0:
                            self.fuel_samples.append(used)
                            self.fuel_samples = self.fuel_samples[-5:]
                    self.fuel_lap_start = end_fuel

                    # The just-completed lap becomes the positional reference
                    # only if it is a complete, usable trace. This is what
                    # makes the delta change continuously in every corner.
                    completed_ms = int(p.get("last_lap_ms", 0) or 0)
                    if completed_ms <= 0:
                        completed_ms = int(p.get("current_lap_ms", 0) or 0)
                    if len(self.trace) >= 20 and completed_ms > 0:
                        if (self.reference_trace is None or
                            completed_ms < self.reference_trace.get("lap_ms", 10**12)):
                            self.reference_trace = {
                                "points": self.trace[:],
                                "lap_ms": completed_ms
                            }
                            self.reference_lap_ms = completed_ms
                            save_live_reference_trace(self.reference_trace)

                    # Start a fresh trace for the new lap.
                    self.distance_m = 0.0
                    self.trace = []
                    self.prev_sample_time = sample_now

                fuel_per_lap = median(self.fuel_samples)
                fuel_laps = None
                if fuel_per_lap and fuel_per_lap > 0:
                    fuel_laps = max(0.0, float(p.get("fuel", 0) or 0) / fuel_per_lap)


                # The reference MUST be the real GT7 best lap whenever GT7
                # provides one.  A partially elapsed lap measured by the
                # bridge must NEVER replace that best lap.  That was the bug
                # causing values such as "Referenz: 0:10.961" while GT7
                # showed a real Best Lap of 0:14.236.
                #
                # If GT7 has no best lap yet, a completed lap may be used as
                # a temporary reference.  As soon as GT7 supplies a best lap,
                # that real value takes priority.
                if p["best_lap_ms"] > 0:
                    if self.reference is None or self.reference_source != "best":
                        self.reference = p["best_lap_ms"]
                        self.reference_source = "best"
                    elif p["best_lap_ms"] < self.reference:
                        self.reference = p["best_lap_ms"]

                if p["packet"] == "A":
                    self.mode = "A"
                    if self.last_lap is None:
                        self.lap_start = now
                        self.last_lap = p["lap"]
                    elif p["lap"] != self.last_lap:
                        completed = p["last_lap_ms"]
                        if completed <= 0 and self.lap_start is not None:
                            completed = int((now - self.lap_start) * 1000)

                        # Only use a completed lap as fallback when GT7 has
                        # not supplied a real best lap.
                        if completed > 0 and self.reference_source != "best":
                            if self.reference is None or completed < self.reference:
                                self.reference = completed
                                self.reference_source = "completed"

                        self.lap_start = now
                        self.last_lap = p["lap"]

                    if self.lap_start is None:
                        self.lap_start = now
                    p["current_lap_ms"] = int((now - self.lap_start) * 1000)

                else:
                    # Packet C: detect completed laps, but never let the
                    # freshly reset current_lap_ms or a partial measurement
                    # overwrite GT7's real best-lap reference.
                    if self.last_lap is None:
                        self.last_lap = p["lap"]
                    elif p["lap"] != self.last_lap:
                        completed = p["last_lap_ms"]
                        if completed > 0 and self.reference_source != "best":
                            if self.reference is None or completed < self.reference:
                                self.reference = completed
                                self.reference_source = "completed"
                        self.last_lap = p["lap"]

                # Position-based live delta: compare current elapsed lap time
                # with the reference trace at the same travelled distance.
                delta = None
                reference_ms_at_pos = None
                if (self.reference_trace is not None and
                    p["current_lap_ms"] >= 0 and self.distance_m > 0):
                    reference_ms_at_pos = trace_time_at_distance(
                        self.reference_trace["points"], self.distance_m)
                    if reference_ms_at_pos is not None:
                        delta = int(p["current_lap_ms"] - reference_ms_at_pos)

                with lock:
                    state.update(p)
                    state["fuel_laps"] = fuel_laps
                    state["fuel_lap_consumption"] = fuel_per_lap
                    state["rpm_limit"] = self.rpm_limit
                    state["rpm_learning"] = self.rpm_limit is None or self.rpm_near_limit_count < 8
                    state["connected"] = True
                    state["mode"] = p["packet"]
                    state["valid"] += 1
                    state["delta_ms"] = delta
                    state["reference_ms"] = reference_ms_at_pos if reference_ms_at_pos is not None else self.reference_lap_ms
                    state["reference_ready"] = self.reference_trace is not None
                    valid = state["valid"]

                self.status(
                    f"GT7 LIVE VERBUNDEN · Packet {p['packet']} · "
                    f"{len(raw)} Bytes · gültige Pakete: {valid}"
                )
            except socket.timeout:
                pass
            except Exception as e:
                self.status("UDP-Fehler: " + str(e))

        sock.close()

class App:
    def __init__(self):
        self.bridge = None
        self.root = tk.Tk()
        self.root.title(APP)
        self.root.geometry("650x460")
        self.root.configure(bg="#111111")

        box = tk.Frame(self.root, bg="#111111")
        box.pack(fill="both", expand=True, padx=32, pady=25)

        tk.Label(box, text="SPEED DYNAMICS",
                 font=("Segoe UI", 26, "bold"),
                 fg="#d7b45a", bg="#111111").pack(anchor="w")
        tk.Label(box, text="GT7 Race Engineer · Complete Edition",
                 font=("Segoe UI", 11),
                 fg="#aaaaaa", bg="#111111").pack(anchor="w", pady=(0, 22))

        tk.Label(box, text="IP-Adresse deiner PS5",
                 font=("Segoe UI", 10, "bold"),
                 fg="white", bg="#111111").pack(anchor="w")
        self.ip = tk.StringVar(value="192.168.178.200")
        ttk.Entry(box, textvariable=self.ip,
                  font=("Segoe UI", 14)).pack(fill="x", pady=(6, 12))

        tk.Button(box, text="MIT GT7 VERBINDEN",
                  command=self.connect,
                  font=("Segoe UI", 12, "bold"),
                  bg="#d7b45a", fg="#111111",
                  relief="flat", pady=10).pack(fill="x")

        self.msg = tk.StringVar(value="Bereit")
        tk.Label(box, textvariable=self.msg,
                 font=("Segoe UI", 11, "bold"),
                 fg="#dddddd", bg="#111111",
                 wraplength=580, justify="left").pack(anchor="w", pady=(16, 8))

        self.diag = tk.StringVar(value="Empfangen: 0    Gültig: 0    Bytes: 0    Paket: 0")
        tk.Label(box, textvariable=self.diag,
                 font=("Consolas", 9),
                 fg="#aaaaaa", bg="#111111").pack(anchor="w", pady=(2, 14))

        self.url = f"http://{lan_ip()}:{WEB_PORT}"
        tk.Label(box, text="iPhone – im selben WLAN in Safari öffnen:",
                 font=("Segoe UI", 9),
                 fg="#888888", bg="#111111").pack(anchor="w")
        tk.Label(box, text=self.url,
                 font=("Consolas", 14, "bold"),
                 fg="#d7b45a", bg="#111111").pack(anchor="w", pady=(3, 7))
        tk.Button(box, text="ADRESSE KOPIEREN",
                  command=self.copy_url,
                  bg="#282828", fg="white",
                  relief="flat", pady=7).pack(fill="x")

        threading.Thread(target=start_web, daemon=True).start()

    def status(self, text):
        with lock:
            d = (f"Empfangen: {state['packets']}    "
                 f"Gültig: {state['valid']}    "
                 f"Bytes: {state['bytes']}    "
                 f"Paket: {state['packet_size']}")
        self.root.after(0, lambda: (self.msg.set(text), self.diag.set(d)))

    def connect(self):
        ip = self.ip.get().strip()
        try:
            socket.inet_aton(ip)
        except OSError:
            messagebox.showerror("PS5-IP", "Bitte eine gültige PS5-IP eingeben.")
            return
        if self.bridge:
            self.bridge.stop()
        with lock:
            state.update({
                "connected": False, "mode": "", "packets": 0, "valid": 0,
                "bytes": 0, "packet_size": 0, "delta_ms": None,
                "reference_ms": None, "reference_ready": False,
                "fuel_laps": None, "fuel_lap_consumption": None,
                "rpm_limit": None, "rpm_learning": True,
    "fuel_laps": None, "fuel_lap_consumption": None,
    "rpm_limit": None, "rpm_learning": True
            })
        self.bridge = Bridge(ip, self.status)
        self.bridge.start()

    def copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)
        self.msg.set("iPhone-Adresse kopiert")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    App().run()
