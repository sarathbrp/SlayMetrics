import React from 'react'
import { BarChart, Bar, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid } from 'recharts'

const fmt = (v) => {
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(0)}K`
  return v.toFixed(0)
}

export default function BenchmarkChart({ baselines, finals, title }) {
  if (!baselines || !finals) return null

  const workloads = ['homepage', 'small', 'medium', 'large', 'mixed']
  const data = workloads
    .filter(w => baselines[w] || finals[w])
    .map(w => ({
      workload: w,
      baseline: baselines[w]?.rps || 0,
      after: finals[w]?.rps || 0,
      baseline_p99: baselines[w]?.p99 || 0,
      after_p99: finals[w]?.p99 || 0,
    }))

  return (
    <div className="glass-card p-5">
      <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>
        {title || 'Benchmark Results'}
      </h3>
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={data} barGap={4}>
          <defs>
            <linearGradient id="gradBaseline" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#64748b" stopOpacity={0.8} />
              <stop offset="100%" stopColor="#64748b" stopOpacity={0.3} />
            </linearGradient>
            <linearGradient id="gradAfter" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10b981" stopOpacity={1} />
              <stop offset="100%" stopColor="#06b6d4" stopOpacity={0.7} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
          <XAxis dataKey="workload" stroke="var(--text-muted)" tick={{ fontSize: 12 }} />
          <YAxis tickFormatter={fmt} stroke="var(--text-muted)" tick={{ fontSize: 12 }} />
          <Tooltip
            contentStyle={{
              background: 'var(--tooltip-bg)',
              border: '1px solid var(--tooltip-border)',
              borderRadius: '12px',
              backdropFilter: 'blur(12px)',
              boxShadow: '0 8px 32px rgba(0,0,0,0.12)',
              color: 'var(--tooltip-text)',
            }}
            labelStyle={{ color: 'var(--text-primary)', fontWeight: 600 }}
            formatter={(v) => [fmt(v) + ' RPS', '']}
          />
          <Legend wrapperStyle={{ fontSize: '12px', color: 'var(--text-secondary)' }} />
          <Bar dataKey="baseline" fill="url(#gradBaseline)" name="Baseline" radius={[6, 6, 0, 0]} />
          <Bar dataKey="after" fill="url(#gradAfter)" name="After Tuning" radius={[6, 6, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
