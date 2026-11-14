import { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import { Notice, Score, Seg, SignalTable, useApi } from './components.jsx'
import Ticket from './Ticket.jsx'
import { date, datetime, int, money, pct, tone, usd } from './format.js'

/* ------------------------------------------------------------ Backtest du score */

const HLABEL = { 21: '1 mois', 63: '3 mois', 126: '6 mois' }
const LABEL_ORDER = [
  '< 50 K$', '50-250 K$', '250 K$-1 M$', '≥ 1 M$', '≤ 2 j', '3-5 j', '> 5 j', 'seul', '2 initiés', '3 initiés ou plus',
  'Dirigeant de premier plan', 'Autre dirigeant ou administrateur', 'Actionnaire > 10 %', 'Autre',
  'première ligne / inconnue', '+ < 10 %', '+10-50 %', '+50-100 %', '+ ≥ 100 %',
  '< 10 % vendus', '10-50 % vendus', '50-99 % vendus', 'tout vendu', 'non', 'oui', '< 5 $', '≥ 5 $',
]
const rank = (l) => { const i = LABEL_ORDER.indexOf(l); return i < 0 ? 999 : i }

export function Backtest() {
  const { data, error } = useApi('/backtest')
  const [side, setSide] = useState('Achats (P)')
  const [h, setH] = useState('63')
  const rep = data?.report
  const groups = useMemo(() => {
    if (!rep) return []
    const order = ['Tous', 'Score', 'Rôle', 'Montant', 'Délai de déclaration', 'Cluster (30 j)', 'Plan 10b5-1', 'Taille vs position', "Prix d'entrée"]
    const byDim = {}
    rep.groups.filter((g) => g.side === side).forEach((g) => { (byDim[g.dimension] ||= []).push(g) })
    return order.filter((d) => byDim[d]).map((d) => [d, byDim[d].sort((a, b) => rank(a.label) - rank(b.label) || a.label.localeCompare(b.label, 'fr', { numeric: true }))])
  }, [rep, side])

  return (
    <section className="panel" aria-label="Backtest du score">
      <h2>Ce que vaut le score <span className="muted small">backtest sur l'historique des Form 4</span></h2>
      {error && <Notice msg={{ type: 'error', text: error }} />}
      {!rep ? (
        <p className="muted">
          {data?.enabled === false ? 'Backtest désactivé dans les réglages.' :
            `Calcul en cours : ${data?.progress?.quarters || 'aucun trimestre'} importé(s), ${int(data?.progress?.histories)} historiques de cours sur ${int(data?.progress?.tickers)} titres. Le premier rapport arrive après quelques cycles de collecte.`}
        </p>
      ) : (
        <>
          <p className="muted small">
            {int(rep.signals)} signaux déclarés du {date(rep.period?.[0])} au {date(rep.period?.[1])}. Achat fictif à la
            clôture qui suit la déclaration, puis écart de performance avec le S&amp;P 500 (SPY) sur la même période.
            « Moyenne » écrêtée entre −100 % et +200 % ; « gagnants » = part des signaux qui battent le S&amp;P 500.
            Mis à jour le {datetime(data.created_at)}. Les performances passées ne préjugent pas des performances futures.
          </p>
          <div className="filters">
            <Seg label="Signaux" value={side} onChange={setSide} options={[['Achats (P)', 'Achats'], ['Ventes (S)', 'Ventes']]} />
            <Seg label="Horizon" value={h} onChange={setH} options={rep.horizons.map((x) => [String(x), HLABEL[x] || `${x} séances`])} />
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Critère</th><th>Tranche</th><th className="r">Signaux</th><th className="r">Moyenne vs S&amp;P</th><th className="r">Médiane</th><th className="r">Gagnants</th></tr></thead>
              <tbody>
                {groups.map(([dim, rows]) => rows.map((g, i) => {
                  const s = g.stats[h]
                  return (
                    <tr key={dim + g.label}>
                      <td className="muted">{i === 0 ? dim : ''}</td>
                      <td>{g.label}</td>
                      <td className="r num">{s ? int(s.n) : '—'}</td>
                      <td className={`r num ${tone(s?.mean)}`}>{s ? pct(s.mean) : '—'}</td>
                      <td className={`r num ${tone(s?.median)}`}>{s ? pct(s.median) : '—'}</td>
                      <td className="r num">{s ? `${String(s.win).replace('.', ',')} %` : '—'}</td>
                    </tr>
                  )
                }))}
              </tbody>
            </table>
          </div>
          <p className="muted small">Moins de 100 signaux dans une tranche : résultat fragile.</p>
        </>
      )}
    </section>
  )
}

/* ------------------------------------------------------------ Alertes Telegram */

export function Alerts() {
  const { data, reload } = useApi('/alerts')
  const [prefs, setPrefs] = useState(null)
  const [msg, setMsg] = useState(null)
  const [link, setLink] = useState(null)
  useEffect(() => { if (data) setPrefs({ min_score: data.min_score, watchlist: data.watchlist, stops: data.stops, digest: data.digest }) }, [data])

  async function run(fn) {
    setMsg(null)
    try { await fn() } catch (e) { setMsg({ type: 'error', text: e.message }) }
  }
  if (!data || !prefs) return null
  return (
    <section className="panel" aria-label="Alertes Telegram">
      <h2>Alertes Telegram</h2>
      {!data.enabled ? (
        <p className="muted">Pas encore disponibles : l'administrateur doit régler le jeton du bot Telegram (Compte → Réglages).</p>
      ) : (
        <>
          <p className="muted small">
            Après chaque collecte : nouveaux signaux au-dessus de votre seuil, activité sur votre watchlist, stops déclenchés en fictif.
          </p>
          {data.linked ? (
            <p className="green">Telegram est lié à ce compte (bot @{data.bot}).</p>
          ) : (
            <>
              <p>1. Cliquez sur « Lier Telegram », 2. ouvrez le lien, 3. appuyez sur <b>Démarrer</b> dans Telegram.</p>
              {(link || data.link_url) && <p><a className="btn" href={link || data.link_url} target="_blank" rel="noopener noreferrer">Ouvrir @{data.bot} dans Telegram ↗</a></p>}
            </>
          )}
          <div style={{ display: 'grid', gap: 12, maxWidth: 380, marginTop: 8 }}>
            <div className="field">
              <label htmlFor="al-score">Score minimum pour être alerté</label>
              <input id="al-score" type="number" min="0" max="100" value={prefs.min_score} onChange={(e) => setPrefs({ ...prefs, min_score: e.target.value })} />
            </div>
            <label className="check"><input type="checkbox" checked={prefs.watchlist} onChange={(e) => setPrefs({ ...prefs, watchlist: e.target.checked })} /> Tout ce qui touche ma watchlist</label>
            <label className="check"><input type="checkbox" checked={prefs.stops} onChange={(e) => setPrefs({ ...prefs, stops: e.target.checked })} /> Stops déclenchés (fictif)</label>
            <label className="check"><input type="checkbox" checked={!!prefs.digest} onChange={(e) => setPrefs({ ...prefs, digest: e.target.checked })} /> Résumé quotidien le matin (aussi visible dans l'onglet Résumé)</label>
          </div>
          <div className="btn-row" style={{ marginTop: 12 }}>
            <button type="button" className="btn primary" onClick={() => run(async () => {
              await api('/alerts', { method: 'PUT', body: { ...prefs, min_score: Number(prefs.min_score) } })
              setMsg({ type: 'ok', text: 'Préférences enregistrées.' })
            })}>Enregistrer</button>
            {data.linked ? (
              <>
                <button type="button" className="btn" onClick={() => run(async () => { await api('/alerts/test', { method: 'POST' }); setMsg({ type: 'ok', text: 'Message de test envoyé.' }) })}>Envoyer un test</button>
                <button type="button" className="btn danger" onClick={() => run(async () => { await api('/alerts/link', { method: 'DELETE' }); reload() })}>Délier</button>
              </>
            ) : (
              <button type="button" className="btn" onClick={() => run(async () => { const r = await api('/alerts/link', { method: 'POST' }); setLink(r.link_url); window.open(r.link_url, '_blank', 'noopener') })}>Lier Telegram</button>
            )}
            {!data.linked && <button type="button" className="btn" onClick={reload}>J'ai appuyé sur Démarrer</button>}
          </div>
        </>
      )}
      <Notice msg={msg} />
    </section>
  )
}

/* ------------------------------------------------------------ Réglages (admin) */

export function Settings() {
  const { data, error, reload } = useApi('/admin/settings')
  const [draft, setDraft] = useState({})
  const [msg, setMsg] = useState(null)
  const sections = useMemo(() => {
    const out = {}
    ;(data?.items || []).forEach((it) => { (out[it.section] ||= []).push(it) })
    return Object.entries(out)
  }, [data])

  async function save(values) {
    setMsg(null)
    try {
      const r = await api('/admin/settings', { method: 'PUT', body: { values } })
      setDraft({}); setMsg({ type: 'ok', text: r.message }); reload()
    } catch (e) { setMsg({ type: 'error', text: e.message }) }
  }
  if (error) return <section className="panel"><Notice msg={{ type: 'error', text: error }} /></section>
  if (!data) return null
  const changed = Object.keys(draft).length > 0
  return (
    <section className="panel" aria-label="Réglages">
      <h2>Réglages <span className="muted small">administrateur</span></h2>
      <p className="muted small">Ces valeurs priment sur les variables d'environnement. « Rétablir » revient à la variable ou au défaut.</p>
      {sections.map(([section, items]) => (
        <fieldset key={section} style={{ border: '1px solid var(--border)', borderRadius: 8, margin: '12px 0', padding: '8px 12px' }}>
          <legend className="muted small">{section}</legend>
          {items.map((it) => {
            const id = 'set-' + it.key
            const val = draft[it.key] ?? (it.secret ? '' : it.type === 'bool' ? (it.value ? 'oui' : 'non') : String(it.value ?? ''))
            return (
              <div key={it.key} className="field" style={{ margin: '10px 0' }}>
                <label htmlFor={id}>{it.label} {it.source === 'interface' && <span className="badge buy">interface</span>}</label>
                {it.type.startsWith('choice:') ? (
                  <select id={id} value={val} onChange={(e) => setDraft({ ...draft, [it.key]: e.target.value })}>
                    {it.type.slice(7).split(',').map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                ) : it.type === 'bool' ? (
                  <select id={id} value={val} onChange={(e) => setDraft({ ...draft, [it.key]: e.target.value })}>
                    <option value="oui">oui</option><option value="non">non</option>
                  </select>
                ) : (
                  <input id={id} type={it.secret ? 'password' : 'text'} autoComplete="off" value={val}
                    placeholder={it.secret ? (it.is_set ? `réglé (${it.value})` : 'non réglé') : ''}
                    onChange={(e) => setDraft({ ...draft, [it.key]: e.target.value })} />
                )}
                <div className="inline">
                  {it.help && <span className="muted small">{it.help}</span>}
                  {it.source === 'interface' && <button type="button" className="link-btn small" onClick={() => save({ [it.key]: null })}>Rétablir</button>}
                </div>
              </div>
            )
          })}
        </fieldset>
      ))}
      <div className="btn-row">
        <button type="button" className="btn primary" disabled={!changed} onClick={() => save(draft)}>Enregistrer</button>
        <button type="button" className="btn" onClick={async () => {
          setMsg({ text: 'Test de l\'accès au Sénat en cours…' })
          try { const r = await api('/admin/senate-test', { method: 'POST' }); setMsg({ type: r.ok ? 'ok' : 'error', text: r.message }) }
          catch (e) { setMsg({ type: 'error', text: e.message }) }
        }}>Tester l'accès au Sénat</button>
        {changed && <button type="button" className="btn" onClick={() => setDraft({})}>Annuler</button>}
      </div>
      <Notice msg={msg} />
    </section>
  )
}

/* ------------------------------------------------------------ Fiche action */

function PriceChart({ history, signals }) {
  const W = 760, H = 240, P = { l: 52, r: 12, t: 12, b: 26 }
  if (!history?.length) return <p className="muted">Pas d'historique de cours disponible pour ce titre.</p>
  const xs = history.map((p) => new Date(p.d + 'T12:00:00').getTime())
  const ys = history.map((p) => p.c)
  const x0 = xs[0], x1 = xs[xs.length - 1], y0 = Math.min(...ys) * 0.97, y1 = Math.max(...ys) * 1.03
  const X = (t) => P.l + ((t - x0) / Math.max(1, x1 - x0)) * (W - P.l - P.r)
  const Y = (v) => P.t + (1 - (v - y0) / Math.max(1e-9, y1 - y0)) * (H - P.t - P.b)
  const path = history.map((p, i) => `${i ? 'L' : 'M'}${X(xs[i]).toFixed(1)},${Y(p.c).toFixed(1)}`).join('')
  const marks = (signals || []).filter((s) => s.filed_date && (s.side === 'buy' || s.side === 'sell')).map((s) => {
    const t = new Date(s.filed_date + 'T12:00:00').getTime()
    if (t < x0 || t > x1) return null
    let i = xs.findIndex((v) => v >= t); if (i < 0) i = xs.length - 1
    return { x: X(xs[i]), y: Y(ys[i]), s }
  }).filter(Boolean)
  const ticks = [y0, (y0 + y1) / 2, y1]
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Cours sur un an avec les signaux">
      {ticks.map((v) => (
        <g key={v}>
          <line x1={P.l} x2={W - P.r} y1={Y(v)} y2={Y(v)} stroke="var(--border)" />
          <text x={P.l - 6} y={Y(v) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">{v.toFixed(v < 10 ? 2 : 0)}</text>
        </g>
      ))}
      <text x={P.l} y={H - 6} fontSize="11" fill="var(--muted)">{date(history[0].d)}</text>
      <text x={W - P.r} y={H - 6} fontSize="11" fill="var(--muted)" textAnchor="end">{date(history[history.length - 1].d)}</text>
      <path d={path} fill="none" stroke="var(--accent)" strokeWidth="1.8" />
      {marks.map((m, i) => (
        <circle key={i} cx={m.x} cy={m.y} r="5" fill={m.s.side === 'buy' ? 'var(--up)' : 'var(--sell)'} stroke="var(--bg)" strokeWidth="1.5">
          <title>{`${date(m.s.filed_date)} — ${m.s.side === 'buy' ? 'achat' : 'vente'} ${m.s.person}`}</title>
        </circle>
      ))}
    </svg>
  )
}

const MOVE = { new: 'Nouvelle', added: 'Renforcée', reduced: 'Allégée', sold: 'Soldée', held: 'Inchangée' }

export function StockPage({ ticker }) {
  const { data, error, reload } = useApi(`/ticker/${encodeURIComponent(ticker)}`)
  const [selected, setSelected] = useState(null)
  const [msg, setMsg] = useState(null)
  if (error) return <Notice msg={{ type: 'error', text: error }} />
  if (!data) return <p className="muted">Chargement de {ticker}…</p>
  const lastShort = data.short[data.short.length - 1]
  const avgShort = data.short.length ? data.short.slice(-20).reduce((a, b) => a + b.pct, 0) / Math.min(20, data.short.length) : null
  const first = data.history[0]?.c, last = data.history[data.history.length - 1]?.c
  async function toggleWatch() {
    try { await api(`/watchlist/${encodeURIComponent(data.ticker)}`, { method: data.in_watchlist ? 'DELETE' : 'PUT' }); reload() }
    catch (e) { setMsg({ type: 'error', text: e.message }) }
  }
  return (
    <div className="grid-live">
      <div>
        <section className="panel" aria-label={`Fiche ${data.ticker}`}>
          <h2><span className="ticker">{data.ticker}</span> <span className="muted small">{data.name || ''}</span></h2>
          <div className="stats num">
            <div className="stat"><div className="label">Cours</div><div className="value">{data.quote ? (data.quote.currency === 'EUR' ? `${data.quote.price.toFixed(2).replace('.', ',')} €` : usd(data.quote.price)) : '—'}</div></div>
            {data.market_cap && <div className="stat"><div className="label">Capitalisation</div><div className="value">{money(data.market_cap)}</div></div>}
            {data.next_earnings && <div className="stat"><div className="label">Prochains résultats</div><div className="value">{date(data.next_earnings.day)}</div>{data.next_earnings.timing && <div className="muted small">{data.next_earnings.timing}</div>}</div>}
            <div className="stat"><div className="label">Sur 1 an</div><div className={`value ${tone(last - first)}`}>{first ? pct((last / first - 1) * 100) : '—'}</div></div>
            <div className="stat"><div className="label">Signaux (1 an)</div><div className="value">{data.signals.length}</div></div>
            <div className="stat"><div className="label">Vente à découvert</div><div className="value">{lastShort ? `${String(lastShort.pct).replace('.', ',')} %` : '—'}</div>
              {avgShort != null && <div className="muted small">moyenne 20 j : {avgShort.toFixed(1).replace('.', ',')} %</div>}</div>
          </div>
          <PriceChart history={data.history} signals={data.signals} />
          <p className="muted small"><span className="up">●</span> achat déclaré · <span className="sell">●</span> vente déclarée (date de déclaration)</p>
          <div className="btn-row">
            <button type="button" className="btn" onClick={toggleWatch}>{data.in_watchlist ? '− Watchlist' : '+ Watchlist'}</button>
            <a className="btn" href="#live">← Flux live</a>
          </div>
          <Notice msg={msg} />
        </section>
        <section className="panel" aria-label="Signaux sur ce titre">
          <h2>Signaux <span className="muted small">12 derniers mois</span></h2>
          <SignalTable items={data.signals} selected={selected} onSelect={setSelected} />
        </section>
        {data.holders.length > 0 && (
          <section className="panel" aria-label="Grands fonds actionnaires">
            <h2>Grands fonds actionnaires <span className="muted small">13F, dernier trimestre connu</span></h2>
            <div className="table-wrap"><table>
              <thead><tr><th>Fonds</th><th>Trimestre</th><th>Mouvement</th><th className="r">Actions</th><th className="r">Valeur</th><th className="r">Poids</th></tr></thead>
              <tbody>{data.holders.map((h) => (
                <tr key={h.fund_name}><td className="wrap">{h.fund_name}</td><td className="num">{date(h.period)}</td>
                  <td>{MOVE[h.status]}</td><td className="r num">{int(h.shares)}</td><td className="r num">{money(h.value)}</td>
                  <td className="r num">{h.weight_pct != null ? pct(h.weight_pct, false) : '—'}</td></tr>
              ))}</tbody>
            </table></div>
          </section>
        )}
        {(data.contracts?.amount_12m > 0 || data.buybacks?.length > 0) && (
          <section className="panel" aria-label="Contrats publics et rachats">
            <h2>Rachats d'actions et contrats publics</h2>
            <ul className="reasons">
              {data.buybacks.map((b) => <li key={b.accession}>{date(b.filed)} — annonce de rachat d'actions (8-K)</li>)}
              {data.contracts?.amount_12m > 0 && <li>Contrats fédéraux américains sur 12 mois : <b>{money(Number(data.contracts.amount_12m))}</b> (USAspending, recherche « {data.contracts.search_name} »{data.contracts.recipients?.length ? ` : ${data.contracts.recipients.map((r) => r.name).slice(0, 3).join(', ')}` : ''})</li>}
            </ul>
          </section>
        )}
        {(data.notices.length > 0 || data.ownership.length > 0) && (
          <section className="panel" aria-label="Autres déclarations">
            <h2>Autres déclarations</h2>
            <ul className="reasons">
              {data.notices.map((n) => <li key={n.accession}>{date(n.filed_date)} — Form 144 : {n.person} ({n.relationship || '—'}) annonce une vente de {money(n.market_value)}{n.plan_10b5_1 ? ' (plan 10b5-1)' : ''}</li>)}
              {data.ownership.map((o) => <li key={o.accession}>{date(o.filed_date)} — {o.form_type} : {o.filer_name || '—'} {o.url && <a href={o.url} target="_blank" rel="noopener noreferrer">SEC ↗</a>}</li>)}
            </ul>
          </section>
        )}
      </div>
      <div className="side-col"><Ticket id={selected} /></div>
    </div>
  )
}

export { Score }
