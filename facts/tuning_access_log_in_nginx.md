# Tuning Access Log in Nginx

Source: https://access.redhat.com/solutions/7025193

## Problem

Under high request rates, writing one access log entry per request causes significant disk I/O. This bottlenecks throughput, especially for small file workloads where request processing is fast but disk writes are slow.

## Buffered Logging

Buffer multiple log entries in memory and flush them as a batch, reducing disk write frequency.

```nginx
access_log /var/log/nginx/access.log main buffer=32k flush=5m;
```

- `buffer=32k`: Accumulate log entries in a 32KB memory buffer before writing to disk.
- `flush=5m`: Force a write every 5 minutes even if buffer isn't full.
- Default buffer size is 64KB if not specified.

Nginx writes the buffer to disk when:
- The next log line doesn't fit in the buffer
- Buffered data is older than the `flush` interval
- A worker process is shutting down or reopening log files

## Compressed Logging

Combine buffering with gzip compression to further reduce disk I/O.

```nginx
access_log /var/log/nginx/access.log.gz main gzip=1 buffer=32k flush=5m;
```

- `gzip=1`: Fastest compression, minimal CPU overhead. Range is 1 (fastest) to 9 (best compression).
- Buffered data is compressed before writing to disk.
- Reduces both write frequency and write size.

## Disabling Access Log

For maximum throughput when logging is not required:

```nginx
access_log off;
```

- Eliminates all disk I/O from logging.
- Diagnostic: `grep access_log /etc/nginx/nginx.conf`
- Impact: high on small file workloads where log writes per second approach request rate.

## When to Apply

- High request rate workloads (thousands of req/sec)
- Small file serving where per-request overhead matters
- Systems with slow disk I/O or limited IOPS
- Benchmark: compare req/sec with logging on vs off to measure impact
