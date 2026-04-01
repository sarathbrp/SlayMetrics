import React, { useMemo, useState } from 'react'

const STATE_STYLES = {
  applied: { label: 'Applied', bg: 'rgba(16,185,129,0.18)', color: '#34d399', border: 'rgba(16,185,129,0.35)' },
  rejected: { label: 'Rejected', bg: 'rgba(244,63,94,0.16)', color: '#fb7185', border: 'rgba(244,63,94,0.35)' },
  recommended: { label: 'Recommended', bg: 'rgba(245,158,11,0.16)', color: '#fbbf24', border: 'rgba(245,158,11,0.35)' },
  none: { label: 'Not seen', bg: 'rgba(148,163,184,0.08)', color: 'var(--text-muted)', border: 'rgba(148,163,184,0.18)' },
}

const AGENT_SOURCE_STYLES = {
  nginx_expert: { label: 'Nginx', color: '#38bdf8', bg: 'rgba(56,189,248,0.14)', border: 'rgba(56,189,248,0.3)' },
  rhel_expert: { label: 'RHEL', color: '#f59e0b', bg: 'rgba(245,158,11,0.14)', border: 'rgba(245,158,11,0.3)' },
  synthesizer: { label: 'Synth', color: '#a78bfa', bg: 'rgba(167,139,250,0.14)', border: 'rgba(167,139,250,0.3)' },
}

const fmtPct = (value) => {
  const num = Number(value || 0)
  return `${num >= 0 ? '+' : ''}${num.toFixed(1)}%`
}

function SummaryStat({ label, value, accent }) {
  return (
    <div className="gradient-border p-3">
      <p className="text-[0.65rem] uppercase tracking-wider font-medium mb-1" style={{ color: 'var(--text-muted)' }}>
        {label}
      </p>
      <p
        className="text-sm font-semibold"
        style={accent ? { color: accent } : { color: 'var(--text-primary)' }}
      >
        {value}
      </p>
    </div>
  )
}

function TemperatureBadge({ value }) {
  const style = value === 'hot'
    ? { color: '#34d399', background: 'rgba(16,185,129,0.14)', border: '1px solid rgba(16,185,129,0.3)' }
    : value === 'cold'
      ? { color: '#fb7185', background: 'rgba(244,63,94,0.14)', border: '1px solid rgba(244,63,94,0.3)' }
      : { color: '#fbbf24', background: 'rgba(245,158,11,0.14)', border: '1px solid rgba(245,158,11,0.3)' }

  return (
    <span className="pill-badge uppercase text-[0.65rem] tracking-wider" style={style}>
      {value}
    </span>
  )
}

