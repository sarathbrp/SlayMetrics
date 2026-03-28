import React from 'react'
import { BarChart, Bar, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid } from 'recharts'

const fmt = (v) => {
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(0)}K`
  return v.toFixed(0)
}

const pctChange = (baseline, after) => {
  if (!baseline) return ''
  const pct = ((after - baseline) / baseline) * 100
  return pct >= 0 ? `+${pct.toFixed(1)}%` : `${pct.toFixed(1)}%`
}

function MiniChart({ data, title, subtitle }) {
  if (!data || data.length === 0) return null

  return (
    <div className="flex-1 min-w-0">
      <div className="flex items-baseline gap-2 mb-3">
        <h4 className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>{title}</h4>
        {subtitle && <span className="text-xs" style={{ color: 'var(--text-muted)' }}>{subtitle}</span>}
      </div>
      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={data} barGap={4}>
          <defs>
            <linearGradient id={`gradBase-${title}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#64748b" stopOpacity={0.8} />
              <stop offset="100%" stopColor="#64748b" stopOpacity={0.3} />
            </linearGradient>
            <linearGradient id={`gradAfter-${title}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10b981" stopOpacity={1} />
              <stop offset="100%" stopColor="#06b6d4" stopOpacity={0.7} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
          <XAxis dataKey="workload" stroke="var(--text-muted)" tick={{ fontSize: 11 }} />
          <YAxis tickFormatter={fmt} stroke="var(--text-muted)" tick={{ fontSize: 11 }} width={55} />
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
          <Legend wrapperStyle={{ fontSize: '11px', color: 'var(--text-secondary)' }} />
          <Bar dataKey="baseline" fill={`url(#gradBase-${title})`} name="Baseline" radius={[6, 6, 0, 0]} />
          <Bar dataKey="after" fill={`url(#gradAfter-${title})`} name="After Tuning" radius={[6, 6, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

export default function BenchmarkChart({ baselines, finals, title }) {
  if (!baselines || !finals) return null

  const allWorkloads = ['homepage', 'small', 'medium', 'large', 'mixed']
  const allData = allWorkloads
    .filter(w => baselines[w] || finals[w])
    .map(w => ({
      workload: w,
      baseline: baselines[w]?.rps || 0,
      after: finals[w]?.rps || 0,
    }))

  // Split into high-RPS (CPU-bound) and low-RPS (NIC-bound) groups
  const maxRps = Math.max(...allData.map(d => Math.max(d.baseline, d.after)))
  const minRps = Math.min(...allData.filter(d => d.baseline > 0 || d.after > 0).map(d => Math.max(d.baseline, d.after)))
  const needsSplit = maxRps > 0 && minRps > 0 && maxRps / minRps > 50

  const highRps = allData.filter(d => Math.max(d.baseline, d.after) > maxRps * 0.1)
  const lowRps = allData.filter(d => Math.max(d.baseline, d.after) <= maxRps * 0.1 && (d.baseline > 0 || d.after > 0))

  return (
    <div className="glass-card p-5">
      <h3 className="text-lg font-semibold mb-2" style={{ color: 'var(--text-primary)' }}>
        {title || 'Benchmark Results'}
      </h3>

      {/* Summary cards */}
      <div className="grid grid-cols-5 gap-2 mb-4">
        {allData.map(d => (
          <div key={d.workload} className="gradient-border p-2.5 text-center">
            <p className="text-[0.6rem] uppercase tracking-wider font-medium" style={{ color: 'var(--text-muted)' }}>
              {d.workload}
            </p>
            <p className="text-sm font-bold font-mono" style={{ color: 'var(--text-primary)' }}>
              {fmt(d.after)}
            </p>
            <p className={`text-xs font-semibold ${d.after >= d.baseline ? 'text-emerald-400' : 'text-red-400'}`}>
              {pctChange(d.baseline, d.after)}
            </p>
          </div>
        ))}
      </div>

      {needsSplit ? (
        <div className="flex gap-4">
          <MiniChart data={highRps} title="CPU-bound workloads" subtitle="homepage, small" />
          <MiniChart data={lowRps} title="NIC-bound workloads" subtitle="medium, large, mixed" />
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={300}>
          <BarChart data={allData} barGap={4}>
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
      )}
    </div>
  )
}
