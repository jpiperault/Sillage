import { useState } from 'react'
import { api } from './api.js'
import { Notice, Seg, SignalTable, useApi } from './components.jsx'
import Ticket from './Ticket.jsx'
import { date, datetime, int, money, pct, tone, usd } from './format.js'
import { EquityChart } from './v2.jsx'

const fr = (v, d = 1) => (v == null ? '—' : String(Number(v).toFixed(d)).replace('.', ','))
const bytes = (b) => (b >= 1e9 ? `${fr(b / 1e9)} Go` : b >= 1e6 ? `${fr(b / 1e6)} Mo` : `${fr(b / 1e3, 0)} Ko`)

/* ------------------------------------------------------------ Tester une règle sur l'historique */

export function RuleSimulator() {
  const [f, setF] = useState({ score: 'v1', min_score: 75, alloc_pct: 2, hold_days: 90, max_positions: 30, stop_pct: 8, cost_pct: 0.1 })
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  async function run() {
    setBusy(true); setMsg(null)
    try {
      const body = Object.fromEntries(Object.entries(f).map(([k, v]) => [k, k === 'score' ? v : Number(v)]))
      const r = await api('/paper/rule/simulate', { method: 'POST', body })
      if (r.error) { setMsg({ type: 'error', text: r.error }); setRes(null) } else setRes(r)
    } catch (e) { setMsg({ type: 'error', text: e.message }) } finally { setBusy(false) }
  }
  const field = (k, label, props = {}) => (
    <div className="field"><label htmlFor={'sim-' + k}>{label}</label>
      <input id={'sim-' + k} type="number" value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} {...props} /></div>
  )
  return (
    <section className="panel" aria-label="Tester une règle sur l'historique">
      <h2>Tester une règle sur l'historique <span className="muted small">achats d'initiés américains, 18 mois</span></h2>
      <p className="muted small">Rejoue jour par jour : achat à la clôture qui suit chaque déclaration retenue, vente au stop ou à l'horizon, frais à l'achat et à la vente, portefeuille de départ 100 000 $. Comparaison avec le S&amp;P 500.</p>
      <div className="filters" style={{ alignItems: 'end' }}>
        <Seg label="Score" value={f.score} onChange={(v) => setF({ ...f, score: v })} options={[['v1', 'v1'], ['v2', 'v2']]} />
        {field('min_score', 'Score minimum', { min: 0, max: 100 })}
        {field('alloc_pct', 'Part par ligne (%)', { min: 0.5, max: 20, step: 0.5 })}
        {field('hold_days', 'Horizon (jours)', { min: 5, max: 400 })}
        {field('max_positions', 'Lignes max.', { min: 1, max: 200 })}
        {field('stop_pct', 'Stop (%)', { min: 1, max: 50 })}
        {field('cost_pct', 'Frais (%)', { min: 0, max: 2, step: 0.05 })}
        <button type="button" className="btn primary" disabled={busy} onClick={run}>{busy ? 'Calcul…' : 'Lancer le test'}</button>
      </div>
      {f.score === 'v2' && <p className="muted small">En v2, seule la période de test du modèle est rejouée (signaux qu'il n'a pas vus à l'entraînement) : sinon le résultat serait flatteur.</p>}
      <Notice msg={msg} />
      {res && (<>
        <div className="stats num">
          <div className="stat"><div className="label">Règle</div><div className={`value ${tone(res.total_pct)}`}>{pct(res.total_pct)}</div></div>
          <div className="stat"><div className="label">S&amp;P 500</div><div className={`value ${tone(res.bench_pct)}`}>{pct(res.bench_pct)}</div></div>
          <div className="stat"><div className="label">Pire baisse</div><div className="value down">−{fr(res.max_drawdown_pct)} %</div></div>
          <div className="stat"><div className="label">Trades gagnants</div><div className="value">{res.win_rate == null ? '—' : `${fr(res.win_rate)} %`}</div><div className="muted small">{int(res.trades)} clos, moyenne {pct(res.avg_trade_pct)}</div></div>
          <div className="stat"><div className="label">Investi en moyenne</div><div className="value">{fr(res.exposure_pct, 0)} %</div><div className="muted small">{res.exits.stop} stops · {res.exits.horizon} horizons</div></div>
        </div>
        <p className="muted small">Du {date(res.period[0])} au {date(res.period[1])} · {int(res.signals)} signaux retenus{res.test_only_from ? ` (à partir du ${date(res.test_only_from)})` : ''} · {res.open} ligne(s) encore ouverte(s) à la fin, valorisées au dernier cours.</p>
        <EquityChart curve={res.curve} />
        <div className="grid-2" style={{ marginTop: 8 }}>
          {[['Meilleurs trades', res.best], ['Pires trades', res.worst]].map(([title, list]) => (
            <div key={title}><h3>{title}</h3><ul className="reasons">{list.map((t, i) => (
              <li key={i}><a href={`#action/${t.ticker}`}>{t.ticker}</a> {pct(t.ret * 100)} ({date(t.entry_day)} → {date(t.exit_day)}, {t.reason === 'stop' ? 'stop' : 'horizon'}, score {t.score})</li>
            ))}</ul></div>
          ))}
        </div>
        <p className="muted small">Passé simulé, pas une promesse : pas de glissement de prix, titres radiés absents, période de 18 mois seulement.</p>
      </>)}
    </section>
  )
}

/* ------------------------------------------------------------ Gestion d'une position */

export function PositionEditor({ pos, book, onDone }) {
  const [f, setF] = useState({
    stop_pct: pos.stop_price ? fr((1 - pos.stop_price / pos.avg_price) * 100) .replace(',', '.') : '8',
    trailing: pos.trailing_pct != null, trailing_pct: pos.trailing_pct ?? 10,
    target_price: pos.target_price ?? '', note: pos.note ?? '', sell_qty: '',
  })
  const [msg, setMsg] = useState(null)
  async function save() {
    try {
      await api(`/paper/positions/${encodeURIComponent(pos.ticker)}`, { method: 'PUT', body: {
        book, stop_pct: f.stop_pct === '' ? null : Number(f.stop_pct), trailing_pct: f.trailing ? Number(f.trailing_pct) : null,
        target_price: f.target_price === '' ? null : Number(f.target_price), note: f.note } })
      onDone('Règles de sortie enregistrées.')
    } catch (e) { setMsg({ type: 'error', text: e.message }) }
  }
  async function sellPart() {
    try {
      const r = await api('/paper/orders', { method: 'POST', body: { ticker: pos.ticker, side: 'sell', book, quantity: Number(f.sell_qty) } })
      onDone(`${r.quantity} ${r.ticker} vendus à ${usd(r.price)}, reste ${r.remaining}.`)
    } catch (e) { setMsg({ type: 'error', text: e.message }) }
  }
  return (
    <div style={{ padding: '8px 0' }}>
      <div className="filters" style={{ alignItems: 'end' }}>
        <div className="field"><label htmlFor="pe-stop">Stop sous le prix moyen (%)</label><input id="pe-stop" type="number" min="1" max="90" value={f.stop_pct} onChange={(e) => setF({ ...f, stop_pct: e.target.value })} /></div>
        <label className="check"><input type="checkbox" checked={f.trailing} onChange={(e) => setF({ ...f, trailing: e.target.checked })} /> Stop suiveur</label>
        {f.trailing && <div className="field"><label htmlFor="pe-trail">sous le plus haut (%)</label><input id="pe-trail" type="number" min="1" max="90" value={f.trailing_pct} onChange={(e) => setF({ ...f, trailing_pct: e.target.value })} /></div>}
        <div className="field"><label htmlFor="pe-target">Objectif de cours ($)</label><input id="pe-target" type="number" min="0" step="0.01" value={f.target_price} onChange={(e) => setF({ ...f, target_price: e.target.value })} /></div>
      </div>
      <div className="field"><label htmlFor="pe-note">Note</label><input id="pe-note" type="text" maxLength={500} value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} placeholder="Pourquoi j'ai pris cette ligne…" /></div>
      <div className="btn-row" style={{ marginTop: 8, alignItems: 'end' }}>
        <button type="button" className="btn primary" onClick={save}>Enregistrer</button>
        <div className="field"><label htmlFor="pe-sell">Vendre une partie (quantité)</label><input id="pe-sell" type="number" min="0" max={pos.quantity} value={f.sell_qty} onChange={(e) => setF({ ...f, sell_qty: e.target.value })} /></div>
        <button type="button" className="btn danger" disabled={!(Number(f.sell_qty) > 0)} onClick={sellPart}>Vendre cette quantité</button>
      </div>
      <Notice msg={msg} />
    </div>
  )
}

