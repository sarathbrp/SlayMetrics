import React, { useState, useEffect } from 'react'
import SessionView from './components/SessionView'
import CompareView from './components/CompareView'

const API = '/api'

function ThemeToggle({ dark, onToggle }) {
  return (
    <button
      onClick={onToggle}
      className="relative w-14 h-7 rounded-full transition-all duration-300 focus:outline-none focus:ring-2 focus:ring-emerald-400/50"
      style={{
        background: dark
          ? 'linear-gradient(135deg, #1e293b, #334155)'
          : 'linear-gradient(135deg, #e0f2fe, #bae6fd)',
        border: `1px solid ${dark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.1)'}`,
      }}
      aria-label="Toggle dark mode"
    >
      <div
        className="absolute top-0.5 w-6 h-6 rounded-full flex items-center justify-center text-xs transition-all duration-300 shadow-lg"
        style={{
          left: dark ? '1.75rem' : '0.125rem',
          background: dark
            ? 'linear-gradient(135deg, #334155, #475569)'
            : 'linear-gradient(135deg, #fbbf24, #f59e0b)',
        }}
      >
        {dark ? '🌙' : '☀️'}
      </div>
    </button>
  )
}

export default function App() {
  const [tab, setTab] = useState('session')
  const [sessions, setSessions] = useState([])
  const [selectedSession, setSelectedSession] = useState(null)
  const [sessionData, setSessionData] = useState(null)
  const [compareData, setCompareData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [dark, setDark] = useState(() => {
    const stored = localStorage.getItem('slaymetrics-theme')
    if (stored) return stored === 'dark'
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  })

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('slaymetrics-theme', dark ? 'dark' : 'light')
  }, [dark])

  useEffect(() => {
    fetch(`${API}/sessions`)
      .then(r => r.json())
      .then(data => {
        setSessions(data)
        if (data.length > 0) {
          setSelectedSession(data[0].session_id)
        }
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (!selectedSession) return
    fetch(`${API}/sessions/${selectedSession}`)
      .then(r => r.json())
      .then(setSessionData)
  }, [selectedSession])

  useEffect(() => {
    if (sessions.length < 2) return
    const ids = sessions.slice(0, 3).map(s => s.session_id).join(',')
    fetch(`${API}/compare?sessions=${ids}`)
      .then(r => r.json())
      .then(setCompareData)
  }, [sessions])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen" style={{ background: 'var(--bg-primary)' }}>
        <div className="flex flex-col items-center gap-4 animate-fade-in">
          <div className="w-12 h-12 rounded-full border-2 border-emerald-400/30 border-t-emerald-400 animate-spin" />
          <span className="text-lg font-medium" style={{ color: 'var(--text-muted)' }}>
            Loading sessions...
          </span>
        </div>
      </div>
    )
  }

  const tabs = [
    { id: 'session', label: 'Session View', icon: '📊' },
    { id: 'compare', label: 'Compare', icon: '🔀' },
  ]

  return (
    <div className="min-h-screen transition-colors duration-300" style={{ background: 'var(--bg-primary)' }}>
      {/* Header */}
      <header className="glass-card sticky top-0 z-50" style={{ borderRadius: 0, borderTop: 'none', borderLeft: 'none', borderRight: 'none' }}>
        <div className="flex items-center justify-between max-w-7xl mx-auto px-6 py-3">
          {/* Logo */}
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center text-white font-bold text-sm"
              style={{ background: 'var(--accent-gradient)' }}>
              S
            </div>
            <h1 className="text-lg font-bold gradient-text">SlayMetrics</h1>
          </div>

          {/* Tab navigation */}
          <nav className="flex items-center gap-1 p-1 rounded-xl" style={{ background: 'var(--progress-bg)' }}>
            {tabs.map(t => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-all duration-300 ${
                  tab === t.id
                    ? 'text-white shadow-lg'
                    : 'hover:opacity-80'
                }`}
                style={{
                  background: tab === t.id ? 'var(--accent-gradient)' : 'transparent',
                  color: tab !== t.id ? 'var(--text-secondary)' : undefined,
                }}
              >
                <span className="mr-1.5">{t.icon}</span>
                {t.label}
              </button>
            ))}
          </nav>

          {/* Theme toggle */}
          <ThemeToggle dark={dark} onToggle={() => setDark(!dark)} />
        </div>
      </header>

      {/* Content */}
      <main className="max-w-7xl mx-auto px-6 py-6 animate-fade-in">
        {tab === 'session' ? (
          <SessionView
            sessions={sessions}
            selectedSession={selectedSession}
            onSelectSession={setSelectedSession}
            data={sessionData}
          />
        ) : (
          <CompareView sessions={sessions} data={compareData} />
        )}
      </main>
    </div>
  )
}
