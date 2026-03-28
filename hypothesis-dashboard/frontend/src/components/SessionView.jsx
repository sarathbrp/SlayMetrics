import React from 'react'
import BenchmarkChart from './BenchmarkChart'
import IterationTimeline from './IterationTimeline'

const fmtRps = (v) => {
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(0)}K`
  return v?.toFixed(0) || '0'
}

export default function SessionView({ sessions, selectedSession, onSelectSession, data }) {
  const report = data?.report || {}
  const profile = report.profile || {}
  const tokens = report.tokens || {}
  const iterations = data?.iterations || []
  const fixes = report.fixes_applied || []

  return (
    <div className="space-y-6">
      {/* Session selector + header */}
      <div className="glass-card p-5 animate-slide-up">
        <div className="flex items-center gap-4 mb-5">
          <label className="text-sm font-medium" style={{ color: 'var(--text-muted)' }}>Session:</label>
          <select
            value={selectedSession || ''}
            onChange={(e) => onSelectSession(e.target.value)}
            className="styled-select rounded-xl px-4 py-2 text-sm font-medium transition-all duration-200 focus:outline-none focus:ring-2 focus:ring-emerald-400/50"
            style={{
              background: 'var(--bg-card)',
              color: 'var(--text-primary)',
              border: '1px solid var(--border-card)',
            }}
          >
            {sessions.map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.session_id} -- {s.timestamp ? new Date(s.timestamp).toLocaleString() : 'no timestamp'}
                {s.improvement_pct ? ` (+${s.improvement_pct.toFixed(1)}%)` : ''}
              </option>
            ))}
          </select>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4">
          <Stat label="Session" value={data?.session_id || '--'} mono />
          <Stat label="Host" value={profile.host || '--'} />
          <Stat label="CPU" value={profile.cpu_cores ? `${profile.cpu_cores} cores` : '--'} />
          <Stat label="RAM" value={profile.ram_gb ? `${profile.ram_gb} GB` : '--'} />
          <Stat label="LLM" value={profile.llm_profile || '--'} />
          <Stat label="Tokens" value={tokens.total?.toLocaleString() || '--'} />
          <Stat label="Iterations" value={iterations.length || '--'} />
          <Stat
            label="Improvement"
            value={report.total_improvement_pct ? `+${report.total_improvement_pct.toFixed(1)}%` : '--'}
            highlight="green"
          />
          <Stat label="Baseline RPS" value={fmtRps(report.baseline_rps)} />
          <Stat label="Best RPS" value={fmtRps(report.best_rps)} highlight="green" />
        </div>
      </div>

      {/* Benchmark chart */}
      <div className="animate-slide-up" style={{ animationDelay: '100ms' }}>
        <BenchmarkChart
          baselines={report.baselines_by_size}
          finals={report.finals_by_size}
          title="Benchmark: Baseline vs After Tuning"
        />
      </div>

      {/* Iteration timeline */}
      <div className="animate-slide-up" style={{ animationDelay: '200ms' }}>
        <IterationTimeline iterations={iterations} />
      </div>

      {/* Applied fixes table */}
      {fixes.length > 0 && (
        <div className="glass-card p-5 animate-slide-up" style={{ animationDelay: '300ms' }}>
          <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>
            Applied Fixes
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm styled-table">
              <thead>
                <tr style={{ borderBottom: '1px solid var(--table-border)' }}>
                  <th className="text-left py-2.5 px-2">Parameter</th>
                  <th className="text-left py-2.5 px-2">Before</th>
                  <th className="text-left py-2.5 px-2">After</th>
                  <th className="text-right py-2.5 px-2">Impact</th>
                  <th className="text-left py-2.5 px-2">Reasoning</th>
                </tr>
              </thead>
              <tbody>
                {fixes.map((fix, i) => (
                  <tr key={i} className="stagger-item">
                    <td className="py-2.5 px-2 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                      {fix.parameter}
                    </td>
                    <td className="py-2.5 px-2 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
                      {fix.before_value || '--'}
                    </td>
                    <td className="py-2.5 px-2 font-mono text-xs text-emerald-400 font-medium">
                      {fix.after_value || '--'}
                    </td>
                    <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                      {fix.impact_pct != null ? (
                        <span className={fix.impact_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                          {fix.impact_pct >= 0 ? '+' : ''}{fix.impact_pct.toFixed(1)}%
                        </span>
                      ) : '--'}
                    </td>
                    <td className="py-2.5 px-2 text-xs max-w-xs truncate" style={{ color: 'var(--text-secondary)' }}>
                      {fix.reasoning}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Token usage */}
      {tokens.total > 0 && (
        <div className="glass-card p-5 animate-slide-up" style={{ animationDelay: '400ms' }}>
          <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>
            Token Usage
          </h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Stat label="Input" value={tokens.input?.toLocaleString()} />
            <Stat label="Output" value={tokens.output?.toLocaleString()} />
            <Stat label="Total" value={tokens.total?.toLocaleString()} highlight="purple" />
            <Stat label="Tool Calls" value={tokens.tool_calls} />
          </div>
          {/* Token distribution bar */}
          <div className="mt-4 pt-4" style={{ borderTop: '1px solid var(--table-border)' }}>
            <div className="flex items-center gap-3 text-xs" style={{ color: 'var(--text-muted)' }}>
              <span>Input/Output ratio:</span>
              <div className="flex-1 h-3 rounded-full overflow-hidden" style={{ background: 'var(--progress-bg)' }}>
                <div
                  className="h-full rounded-full progress-bar-animated"
                  style={{
                    width: `${tokens.input && tokens.total ? (tokens.input / tokens.total * 100) : 50}%`,
                    background: 'linear-gradient(90deg, #8b5cf6, #06b6d4)',
                  }}
                />
              </div>
              <span className="font-mono">
                {tokens.input && tokens.output ? `${(tokens.input / tokens.output).toFixed(1)}:1` : '--'}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, highlight, mono }) {
  const getValueStyle = () => {
    if (highlight === 'green') return { background: 'var(--accent-gradient)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', backgroundClip: 'text' }
    if (highlight === 'purple') return { background: 'var(--accent-gradient-purple)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', backgroundClip: 'text' }
    return { color: 'var(--text-primary)' }
  }

  return (
    <div className="gradient-border p-3">
      <p className="text-[0.65rem] uppercase tracking-wider font-medium mb-1" style={{ color: 'var(--text-muted)' }}>
        {label}
      </p>
      <p
        className={`text-sm font-semibold ${mono ? 'font-mono' : ''}`}
        style={getValueStyle()}
      >
        {value}
      </p>
    </div>
  )
}
