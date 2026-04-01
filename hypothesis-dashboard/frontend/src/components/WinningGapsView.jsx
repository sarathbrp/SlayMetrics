import React, { useMemo, useState } from 'react'

const PAGE_SIZE = 10
const MAX_ROWS = 30
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
      <p className="text-sm font-semibold" style={accent ? { color: accent } : { color: 'var(--text-primary)' }}>
        {value}
      </p>
    </div>
  )
}

export default function WinningGapsView({ data }) {
  const [page, setPage] = useState(1)
  const [hideExactMatches, setHideExactMatches] = useState(true)
  const [compareSessionId, setCompareSessionId] = useState('')
  const [compareFilter, setCompareFilter] = useState('all')
  const [showReferencePanel, setShowReferencePanel] = useState(false)
  const [customSessionId, setCustomSessionId] = useState('')
  const [pinnedSessionIds, setPinnedSessionIds] = useState([])

  const winningGaps = data?.winning_gaps || {}
  const reference = winningGaps.reference

  const rankedRows = useMemo(() => {
    const rows = [...(winningGaps.sessions || [])]
      .filter((row) => Number(row.best_small_rps || 0) > 0)
      .sort((a, b) => Number(b.best_small_rps || 0) - Number(a.best_small_rps || 0))
      .slice(0, MAX_ROWS)

    const filtered = hideExactMatches
      ? rows.filter((row) => row.missing_count > 0 || row.differing_count > 0)
      : rows

    return filtered
  }, [winningGaps.sessions, hideExactMatches])

  const customCandidates = useMemo(() => {
    return [...(winningGaps.sessions || [])]
      .filter((row) => row.session_id !== reference?.session_id)
      .sort((a, b) => Number(b.best_small_rps || 0) - Number(a.best_small_rps || 0))
  }, [reference?.session_id, winningGaps.sessions])

  const pinnedRows = useMemo(() => {
    const pinned = new Set(pinnedSessionIds)
    return customCandidates.filter((row) => pinned.has(row.session_id))
  }, [customCandidates, pinnedSessionIds])

  const totalPages = Math.max(1, Math.ceil(rankedRows.length / PAGE_SIZE))
  const currentPage = Math.min(page, totalPages)
  const pageRows = rankedRows.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE)
  const compareRow =
    pageRows.find((row) => row.session_id === compareSessionId) ||
    rankedRows.find((row) => row.session_id === compareSessionId) ||
    pinnedRows.find((row) => row.session_id === compareSessionId)
  const compareItems = useMemo(() => {
    if (!compareRow) return []
    const rows = buildCompareRows(reference, compareRow)
    if (compareFilter === 'all') return rows
    return rows.filter((item) => item.status === compareFilter)
  }, [reference, compareRow, compareFilter])
  const compareCounts = useMemo(() => {
    if (!compareRow) return { all: 0, match: 0, different: 0, missing: 0, extra: 0 }
    const rows = buildCompareRows(reference, compareRow)
    const counts = { all: rows.length, match: 0, different: 0, missing: 0, extra: 0 }
    for (const row of rows) {
      counts[row.status] = (counts[row.status] || 0) + 1
    }
    return counts
  }, [reference, compareRow])
  const compareAllItems = useMemo(() => {
    if (!compareRow) return []
    return buildCompareRows(reference, compareRow)
  }, [reference, compareRow])

  const addPinnedSession = () => {
    if (!customSessionId) return
    const requestedIds = customSessionId
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
    setPinnedSessionIds((current) => {
      const next = new Set(current)
      const availableIds = new Set(customCandidates.map((row) => row.session_id))
      for (const sessionId of requestedIds) {
        if (availableIds.has(sessionId)) {
          next.add(sessionId)
        }
      }
      return Array.from(next)
    })
    setCustomSessionId('')
  }

  const removePinnedSession = (sessionId) => {
    setPinnedSessionIds((current) => current.filter((item) => item !== sessionId))
    if (compareSessionId === sessionId) {
      setCompareSessionId('')
      setCompareFilter('all')
    }
  }

  if (!data || !reference) {
    return (
      <div className="glass-card p-12 text-center animate-fade-in" style={{ color: 'var(--text-muted)' }}>
        <div className="text-4xl mb-3">⏳</div>
        <div className="text-lg font-medium">Loading winning-gap analysis...</div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="glass-card p-5 animate-slide-up">
        <div className="flex items-start justify-between gap-6 flex-wrap mb-5">
          <div>
            <h3 className="text-lg font-semibold mb-2" style={{ color: 'var(--text-primary)' }}>
              Winning Gaps
            </h3>
            <p className="text-sm max-w-3xl" style={{ color: 'var(--text-secondary)' }}>
              Compares the strongest observed session against the top 30 positive-RPS sessions. Each page shows 10 sessions at a time so the missing combinations stay readable.
            </p>
          </div>
          <div className="flex items-start gap-4 flex-wrap">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <SummaryStat label="Reference" value={reference.session_id} />
              <SummaryStat label="Best Small RPS" value={Math.round(reference.best_small_rps || 0).toLocaleString()} accent="#34d399" />
              <SummaryStat label="Homepage RPS" value={Math.round(reference.best_homepage_rps || 0).toLocaleString()} />
              <SummaryStat label="Rows" value={`${rankedRows.length}/${MAX_ROWS}`} />
            </div>
            <button
              onClick={() => setShowReferencePanel(true)}
              className="px-4 py-3 rounded-xl text-sm font-medium"
              style={{ background: 'var(--accent-gradient)', color: '#fff' }}
            >
              Show Best Parameters
            </button>
          </div>
        </div>

        <div className="mb-5">
          <p className="text-xs uppercase tracking-wider mb-2" style={{ color: 'var(--text-muted)' }}>
            Reference Winning Set
          </p>
          <div className="flex items-center gap-3 flex-wrap mb-3 text-xs" style={{ color: 'var(--text-secondary)' }}>
            <span>Source Legend:</span>
            <SourceBadges sources={Object.keys(AGENT_SOURCE_STYLES)} />
          </div>
          <div className="flex flex-wrap gap-2">
            {(reference.applied_parameters || []).map((item) => (
              <span
                key={item.parameter}
                className="pill-badge"
                style={{
                  background: 'var(--progress-bg)',
                  color: 'var(--text-secondary)',
                  border: '1px solid var(--border-card)',
                }}
                title={`${item.bundle}: ${item.value}`}
              >
                {item.parameter}={item.value}
              </span>
            ))}
          </div>
        </div>

        <div className="flex items-center justify-between gap-4 flex-wrap mb-4">
          <p className="text-xs" style={{ color: 'var(--text-secondary)' }}>
            Reference session is rank #1. Page {currentPage}: showing ranks {((currentPage - 1) * PAGE_SIZE) + 2}-{Math.min((currentPage * PAGE_SIZE) + 1, rankedRows.length + 1)} of top {rankedRows.length + 1}
          </p>
          <div className="flex items-center gap-3 flex-wrap">
            <button
              onClick={() => setPage(1)}
              className="px-3 py-2 rounded-lg text-sm font-medium"
              style={{ background: currentPage === 1 ? 'var(--accent-gradient)' : 'var(--progress-bg)', color: currentPage === 1 ? '#fff' : 'var(--text-secondary)' }}
            >
              1-10
            </button>
            <button
              onClick={() => setPage(2)}
              className="px-3 py-2 rounded-lg text-sm font-medium"
              style={{ background: currentPage === 2 ? 'var(--accent-gradient)' : 'var(--progress-bg)', color: currentPage === 2 ? '#fff' : 'var(--text-secondary)' }}
            >
              11-20
            </button>
            <button
              onClick={() => setPage(3)}
              className="px-3 py-2 rounded-lg text-sm font-medium"
              style={{ background: currentPage === 3 ? 'var(--accent-gradient)' : 'var(--progress-bg)', color: currentPage === 3 ? '#fff' : 'var(--text-secondary)' }}
            >
              21-30
            </button>
            <label className="inline-flex items-center gap-2 text-xs" style={{ color: 'var(--text-secondary)' }}>
              <input
                type="checkbox"
                checked={hideExactMatches}
                onChange={(e) => {
                  setHideExactMatches(e.target.checked)
                  setPage(1)
                }}
              />
              Hide exact matches
            </label>
          </div>
        </div>

        <div className="mb-5 p-4 rounded-2xl" style={{ background: 'var(--progress-bg)', border: '1px solid var(--border-card)' }}>
          <div className="flex items-center justify-between gap-4 flex-wrap mb-3">
            <div>
              <p className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
                Custom Session Gap Check
              </p>
              <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>
                Type one or more session IDs like `xyz,abc` to add positive-RPS sessions outside the ranked top 30.
              </p>
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <input
                value={customSessionId}
                onChange={(e) => setCustomSessionId(e.target.value)}
                placeholder="Add sessions: xyz,abc"
                className="px-3 py-2 rounded-lg text-sm min-w-[20rem]"
                style={{ background: 'var(--bg-primary)', color: 'var(--text-primary)', border: '1px solid var(--border-card)' }}
              />
              <button
                onClick={addPinnedSession}
                disabled={!customSessionId}
                className="px-3 py-2 rounded-lg text-sm font-medium"
                style={{
                  background: customSessionId ? 'var(--accent-gradient)' : 'var(--progress-bg)',
                  color: customSessionId ? '#fff' : 'var(--text-muted)',
                  opacity: customSessionId ? 1 : 0.8,
                }}
              >
                Add
              </button>
            </div>
          </div>

          {pinnedRows.length > 0 && (
            <div className="mt-4">
              <div className="mb-3">
                <p className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
                  Custom Session Rows
                </p>
                <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>
                  Sessions you entered manually, shown directly below the input.
                </p>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm styled-table">
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--table-border)' }}>
                      <th className="text-left py-2.5 px-2">Session</th>
                      <th className="text-left py-2.5 px-2">Compare</th>
                      <th className="text-right py-2.5 px-2">Small RPS</th>
                      <th className="text-right py-2.5 px-2">% of Ref</th>
                      <th className="text-right py-2.5 px-2">Missing</th>
                      <th className="text-right py-2.5 px-2">Different</th>
                      <th className="text-left py-2.5 px-2">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pinnedRows.map((row) => (
                      <tr key={`pinned-${row.session_id}`}>
                        <td className="py-2.5 px-2">
                          <div className="font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                            {row.session_id}
                          </div>
                          <div className="text-[0.7rem] flex items-center gap-1" style={{ color: 'var(--text-muted)' }}>
                            <BenchmarkTrend value={row.improvement_pct} />
                            <span>{fmtPct(row.improvement_pct)}</span>
                          </div>
                        </td>
                        <td className="py-2.5 px-2">
                          <button
                            onClick={() => {
                              setCompareSessionId(compareSessionId === row.session_id ? '' : row.session_id)
                              setCompareFilter('all')
                              setShowReferencePanel(false)
                            }}
                            className="px-3 py-1.5 rounded-lg text-xs font-medium"
                            style={{
                              background: compareSessionId === row.session_id ? 'var(--accent-gradient)' : 'var(--progress-bg)',
                              color: compareSessionId === row.session_id ? '#fff' : 'var(--text-secondary)',
                            }}
                          >
                            {compareSessionId === row.session_id ? 'Hide' : 'Compare'}
                          </button>
                        </td>
                        <td className="py-2.5 px-2 text-right font-medium" style={{ color: 'var(--text-primary)' }}>
                          <div className="flex items-center justify-end gap-1">
                            <BenchmarkTrend value={row.improvement_pct} />
                            <span>{Math.round(row.best_small_rps || 0).toLocaleString()}</span>
                          </div>
                        </td>
                        <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                          {row.performance_vs_reference_pct?.toFixed(1)}%
                        </td>
                        <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                          {row.missing_count}
                        </td>
                        <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                          {row.differing_count}
                        </td>
                        <td className="py-2.5 px-2">
                          <button
                            onClick={() => removePinnedSession(row.session_id)}
                            className="px-3 py-1.5 rounded-lg text-xs font-medium"
                            style={{ background: 'rgba(244,63,94,0.14)', color: '#fb7185', border: '1px solid rgba(244,63,94,0.25)' }}
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm styled-table">
            <thead>
                <tr style={{ borderBottom: '1px solid var(--table-border)' }}>
                  <th className="text-left py-2.5 px-2">Rank</th>
                  <th className="text-left py-2.5 px-2">Session</th>
                  <th className="text-left py-2.5 px-2">Compare</th>
                  <th className="text-right py-2.5 px-2">Small RPS</th>
                  <th className="text-right py-2.5 px-2">% of Ref</th>
                  <th className="text-right py-2.5 px-2">Missing</th>
                <th className="text-right py-2.5 px-2">Different</th>
                <th className="text-left py-2.5 px-2">Bundle Gaps</th>
                <th className="text-left py-2.5 px-2">Missing Params</th>
                <th className="text-left py-2.5 px-2">Value Mismatches</th>
              </tr>
            </thead>
              <tbody>
                {pageRows.map((row, idx) => (
                  <tr key={row.session_id}>
                    <td className="py-2.5 px-2 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
                      {(currentPage - 1) * PAGE_SIZE + idx + 2}
                    </td>
                    <td className="py-2.5 px-2">
                      <div className="font-mono text-xs" style={{ color: 'var(--text-primary)' }}>{row.session_id}</div>
                      <div className="text-[0.7rem] flex items-center gap-1" style={{ color: 'var(--text-muted)' }}>
                        <BenchmarkTrend value={row.improvement_pct} />
                        <span>{fmtPct(row.improvement_pct)}</span>
                      </div>
                    </td>
                    <td className="py-2.5 px-2">
                      <button
                        onClick={() => {
                          setCompareSessionId(compareSessionId === row.session_id ? '' : row.session_id)
                          setCompareFilter('all')
                          setShowReferencePanel(false)
                        }}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium"
                        style={{
                          background: compareSessionId === row.session_id ? 'var(--accent-gradient)' : 'var(--progress-bg)',
                          color: compareSessionId === row.session_id ? '#fff' : 'var(--text-secondary)',
                        }}
                      >
                        {compareSessionId === row.session_id ? 'Hide' : 'Compare'}
                      </button>
                    </td>
                    <td className="py-2.5 px-2 text-right font-medium" style={{ color: 'var(--text-primary)' }}>
                      <div className="flex items-center justify-end gap-1">
                        <BenchmarkTrend value={row.improvement_pct} />
                        <span>{Math.round(row.best_small_rps || 0).toLocaleString()}</span>
                      </div>
                    </td>
                  <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                    {row.performance_vs_reference_pct?.toFixed(1)}%
                  </td>
                  <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                    {row.missing_count}
                  </td>
                  <td className="py-2.5 px-2 text-right" style={{ color: 'var(--text-secondary)' }}>
                    {row.differing_count}
                  </td>
                  <td className="py-2.5 px-2 text-xs" style={{ color: 'var(--text-secondary)' }}>
                    {(row.bundle_gaps || []).slice(0, 4).map((bundle) => (
                      <div key={`${row.session_id}-${bundle.bundle}`}>
                        {bundle.bundle}: {bundle.present}/{bundle.reference}
                      </div>
                    ))}
                    {(!row.bundle_gaps || row.bundle_gaps.length === 0) && <span>Fully covered</span>}
                  </td>
                  <td className="py-2.5 px-2 text-xs" style={{ color: 'var(--text-secondary)' }}>
                    {(row.missing_parameters || []).join(', ') || '--'}
                  </td>
                  <td className="py-2.5 px-2 text-xs" style={{ color: 'var(--text-secondary)' }}>
                    {(row.differing_parameters || []).map((item) => (
                      <div key={`${row.session_id}-${item.parameter}`}>
                        {item.parameter}: {item.session_value} {'->'} {item.reference_value}
                      </div>
                    ))}
                    {(!row.differing_parameters || row.differing_parameters.length === 0) && <span>--</span>}
                  </td>
                </tr>
              ))}
              {pageRows.length === 0 && (
                <tr>
                  <td colSpan={10} className="py-8 text-center" style={{ color: 'var(--text-muted)' }}>
                    No rows for this page with the current filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {(compareRow || showReferencePanel) && (
        <>
          <div
            className="fixed inset-0 z-40"
            style={{ background: 'rgba(15, 23, 42, 0.45)' }}
            onClick={() => {
              setCompareSessionId('')
              setShowReferencePanel(false)
            }}
          />
          <aside
            className="fixed top-0 right-0 h-full w-full max-w-5xl z-50 glass-card rounded-none border-l animate-slide-up"
            style={{
              background: 'var(--bg-primary)',
              borderTop: 'none',
              borderRight: 'none',
              borderBottom: 'none',
              borderLeft: '1px solid var(--border-card)',
            }}
          >
            <div className="flex items-center justify-between gap-4 px-6 py-4" style={{ borderBottom: '1px solid var(--table-border)' }}>
              {showReferencePanel ? (
                <div>
                  <h3 className="text-lg font-semibold mb-1" style={{ color: 'var(--text-primary)' }}>
                    Best Session Parameters: `{reference.session_id}`
                  </h3>
                  <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>
                    Full reconstructed parameter/value set for the all-time best session.
                  </p>
                </div>
              ) : (
                <div>
                  <h3 className="text-lg font-semibold mb-1" style={{ color: 'var(--text-primary)' }}>
                    Compare: best `{reference.session_id}` vs `{compareRow.session_id}`
                  </h3>
                  <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>
                    Side-by-side parameter/value comparison between the all-time best session and the selected session.
                  </p>
                </div>
              )}
              <button
                onClick={() => {
                  setCompareSessionId('')
                  setShowReferencePanel(false)
                }}
                className="px-3 py-2 rounded-lg text-sm font-medium"
                style={{ background: 'var(--progress-bg)', color: 'var(--text-secondary)' }}
              >
                Close
              </button>
            </div>

            <div className="p-6 overflow-y-auto h-[calc(100%-81px)]">
              {showReferencePanel ? (
                <>
                  <div className="grid grid-cols-2 gap-4 mb-5">
                    <SummaryStat label="Reference Session" value={reference.session_id} accent="#34d399" />
                    <SummaryStat label="Best Small RPS" value={Math.round(reference.best_small_rps || 0).toLocaleString()} />
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm styled-table" style={{ tableLayout: 'fixed' }}>
                      <thead>
                        <tr style={{ borderBottom: '1px solid var(--table-border)' }}>
                          <th className="text-left py-2.5 px-2 w-[28%]">Parameter</th>
                          <th className="text-left py-2.5 px-2 w-[28%]">Bundle</th>
                          <th className="text-left py-2.5 px-2 w-[28%]">Source</th>
                          <th className="text-left py-2.5 px-2 w-[16%]">Value</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(reference.applied_parameters || []).map((item) => (
                          <tr key={item.parameter}>
                            <td className="py-2.5 px-2 font-mono text-xs align-top break-words" style={{ color: 'var(--text-primary)', wordBreak: 'break-word' }}>
                              {item.parameter}
                            </td>
                            <td className="py-2.5 px-2 text-xs align-top break-words" style={{ color: 'var(--text-secondary)', wordBreak: 'break-word' }}>
                              {item.bundle}
                            </td>
                            <td className="py-2.5 px-2 text-xs align-top">
                              <SourceBadges sources={item.sources} />
                            </td>
                            <td className="py-2.5 px-2 font-mono text-xs align-top break-words" style={{ color: 'var(--text-secondary)', wordBreak: 'break-word', whiteSpace: 'normal' }}>
                              {item.value || '--'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : (
                <>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-5">
                    <SummaryStat label="Best Session" value={reference.session_id} accent="#34d399" />
                    <SummaryStat label="Best Small RPS" value={Math.round(reference.best_small_rps || 0).toLocaleString()} accent="#34d399" />
                    <SummaryStat label="Current Session" value={compareRow.session_id} />
                    <SummaryStat
                      label="Current Small RPS"
                      value={`${Math.round(compareRow.best_small_rps || 0).toLocaleString()} ${trendLabel(compareRow.improvement_pct)}`}
                    />
                  </div>

                  <div className="flex items-center justify-between gap-4 flex-wrap mb-4">
                    <div className="flex items-center gap-3 flex-wrap">
                    <span className="text-xs uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>
                      Compare Filter
                    </span>
                    {[
                      ['all', 'All'],
                      ['match', 'Same'],
                      ['different', 'Different'],
                      ['missing', 'Missing'],
                      ['extra', 'Extra'],
                    ].map(([value, label]) => (
                      <button
                        key={value}
                        onClick={() => setCompareFilter(value)}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium"
                        style={{
                          background: compareFilter === value ? 'var(--accent-gradient)' : 'var(--progress-bg)',
                          color: compareFilter === value ? '#fff' : 'var(--text-secondary)',
                        }}
                      >
                        {label} ({compareCounts[value] || 0})
                      </button>
                    ))}
                    </div>
                    <button
                      onClick={() => exportCompareJson(reference, compareRow, compareAllItems)}
                      className="px-3 py-2 rounded-lg text-xs font-medium"
                      style={{ background: 'var(--accent-gradient)', color: '#fff' }}
                    >
                      Export All JSON
                    </button>
                  </div>

                  <p className="text-xs mb-4" style={{ color: 'var(--text-secondary)' }}>
                    `Same` means the parameter exists in both sessions with the same value. `Different` means both have the parameter but with different values. `Missing` means best has it and current does not. `Extra` means current has it and best does not.
                  </p>

                  <div className="overflow-x-auto">
                    <table className="w-full text-sm styled-table" style={{ tableLayout: 'fixed' }}>
                      <thead>
                        <tr style={{ borderBottom: '1px solid var(--table-border)' }}>
                          <th className="text-left py-2.5 px-2 w-[18%]">Parameter</th>
                          <th className="text-left py-2.5 px-2 w-[18%]">Bundle</th>
                          <th className="text-left py-2.5 px-2 w-[12%]">Source</th>
                          <th className="text-left py-2.5 px-2 w-[20%]">Best</th>
                          <th className="text-left py-2.5 px-2 w-[20%]">Current</th>
                          <th className="text-left py-2.5 px-2 w-[12%]">Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {compareItems.map((item) => (
                          <tr key={item.parameter}>
                            <td
                              className="py-2.5 px-2 font-mono text-xs align-top break-words"
                              style={{ color: 'var(--text-primary)', wordBreak: 'break-word' }}
                            >
                              {item.parameter}
                            </td>
                            <td
                              className="py-2.5 px-2 text-xs align-top break-words"
                              style={{ color: 'var(--text-secondary)', wordBreak: 'break-word' }}
                            >
                              {item.bundle}
                            </td>
                            <td className="py-2.5 px-2 text-xs align-top">
                              <SourceBadges sources={item.sources} />
                            </td>
                            <td
                              className="py-2.5 px-2 font-mono text-xs align-top break-words"
                              style={{ color: 'var(--text-secondary)', wordBreak: 'break-word', whiteSpace: 'normal' }}
                            >
                              {item.referenceValue || '--'}
                            </td>
                            <td
                              className="py-2.5 px-2 font-mono text-xs align-top break-words"
                              style={{ color: 'var(--text-secondary)', wordBreak: 'break-word', whiteSpace: 'normal' }}
                            >
                              {item.currentValue || '--'}
                            </td>
                            <td className="py-2.5 px-2 align-top">
                              <span
                                className="pill-badge"
                                style={
                                  item.status === 'match'
                                    ? { color: '#34d399', background: 'rgba(16,185,129,0.14)', border: '1px solid rgba(16,185,129,0.3)' }
                                    : item.status === 'different'
                                      ? { color: '#fbbf24', background: 'rgba(245,158,11,0.14)', border: '1px solid rgba(245,158,11,0.3)' }
                                      : item.status === 'extra'
                                        ? { color: '#60a5fa', background: 'rgba(96,165,250,0.14)', border: '1px solid rgba(96,165,250,0.3)' }
                                        : { color: '#fb7185', background: 'rgba(244,63,94,0.14)', border: '1px solid rgba(244,63,94,0.3)' }
                                }
                              >
                                {item.status}
                              </span>
                            </td>
                          </tr>
                        ))}
                        {compareItems.length === 0 && (
                          <tr>
                            <td colSpan={6} className="py-8 text-center" style={{ color: 'var(--text-muted)' }}>
                              No rows for the selected compare filter.
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>
          </aside>
        </>
      )}
    </div>
  )
}

function BenchmarkTrend({ value }) {
  const numeric = Number(value || 0)
  if (numeric > 0) {
    return <span title="Benchmark improved" style={{ color: '#34d399' }}>↑</span>
  }
  if (numeric < 0) {
    return <span title="Benchmark regressed" style={{ color: '#fb7185' }}>↓</span>
  }
  return <span title="No benchmark change" style={{ color: 'var(--text-muted)' }}>→</span>
}

function trendLabel(value) {
  const numeric = Number(value || 0)
  if (numeric > 0) return '↑'
  if (numeric < 0) return '↓'
  return '→'
}

function buildCompareRows(reference, current) {
  const referenceMap = Object.fromEntries((reference.applied_parameters || []).map((item) => [item.parameter, item]))
  const currentMap = Object.fromEntries((current.current_applied_parameters || []).map((item) => [item.parameter, item]))
  const keys = Array.from(new Set([...Object.keys(referenceMap), ...Object.keys(currentMap)])).sort()
  const rows = keys.map((parameter) => {
    const ref = referenceMap[parameter]
    const cur = currentMap[parameter]
    let status = 'missing'
    if (ref && cur && ref.value === cur.value) status = 'match'
    else if (ref && cur) status = 'different'
    else if (!ref && cur) status = 'extra'
    return {
      parameter,
      bundle: ref?.bundle || cur?.bundle || 'other',
      referenceValue: ref?.value || '',
      currentValue: cur?.value || '',
      status,
      sources: Array.from(new Set([...(ref?.sources || []), ...(cur?.sources || [])])).sort(),
    }
  })
  const statusRank = { missing: 0, different: 1, extra: 2, match: 3 }
  rows.sort((a, b) => {
    const delta = (statusRank[a.status] ?? 9) - (statusRank[b.status] ?? 9)
    if (delta !== 0) return delta
    return a.parameter.localeCompare(b.parameter)
  })
  return rows
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

function exportCompareJson(reference, current, rows) {
  const payload = {
    reference_session_id: reference.session_id,
    reference_small_rps: reference.best_small_rps || 0,
    reference_applied_parameters: reference.applied_parameters || [],
    current_session_id: current.session_id,
    current_small_rps: current.best_small_rps || 0,
    current_applied_parameters: current.current_applied_parameters || [],
    exported_at: new Date().toISOString(),
    rows,
  }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `compare_${reference.session_id}_vs_${current.session_id}.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