/* ------------------------------------------------------------ Indicateur de marché */

function RatioChart({ series }) {
  const pts = series.filter((s) => s.ratio != null)
  if (pts.length < 3) return <p className="muted">Pas encore assez de semaines.</p>
  const W = 760, H = 220, P = { l: 40, r: 44, t: 10, b: 24 }
  const X = (i) => P.l + (i / (pts.length - 1)) * (W - P.l - P.r)
  const Y = (v) => P.t + (1 - v / 100) * (H - P.t - P.b)
  const spys = pts.map((p) => p.spy).filter(Boolean).map(Number)
  const s0 = Math.min(...spys), s1 = Math.max(...spys)
  const YS = (v) => P.t + (1 - (v - s0) / Math.max(1e-9, s1 - s0)) * (H - P.t - P.b)
  const ratio = pts.map((p, i) => `${X(i).toFixed(1)},${Y(p.ratio).toFixed(1)}`).join(' L')
  const spy = pts.map((p, i) => (p.spy ? `${X(i).toFixed(1)},${YS(Number(p.spy)).toFixed(1)}` : null)).filter(Boolean).join(' L')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Part des initiés acheteurs par semaine et S&P 500">
      {[0, 25, 50].map((v) => (<g key={v}><line x1={P.l} x2={W - P.r} y1={Y(v)} y2={Y(v)} stroke="var(--border)" /><text x={P.l - 6} y={Y(v) + 4} fontSize="11" textAnchor="end" fill="var(--muted)">{v} %</text></g>))}
      <text x={W - P.r + 4} y={YS(s1) + 4} fontSize="11" fill="var(--muted)">{s1.toFixed(0)}</text>
      <text x={W - P.r + 4} y={YS(s0) + 4} fontSize="11" fill="var(--muted)">{s0.toFixed(0)}</text>
      <text x={P.l} y={H - 6} fontSize="11" fill="var(--muted)">{date(pts[0].week)}</text>
      <text x={W - P.r} y={H - 6} fontSize="11" textAnchor="end" fill="var(--muted)">{date(pts[pts.length - 1].week)}</text>
      <path d={'M' + spy} fill="none" stroke="var(--muted)" strokeWidth="1.4" strokeDasharray="4 3" />
      <path d={'M' + ratio} fill="none" stroke="var(--accent)" strokeWidth="2" />
    </svg>
  )
}

