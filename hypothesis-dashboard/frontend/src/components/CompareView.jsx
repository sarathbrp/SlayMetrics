import React from 'react'
import { BarChart, Bar, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid } from 'recharts'

const COLORS = ['#10B981', '#06B6D4', '#8B5CF6', '#F43F5E', '#F59E0B']

const fmt = (v) => {
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(0)}K`
  return v?.toFixed(0) || '0'
}

export default function CompareView({ sessions, data }) {
  if (!data || data.length === 0) {
    return (
      <div className="glass-card p-12 text-center animate-fade-in" style={{ color: 'var(--text-muted)' }}>
        <div className="text-4xl mb-3">📭</div>
        <div className="text-lg font-medium">No sessions to compare</div>
        <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>Run at least two sessions to see comparisons</p>
      </div>
    )
  }

  const workloads = ['homepage', 'small', 'medium', 'large', 'mixed']

  const chartData = workloads.map(w => {
    const entry = { workload: w }
    data.forEach((s) => {
      entry[`${s.session_id}_baseline`] = s.baselines_by_size?.[w]?.rps || 0
      entry[s.session_id] = s.finals_by_size?.[w]?.rps || 0
    })
    return entry
  })

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="glass-card p-5 animate-slide-up">
        <h3 className="text-lg font-semibold mb-3" style={{ color: 'var(--text-primary)' }}>
          Comparing {data.length} Sessions
        </h3>
        <div className="flex gap-3 flex-wrap">
          {data.map((s, i) => (
            <span
              key={s.session_id}
              className="pill-badge font-mono"
              style={{
                borderColor: COLORS[i],
                color: COLORS[i],
                background: `${COLORS[i]}15`,
              }}
            >
              {s.session_id}
              {s.improvement_pct ? ` (+${s.improvement_pct.toFixed(0)}%)` : ''}
            </span>
          ))}
        </div>
      </div>

      {/* Final RPS comparison chart */}
      <div className="glass-card p-5 animate-slide-up" style={{ animationDelay: '100ms' }}>
        <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>
          Final RPS by Workload
        </h3>
        <ResponsiveContainer width="100%" height={350}>
          <BarChart data={chartData} barGap={2}>
            <defs>
              {data.map((s, i) => (
                <linearGradient key={s.session_id} id={`grad-${i}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={COLORS[i]} stopOpacity={1} />
                  <stop offset="100%" stopColor={COLORS[i]} stopOpacity={0.6} />
                </linearGradient>
              ))}
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
            {data.map((s, i) => (
              <Bar
                key={s.session_id}
                dataKey={s.session_id}
                fill={`url(#grad-${i})`}
                name={s.session_id}
                radius={[6, 6, 0, 0]}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Summary table */}
      <div className="glass-card p-5 animate-slide-up" style={{ animationDelay: '200ms' }}>
        <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>
          Session Summary
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm styled-table">
            <thead>
              <tr style={{ borderBottom: '1px solid var(--table-border)' }}>
                <th className="text-left py-2.5 px-2">Session</th>
                <th className="text-left py-2.5 px-2">Date</th>
                <th className="text-right py-2.5 px-2">Iters</th>
                <th className="text-right py-2.5 px-2">Small</th>
                <th className="text-right py-2.5 px-2">Medium</th>
                <th className="text-right py-2.5 px-2">Large</th>
                <th className="text-right py-2.5 px-2">Improvement</th>
                <th className="text-right py-2.5 px-2">Tokens</th>
              </tr>
            </thead>
            <tbody>
              {data.map((s, i) => (
                <tr key={s.session_id} className="stagger-item">
                  <td className="py-2.5 px-2">
                    <span className="pill-badge font-mono text-xs"
                      style={{ color: COLORS[i], background: `${COLORS[i]}15`, border: `1px solid ${COLORS[i]}40` }}>
                      {s.session_id}
                    </span>
                  </td>
                  <td className="py-2.5 px-2 text-xs" style={{ color: 'var(--text-muted)' }}>
                    {s.timestamp ? new Date(s.timestamp).toLocaleString() : '--'}
                  </td>
                  <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                    {s.iterations}
                  </td>
                  <td className="py-2.5 px-2 text-right font-medium" style={{ color: 'var(--text-primary)' }}>
                    {fmt(s.finals_by_size?.small?.rps || 0)}
                  </td>
                  <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-primary)' }}>
                    {fmt(s.finals_by_size?.medium?.rps || 0)}
                  </td>
                  <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-primary)' }}>
                    {fmt(s.finals_by_size?.large?.rps || 0)}
                  </td>
                  <td className="py-2.5 px-2 text-right">
                    <span className="gradient-text font-semibold">
                      +{s.improvement_pct?.toFixed(1) || 0}%
                    </span>
                  </td>
                  <td className="py-2.5 px-2 text-right font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
                    {(s.tokens?.total || 0).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Token efficiency */}
      <div className="glass-card p-5 animate-slide-up" style={{ animationDelay: '300ms' }}>
        <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>
          Token Efficiency
        </h3>
        <div className="space-y-3">
          {data.map((s, i) => {
            const total = s.tokens?.total || 0
            const improv = s.improvement_pct || 0
            const efficiency = improv > 0 ? (total / improv).toFixed(1) : '--'
            const best = data.reduce((min, d) => {
              const e = d.tokens?.total && d.improvement_pct > 0
                ? d.tokens.total / d.improvement_pct : Infinity
              return e < min ? e : min
            }, Infinity)
            const isBest = total && improv > 0 && (total / improv) <= best
            const barWidth = Math.min((improv / Math.max(...data.map(d => d.improvement_pct || 1))) * 100, 100)

            return (
              <div key={s.session_id} className="flex items-center gap-3 stagger-item">
                <span className="font-mono text-xs w-20 font-medium" style={{ color: COLORS[i] }}>
                  {s.session_id}
                </span>
                <div className="flex-1 h-5 rounded-full overflow-hidden" style={{ background: 'var(--progress-bg)' }}>
                  <div
                    className="h-full rounded-full progress-bar-animated relative"
                    style={{
                      width: `${barWidth}%`,
                      background: `linear-gradient(90deg, ${COLORS[i]}, ${COLORS[i]}99)`,
                      boxShadow: `0 0 12px ${COLORS[i]}40`,
                    }}
                  />
                </div>
                <span className="text-xs w-36 text-right font-mono" style={{ color: 'var(--text-secondary)' }}>
                  {total.toLocaleString()} tok {'->'} +{improv.toFixed(0)}%
                </span>
                <span className="text-xs w-28 text-right" style={{ color: 'var(--text-muted)' }}>
                  {efficiency} tok/%
                  {isBest && <span className="ml-1 text-amber-400">★ best</span>}
                </span>
              </div>
            )
          })}
        </div>
      </div>

      {/* Iteration history */}
      <div className="glass-card p-5 animate-slide-up" style={{ animationDelay: '400ms' }}>
        <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>
          Iteration History
        </h3>
        <div className="space-y-4">
          {data.map((s, i) => (
            <div key={s.session_id} className="flex items-start gap-3">
              <span className="font-mono text-xs w-20 pt-0.5 font-medium" style={{ color: COLORS[i] }}>
                {s.session_id}
              </span>
              <div className="flex items-center gap-2 flex-wrap">
                {(s.iteration_summaries || []).map((summary, j) => {
                  const hasRegression = summary?.regressions?.length > 0
                  const decision = summary?.decision || ''
                  const isOk = decision.includes('OK')

                  return (
                    <React.Fragment key={j}>
                      <span className={`pill-badge text-xs ${
                        isOk ? 'pill-ok' : hasRegression ? 'pill-regressed' : 'pill-neutral'
                      }`}>
                        iter{j + 1}
                        {isOk && ' ✓'}
                        {hasRegression && ` (${summary.regressions.length} regr.)`}
                      </span>
                      {j < (s.iteration_summaries?.length || 0) - 1 && (
                        <span style={{ color: 'var(--text-muted)' }}>→</span>
                      )}
                    </React.Fragment>
                  )
                })}
                {(s.iteration_summaries || []).length === 0 && (
                  <span className="text-xs" style={{ color: 'var(--text-muted)' }}>no iteration data</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
