from __future__ import annotations

from telemetry.collector import summarize_csv


def test_summarize_csv_returns_window_metrics():
    csv_content = (
        "timestamp,nginx_worker_count,nginx_worker_cores,somaxconn,tcp_max_syn_backlog,"
        "ip_local_port_range,rx_drop_total,tx_drop_total,tcp_established,mem_used_mb,"
        "vmstat_run_queue,vmstat_blocked\n"
        "1,112,\"0,1\",4096,1024,\"32768 60999\",10,0,900,1000,1,0\n"
        "3,112,\"0,1,2,3\",4096,1024,\"32768 60999\",16,0,1100,1024,4,1\n"
    )

    summary = summarize_csv(csv_content)

    assert summary["sample_count"] == 2
    assert summary["duration_sec"] == 2
    assert summary["rx_drop_delta"] == 6
    assert summary["rx_drop_rate_per_sec"] == 3.0
    assert summary["run_queue_avg"] == 2.5
    assert summary["run_queue_max"] == 4
    assert summary["worker_core_spread_max"] == 4
    assert summary["last_sample"]["tcp_established"] == 1100
