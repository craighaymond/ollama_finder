import socket
import concurrent.futures
import urllib.request
import urllib.error
import json
import time
import subprocess
import re
from datetime import datetime, timezone

OLLAMA_PORT = 11434
TIMEOUT = 1.5  # Faster timeout for initial probe
MAX_THREADS = 100
TEST_PROMPT = "Tell me a joke."

def format_relative_time(iso_str):
    """Converts ISO date to relative string (e.g., 2d ago)."""
    try:
        # Ollama returns "2024-05-14T10:11:12.123456789Z"
        # We need to strip the sub-second part for standard Python parsing
        base_time = iso_str.split(".")[0]
        dt = datetime.strptime(base_time, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - dt
        
        if diff.days > 365: return f"{diff.days // 365}y ago"
        if diff.days > 30: return f"{diff.days // 30}mo ago"
        if diff.days > 0: return f"{diff.days}d ago"
        if diff.seconds > 3600: return f"{diff.seconds // 3600}h ago"
        if diff.seconds > 60: return f"{diff.seconds // 60}m ago"
        return "just now"
    except Exception:
        return "unknown"

def http_request(url, method="GET", data=None, timeout=5):
    """Zero-dependency HTTP request helper."""
    try:
        req = urllib.request.Request(url, method=method)
        json_data = None
        if data:
            req.add_header('Content-Type', 'application/json')
            json_data = json.dumps(data).encode('utf-8')
            
        with urllib.request.urlopen(req, data=json_data, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, socket.timeout, ConnectionRefusedError):
        return None, None
    except Exception:
        return None, None

def get_local_subnets():
    """Finds all local subnets from active network interfaces."""
    subnets = set()
    try:
        # Method 1: The 'Internet route' trick
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            subnets.add(".".join(ip.split(".")[:-1]))
        except Exception:
            pass
        finally:
            s.close()

        # Method 2: Fallback - Get all IPs assigned to this hostname
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                subnets.add(".".join(ip.split(".")[:-1]))
    except Exception:
        pass
    return list(subnets)

def get_arp_ips():
    """Extracts all IPs from the ARP table."""
    ips = set()
    try:
        # 'arp -a' works on Windows, Linux, and macOS
        output = subprocess.check_output(["arp", "-a"]).decode("ascii", errors="ignore")
        found = re.findall(r"(\d+\.\d+\.\d+\.\d+)", output)
        for ip in found:
            # Filter out multicast, broadcast, and loopback
            if not (ip.startswith("224.") or ip.startswith("239.") or 
                    ip.startswith("127.") or ip.endswith(".255") or ip == "255.255.255.255"):
                ips.add(ip)
    except Exception:
        pass
    return list(ips)

def get_mdns_ips():
    """Resolves common Ollama hostnames in parallel."""
    names = [
        "macmini.local", "mac-mini.local", "mac-mini-m4.local", 
        "ollama.local", "raspberrypi.local", "ubuntu.local",
        "studio.local", "pro.local", "air.local"
    ]
    ips = set()
    
    def resolve(name):
        try:
            return socket.gethostbyname(name)
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as executor:
        results = executor.map(resolve, names)
        for ip in results:
            if ip:
                ips.add(ip)
    return list(ips)

def check_ip(ip):
    """Checks if the Ollama port is open and responding."""
    # Fast TCP probe first
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)  # Very fast probe
            if s.connect_ex((ip, OLLAMA_PORT)) != 0:
                return None
    except Exception:
        return None

    # If port is open, confirm it's Ollama and check for loaded models
    status, _ = http_request(f"http://{ip}:{OLLAMA_PORT}/api/tags", timeout=TIMEOUT)
    if status == 200:
        _, ps_data = http_request(f"http://{ip}:{OLLAMA_PORT}/api/ps", timeout=TIMEOUT)
        loaded = ""
        if ps_data and ps_data.get("models"):
            loaded = ps_data["models"][0]["name"]
        return (ip, loaded)
    return None

