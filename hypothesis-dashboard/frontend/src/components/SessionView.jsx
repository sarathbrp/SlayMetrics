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
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <div className="flex items-center gap-4 mb-4">
          <label className="text-sm text-gray-400">Session:</label>
          <select
            value={selectedSession || ''}
            onChange={(e) => onSelectSession(e.target.value)}
            className="bg-gray-800 text-gray-200 border border-gray-700 rounded px-3 py-1.5 text-sm"
          >
            {sessions.map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.session_id} — {s.timestamp ? new Date(s.timestamp).toLocaleString() : 'no timestamp'}
                {s.improvement_pct ? ` (+${s.improvement_pct.toFixed(1)}%)` : ''}
              </option>
            ))}
          </select>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
          <Stat label="Session" value={data?.session_id || '—'} />
          <Stat label="Host" value={profile.host || '—'} />
          <Stat label="CPU" value={profile.cpu_cores ? `${profile.cpu_cores} cores` : '—'} />
          <Stat label="RAM" value={profile.ram_gb ? `${profile.ram_gb} GB` : '—'} />
          <Stat label="LLM" value={profile.llm_profile || '—'} />
          <Stat label="Tokens" value={tokens.total?.toLocaleString() || '—'} />
          <Stat label="Iterations" value={iterations.length || '—'} />
          <Stat
            label="Improvement"
            value={report.total_improvement_pct ? `+${report.total_improvement_pct.toFixed(1)}%` : '—'}
            highlight
          />
          <Stat label="Baseline RPS" value={fmtRps(report.baseline_rps)} />
          <Stat label="Best RPS" value={fmtRps(report.best_rps)} highlight />
        </div>
      </div>

      {/* Benchmark chart */}
      <BenchmarkChart
        baselines={report.baselines_by_size}
        finals={report.finals_by_size}
        title="Benchmark: Baseline vs After Tuning"
      />

      {/* Iteration timeline */}
      <IterationTimeline iterations={iterations} />

      {/* Applied fixes table */}
      {fixes.length > 0 && (
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <h3 className="text-lg font-semibold text-gray-200 mb-3">Applied Fixes</h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-400 border-b border-gray-700">
                <th className="text-left py-2">Parameter</th>
                <th className="text-left py-2">Before</th>
                <th className="text-left py-2">After</th>
                <th className="text-right py-2">Impact</th>
                <th className="text-left py-2">Reasoning</th>
              </tr>
            </thead>
            <tbody>
              {fixes.map((fix, i) => (
                <tr key={i} className="border-b border-gray-800">
                  <td className="py-2 text-gray-200 font-mono text-xs">{fix.parameter}</td>
                  <td className="py-2 text-gray-400 font-mono text-xs">{fix.before_value || '—'}</td>
                  <td className="py-2 text-emerald-400 font-mono text-xs">{fix.after_value || '—'}</td>
                  <td className="py-2 text-right text-gray-400">
                    {fix.impact_pct != null ? `${fix.impact_pct.toFixed(1)}%` : '—'}
                  </td>
                  <td className="py-2 text-gray-400 text-xs max-w-xs truncate">{fix.reasoning}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Token usage */}
      {tokens.total > 0 && (
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <h3 className="text-lg font-semibold text-gray-200 mb-3">Token Usage</h3>
          <div className="grid grid-cols-4 gap-4">
            <Stat label="Input" value={tokens.input?.toLocaleString()} />
            <Stat label="Output" value={tokens.output?.toLocaleString()} />
            <Stat label="Total" value={tokens.total?.toLocaleString()} highlight />
            <Stat label="Tool Calls" value={tokens.tool_calls} />
          </div>
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, highlight }) {
  return (
    <div>
      <p className="text-xs text-gray-500 uppercase tracking-wide">{label}</p>
      <p className={`text-sm font-semibold ${highlight ? 'text-emerald-400' : 'text-gray-200'}`}>
        {value}
      </p>
    </div>
  )
}
