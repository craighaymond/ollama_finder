# Ollama Finder 🚀

A high-performance, **zero-dependency** Python utility to automatically discover and interact with Ollama servers on your local network (LAN). It uses aggressive, parallelized discovery techniques to find servers, list models, and verify connectivity with a compact, real-time status output.

## Key Features

- **Zero External Dependencies**: Uses only standard Python libraries (`socket`, `urllib`, `concurrent.futures`). No `pip install` required!
- **Multi-Layered Parallel Discovery**:
  - **mDNS Resolution**: Concurrently resolves common hostnames like `macmini.local`, `ollama.local`, `ubuntu.local`, etc.
  - **ARP Table Analysis**: Scans active devices in your system's ARP table for open port `11434`.
  - **Multi-Subnet Scanning**: Automatically detects all local network interfaces and performs a threaded scan of the entire `/24` range as a fallback.
- **Enhanced Interaction**:
  - **Server Selection**: Provides an interactive menu if multiple Ollama servers are found on the network.
  - **Deep Model Inspection**: Reports the `num_ctx` size and memory status (via `/api/ps`) for the target model.
  - **Streaming Responses**: Sends a test prompt ("Greet me with a joke.") and streams the response token-by-token.
- **Compact CLI Output**: Uses a streamlined format for quick readability.

## Requirements

- **Python 3.x** (Standard installation)

## Usage

Simply run the script:

```bash
python ollama_finder.py
```

## Network Configuration (Crucial)

By default, Ollama only listens on `127.0.0.1`. To allow this script to find your Ollama server across your LAN, you **must** bind it to all network interfaces.

## How it Works

1. **Phase 1: Fast Probe**: The script checks `localhost` and performs parallel mDNS and ARP-based discovery.
2. **Phase 2: Subnet Fallback**: If no server is found, it identifies all local subnets and scans up to 254 IPs per interface simultaneously.
3. **Phase 3: Selection & Validation**: If multiple servers are found, you choose which one to use. The script confirms it's an Ollama instance by hitting the `/api/tags` endpoint.
4. **Phase 4: Interaction**: It retrieves model metadata, checks memory status, and streams a test generation.

---
*Created with 🧠 by Gemini CLI*
