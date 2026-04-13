#!/usr/bin/env python3
"""Compare two performance test results"""

import sys
import json
from pathlib import Path


def load_result(filepath):
    """Load a result JSON file"""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: File not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {filepath}: {e}", file=sys.stderr)
        sys.exit(1)


def compare_results(baseline_file, current_file):
    """Compare two result files and return comparison data"""

    baseline = load_result(baseline_file)
    current = load_result(current_file)

    # Extract metrics
    baseline_rps = baseline.get("results", {}).get("requests", {}).get("per_sec", 0)
    current_rps = current.get("results", {}).get("requests", {}).get("per_sec", 0)

    # Calculate percentage change
    if baseline_rps > 0:
        rps_change_pct = ((current_rps - baseline_rps) / baseline_rps) * 100
    else:
        rps_change_pct = 0

    # Determine verdict
    if rps_change_pct < -10:
        verdict = "DEGRADED"
    elif rps_change_pct > 10:
        verdict = "IMPROVED"
    else:
        verdict = "STABLE"

    # Build comparison
    comparison = {
        "baseline_file": baseline_file,
        "current_file": current_file,
        "baseline_date": baseline.get("test_date", "unknown"),
        "current_date": current.get("test_date", "unknown"),
        "baseline_label": baseline.get("test_type", "baseline"),
        "current_label": current.get("test_type", "current"),
        "verdict": verdict,
        "metrics": {
            "requests_per_sec": {
                "baseline": round(baseline_rps, 0),
                "current": round(current_rps, 0),
                "change_pct": round(rps_change_pct, 2)
            },
            "latency_avg": {
                "baseline": baseline.get("results", {}).get("latency", {}).get("avg", "N/A"),
                "current": current.get("results", {}).get("latency", {}).get("avg", "N/A")
            },
            "transfer_rate": {
                "baseline": baseline.get("results", {}).get("transfer", {}).get("human", "N/A"),
                "current": current.get("results", {}).get("transfer", {}).get("human", "N/A")
            }
        },
        "workload": {
            "baseline": baseline.get("workload", {}),
            "current": current.get("workload", {})
        }
    }

    # Add system metrics if available
    baseline_metrics = baseline.get("system_metrics", {})
    current_metrics = current.get("system_metrics", {})

    if not baseline_metrics.get("error") and not current_metrics.get("error"):
        comparison["system_resources"] = {
            "cpu": {
                "baseline_load": baseline_metrics.get("cpu", {}).get("load_avg", 0),
                "current_load": current_metrics.get("cpu", {}).get("load_avg", 0),
                "delta_load": round(
                    current_metrics.get("cpu", {}).get("load_avg", 0) -
                    baseline_metrics.get("cpu", {}).get("load_avg", 0), 2
                )
            },
            "memory": {
                "baseline_swap_mb": baseline_metrics.get("memory", {}).get("swap_used_avg_mb", 0),
                "current_swap_mb": current_metrics.get("memory", {}).get("swap_used_avg_mb", 0),
                "delta_swap_mb": round(
                    current_metrics.get("memory", {}).get("swap_used_avg_mb", 0) -
                    baseline_metrics.get("memory", {}).get("swap_used_avg_mb", 0), 1
                )
            },
            "network": {
                "baseline_in_mbps": baseline_metrics.get("network", {}).get("in_mb_per_sec", 0),
                "current_in_mbps": current_metrics.get("network", {}).get("in_mb_per_sec", 0),
                "delta_in_mbps": round(
                    current_metrics.get("network", {}).get("in_mb_per_sec", 0) -
                    baseline_metrics.get("network", {}).get("in_mb_per_sec", 0), 2
                ),
                "baseline_out_mbps": baseline_metrics.get("network", {}).get("out_mb_per_sec", 0),
                "current_out_mbps": current_metrics.get("network", {}).get("out_mb_per_sec", 0),
                "delta_out_mbps": round(
                    current_metrics.get("network", {}).get("out_mb_per_sec", 0) -
                    baseline_metrics.get("network", {}).get("out_mb_per_sec", 0), 2
                )
            },
            "disk": {
                "baseline_read_mbps": baseline_metrics.get("disk", {}).get("read_mb_per_sec", 0),
                "current_read_mbps": current_metrics.get("disk", {}).get("read_mb_per_sec", 0),
                "delta_read_mbps": round(
                    current_metrics.get("disk", {}).get("read_mb_per_sec", 0) -
                    baseline_metrics.get("disk", {}).get("read_mb_per_sec", 0), 2
                ),
                "baseline_write_mbps": baseline_metrics.get("disk", {}).get("write_mb_per_sec", 0),
                "current_write_mbps": current_metrics.get("disk", {}).get("write_mb_per_sec", 0),
                "delta_write_mbps": round(
                    current_metrics.get("disk", {}).get("write_mb_per_sec", 0) -
                    baseline_metrics.get("disk", {}).get("write_mb_per_sec", 0), 2
                )
            }
        }

    return comparison