export function Market() {
  const { data, error } = useApi('/market/insiders?weeks=78')
  if (error) return <Notice msg={{ type: 'error', text: error }} />
  if (!data) return <p className="muted">Chargement…</p>
  const diff = data.now != null && data.average != null ? data.now - data.average : null
  return (
    <div className="grid-2">
      <section className="panel" style={{ gridColumn: '1 / -1' }} aria-label="Indicateur de marché initiés">
        <h2>Sentiment des initiés <span className="muted small">{data.note}</span></h2>
        <div className="stats num">
          <div className="stat"><div className="label">4 dernières semaines</div><div className="value">{fr(data.now)} %</div><div className="muted small">d'initiés acheteurs</div></div>
          <div className="stat"><div className="label">Moyenne sur la période</div><div className="value">{fr(data.average)} %</div></div>
          <div className="stat"><div className="label">Écart</div><div className={`value ${tone(diff)}`}>{diff == null ? '—' : `${diff > 0 ? '+' : ''}${fr(diff)} pts`}</div>
            <div className="muted small">{diff == null ? '' : diff > 3 ? 'plus d\'achats que d\'habitude' : diff < -3 ? 'moins d\'achats que d\'habitude' : 'dans la norme'}</div></div>
        </div>
        <RatioChart series={data.series} />
        <p className="muted small"><span style={{ color: 'var(--accent)' }}>━</span> part des initiés acheteurs (échelle de gauche) · <span>┅</span> S&amp;P 500 (échelle de droite). Historiquement, une hausse marquée des achats d'initiés accompagne souvent des marchés en baisse : ils achètent quand c'est bas. Ce n'est pas un signal de timing fiable à lui seul.</p>
      </section>
      <section className="panel" style={{ gridColumn: '1 / -1' }} aria-label="Par secteur">
        <h2>Par secteur <span className="muted small">part d'acheteurs sur 8 semaines, comparée à toute la période</span></h2>
        <div className="table-wrap"><table>
          <thead><tr><th>Secteur</th><th className="r">Acheteurs 8 sem.</th><th className="r">Vendeurs 8 sem.</th><th className="r">Part 8 sem.</th><th className="r">Part habituelle</th><th className="r">Écart</th></tr></thead>
          <tbody>{data.sectors.filter((s) => s.buyers_all + s.sellers_all > 0).sort((a, b) => ((b.ratio_8w ?? 0) - (b.ratio_all ?? 0)) - ((a.ratio_8w ?? 0) - (a.ratio_all ?? 0))).map((s) => {
            const d = s.ratio_8w != null && s.ratio_all != null ? s.ratio_8w - s.ratio_all : null
            return (<tr key={s.sector}><td>{s.sector}</td><td className="r num up">{s.buyers_8w}</td><td className="r num sell">{s.sellers_8w}</td>
              <td className="r num">{s.ratio_8w == null ? '—' : `${fr(s.ratio_8w)} %`}</td><td className="r num">{s.ratio_all == null ? '—' : `${fr(s.ratio_all)} %`}</td>
              <td className={`r num ${tone(d)}`}>{d == null ? '—' : `${d > 0 ? '+' : ''}${fr(d)} pts`}</td></tr>)
          })}</tbody>
        </table></div>
        <p className="muted small">Secteurs déduits du code d'activité (SIC) déclaré à la SEC ; « Inconnu » tant que la société n'a pas encore été classée.</p>
      </section>
    </div>
  )
}

