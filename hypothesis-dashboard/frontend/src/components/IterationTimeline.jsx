import React, { useState } from 'react'

function StatusBadge({ status }) {
  if (status === 'OK') return <span className="text-emerald-400 font-medium">✓ OK</span>
  if (status === 'REGRESSED') return <span className="text-red-400 font-medium">✗ REGRESSED</span>
  return <span className="text-gray-400">{status}</span>
}

function DecisionBadge({ decision }) {
  if (!decision) return null
  const isOk = decision.includes('OK') || decision.includes('stopping')
  const isMax = decision.includes('Max iterations')
  const color = isOk && !isMax ? 'bg-emerald-900 text-emerald-300 border-emerald-700'
    : isMax ? 'bg-yellow-900 text-yellow-300 border-yellow-700'
    : 'bg-red-900 text-red-300 border-red-700'
  return <span className={`px-3 py-1 rounded-full text-xs font-medium border ${color}`}>{decision}</span>
}

function AgentSection({ title, data }) {
  const [open, setOpen] = useState(false)
  if (!data || (!data.summary && !data.payload)) return null

  return (
    <div className="border-t border-gray-700 mt-2 pt-2">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 text-sm text-gray-400 hover:text-gray-200 transition"
      >
        <span className={`transform transition ${open ? 'rotate-90' : ''}`}>▶</span>
        {title}
      </button>
      {open && (
        <div className="mt-2 pl-4">
          {data.summary && <p className="text-sm text-gray-300 mb-2">{data.summary}</p>}
          {data.payload && Object.keys(data.payload).length > 0 && (
            <pre className="text-xs text-gray-400 bg-gray-800 p-3 rounded overflow-x-auto max-h-64 overflow-y-auto">
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
      <h3 className="text-lg font-semibold text-gray-200">Iteration Timeline</h3>
      {iterations.map((iter) => {
        const summary = iter.summary || {}
        const benchmarks = summary.benchmarks || []
        const decision = summary.decision || ''

        return (
          <div key={iter.iteration} className="bg-gray-900 rounded-lg border border-gray-800 p-4">
            <div className="flex items-center justify-between mb-3">
              <h4 className="font-semibold text-gray-200">Iteration {iter.iteration}</h4>
              <DecisionBadge decision={decision} />
            </div>

            {/* Benchmark table */}
            {benchmarks.length > 0 && (
              <table className="w-full text-sm mb-3">
                <thead>
                  <tr className="text-gray-400 border-b border-gray-700">
                    <th className="text-left py-1">Workload</th>
                    <th className="text-right py-1">Baseline</th>
                    <th className="text-right py-1">Current</th>
                    <th className="text-right py-1">Change</th>
                    <th className="text-right py-1">p99</th>
                    <th className="text-right py-1">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {benchmarks.map((b) => (
                    <tr key={b.workload} className="border-b border-gray-800">
                      <td className="py-1 text-gray-300">{b.workload}</td>
                      <td className="py-1 text-right text-gray-400">{fmtRps(b.baseline_rps)}</td>
                      <td className="py-1 text-right text-gray-200 font-medium">{fmtRps(b.current_rps)}</td>
                      <td className="py-1 text-right">
                        <span className={b.change?.includes('-') && !b.change?.includes('-0') ? 'text-red-400' : 'text-emerald-400'}>
                          {b.change}
                        </span>
                      </td>
                      <td className="py-1 text-right text-gray-400">{b.p99_ms?.toFixed(1)}ms</td>
                      <td className="py-1 text-right"><StatusBadge status={b.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {/* Agent analyses */}
            <AgentSection title="nginx_expert" data={iter.nginx_expert} />
            <AgentSection title="rhel_expert" data={iter.rhel_expert} />
            <AgentSection title="synthesizer" data={iter.synthesizer} />
            <AgentSection title="apply_planner" data={iter.apply_planner} />

            {/* Applied changes */}
            {summary.applied_changes?.length > 0 && (
              <div className="mt-3 pt-2 border-t border-gray-700">
                <p className="text-xs text-gray-400 mb-1">Applied:</p>
                <div className="flex flex-wrap gap-1">
                  {summary.applied_changes.map((c, i) => (
                    <span key={i} className="px-2 py-0.5 bg-gray-800 text-xs text-gray-300 rounded">
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
