import React from 'react'
import { BarChart, Bar, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid } from 'recharts'

const COLORS = ['#10B981', '#3B82F6', '#F59E0B', '#EF4444', '#8B5CF6']

const fmt = (v) => {
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(0)}K`
  return v?.toFixed(0) || '0'
}

export default function CompareView({ sessions, data }) {
  if (!data || data.length === 0) {
    return <div className="text-gray-400 text-center py-12">No sessions to compare</div>
  }

  const workloads = ['homepage', 'small', 'medium', 'large', 'mixed']

  // Build chart data: one entry per workload, bars for each session
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
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-lg font-semibold text-gray-200 mb-2">
          Comparing {data.length} Sessions
        </h3>
        <div className="flex gap-3">
          {data.map((s, i) => (
            <span
              key={s.session_id}
              className="px-3 py-1 rounded-full text-xs font-medium border"
              style={{ borderColor: COLORS[i], color: COLORS[i] }}
            >
              {s.session_id}
              {s.improvement_pct ? ` (+${s.improvement_pct.toFixed(0)}%)` : ''}
            </span>
          ))}
        </div>
      </div>

      {/* Final RPS comparison chart */}
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-lg font-semibold text-gray-200 mb-4">Final RPS by Workload</h3>
        <ResponsiveContainer width="100%" height={350}>
          <BarChart data={chartData} barGap={2}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="workload" stroke="#9CA3AF" />
            <YAxis tickFormatter={fmt} stroke="#9CA3AF" />
            <Tooltip
              contentStyle={{ backgroundColor: '#1F2937', border: '1px solid #374151', borderRadius: '8px' }}
              labelStyle={{ color: '#E5E7EB' }}
              formatter={(v) => [fmt(v) + ' RPS', '']}
            />
            <Legend />
            {data.map((s, i) => (
              <Bar
                key={s.session_id}
                dataKey={s.session_id}
                fill={COLORS[i]}
                name={s.session_id}
                radius={[4, 4, 0, 0]}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Summary table */}
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-lg font-semibold text-gray-200 mb-3">Session Summary</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-400 border-b border-gray-700">
                <th className="text-left py-2">Session</th>
                <th className="text-left py-2">Date</th>
                <th className="text-right py-2">Iterations</th>
                <th className="text-right py-2">Small RPS</th>
                <th className="text-right py-2">Medium RPS</th>
                <th className="text-right py-2">Large RPS</th>
                <th className="text-right py-2">Improvement</th>
                <th className="text-right py-2">Tokens</th>
              </tr>
            </thead>
            <tbody>
              {data.map((s, i) => (
                <tr key={s.session_id} className="border-b border-gray-800">
                  <td className="py-2">
                    <span className="font-mono text-xs px-2 py-0.5 rounded"
                      style={{ color: COLORS[i], backgroundColor: COLORS[i] + '20' }}>
                      {s.session_id}
                    </span>
                  </td>
                  <td className="py-2 text-gray-400 text-xs">
                    {s.timestamp ? new Date(s.timestamp).toLocaleString() : '—'}
                  </td>
                  <td className="py-2 text-right text-gray-300">{s.iterations}</td>
                  <td className="py-2 text-right text-gray-200 font-medium">
                    {fmt(s.finals_by_size?.small?.rps || 0)}
                  </td>
                  <td className="py-2 text-right text-gray-200">
                    {fmt(s.finals_by_size?.medium?.rps || 0)}
                  </td>
                  <td className="py-2 text-right text-gray-200">
                    {fmt(s.finals_by_size?.large?.rps || 0)}
                  </td>
                  <td className="py-2 text-right">
                    <span className="text-emerald-400 font-medium">
                      +{s.improvement_pct?.toFixed(1) || 0}%
                    </span>
                  </td>
                  <td className="py-2 text-right text-gray-400">
                    {(s.tokens?.total || 0).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Token efficiency */}
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-lg font-semibold text-gray-200 mb-3">Token Efficiency</h3>
        <div className="space-y-2">
          {data.map((s, i) => {
            const total = s.tokens?.total || 0
            const improv = s.improvement_pct || 0
            const efficiency = improv > 0 ? (total / improv).toFixed(1) : '—'
            const best = data.reduce((min, d) => {
              const e = d.tokens?.total && d.improvement_pct > 0
                ? d.tokens.total / d.improvement_pct : Infinity
              return e < min ? e : min
            }, Infinity)
            const isBest = total && improv > 0 && (total / improv) <= best

            return (
              <div key={s.session_id} className="flex items-center gap-3">
                <span className="font-mono text-xs w-20" style={{ color: COLORS[i] }}>
                  {s.session_id}
                </span>
                <div className="flex-1 bg-gray-800 rounded-full h-4 overflow-hidden">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${Math.min((improv / Math.max(...data.map(d => d.improvement_pct || 1))) * 100, 100)}%`,
                      backgroundColor: COLORS[i],
                    }}
                  />
                </div>
                <span className="text-xs text-gray-400 w-32 text-right">
                  {total.toLocaleString()} tok → +{improv.toFixed(0)}%
                </span>
                <span className="text-xs text-gray-500 w-24 text-right">
                  {efficiency} tok/%
                  {isBest && <span className="text-yellow-400 ml-1">★</span>}
                </span>
              </div>
            )
          })}
        </div>
      </div>

      {/* Iteration history */}
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-lg font-semibold text-gray-200 mb-3">Iteration History</h3>
        <div className="space-y-3">
          {data.map((s, i) => (
            <div key={s.session_id} className="flex items-start gap-3">
              <span className="font-mono text-xs w-20 pt-0.5" style={{ color: COLORS[i] }}>
                {s.session_id}
              </span>
              <div className="flex items-center gap-2 flex-wrap">
                {(s.iteration_summaries || []).map((summary, j) => {
                  const hasRegression = summary?.regressions?.length > 0
                  const decision = summary?.decision || ''
                  const isOk = decision.includes('OK')
                  const color = isOk ? 'border-emerald-600 text-emerald-400'
                    : hasRegression ? 'border-red-600 text-red-400'
                    : 'border-gray-600 text-gray-400'

                  return (
                    <React.Fragment key={j}>
                      <span className={`px-2 py-0.5 rounded border text-xs ${color}`}>
                        iter{j + 1}
                        {isOk && ' ✓'}
                        {hasRegression && ` (${summary.regressions.length} regr.)`}
                      </span>
                      {j < (s.iteration_summaries?.length || 0) - 1 && (
                        <span className="text-gray-600">→</span>
                      )}
                    </React.Fragment>
                  )
                })}
                {(s.iteration_summaries || []).length === 0 && (
                  <span className="text-xs text-gray-500">no iteration data</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
