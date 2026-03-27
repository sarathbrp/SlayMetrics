# Nginx Advanced Tuning

Source: https://github.com/denji/nginx-tuning

## Event Model

use epoll optimized for serving many clients on Linux.
multi_accept on accepts as many connections as possible per event.
These should be set in the events block.

## Compression Details

gzip on enables response compression.
gzip_min_length 10240 only compress responses larger than 10KB to avoid CPU waste on tiny files.
gzip_comp_level 1 fastest compression with minimal CPU cost.
gzip_vary on adds Vary: Accept-Encoding header for caching proxies.
gzip_types covers text/css, text/javascript, application/json, application/xml, text/plain, image/svg+xml.
Do not gzip already-compressed formats (images, video).

## Socket Sharding (reuseport)

reuseport in listen directive enables SO_REUSEPORT. Available Linux 3.9+.
Reduces latency from 15.65ms to 12.35ms. Reduces latency stdev from 26.59ms to 3.15ms.
CPU load stays same at 0.3 versus 10 with accept_mutex off.
Add to listen directive: listen 80 reuseport;

## Connection Timeouts

reset_timedout_connection on closes non-responding client connections to free memory.
client_body_timeout 10 request body read timeout in seconds.
send_timeout 2 frees memory if client stops responding.
keepalive_timeout 30 server closes connection after this time.

## BBR Congestion Control (Linux 4.9+)

TCP BBR improves throughput and reduces latency on high-bandwidth links.
Enable: modprobe tcp_bbr and set net.ipv4.tcp_congestion_control=bbr.
Recommended: net.core.default_qdisc=fq for production.
Persist in /etc/sysctl.d/99-bbr.conf.

## SELinux and File Descriptors

SELinux may block worker_rlimit_nofile with setrlimit denied error.
Fix: setsebool -P httpd_setrlimit 1 to allow nginx to set its own file limits.
With systemd: create /etc/systemd/system/nginx.service.d/nginx.conf with LimitNOFILE=65536.
