import socket
import concurrent.futures
import urllib.request
import urllib.error
import json
import time
import subprocess
import re

OLLAMA_PORT = 11434
TIMEOUT = 1.5  # Faster timeout for initial probe
MAX_THREADS = 100
TEST_PROMPT = "Greet me with a joke."

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

    # If port is open, confirm it's Ollama via HTTP
    status, _ = http_request(f"http://{ip}:{OLLAMA_PORT}/api/tags", timeout=TIMEOUT)
    if status == 200:
        return ip
    return None

def find_ollama_servers():
    """Aggressive, fully parallelized discovery."""
    found_servers = set()
    
    # 1. Localhost check is nearly instant
    if check_ip("127.0.0.1"):
        found_servers.add("127.0.0.1")

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
                found_servers.add(res)

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
                            found_servers.add(res)
    
    return sorted(list(found_servers))

def interact_with_ollama(ip):
    """Lists models, checks memory/context status, and streams a test prompt."""
    base_url = f"http://{ip}:{OLLAMA_PORT}/api"
    
    try:
        # 1. Get available models
        status, data = http_request(f"{base_url}/tags", timeout=5)
        if status != 200 or not data:
            print(f"[-] Failed to connect to {ip} (Status: {status})")
            return

        models = [m['name'] for m in data.get("models", [])]
        if not models:
            print(f"[!] No models found on {ip}")
            return

        print(f"\n[+] Server: {ip} | Models: {', '.join(models)}")

        target_model = models[0]
        
        # 2. Get Model Details & Memory Status in parallel-ish logic
        _, show_data = http_request(f"{base_url}/show", method="POST", data={"name": target_model}, timeout=5)
        _, ps_data = http_request(f"{base_url}/ps", timeout=5)
        
        # Parse context
        ctx_size = "Default"
        if show_data:
            ctx_match = re.search(r"num_ctx\s+(\d+)", show_data.get("parameters", ""))
            if ctx_match: ctx_size = ctx_match.group(1)

        # Parse memory status
        loaded_models = [m['name'] for m in ps_data.get("models", [])] if ps_data else []
        mem_status = "Loaded" if target_model in loaded_models else "Not Loaded"
            
        print(f"[i] Testing: {target_model} | Context: {ctx_size} | Status: {mem_status}")

        # 3. Stream the generation
        print(f"[>] Prompt: \"{TEST_PROMPT}\"")
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
            for i, ip in enumerate(found_ips, 1):
                label = " (Localhost)" if ip == "127.0.0.1" else ""
                print(f"  {i}. {ip}{label}")
            
            try:
                choice = input(f"\nSelect server [1-{len(found_ips)}, default 1]: ").strip()
                target_ip = found_ips[int(choice)-1] if choice and 0 < int(choice) <= len(found_ips) else found_ips[0]
            except (ValueError, KeyboardInterrupt, IndexError):
                print("Using default.")
                target_ip = found_ips[0]
        else:
            target_ip = found_ips[0]
        
        if target_ip:
            interact_with_ollama(target_ip)
    else:
        print("\n[-] No Ollama servers found.")
        print("Tips: Ensure Ollama is running and OLLAMA_HOST=0.0.0.0 is set.")
