#!/usr/bin/env python3
"""Parse wrk output into JSON format for comparison"""

import sys
import re
import json
from datetime import datetime


def parse_wrk_output(output):
    """Parse wrk text output into structured JSON"""

    result = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "requests": {},
        "latency": {},
        "transfer": {},
        "raw_output": output
    }

    # Parse requests per second
    req_match = re.search(r'Requests/sec:\s+([\d.]+)', output)
    if req_match:
        result["requests"]["per_sec"] = float(req_match.group(1))

    # Parse transfer rate
    transfer_match = re.search(r'Transfer/sec:\s+([\d.]+)(KB|MB|GB)', output)
    if transfer_match:
        value = float(transfer_match.group(1))
        unit = transfer_match.group(2)
        # Convert to bytes/sec
        multipliers = {"KB": 1024, "MB": 1024*1024, "GB": 1024*1024*1024}
        result["transfer"]["bytes_per_sec"] = value * multipliers.get(unit, 1)
        result["transfer"]["human"] = f"{value}{unit}/sec"

    # Parse latency stats
    # wrk format: "Latency     10.52ms    5.23ms  50.00ms   75.00%"
    latency_match = re.search(r'Latency\s+([\d.]+\w+)\s+([\d.]+\w+)\s+([\d.]+\w+)\s+([\d.]+%)', output)
    if latency_match:
        result["latency"]["avg"] = latency_match.group(1)
        result["latency"]["stdev"] = latency_match.group(2)
        result["latency"]["max"] = latency_match.group(3)
        result["latency"]["stdev_pct"] = latency_match.group(4)

    # Parse latency distribution percentiles
    latency_lines = output.split('\n')
    percentiles = {}
    for line in latency_lines:
        percentile_match = re.match(r'\s+([\d.]+)%\s+([\d.]+\w+)', line)
        if percentile_match:
            pct = percentile_match.group(1)
            value = percentile_match.group(2)
            percentiles[f"p{pct}"] = value
    if percentiles:
        result["latency"]["percentiles"] = percentiles

    # Parse total requests
    total_req_match = re.search(r'(\d+) requests in ([\d.]+\w+)', output)
    if total_req_match:
        result["requests"]["total"] = int(total_req_match.group(1))
        result["requests"]["duration"] = total_req_match.group(2)

    # Parse total transfer
    total_transfer_match = re.search(r'([\d.]+)(KB|MB|GB) read', output)
    if total_transfer_match:
        value = float(total_transfer_match.group(1))
        unit = total_transfer_match.group(2)
        result["transfer"]["total_human"] = f"{value}{unit}"

    # Parse thread and connection info
    thread_match = re.search(r'(\d+) threads and (\d+) connections', output)
    if thread_match:
        result["config"] = {
            "threads": int(thread_match.group(1)),
            "connections": int(thread_match.group(2))
        }

    # Parse errors
    errors = {}
    socket_errors = re.search(r'Socket errors: connect (\d+), read (\d+), write (\d+), timeout (\d+)', output)
    if socket_errors:
        errors = {
            "connect": int(socket_errors.group(1)),
            "read": int(socket_errors.group(2)),
            "write": int(socket_errors.group(3)),
            "timeout": int(socket_errors.group(4))
        }
    non_2xx = re.search(r'Non-2xx or 3xx responses: (\d+)', output)
    if non_2xx:
        errors["non_2xx_3xx"] = int(non_2xx.group(1))
    if errors:
        result["errors"] = errors

    return result


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Read from file
        with open(sys.argv[1], 'r') as f:
            wrk_output = f.read()
    else:
        # Read from stdin
        wrk_output = sys.stdin.read()

    result = parse_wrk_output(wrk_output)
    print(json.dumps(result, indent=2))