function ParameterTable({ title, rows }) {
  return (
    <div className="glass-card p-5 animate-slide-up">
      <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>
        {title}
      </h3>
      <div className="overflow-x-auto">
        <table className="w-full text-sm styled-table">
          <thead>
            <tr style={{ borderBottom: '1px solid var(--table-border)' }}>
              <th className="text-left py-2.5 px-2">Parameter</th>
              <th className="text-left py-2.5 px-2">Scope</th>
              <th className="text-right py-2.5 px-2">Seen</th>
              <th className="text-right py-2.5 px-2">Applied</th>
              <th className="text-right py-2.5 px-2">Rejected</th>
              <th className="text-left py-2.5 px-2">Source</th>
              <th className="text-right py-2.5 px-2">Acceptance</th>
              <th className="text-right py-2.5 px-2">Avg Impact</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <tr key={item.parameter}>
                <td className="py-2.5 px-2 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                  {item.parameter}
                </td>
                <td className="py-2.5 px-2" style={{ color: 'var(--text-secondary)' }}>
                  {item.scope}
                </td>
                <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                  {item.sessions_seen}
                </td>
                <td className="py-2.5 px-2 text-right text-emerald-400">
                  {item.applied_sessions}
                </td>
                <td className="py-2.5 px-2 text-right text-rose-400">
                  {item.rejected_sessions}
                </td>
                <td className="py-2.5 px-2 text-xs">
                  <SourceBadges sources={item.source_agents} />
                </td>
                <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                  {(item.acceptance_rate * 100).toFixed(0)}%
                </td>
                <td className="py-2.5 px-2 text-right font-medium" style={{ color: 'var(--text-primary)' }}>
                  {fmtPct(item.avg_improvement_when_applied)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function ParameterView({ data }) {
  const [scope, setScope] = useState('all')
  const [search, setSearch] = useState('')
  const [sessionSort, setSessionSort] = useState('impact')
  const [sessionLimit, setSessionLimit] = useState(12)

  const parameters = data?.parameters || []
  const summary = data?.summary || {}
  const sessions = data?.sessions || []
  const matrix = data?.matrix || []

  const visibleSessions = useMemo(() => {
    const rows = [...sessions]
    if (sessionSort === 'recent') {
      rows.sort((a, b) => String(b.timestamp || '').localeCompare(String(a.timestamp || '')))
    } else {
      rows.sort((a, b) => (Number(b.improvement_pct || 0) - Number(a.improvement_pct || 0)))
    }
    return rows.slice(0, sessionLimit)
  }, [sessions, sessionSort, sessionLimit])

  const filtered = useMemo(() => {
    return parameters.filter((item) => {
      const scopeMatch = scope === 'all' || item.scope === scope
      const searchMatch = !search || item.parameter.toLowerCase().includes(search.toLowerCase())
      return scopeMatch && searchMatch
    })
  }, [parameters, scope, search])

  const hot = filtered.filter((item) => item.temperature === 'hot').slice(0, 10)
  const cold = filtered.filter((item) => item.temperature === 'cold').slice(0, 10)
  const mixed = filtered.filter((item) => item.temperature === 'mixed').slice(0, 12)
  const filteredMatrix = matrix
    .filter((item) => filtered.some((row) => row.parameter === item.parameter))
    .slice(0, 25)
    .map((row) => ({
      ...row,
      sessions: row.sessions.filter((cell) => visibleSessions.some((session) => session.session_id === cell.session_id)),
    }))

  if (!data) {
    return (
      <div className="glass-card p-12 text-center animate-fade-in" style={{ color: 'var(--text-muted)' }}>
        <div className="text-4xl mb-3">⏳</div>
        <div className="text-lg font-medium">Loading parameter evidence...</div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="glass-card p-5 animate-slide-up">
        <div className="flex items-start justify-between gap-6 flex-wrap mb-5">
          <div>
            <h3 className="text-lg font-semibold mb-2" style={{ color: 'var(--text-primary)' }}>
              Cross-Session Parameter Intelligence
            </h3>
            <p className="text-sm max-w-3xl" style={{ color: 'var(--text-secondary)' }}>
              Every folder in <span className="font-mono">hypothesis/</span> is aggregated automatically. Applied, recommended, and rejected parameter states are folded into a single evidence model.
            </p>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <select
              value={scope}
              onChange={(e) => setScope(e.target.value)}
              className="styled-select rounded-xl px-4 py-2 text-sm font-medium focus:outline-none focus:ring-2 focus:ring-emerald-400/50"
              style={{
                background: 'var(--bg-card)',
                color: 'var(--text-primary)',
                border: '1px solid var(--border-card)',
              }}
            >
              <option value="all">All scopes</option>
              <option value="nginx">nginx</option>
              <option value="system">system</option>
            </select>
            <select
              value={sessionSort}
              onChange={(e) => setSessionSort(e.target.value)}
              className="styled-select rounded-xl px-4 py-2 text-sm font-medium focus:outline-none focus:ring-2 focus:ring-emerald-400/50"
              style={{
                background: 'var(--bg-card)',
                color: 'var(--text-primary)',
                border: '1px solid var(--border-card)',
              }}
            >
              <option value="impact">Top impact sessions</option>
              <option value="recent">Most recent sessions</option>
            </select>
            <select
              value={sessionLimit}
              onChange={(e) => setSessionLimit(Number(e.target.value))}
              className="styled-select rounded-xl px-4 py-2 text-sm font-medium focus:outline-none focus:ring-2 focus:ring-emerald-400/50"
              style={{
                background: 'var(--bg-card)',
                color: 'var(--text-primary)',
                border: '1px solid var(--border-card)',
              }}
            >
              <option value={8}>8 sessions</option>
              <option value={12}>12 sessions</option>
              <option value={16}>16 sessions</option>
              <option value={20}>20 sessions</option>
            </select>
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter parameters"
              className="rounded-xl px-4 py-2 text-sm"
              style={{
                background: 'var(--bg-card)',
                color: 'var(--text-primary)',
                border: '1px solid var(--border-card)',
              }}
            />
          </div>
        </div>

        <div className="flex items-center gap-3 flex-wrap mb-4 text-xs" style={{ color: 'var(--text-secondary)' }}>
          <span>Source Legend:</span>
          <SourceBadges sources={Object.keys(AGENT_SOURCE_STYLES)} />
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <SummaryStat label="Sessions" value={summary.session_count || 0} />
          <SummaryStat label="Parameters" value={summary.parameter_count || 0} />
          <SummaryStat label="Hot" value={summary.hot_count || 0} accent="#34d399" />
          <SummaryStat label="Cold" value={summary.cold_count || 0} accent="#fb7185" />
        </div>
      </div>

      <div className="grid lg:grid-cols-2 gap-6">
        <ParameterTable title="Hot Parameters" rows={hot} />
        <ParameterTable title="Cold Parameters" rows={cold} />
      </div>

      <div className="glass-card p-5 animate-slide-up">
        <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>
          Parameter Rankings
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm styled-table">
            <thead>
              <tr style={{ borderBottom: '1px solid var(--table-border)' }}>
                <th className="text-left py-2.5 px-2">Parameter</th>
                <th className="text-left py-2.5 px-2">Temp</th>
                <th className="text-right py-2.5 px-2">Score</th>
                <th className="text-right py-2.5 px-2">Seen</th>
                <th className="text-right py-2.5 px-2">Applied</th>
                <th className="text-right py-2.5 px-2">Rejected</th>
                <th className="text-left py-2.5 px-2">Source</th>
                <th className="text-right py-2.5 px-2">Avg Applied Impact</th>
                <th className="text-left py-2.5 px-2">Values</th>
              </tr>
            </thead>
            <tbody>
              {mixed.map((item) => (
                <tr key={item.parameter}>
                  <td className="py-2.5 px-2 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                    {item.parameter}
                  </td>
                  <td className="py-2.5 px-2"><TemperatureBadge value={item.temperature} /></td>
                  <td className="py-2.5 px-2 text-right font-medium" style={{ color: 'var(--text-primary)' }}>
                    {item.score.toFixed(1)}
                  </td>
                  <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>{item.sessions_seen}</td>
                  <td className="py-2.5 px-2 text-right text-emerald-400">{item.applied_sessions}</td>
                  <td className="py-2.5 px-2 text-right text-rose-400">{item.rejected_sessions}</td>
                  <td className="py-2.5 px-2 text-xs"><SourceBadges sources={item.source_agents} /></td>
                  <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                    {fmtPct(item.avg_improvement_when_applied)}
                  </td>
                  <td className="py-2.5 px-2 text-xs" style={{ color: 'var(--text-secondary)' }}>
                    {(item.values || []).slice(0, 3).join(', ') || '--'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="glass-card p-5 animate-slide-up">
        <h3 className="text-lg font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>
          Session-Parameter Matrix
        </h3>
        <div className="flex items-center gap-3 flex-wrap mb-4 text-xs" style={{ color: 'var(--text-secondary)' }}>
          <span>Showing {visibleSessions.length} sessions and top 25 filtered parameters.</span>
          {Object.entries(STATE_STYLES).map(([key, style]) => (
            <span key={key} className="inline-flex items-center gap-2">
              <span
                className="inline-block w-3 h-3 rounded-sm"
                style={{ background: style.bg, border: `1px solid ${style.border}` }}
              />
              {style.label}
            </span>
          ))}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm styled-table">
            <thead>
              <tr style={{ borderBottom: '1px solid var(--table-border)' }}>
                <th className="text-left py-2.5 px-2 min-w-[220px]">Parameter</th>
                <th className="text-left py-2.5 px-2">Scope</th>
                {visibleSessions.map((session) => (
                  <th key={session.session_id} className="text-center py-2.5 px-2 min-w-[96px]">
                    <div className="font-mono text-xs" style={{ color: 'var(--text-primary)' }}>{session.session_id}</div>
                    <div className="text-[0.65rem] font-normal" style={{ color: 'var(--text-muted)' }}>
                      {fmtPct(session.improvement_pct)}
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filteredMatrix.map((row) => (
                <tr key={row.parameter}>
                  <td className="py-2.5 px-2 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                    {row.parameter}
                  </td>
                  <td className="py-2.5 px-2" style={{ color: 'var(--text-secondary)' }}>
                    {row.scope}
                  </td>
                  {row.sessions.map((cell) => {
                    const style = STATE_STYLES[cell.state] || STATE_STYLES.none
                    return (
                      <td key={`${row.parameter}-${cell.session_id}`} className="py-2 px-2 text-center">
                        <span
                          className="inline-flex items-center justify-center rounded-md text-[0.65rem] font-medium uppercase tracking-wider"
                          style={{
                            width: '1.65rem',
                            height: '1.65rem',
                            background: style.bg,
                            color: style.color,
                            border: `1px solid ${style.border}`,
                          }}
                          title={`${cell.session_id}: ${style.label} (${fmtPct(cell.improvement_pct)})`}
                        >
                          {cell.state === 'applied' ? 'A' : cell.state === 'rejected' ? 'R' : cell.state === 'recommended' ? 'S' : '·'}
                        </span>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function SourceBadges({ sources }) {
  if (!sources || sources.length === 0) return <span style={{ color: 'var(--text-muted)' }}>--</span>
  return (
    <div className="flex flex-wrap gap-1">
      {sources.map((source) => {
        const style = AGENT_SOURCE_STYLES[source]
        if (!style) return null
        return (
          <span
            key={source}
            className="pill-badge"
            style={{ color: style.color, background: style.bg, border: `1px solid ${style.border}`, padding: '0.15rem 0.5rem' }}
          >
            {style.label}
          </span>
        )
      })}
    </div>
  )
}
