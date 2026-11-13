import { useEffect, useState } from 'react'
import { api } from './api.js'
import { Logo, Notice } from './components.jsx'
import { Account, Congress, Funds, Guide, Insiders, Live, Training } from './pages.jsx'

const TABS = [
  ['live', 'Flux live', Live],
  ['paper', 'Entraînement', Training],
  ['insiders', 'Insiders', Insiders],
  ['congress', 'Élus', Congress],
  ['funds', 'Fonds', Funds],
  ['guide', 'Signaux', Guide],
  ['account', 'Compte', Account],
]

function useHashTab() {
  const read = () => {
    const h = window.location.hash.replace('#', '')
    return TABS.some(([k]) => k === h) ? h : 'live'
  }
  const [tab, setTab] = useState(read)
  useEffect(() => {
    const on = () => setTab(read())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return [tab, (t) => { window.location.hash = t }]
}

export default function App() {
  const [me, setMe] = useState(undefined)
  const [tab, go] = useHashTab()

  useEffect(() => {
    api('/auth/me').then(setMe).catch(() => setMe(null))
    const out = () => setMe(null)
    window.addEventListener('sillage:logout', out)
    return () => window.removeEventListener('sillage:logout', out)
  }, [])

  useEffect(() => {
    const label = TABS.find(([k]) => k === tab)?.[1]
    document.title = me ? `${label} · Sillage` : 'Sillage'
  }, [tab, me])

  async function logout() {
    try { await api('/auth/logout', { method: 'POST' }) } catch { /* */ }
    setMe(null)
  }

  if (me === undefined) return <div className="auth"><p className="muted">Chargement…</p></div>
  if (me === null) return <Auth onDone={() => api('/auth/me').then(setMe)} />

  const Page = TABS.find(([k]) => k === tab)[2]
  return (
    <>
      <header className="topbar">
        <a className="brand" href="#live" aria-label="Sillage, accueil">
          <Logo />
          <div><strong>Sillage</strong><br /><span>Suivez l'argent informé</span></div>
        </a>
        <nav className="tabs" aria-label="Sections">
          {TABS.map(([k, label]) => (
            <button key={k} type="button" className="tab" aria-current={tab === k ? 'page' : undefined} onClick={() => go(k)}>{label}</button>
          ))}
        </nav>
      </header>
      <main id="contenu">
        <Page me={me} onLogout={logout} />
      </main>
      <footer>
        Sillage affiche des déclarations publiques (SEC EDGAR{me.congress_enabled ? ', Quiver Quantitative' : ''}) avec retard.
        Ce n'est pas un conseil en investissement : vous décidez et passez vos ordres vous-même. Cours indicatifs, non officiels.
      </footer>
    </>
  )
}

function Auth({ onDone }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setBusy(true); setMsg(null)
    try {
      await api(mode === 'login' ? '/auth/login' : '/auth/register', { method: 'POST', body: { email, password } })
      await onDone()
    } catch (err) {
      setMsg({ type: 'error', text: err.message })
    } finally { setBusy(false) }
  }

  return (
    <div className="auth">
      <main className="panel">
        <div className="brand"><Logo size={40} /><div><strong>Sillage</strong><br /><span>Suivez l'argent informé</span></div></div>
        <h1 style={{ fontSize: '1.3rem', marginTop: 20 }}>{mode === 'login' ? 'Connexion' : 'Créer un compte'}</h1>
        <form onSubmit={submit}>
          <div className="field">
            <label htmlFor="email">E-mail</label>
            <input id="email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="password">Mot de passe{mode === 'register' && ' (10 caractères minimum)'}</label>
            <input id="password" type="password" required minLength={mode === 'register' ? 10 : undefined}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'} value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          <Notice msg={msg} />
          <button className="btn primary block" type="submit" disabled={busy}>{mode === 'login' ? 'Se connecter' : "S'inscrire"}</button>
        </form>
        <div className="switch">
          <button type="button" className="link-btn" onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setMsg(null) }}>
            {mode === 'login' ? 'Pas de compte ? Inscrivez-vous' : 'Déjà un compte ? Connectez-vous'}
          </button>
        </div>
        <p className="muted small">Mode Entraînement inclus : un portefeuille fictif pour s'exercer sans broker. Pas un conseil en investissement.</p>
      </main>
    </div>
  )
}
