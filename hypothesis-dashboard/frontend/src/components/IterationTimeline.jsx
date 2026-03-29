import React, { useState } from 'react'

function StatusBadge({ status }) {
  if (status === 'OK') return <span className="pill-badge pill-ok text-xs">✓ OK</span>
  if (status === 'REGRESSED') return <span className="pill-badge pill-regressed text-xs">✗ REGRESSED</span>
  return <span className="pill-badge pill-neutral text-xs">{status}</span>
}

function DecisionBadge({ decision }) {
  if (!decision) return null
  const isOk = decision.includes('OK') || decision.includes('stopping')
  const isMax = decision.includes('Max iterations')
  const cls = isOk && !isMax ? 'pill-ok' : isMax ? 'pill-warn' : 'pill-regressed'
  return <span className={`pill-badge ${cls}`}>{decision}</span>
}

function AgentSection({ title, data }) {
  const [open, setOpen] = useState(false)
  if (!data || (!data.summary && !data.payload)) return null

  return (
    <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--table-border)' }}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 text-sm font-medium transition-all duration-200 hover:opacity-80 group"
        style={{ color: 'var(--text-secondary)' }}
      >
        <span className={`transform transition-transform duration-200 text-xs ${open ? 'rotate-90' : ''}`}>
          ▶
        </span>
        <span className="font-mono text-xs px-2 py-0.5 rounded-md" style={{ background: 'var(--progress-bg)' }}>
          {title}
        </span>
      </button>
      {open && (
        <div className="mt-3 pl-5 animate-fade-in">
          {data.summary && (
            <p className="text-sm mb-2" style={{ color: 'var(--text-secondary)' }}>{data.summary}</p>
          )}
          {data.payload && Object.keys(data.payload).length > 0 && (
            <pre
              className="text-xs font-mono p-4 rounded-xl overflow-x-auto max-h-64 overflow-y-auto"
              style={{ background: 'var(--code-bg)', color: 'var(--text-secondary)' }}
            >
              {JSON.stringify(data.payload, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

const fmtRps = (v) => {
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(0)}K`
  return v.toFixed(0)
}

export default function IterationTimeline({ iterations }) {
  if (!iterations || iterations.length === 0) return null

  return (
    <div className="space-y-4">
      <h3 className="text-lg font-semibold" style={{ color: 'var(--text-primary)' }}>
        Iteration Timeline
      </h3>
      {iterations.map((iter, idx) => {
        const summary = iter.summary || {}
        const benchmarks = summary.benchmarks || []
        const decision = summary.decision || ''

        return (
          <div
            key={iter.iteration}
            className="glass-card p-5 stagger-item"
            style={{ animationDelay: `${idx * 80}ms` }}
          >
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-3">
                <div
                  className="w-8 h-8 rounded-lg flex items-center justify-center text-white text-sm font-bold"
                  style={{ background: 'var(--accent-gradient)' }}
                >
                  {iter.iteration}
                </div>
                <h4 className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                  Iteration {iter.iteration}
                </h4>
              </div>
              <DecisionBadge decision={decision} />
            </div>

            {/* Benchmark table */}
            {benchmarks.length > 0 && (
              <div className="overflow-x-auto mb-3">
                <table className="w-full text-sm styled-table">
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--table-border)' }}>
                      <th className="text-left py-2 px-2">Workload</th>
                      <th className="text-right py-2 px-2">Baseline</th>
                      <th className="text-right py-2 px-2">Current</th>
                      <th className="text-right py-2 px-2">Change</th>
                      <th className="text-right py-2 px-2">p99</th>
                      <th className="text-right py-2 px-2">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {benchmarks.map((b) => (
                      <tr key={b.workload}>
                        <td className="py-2 px-2" style={{ color: 'var(--text-secondary)' }}>{b.workload}</td>
                        <td className="py-2 px-2 text-right font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
                          {fmtRps(b.baseline_rps)}
                        </td>
                        <td className="py-2 px-2 text-right font-mono text-xs font-medium" style={{ color: 'var(--text-primary)' }}>
                          {fmtRps(b.current_rps)}
                        </td>
                        <td className="py-2 px-2 text-right font-mono text-xs">
                          <span className={b.change?.includes('-') && !b.change?.includes('-0') ? 'text-red-400' : 'text-emerald-400'}>
                            {b.change}
                          </span>
                        </td>
                        <td className="py-2 px-2 text-right font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
                          {b.p99_ms?.toFixed(1)}ms
                        </td>
                        <td className="py-2 px-2 text-right"><StatusBadge status={b.status} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Agent analyses */}
            <AgentSection title="nginx_expert" data={iter.nginx_expert} />
            <AgentSection title="rhel_expert" data={iter.rhel_expert} />
            <AgentSection title="synthesizer" data={iter.synthesizer} />
            <AgentSection title="apply_planner" data={iter.apply_planner} />

            {/* Applied changes */}
            {summary.applied_changes?.length > 0 && (
              <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--table-border)' }}>
                <p className="text-xs font-medium mb-2" style={{ color: 'var(--text-muted)' }}>Applied:</p>
                <div className="flex flex-wrap gap-2">
                  {summary.applied_changes.map((c, i) => (
                    <span
                      key={i}
                      className="px-3 py-1 text-xs font-medium rounded-lg transition-all duration-200 hover:scale-105"
                      style={{
                        background: 'var(--progress-bg)',
                        color: 'var(--text-secondary)',
                        border: '1px solid var(--border-card)',
                      }}
                    >
                      {c.title}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
