import React, { useState, useEffect } from 'react'
import SessionView from './components/SessionView'
import CompareView from './components/CompareView'

const API = '/api'

export default function App() {
  const [tab, setTab] = useState('session')
  const [sessions, setSessions] = useState([])
  const [selectedSession, setSelectedSession] = useState(null)
  const [sessionData, setSessionData] = useState(null)
  const [compareData, setCompareData] = useState(null)
  const [loading, setLoading] = useState(true)

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
      <div className="flex items-center justify-center h-screen">
        <div className="text-xl text-gray-400">Loading sessions...</div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gray-950">
      {/* Header */}
      <header className="bg-gray-900 border-b border-gray-800 px-6 py-4">
        <div className="flex items-center justify-between max-w-7xl mx-auto">
          <h1 className="text-xl font-bold text-emerald-400">SlayMetrics Dashboard</h1>
          <div className="flex gap-1 bg-gray-800 rounded-lg p-1">
            <button
              onClick={() => setTab('session')}
              className={`px-4 py-2 rounded-md text-sm font-medium transition ${
                tab === 'session'
                  ? 'bg-emerald-600 text-white'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              Session View
            </button>
            <button
              onClick={() => setTab('compare')}
              className={`px-4 py-2 rounded-md text-sm font-medium transition ${
                tab === 'compare'
                  ? 'bg-emerald-600 text-white'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              Compare Sessions
            </button>
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="max-w-7xl mx-auto px-6 py-6">
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
