# RHEL Network and System Performance Tuning

Source: https://access.redhat.com/articles/1391433

These OS-level tunings directly affect nginx and any network-intensive service on RHEL. The agent should check and apply these alongside service-specific configuration.

## CPU Power States and Governor

The CPU frequency governor controls whether cores run at full speed or scale down to save power. For webservers, powersave mode causes higher latency and lower throughput.

- Diagnostic: `cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor | sort -u`
- Fix: `echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor`
- Also disable deep C-states in BIOS/EFI (C6, C3, C1E) for lowest latency.
- Kernel parameter: `processor.max_cstate=1 intel_idle.max_cstate=0`
- Verify: `cat /sys/module/intel_idle/parameters/max_cstate` should be 0.
- Impact on nginx: eliminates CPU wake-up latency on every incoming request. Critical for small file workloads where per-request CPU time is minimal.

## Tuned Profiles

RHEL's `tuned` daemon applies pre-defined performance profiles covering CPU governor, I/O scheduler, and kernel tunables.

- Install: `dnf install tuned && systemctl enable --now tuned`
- List profiles: `tuned-adm list`
- Recommended for webservers: `tuned-adm profile throughput-performance` or `latency-performance`
- Verify active: `tuned-adm active`
- Impact on nginx: applies multiple tunings (governor, scheduler, vm settings) in one command.

## TCP Listen Backlog (somaxconn)

Controls the maximum queue length for incoming TCP connections. When nginx receives a burst of connections and this value is too low, connections are dropped.

- Diagnostic: `sysctl net.core.somaxconn`
- Default: 128 on older RHEL, 4096 on RHEL 9+
- Fix: `sysctl -w net.core.somaxconn=65535`
- Persist: `echo 'net.core.somaxconn = 65535' > /etc/sysctl.d/99-nginx.conf && sysctl -p`
- Nginx must also match: `listen *:80 backlog=65535;` in the server block. Without this, nginx uses its own default of 511 regardless of OS setting.
- Impact on nginx: prevents connection drops under load spikes. Directly affects req/sec under high concurrency.

## TCP SYN Backlog

Separate from somaxconn, this controls the SYN queue for half-open connections.

- Diagnostic: `sysctl net.ipv4.tcp_max_syn_backlog`
- Fix: `sysctl -w net.ipv4.tcp_max_syn_backlog=65535`
- Impact on nginx: prevents SYN flood related drops. Important when many new connections arrive simultaneously.

## Adapter Backlog Queue (netdev_max_backlog)

Kernel queue where packets wait after arriving from NIC but before protocol processing. If the CPU can't drain packets fast enough, this queue overflows and packets are dropped silently.

- Diagnostic: `sysctl net.core.netdev_max_backlog` (default 1000)
- Check for drops: `cat /proc/net/softnet_stat` — 2nd column (hex) shows backlog overflow count per CPU. Any non-zero value means packets are being dropped.
- Fix: `sysctl -w net.core.netdev_max_backlog=65535`
- Impact on nginx: prevents silent packet loss under high request rates. If the agent sees dropped packets here, doubling this value is the first fix.

## SoftIRQ Budget (netdev_budget)

Controls how many packets a SoftIRQ can process per CPU cycle. Default 300. If SoftIRQs don't get enough CPU time, NIC buffers overflow.

- Diagnostic: `sysctl net.core.netdev_budget` and check 3rd column of `/proc/net/softnet_stat` for time_squeeze count.
- Fix: `sysctl -w net.core.netdev_budget=600` (double the default)
- Only needed for 10Gbps+ or very high packet rates.
- Impact on nginx: allows kernel to drain NIC buffers faster, preventing packet loss.

## IRQ Affinity and Balance

NIC interrupts should be spread across CPU cores. If all interrupts hit one core, that core becomes a bottleneck while others sit idle.

- Diagnostic: `egrep "CPU|eth" /proc/interrupts` — check if all interrupt counts are on one CPU.
- Using irqbalance: `systemctl enable --now irqbalance`
- Manual affinity: `echo <cpu_mask> > /proc/irq/<irq_number>/smp_affinity`
- For NUMA systems: pin NIC interrupts to the NUMA node the NIC is on: `cat /sys/class/net/<iface>/device/numa_node`
- Impact on nginx: distributes packet processing across cores. Directly improves throughput on multi-core systems.

## NUMA Topology

On multi-socket systems, memory access latency depends on which socket the CPU and memory are on. Accessing remote memory is 2-3x slower.

- Diagnostic: `numactl --hardware` and `lscpu | grep NUMA`
- Check NIC locality: `cat /sys/class/net/<iface>/device/numa_node`
- Fix: Bind nginx to the same NUMA node as the NIC: `numactl --cpunodebind=0 --membind=0 nginx`
- Or use `numad` daemon: `systemctl enable --now numad`
- Impact on nginx: reduces memory access latency for packet processing. Most impactful on dual-socket servers.

## NIC Ring Buffer Tuning

The NIC has hardware RX/TX ring buffers. If these are too small, packets are dropped before the kernel even sees them.