def print_comparison(comparison):
    """Print human-readable comparison"""

    print("=== Performance Comparison ===")
    print()
    print(f"Baseline: {comparison['baseline_label']} ({comparison['baseline_date']})")
    print(f"Current:  {comparison['current_label']} ({comparison['current_date']})")
    print(f"Verdict:  {comparison['verdict']}")
    print()

    metrics = comparison["metrics"]
    print("Application Performance:")
    print(f"  Requests/sec:  {metrics['requests_per_sec']['baseline']:.0f} → {metrics['requests_per_sec']['current']:.0f} ({metrics['requests_per_sec']['change_pct']:+.1f}%)")
    print(f"  Latency (avg): {metrics['latency_avg']['baseline']} → {metrics['latency_avg']['current']}")
    print(f"  Transfer Rate: {metrics['transfer_rate']['baseline']} → {metrics['transfer_rate']['current']}")
    print()

    # System resources if available
    if "system_resources" in comparison:
        sr = comparison["system_resources"]
        print("System Resources (during test):")
        print(f"  Load Avg (per-CPU): {sr['cpu']['baseline_load']} → {sr['cpu']['current_load']} ({sr['cpu']['delta_load']:+.2f})")
        print(f"  Swap Used:          {sr['memory']['baseline_swap_mb']}MB → {sr['memory']['current_swap_mb']}MB ({sr['memory']['delta_swap_mb']:+.1f}MB)")
        print(f"  Network In:         {sr['network']['baseline_in_mbps']}MB/s → {sr['network']['current_in_mbps']}MB/s ({sr['network']['delta_in_mbps']:+.2f}MB/s)")
        print(f"  Network Out:        {sr['network']['baseline_out_mbps']}MB/s → {sr['network']['current_out_mbps']}MB/s ({sr['network']['delta_out_mbps']:+.2f}MB/s)")
        print(f"  Disk Reads:         {sr['disk']['baseline_read_mbps']}MB/s → {sr['disk']['current_read_mbps']}MB/s ({sr['disk']['delta_read_mbps']:+.2f}MB/s)")
        print(f"  Disk Writes:        {sr['disk']['baseline_write_mbps']}MB/s → {sr['disk']['current_write_mbps']}MB/s ({sr['disk']['delta_write_mbps']:+.2f}MB/s)")
        print()

        # Resource analysis
        print("Resource Analysis:")
        if sr['cpu']['delta_load'] > 0.5:
            print("  DEGRADED - Higher system load")
        else:
            print("  Load average stable")

        if sr['memory']['delta_swap_mb'] > 100:
            print("  DEGRADED - Swap pressure")
        else:
            print("  Minimal swapping")

        if sr['network']['delta_out_mbps'] < -10:
            print("  DEGRADED - Lower network throughput")
        else:
            print("  Network throughput OK")

        if sr['disk']['delta_write_mbps'] > 10 or sr['disk']['delta_read_mbps'] > 10:
            print("  DEGRADED - Excessive disk activity")
        else:
            print("  Disk activity OK")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: compare_results.py <baseline.json> <current.json> [--json]")
        print()
        print("Compare two performance test result files")
        print()
        print("Options:")
        print("  --json    Output comparison as JSON instead of human-readable")
        sys.exit(1)

    baseline_file = sys.argv[1]
    current_file = sys.argv[2]
    json_output = "--json" in sys.argv

    comparison = compare_results(baseline_file, current_file)

    if json_output:
        print(json.dumps(comparison, indent=2))
    else:
        print_comparison(comparison)
