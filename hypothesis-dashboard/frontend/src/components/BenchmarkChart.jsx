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
    <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
      <h3 className="text-lg font-semibold text-gray-200 mb-4">{title || 'Benchmark Results'}</h3>
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={data} barGap={4}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
          <XAxis dataKey="workload" stroke="#9CA3AF" />
          <YAxis tickFormatter={fmt} stroke="#9CA3AF" />
          <Tooltip
            contentStyle={{ backgroundColor: '#1F2937', border: '1px solid #374151', borderRadius: '8px' }}
            labelStyle={{ color: '#E5E7EB' }}
            formatter={(v) => [fmt(v) + ' RPS', '']}
          />
          <Legend />
          <Bar dataKey="baseline" fill="#6B7280" name="Baseline" radius={[4, 4, 0, 0]} />
          <Bar dataKey="after" fill="#10B981" name="After Tuning" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