/* ------------------------------------------------------------ Réseau d'un initié */

export function InsiderPage({ cik }) {
  const { data, error } = useApi(`/insider/${cik}`)
  const [selected, setSelected] = useState(null)
  if (error) return <Notice msg={{ type: 'error', text: error }} />
  if (!data) return <p className="muted">Chargement…</p>
  const t = data.track
  return (
    <div className="grid-live">
      <div>
        <section className="panel" aria-label={`Initié ${data.name}`}>
          <h2>{data.name} <span className="muted small">CIK {data.cik}</span></h2>
          <div className="stats num">
            <div className="stat"><div className="label">Sociétés</div><div className="value">{data.companies.length}</div></div>
            <div className="stat"><div className="label">Achats mesurés</div><div className="value">{t ? t.n : '—'}</div></div>
            <div className="stat"><div className="label">Battent le marché à 3 mois</div><div className="value">{t && t.n ? `${Math.round(t.wins / t.n * 100)} %` : '—'}</div>
              {t && <div className="muted small">médiane {pct(Number(t.median_63))}</div>}</div>
          </div>
          <div className="table-wrap"><table>
            <thead><tr><th>Société</th><th>Secteur</th><th>Rôle</th><th className="r">Achats</th><th className="r">Ventes</th><th className="r">Acheté</th><th className="r">Vendu</th><th>Dernière</th></tr></thead>
            <tbody>{data.companies.map((c) => (
              <tr key={c.issuer_cik}><td className="wrap">{c.ticker ? <a href={`#action/${c.ticker}`}><b>{c.ticker}</b></a> : null} {c.company}</td>
                <td className="small muted">{c.sector || '—'}</td><td className="wrap small">{c.title || (c.relationship || '').replace('TenPercentOwner', '> 10 %').replace('Director', 'Administrateur').replace('Officer', 'Dirigeant')}</td>
                <td className="r num up">{c.buys}</td><td className="r num sell">{c.sells}</td>
                <td className="r num">{money(Number(c.buy_value))}</td><td className="r num">{money(Number(c.sell_value))}</td><td className="num">{date(c.last)}</td></tr>
            ))}</tbody>
          </table></div>
        </section>
        <section className="panel" aria-label="Réseau">
          <h2>Réseau <span className="muted small">autres initiés des mêmes sociétés</span></h2>
          {!data.peers.length ? <p className="muted">Aucun.</p> : (
            <div className="table-wrap"><table>
              <thead><tr><th>Initié</th><th className="r">Sociétés en commun</th><th>Tickers</th></tr></thead>
              <tbody>{data.peers.map((p) => (
                <tr key={p.owner_cik}><td><a href={`#initie/${p.owner_cik}`}>{p.name}</a></td><td className="r num">{p.shared}</td><td className="small muted">{p.tickers.join(', ')}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </section>
        {data.signals.length > 0 && (
          <section className="panel" aria-label="Ses déclarations récentes"><h2>Déclarations récentes</h2>
            <SignalTable items={data.signals} selected={selected} onSelect={setSelected} showSource={false} /></section>
        )}
      </div>
      <div className="side-col"><Ticket id={selected} /></div>
    </div>
  )
}

/* ------------------------------------------------------------ Admin : comptes */

export function AdminUsers() {
  const { data, reload } = useApi('/admin/users')
  const [msg, setMsg] = useState(null)
  async function act(u, action) {
    if (action === 'delete' && !window.confirm(`Supprimer ${u.email} et toutes ses données ?`)) return
    try {
      const r = action === 'delete' ? await api(`/admin/users/${u.id}`, { method: 'DELETE' })
        : await api(`/admin/users/${u.id}`, { method: 'POST', body: { action } })
      setMsg({ type: 'ok', text: r.temporary_password ? `${r.message} ${r.temporary_password}` : r.message }); reload()
    } catch (e) { setMsg({ type: 'error', text: e.message }) }
  }
  return (
    <section className="panel" aria-label="Comptes">
      <h2>Comptes <span className="muted small">{data ? `${data.items.length} inscrits` : ''}</span></h2>
      <Notice msg={msg} />
      <div className="table-wrap"><table>
        <thead><tr><th>E-mail</th><th>Inscrit</th><th>Dernière connexion</th><th className="r">Ordres</th><th>Telegram</th><th>État</th><th>Actions</th></tr></thead>
        <tbody>{(data?.items || []).map((u) => (
          <tr key={u.id}>
            <td>{u.email} {u.is_admin && <span className="badge buy">admin</span>}</td>
            <td className="num">{date(u.created_at)}</td><td className="num">{datetime(u.last_login_at)}</td>
            <td className="r num">{u.orders}</td><td>{u.telegram ? 'lié' : '—'}</td>
            <td>{u.disabled ? <span className="badge sell">désactivé</span> : 'actif'}</td>
            <td><div className="btn-row">
              {u.disabled ? <button type="button" className="btn" onClick={() => act(u, 'enable')}>Réactiver</button>
                : <button type="button" className="btn" onClick={() => act(u, 'disable')}>Désactiver</button>}
              <button type="button" className="btn" onClick={() => act(u, 'reset_password')}>Nouveau mot de passe</button>
              <button type="button" className="btn" onClick={() => act(u, u.is_admin ? 'remove_admin' : 'make_admin')}>{u.is_admin ? 'Retirer admin' : 'Rendre admin'}</button>
              <button type="button" className="btn danger" onClick={() => act(u, 'delete')}>Supprimer</button>
            </div></td>
          </tr>
        ))}</tbody>
      </table></div>
    </section>
  )
}

/* ------------------------------------------------------------ Admin : santé */

export function Health() {
  const { data, error, reload } = useApi('/admin/health')
  if (error) return <Notice msg={{ type: 'error', text: error }} />
  if (!data) return <p className="muted">Chargement…</p>
  const q = data.queues
  const stale = (j) => j.last_ok_at && (Date.now() - new Date(j.last_ok_at)) > 26 * 3600e3
  return (
    <>
      <div className="stats num">
        <div className="stat"><div className="label">Version</div><div className="value">{data.version}</div></div>
        <div className="stat"><div className="label">Mémoire</div><div className="value">{fr(data.memory_mb, 0)} Mo</div></div>
        <div className="stat"><div className="label">Base de données</div><div className="value">{bytes(q.db_bytes)}</div></div>
        <div className="stat"><div className="label">Collecte</div><div className="value small">{data.loops.ingest_enabled ? `toutes les ${data.loops.interval_min} min` : 'désactivée'}</div>
          <div className="muted small">{data.loops.fast_running ? 'boucle rapide en cours · ' : ''}{data.loops.slow_running ? 'boucle lente en cours' : ''}</div></div>
        <div className="stat"><div className="label">Erreurs (24 h)</div><div className={`value ${data.errors.length ? 'down' : 'green'}`}>{data.errors.length}</div></div>
      </div>
      <section className="panel" aria-label="Files d'attente et sources">
        <h2>Files d'attente et sources</h2>
        <ul className="reasons">
          <li>SEC : {int(q.sec_pending)} dépôts en attente, {int(q.sec_errors)} en erreur</li>
          <li>Greffe de la Chambre : {int(q.house_pending)} en attente · Sénat : {data.sources.senate_enabled ? `${int(q.senate_pending)} en attente` : 'désactivé'}</li>
          <li>AMF : {int(q.amf_pending)} en attente, {int(q.amf_errors)} en erreur</li>
          <li>13F : {int(q.cusips_pending)} CUSIP à convertir, {int(q.cusips_unmapped)} sans ticker</li>
          <li>Cours : {int(q.histories)} historiques, {int(q.stale_prices)} cours de plus d'un jour</li>
          <li>Yahoo : {data.sources.yahoo_blocked ? <span className="down">bloqué (429), secours Nasdaq actif</span> : 'disponible'} · Telegram : {data.sources.telegram ? 'réglé' : 'non réglé'} · Quiver : {data.sources.quiver ? 'réglé' : 'non réglé'}</li>
        </ul>
      </section>
      <section className="panel" aria-label="Tâches">
        <h2>Tâches <span className="muted small">24 dernières heures</span></h2>
        <div className="table-wrap"><table>
          <thead><tr><th>Tâche</th><th>Dernier succès</th><th className="r">Passages</th><th className="r">Erreurs</th><th className="r">Durée moy.</th><th className="r">Max</th><th>Dernière erreur</th></tr></thead>
          <tbody>{data.jobs.map((j) => (
            <tr key={j.name}><td>{j.name}</td><td className={`num ${stale(j) ? 'down' : ''}`}>{datetime(j.last_ok_at)}</td>
              <td className="r num">{j.runs ?? 0}</td><td className={`r num ${j.errors ? 'down' : ''}`}>{j.errors ?? 0}</td>
              <td className="r num">{j.avg_s == null ? '—' : `${fr(j.avg_s)} s`}</td><td className="r num">{j.max_s == null ? '—' : `${fr(j.max_s)} s`}</td>
              <td className="wrap small">{j.last_error ? <span className="down">{datetime(j.last_error_at)} — {j.last_error}</span> : <span className="muted">aucune</span>}</td></tr>
          ))}</tbody>
        </table></div>
      </section>
      <div className="grid-2" style={{ marginTop: 16 }}>
        <section className="panel" aria-label="Erreurs récentes"><h2>Erreurs récentes</h2>
          {!data.errors.length ? <p className="green">Aucune erreur sur 24 h.</p> : (
            <ul className="reasons">{data.errors.map((e, i) => <li key={i}>{datetime(e.finished_at)} — <b>{e.name}</b> : {e.error}</li>)}</ul>)}
        </section>
        <section className="panel" aria-label="Tables"><h2>Plus grosses tables</h2>
          <div className="table-wrap"><table><thead><tr><th>Table</th><th className="r">Lignes</th><th className="r">Taille</th></tr></thead>
            <tbody>{data.tables.map((t) => <tr key={t.name}><td>{t.name}</td><td className="r num">{int(t.rows)}</td><td className="r num">{bytes(t.bytes)}</td></tr>)}</tbody></table></div>
        </section>
      </div>
      <div className="btn-row" style={{ marginTop: 12 }}><button type="button" className="btn" onClick={reload}>Actualiser</button></div>
    </>
  )
}

/* ------------------------------------------------------------ Alertes de cours */

export function PriceAlerts({ ticker }) {
  const { data, reload } = useApi('/price-alerts')
  const [f, setF] = useState({ ticker: ticker || '', direction: 'below', price: '' })
  const [msg, setMsg] = useState(null)
  const items = (data?.items || []).filter((a) => !ticker || a.ticker === ticker)
  async function add(e) {
    e.preventDefault()
    try { await api('/price-alerts', { method: 'POST', body: { ticker: f.ticker.trim().toUpperCase(), direction: f.direction, price: Number(f.price) } }); setF({ ...f, price: '' }); setMsg({ type: 'ok', text: 'Alerte créée.' }); reload() }
    catch (err) { setMsg({ type: 'error', text: err.message }) }
  }
  return (
    <section className="panel" aria-label="Alertes de cours">
      <h2>Alertes de cours</h2>
      <form className="filters" style={{ alignItems: 'end' }} onSubmit={add}>
        {!ticker && <div className="field"><label htmlFor="pa-t">Ticker ou ISIN</label><input id="pa-t" type="text" required size="10" value={f.ticker} onChange={(e) => setF({ ...f, ticker: e.target.value })} /></div>}
        <div className="field"><label htmlFor="pa-d">Quand le cours passe</label>
          <select id="pa-d" value={f.direction} onChange={(e) => setF({ ...f, direction: e.target.value })}><option value="below">en dessous de</option><option value="above">au-dessus de</option></select></div>
        <div className="field"><label htmlFor="pa-p">Prix</label><input id="pa-p" type="number" step="0.01" min="0" required value={f.price} onChange={(e) => setF({ ...f, price: e.target.value })} /></div>
        <button type="submit" className="btn primary">Ajouter</button>
      </form>
      <Notice msg={msg} />
      {items.length > 0 && (
        <div className="table-wrap"><table>
          <thead><tr><th>Ticker</th><th>Condition</th><th className="r">Dernier cours</th><th>État</th><th></th></tr></thead>
          <tbody>{items.map((a) => (
            <tr key={a.id}><td className="ticker"><a href={`#action/${a.ticker}`}>{a.ticker}</a></td>
              <td>{a.direction === 'above' ? 'au-dessus de' : 'en dessous de'} {fr(a.price, 2)}</td>
              <td className="r num">{a.last_price == null ? '—' : fr(a.last_price, 2)}{a.currency === 'EUR' ? ' €' : ''}</td>
              <td>{a.triggered_at ? <span className="badge buy">déclenchée le {datetime(a.triggered_at)}</span> : <span className="muted">en attente</span>}</td>
              <td><button type="button" className="btn" onClick={async () => { await api(`/price-alerts/${a.id}`, { method: 'DELETE' }); reload() }}>Supprimer</button></td></tr>
          ))}</tbody>
        </table></div>
      )}
      <p className="muted small">Vérifiées à chaque collecte sur le dernier cours connu ; une alerte se déclenche une fois. Notification Telegram si votre compte est lié.</p>
    </section>
  )
}