def find_ollama_servers():
    """Aggressive, fully parallelized discovery."""
    found_servers = {} # Use dict to handle duplicates: ip -> loaded_model
    
    # 1. Localhost check is nearly instant
    res = check_ip("127.0.0.1")
    if res:
        found_servers[res[0]] = res[1]

    # 2. Parallel Gathering & Probing
    print("Searching via mDNS and ARP concurrently...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        # Start gathering candidates in background threads
        mdns_future = executor.submit(get_mdns_ips)
        arp_future = executor.submit(get_arp_ips)
        
        # Combine candidates as they arrive
        candidates = set(mdns_future.result()) | set(arp_future.result())
        
        # Probe all candidates in parallel
        futures = {executor.submit(check_ip, ip): ip for ip in candidates}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                ip, loaded = res
                found_servers[ip] = loaded

    # 3. Thorough Subnet Scan (Only if nothing found yet)
    if not found_servers:
        subnets = get_local_subnets()
        if subnets:
            print(f"Nothing found in ARP/mDNS. Scanning {len(subnets)} subnets...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                for subnet in subnets:
                    ips_to_check = [f"{subnet}.{i}" for i in range(1, 255)]
                    futures = {executor.submit(check_ip, ip): ip for ip in ips_to_check}
                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        if res:
                            ip, loaded = res
                            found_servers[ip] = loaded
    
    return sorted(found_servers.items())

def interact_with_ollama(ip):
    """Lists models, checks memory/context status, and streams a test prompt."""
    base_url = f"http://{ip}:{OLLAMA_PORT}/api"
    
    try:
        # 1. Get available models and currently loaded models
        _, ps_data = http_request(f"{base_url}/ps", timeout=5)
        loaded_models = [m['name'] for m in ps_data.get("models", [])] if ps_data else []
        loaded_str = f" (Loaded: {', '.join(loaded_models)})" if loaded_models else ""

        status, data = http_request(f"{base_url}/tags", timeout=5)
        if status != 200 or not data:
            print(f"[-] Failed to connect to {ip} (Status: {status})")
            return

        models_list = sorted(data.get("models", []), key=lambda x: x.get("name", "").lower())
        if not models_list:
            print(f"[!] No models found on {ip}")
            return

        print(f"\n[+] Server: {ip}{loaded_str}")
        print(f"    {'Model':<25} | {'Params':>7} | {'Quant':>8} | {'Size':>7} | {'Pulled':>10}")
        print("    " + "-" * 70)
        for m in models_list:
            name = m.get("name", "Unknown")
            details = m.get("details", {})
            params = details.get("parameter_size", "Unknown")
            quant = details.get("quantization_level", "Unknown")
            size_gb = m.get("size", 0) / (1024**3)
            pulled = format_relative_time(m.get("modified_at", ""))
            print(f"    - {name:<23} | {params:>7} | {quant:>8} | {size_gb:6.1f}GB | {pulled:>10}")

        target_model = models_list[0]["name"]
        
        # 2. Get Model Details & Memory Status
        _, show_data = http_request(f"{base_url}/show", method="POST", data={"name": target_model}, timeout=5)
        
        # Parse context
        ctx_size = "Default"
        if show_data:
            ctx_match = re.search(r"num_ctx\s+(\d+)", show_data.get("parameters", ""))
            if ctx_match: ctx_size = ctx_match.group(1)

        # Parse memory status and VRAM usage for the target model
        mem_status = "Not Loaded"
        loaded_info = next((m for m in ps_data.get("models", []) if m['name'] == target_model), None) if ps_data else None
        if loaded_info:
            vram_gb = loaded_info.get("size_vram", 0) / (1024**3)
            mem_status = f"Loaded (VRAM: {vram_gb:.1f}GB)"
            
        print(f"[i] Testing: {target_model} | Context: {ctx_size} | Status: {mem_status}")

        # 3. Stream the generation
        print(f"[>] Test Prompt: \"{TEST_PROMPT}\"")
        print("[<] LLM response: ", end="", flush=True)

        payload = {"model": target_model, "prompt": TEST_PROMPT, "stream": True}
        req = urllib.request.Request(f"{base_url}/generate", method="POST")
        req.add_header('Content-Type', 'application/json')
        
        with urllib.request.urlopen(req, data=json.dumps(payload).encode('utf-8'), timeout=120) as res:
            for line in res:
                if line:
                    chunk = json.loads(line.decode('utf-8'))
                    text = chunk.get("response", "")
                    print(text, end="", flush=True)
                    if chunk.get("done"):
                        print()
                        break

    except Exception as e:
        print(f"\n[!] Error with {ip}: {e}")

if __name__ == "__main__":
    start_time = time.time()
    print("Searching for Ollama servers...", end=" ", flush=True)
    
    found_ips = find_ollama_servers()
    print(f"Done ({time.time() - start_time:.1f}s)")

    if found_ips:
        target_ip = None
        if len(found_ips) > 1:
            print(f"\nFound {len(found_ips)} servers:")
            for i, (ip, loaded) in enumerate(found_ips, 1):
                label = " (Localhost)" if ip == "127.0.0.1" else ""
                loaded_label = f" [Loaded: {loaded}]" if loaded else ""
                print(f"  {i}. {ip}{label}{loaded_label}")
            
            try:
                choice = input(f"\nSelect server [1-{len(found_ips)}, default 1]: ").strip()
                idx = int(choice)-1 if choice and 0 < int(choice) <= len(found_ips) else 0
                target_ip = found_ips[idx][0]
            except (ValueError, KeyboardInterrupt, IndexError):
                print("Using default.")
                target_ip = found_ips[0][0]
        else:
            target_ip = found_ips[0][0]
        
        if target_ip:
            interact_with_ollama(target_ip)
    else:
        print("\n[-] No Ollama servers found.")
        print("Tips: Ensure Ollama is running and OLLAMA_HOST=0.0.0.0 is set.")
