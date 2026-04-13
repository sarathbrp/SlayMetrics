# Hackathon Performance Benchmark

## Quick Start

Run all benchmarks (5 workloads) with your name:
```bash
./benchmark.sh your-name
```

Compare all your results to baseline:
```bash
./compare-results.sh your-name
```

This compares all 5 workloads: homepage, small, medium, large, mixed

## Files

- `benchmark.sh` - Run performance test
- `compare-results.sh` - Compare your results to baseline
- `../hackathon-results/` - All test results saved here
- `../hackathon-results/baseline_*.json` - Reference baseline performance (5 files)

## Test Configuration

The benchmark tests the nginx web server with 4 different workloads:
- `small.lua` - 2.5M files @ 64 bytes (tests small file performance)
- `medium.lua` - 250k files @ 2MB (tests medium file performance)
- `large.lua` - 250 files @ 15MB (tests large file performance)
- `mixed.lua` - Combined workload (70% small, 25% medium, 5% large)

Default test parameters:
- Duration: 30 seconds
- Threads: 448 (4x CPU cores)
- Connections: 8000 concurrent

You can override these with environment variables:
```bash
WORKLOAD=small DURATION=60s THREADS=100 CONNECTIONS=1000 ./benchmark.sh test-name
```

## Goal

Tune the test machine's nginx and system settings to improve performance
compared to the baseline. The contestant with the highest requests/sec wins!

## Submission

Submit all 5 result files from `/root/hackathon-results/`:
- your-name_homepage.json
- your-name_small.json
- your-name_medium.json
- your-name_large.json
- your-name_mixed.json

## Rules

- You can run the benchmark as many times as you want
- Each run overwrites the previous result with your name
- All raw results are kept with timestamps for your reference
- Do NOT modify the testrunner system (this machine)
- Only tune the test machine (target host)

## Network Configuration

Infrastructure uses standardized internal hostnames managed via `/etc/hosts`.
Hosts have multiple network interfaces configured.
Application teams must select appropriate interface for their requirements.

To use different interface, modify `/etc/hosts` to change active IP.

Good luck!