- Diagnostic: `ethtool -g <iface>` shows current vs maximum ring buffer sizes.
- Fix: `ethtool -G <iface> rx 8192 tx 8192` (set to maximum)
- Check for drops: `ethtool -S <iface> | grep -i 'drop\|error\|miss\|fifo'`
- Impact on nginx: prevents hardware-level packet loss. First thing to check when `ethtool -S` shows rx_missed_errors.

## NIC Offloading

Modern NICs can offload checksum calculation, segmentation, and receive aggregation from the CPU.

- Diagnostic: `ethtool -k <iface>`
- Key features that should be ON: `rx-checksumming`, `tx-checksumming`, `tcp-segmentation-offload`, `generic-receive-offload`
- Fix: `ethtool -K <iface> tso on gro on rx on tx on`
- Verify: `ethtool -k <iface> | grep -E 'checksum|offload|segmentation'`
- Impact on nginx: reduces CPU load for packet processing. Frees CPU cycles for nginx worker processes.

## Interrupt Coalescence

Controls how many packets or microseconds the NIC waits before interrupting the CPU. Too aggressive = wasted CPU on interrupts. Too lazy = latency.

- Diagnostic: `ethtool -c <iface>`
- For latency-sensitive (small files): `ethtool -C <iface> adaptive-rx on`
- For throughput (large files): `ethtool -C <iface> rx-usecs 100 rx-frames 64`
- Impact on nginx: adaptive mode auto-tunes for the workload. Good default for mixed small/medium/large files.

## TCP Buffer Tuning

Socket receive buffers determine how much data can be queued before the application reads it. If buffers are too small, packets are pruned (dropped).

- Check for pruning: `netstat -sn | grep -i prune` — any non-zero value means buffers are too small.
- Diagnostic: `sysctl net.ipv4.tcp_rmem` (min, default, max)
- Fix: `sysctl -w net.ipv4.tcp_rmem="16384 349520 16777216"` and `sysctl -w net.core.rmem_max=16777216`
- Impact on nginx: prevents packet loss at the socket layer. Important for upstream proxy configurations.

## TCP Timestamps and Window Scaling

- `net.ipv4.tcp_timestamps=1`: Enables accurate RTT estimation. Prevents wrapped sequence numbers on fast links. Keep enabled.
- `net.ipv4.tcp_window_scaling=1`: Allows TCP windows larger than 64KB. Required for high-bandwidth connections. Keep enabled.
- `net.ipv4.tcp_sack=1`: Selective ACK. Reduces retransmission overhead. Keep enabled.
- Verify: `sysctl net.ipv4.tcp_timestamps net.ipv4.tcp_window_scaling net.ipv4.tcp_sack`

## Ephemeral Port Range

Under high connection churn, the system can exhaust available source ports.

- Diagnostic: `sysctl net.ipv4.ip_local_port_range`
- Default: `32768 60999` (~28k ports)
- Fix: `sysctl -w net.ipv4.ip_local_port_range="1024 65535"` (~64k ports)
- Also enable: `sysctl -w net.ipv4.tcp_tw_reuse=1` to reuse TIME_WAIT sockets.
- Impact on nginx: prevents port exhaustion when nginx proxies to upstream servers at high rates.

## File Descriptor Limits

Every nginx connection uses up to 2 file descriptors. Under high concurrency the OS limit can be hit.

- Diagnostic: `ulimit -n` (per-process soft limit) and `cat /proc/sys/fs/file-max` (system-wide)
- Fix process limit: add to `/etc/security/limits.conf`: `* soft nofile 65536` and `* hard nofile 65536`
- Fix systemd unit: add `LimitNOFILE=65536` to nginx unit override.
- Fix system-wide: `sysctl -w fs.file-max=2097152`
- Nginx config: `worker_rlimit_nofile 65536;` must match OS limit.
- Impact on nginx: prevents "too many open files" errors under high concurrency.

## Transparent Hugepages

THP can cause latency spikes due to memory compaction in the background.

- Diagnostic: `cat /sys/kernel/mm/transparent_hugepage/enabled`
- Fix: `echo never > /sys/kernel/mm/transparent_hugepage/enabled`
- Persist: add `transparent_hugepage=never` to kernel command line in GRUB.
- Impact on nginx: eliminates random latency spikes caused by THP compaction. Most visible in p99 latency.

## SELinux

SELinux in enforcing mode adds syscall overhead for every file access and network operation.

- Diagnostic: `getenforce`
- Temporary fix: `setenforce 0` (permissive mode — logs but doesn't block)
- Permanent fix: edit `/etc/selinux/config` and set `SELINUX=permissive`
- Alternative: keep enforcing but create policy exceptions with `audit2allow` for nginx-specific operations.
- Impact on nginx: reducing SELinux overhead improves throughput for file-serving workloads, especially small files where the ratio of syscalls to data transferred is high.

## Filesystem Mount Options

The `atime` mount option updates the file access timestamp on every read, generating unnecessary disk writes.

- Diagnostic: `findmnt -o TARGET,OPTIONS | grep -v noatime`
- Fix: add `noatime` to mount options in `/etc/fstab` and remount: `mount -o remount,noatime /`
- Impact on nginx: eliminates one write syscall per static file served. Significant for small file workloads at high request rates.
