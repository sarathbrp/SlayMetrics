# Turbo Charge Nginx on RHEL Bare Metal (112 cores)

## Kernel Network Stack Tuning

Expand the kernel's capacity for high RPS small packet workloads.

net.core.somaxconn=65535 expands the waiting room for new connections.
net.ipv4.tcp_max_syn_backlog=65535 increases SYN queue depth.
net.core.netdev_max_backlog=10000 increases NIC packet buffer before CPU handoff.
net.ipv4.tcp_tw_reuse=1 speeds up recycling of closed connections. Critical for small file benchmarks.
net.ipv4.tcp_fin_timeout=15 reduces TIME_WAIT socket duration.
net.ipv4.ip_local_port_range=1024 65535 maximizes local port range to avoid exhaustion.
Persist in /etc/sysctl.conf and apply with sysctl -p.

## Nginx Worker Configuration for 112 Cores

On 112-core systems, multiple workers fighting over same cores creates NUMA locality issues. CPU has to reach across motherboard to access memory, destroying RPS.

worker_processes 112 matches core count exactly.
worker_cpu_affinity auto pins 1 worker to 1 core strictly. Prevents NUMA cross-socket access.
worker_rlimit_nofile 1000000 allows each worker massive file descriptor capacity.

## Nginx Events Block

worker_connections 20000 per worker. Total capacity = 112 workers x 20000 = 2.24M connections.
use epoll for Linux optimized event model.
multi_accept on accepts multiple connections per event cycle.

## Nginx HTTP Optimization

access_log off eliminates disk I/O at 1M+ RPS. Logging at that rate saturates disk and CPU.
listen 80 reuseport allows kernel to distribute packets to all 112 workers directly. Without reuseport, one accept queue becomes bottleneck.

## IRQ Affinity Tuning

Even with 112 queues, RHEL might send multiple NIC queues to same CPU.

Stop irqbalance: systemctl stop irqbalance. It may not distribute optimally for dedicated workloads.
Set manual affinity: set_irq_affinity -x all ens1f0 spreads NIC interrupts across all cores.
Verify with: watch -n1 "cat /proc/interrupts | grep ens1f0". All 112 CPU columns should increment simultaneously.

## Reuseport

listen 80 reuseport in the server block enables SO_REUSEPORT. Each worker gets its own accept queue from the kernel. Without it, all workers contend on a single accept queue. Critical for 100k+ RPS on multi-core systems.
