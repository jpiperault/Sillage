import { Fragment, useEffect, useState } from 'react'
import { api } from './api.js'
import { Notice, Score, Seg, SignalTable, useApi } from './components.jsx'
import Ticket from './Ticket.jsx'
import { date, datetime, int, money, pct, signed, tone, usd } from './format.js'
import { PositionEditor, RuleSimulator } from './v21.jsx'

export function ExportLink({ href, label = 'Exporter en CSV' }) {
  return <a className="btn" href={'/api/v1' + href} download>{label}</a>
}

/* ------------------------------------------------------------ Résumé (tableau de bord) */

export function Summary() {
  const { data, error } = useApi('/summary')
  const [selected, setSelected] = useState(null)
  if (error) return <Notice msg={{ type: 'error', text: error }} />
  if (!data) return <p className="muted">Chargement du résumé…</p>
  const BOOK = { manual: 'Mon portefeuille fictif', rule: 'Règle automatique' }
  return (
    <div className="grid-live">
      <div>
        <div className="stats num">
          <div className="stat"><div className="label">Signaux en 24 h</div><div className="value">{int(data.counts.signals_24h)}</div></div>
          <div className="stat"><div className="label">Achats en 24 h</div><div className="value">{int(data.counts.buys_24h)}</div></div>
          {Object.entries(data.books).map(([b, s]) => (
            <div className="stat" key={b}><div className="label">{BOOK[b]}</div>
              <div className={`value ${tone(s.total_pct)}`}>{pct(s.total_pct)}</div>
              <div className="muted small">{usd(s.equity)} · {s.positions} ligne(s)</div></div>
          ))}
        </div>
        <section className="panel" aria-label="Meilleurs achats des dernières 24 heures">
          <h2>Meilleurs achats <span className="muted small">déclarés depuis hier · {date(data.date)}</span></h2>
          <SignalTable items={data.top_buys} selected={selected} onSelect={setSelected} />
        </section>
        {data.watch_signals.length > 0 && (
          <section className="panel" aria-label="Activité sur ma watchlist et mes positions">
            <h2>Sur ma watchlist et mes positions <span className="muted small">3 derniers jours</span></h2>
            <SignalTable items={data.watch_signals} selected={selected} onSelect={setSelected} />
          </section>
        )}
        <div className="grid-2" style={{ marginTop: 16 }}>
          <section className="panel" aria-label="Grosses ventes annoncées">
            <h2>Grosses ventes annoncées <span className="muted small">Form 144, 2 jours</span></h2>
            {!data.notices.length ? <p className="muted">Aucune.</p> : (
              <ul className="reasons">{data.notices.map((n) => (
                <li key={n.accession}><a href={`#action/${n.ticker}`}><b>{n.ticker || '—'}</b></a> {money(n.market_value)} — {n.person} ({n.relationship || '—'}){n.plan_10b5_1 ? ', plan 10b5-1' : ''}</li>
              ))}</ul>
            )}
          </section>
          <section className="panel" aria-label="Résultats à venir">
            <h2>Résultats à venir <span className="muted small">watchlist et positions, 14 jours</span></h2>
            {!data.earnings.length ? <p className="muted">Aucune publication prévue.</p> : (
              <ul className="reasons">{data.earnings.map((e) => (
                <li key={e.ticker + e.day}><a href={`#action/${e.ticker}`}><b>{e.ticker}</b></a> le {date(e.day)}{e.timing ? ` (${e.timing})` : ''}</li>
              ))}</ul>
            )}
            {data.price_alerts?.length > 0 && (<>
              <h3>Alertes de cours déclenchées</h3>
              <ul className="reasons">{data.price_alerts.map((a) => <li key={a.id}><a href={`#action/${a.ticker}`}><b>{a.ticker}</b></a> {a.direction === 'above' ? 'au-dessus de' : 'en dessous de'} {Number(a.price)} (à {Number(a.triggered_price)}, {datetime(a.triggered_at)})</li>)}</ul>
            </>)}
            {data.new_13f.length > 0 && (<>
              <h3>Nouveaux 13F (7 jours)</h3>
              <ul className="reasons">{data.new_13f.map((f) => <li key={f.fund_name + f.period}>{f.fund_name} — trimestre au {date(f.period)}</li>)}</ul>
            </>)}
          </section>
        </div>
      </div>
      <div className="side-col"><Ticket id={selected} /></div>
    </div>
  )
}

/* ------------------------------------------------------------ Courbe de valeur */

export function EquityChart({ curve }) {
  if (!curve || curve.length < 2) return <p className="muted small">La courbe apparaît après deux jours de suivi.</p>
  const W = 760, H = 200, P = { l: 48, r: 12, t: 10, b: 24 }
  const all = curve.flatMap((p) => [p.pct, p.spy_pct]).filter((v) => v != null)
  const y0 = Math.min(0, ...all) - 1, y1 = Math.max(0, ...all) + 1
  const X = (i) => P.l + (i / (curve.length - 1)) * (W - P.l - P.r)
  const Y = (v) => P.t + (1 - (v - y0) / (y1 - y0)) * (H - P.t - P.b)
  const line = (key) => curve.map((p, i) => (p[key] == null ? null : `${X(i).toFixed(1)},${Y(p[key]).toFixed(1)}`)).filter(Boolean).join(' L')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Performance du portefeuille comparée au S&P 500">
      <line x1={P.l} x2={W - P.r} y1={Y(0)} y2={Y(0)} stroke="var(--border)" />
      {[y0, y1].map((v) => <text key={v} x={P.l - 6} y={Y(v) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">{v.toFixed(0)} %</text>)}
      <text x={P.l} y={H - 6} fontSize="11" fill="var(--muted)">{date(curve[0].d)}</text>
      <text x={W - P.r} y={H - 6} fontSize="11" fill="var(--muted)" textAnchor="end">{date(curve[curve.length - 1].d)}</text>
      <path d={'M' + line('spy_pct')} fill="none" stroke="var(--muted)" strokeWidth="1.5" strokeDasharray="4 3" />
      <path d={'M' + line('pct')} fill="none" stroke="var(--accent)" strokeWidth="2" />
    </svg>
  )
}

/* ------------------------------------------------------------ Entraînement */

const BOOKS = [['manual', 'Mon portefeuille'], ['rule', 'Règle automatique']]

export function TrainingV2() {
  const [book, setBook] = useState('manual')
  const { data: p, error, reload } = useApi(`/paper?book=${book}`)
  const [msg, setMsg] = useState(null)
  const [editing, setEditing] = useState(null)

  async function sell(ticker) {
    try {
      const r = await api('/paper/orders', { method: 'POST', body: { ticker, side: 'sell', book } })
      setMsg({ type: 'ok', text: `${r.ticker} vendu à ${usd(r.price)} : ${signed(r.pnl)}.` }); reload()
    } catch (e) { setMsg({ type: 'error', text: e.message }) }
  }
  async function reset() {
    if (!window.confirm('Remettre ce portefeuille fictif à zéro ? Positions, journal et courbe seront effacés.')) return
    try { await api(`/paper/reset?book=${book}`, { method: 'POST' }); setMsg({ type: 'ok', text: 'Portefeuille réinitialisé.' }); reload() }
    catch (e) { setMsg({ type: 'error', text: e.message }) }
  }
  return (
    <>
      <div className="filters">
        <Seg label="Portefeuille" value={book} onChange={(b) => { setBook(b); setMsg(null) }} options={BOOKS} />
      </div>
      {book === 'rule' && <><RuleSimulator /><RuleForm onSaved={reload} /></>}
      {error && !p && <Notice msg={{ type: 'error', text: error }} />}
      {p && (<>
        <div className="stats num">
          <div className="stat"><div className="label">Valeur totale</div><div className="value">{usd(p.equity)}</div></div>
          <div className="stat"><div className="label">Performance</div><div className={`value ${tone(p.total_pct)}`}>{pct(p.total_pct)}</div></div>
          <div className="stat"><div className="label">Liquidités</div><div className="value">{usd(p.cash)}</div></div>
          <div className="stat"><div className="label">Latent</div><div className={`value ${tone(p.unrealized)}`}>{signed(p.unrealized)}</div></div>
          <div className="stat"><div className="label">Réalisé</div><div className={`value ${tone(p.realized)}`}>{signed(p.realized)}</div></div>
          <div className="stat"><div className="label">Gagnants</div><div className="value">{p.closed ? `${p.wins}/${p.closed} (${Math.round(p.win_rate)} %)` : '—'}</div></div>
        </div>
        <Notice msg={msg} />
        <section className="panel" aria-label="Courbe de valeur">
          <h2>Courbe <span className="muted small">— portefeuille <span style={{ color: 'var(--accent)' }}>━</span> · S&amp;P 500 (SPY) <span>┅</span>, en % depuis le premier jour suivi</span></h2>
          <EquityChart curve={p.curve} />
        </section>
        <section className="panel" aria-label="Positions fictives">
          <h2>Positions <span className="muted small">capital de départ {usd(p.start_cash)} · stop automatique −8 % · montants en dollars</span></h2>
          {!p.positions.length ? <p className="empty">{book === 'rule' ? 'Aucune position : la règle achète au prochain signal qui passe ses critères.' : 'Aucune position. Copiez un signal en fictif depuis le Flux live.'}</p> : (
            <div className="table-wrap"><table>
              <thead><tr><th>Ticker</th><th className="r">Quantité</th><th className="r">Prix moyen</th><th className="r">Cours</th>
                <th className="r">Valeur</th><th className="r">P&amp;L</th><th className="r">Stop</th><th>Ouverte</th><th></th></tr></thead>
              <tbody>{p.positions.map((x) => (<Fragment key={x.ticker}>
                <tr>
                  <td className="ticker"><a href={`#action/${x.ticker}`}>{x.ticker}</a>{x.currency === 'EUR' && <span className="muted small"> €→$</span>}</td>
                  <td className="r num">{int(x.quantity)}</td><td className="r num">{usd(x.avg_price)}</td>
                  <td className="r num">{usd(x.last_price)}</td><td className="r num">{usd(x.value)}</td>
                  <td className={`r num ${tone(x.pnl)}`}>{signed(x.pnl)} ({pct(x.pnl_pct)})</td>
                  <td className="r num down">{usd(x.stop_price)}{x.trailing_pct != null && <div className="muted small">suiveur {x.trailing_pct} %</div>}{x.target_price != null && <div className="up small">objectif {usd(x.target_price)}</div>}</td>
                  <td className="num">{date(x.opened_at)}{x.note && <div className="muted small wrap" style={{ maxWidth: 200 }}>{x.note}</div>}</td>
                  <td><div className="btn-row">
                    <button type="button" className="btn" aria-expanded={editing === x.ticker} onClick={() => setEditing(editing === x.ticker ? null : x.ticker)}>Gérer</button>
                    <button type="button" className="btn danger" onClick={() => sell(x.ticker)}>Tout vendre</button>
                  </div></td>
                </tr>
                {editing === x.ticker && <tr><td colSpan={9}><PositionEditor pos={x} book={book} onDone={(t) => { setEditing(null); setMsg({ type: 'ok', text: t }); reload() }} /></td></tr>}
              </Fragment>))}</tbody>
            </table></div>
          )}
        </section>
        <section className="panel" aria-label="Journal des ordres">
          <h2>Journal</h2>
          {!p.orders.length ? <p className="muted">Aucun ordre.</p> : (
            <div className="table-wrap"><table>
              <thead><tr><th>Date</th><th>Ticker</th><th>Sens</th><th className="r">Quantité</th><th className="r">Prix</th><th className="r">P&amp;L</th><th>Motif</th></tr></thead>
              <tbody>{p.orders.map((o) => (
                <tr key={o.id}>
                  <td className="num">{datetime(o.created_at)}</td><td className="ticker">{o.ticker}</td>
                  <td className={o.side === 'buy' ? 'up' : 'sell'}>{o.side === 'buy' ? 'Achat' : 'Vente'}</td>
                  <td className="r num">{int(o.quantity)}</td><td className="r num">{usd(o.price)}</td>
                  <td className={`r num ${tone(o.pnl)}`}>{o.pnl == null ? '' : signed(o.pnl)}</td>
                  <td className="muted wrap small">{{ stop: 'Stop −8 %', rule: 'Règle', horizon: 'Horizon atteint' }[o.reason] || 'Manuel'}{o.note ? ` — ${o.note}` : ''}</td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
          <div className="btn-row" style={{ marginTop: 16 }}>
            <ExportLink href={`/export/paper.csv?book=${book}`} />
            <button type="button" className="btn danger" onClick={reset}>Réinitialiser ce portefeuille</button>
          </div>
        </section>
      </>)}
    </>
  )
}

function RuleForm({ onSaved }) {
  const { data } = useApi('/paper/rule')
  const [f, setF] = useState(null)
  const [msg, setMsg] = useState(null)
  useEffect(() => { if (data) setF(data) }, [data])
  if (!f) return null
  const SRC = [['insider', 'Initiés US'], ['congress', 'Élus'], ['amf', 'France'], ['bafin', 'Allemagne']]
  async function save(enabled) {
    try {
      const r = await api('/paper/rule', { method: 'PUT', body: { enabled, min_score: Number(f.min_score), alloc_pct: Number(f.alloc_pct), hold_days: Number(f.hold_days), max_positions: Number(f.max_positions), sources: f.sources } })
      setF(r); setMsg({ type: 'ok', text: enabled ? 'Règle active : elle s\'applique aux nouveaux signaux à chaque collecte.' : 'Règle arrêtée.' }); onSaved?.()
    } catch (e) { setMsg({ type: 'error', text: e.message }) }
  }
  return (
    <section className="panel" aria-label="Règle automatique">
      <h2>Règle automatique {f.enabled ? <span className="badge buy">active</span> : <span className="badge option">arrêtée</span>}</h2>
      <p className="muted small">Achète en fictif chaque nouvel achat déclaré dont le score atteint le seuil, avec une part fixe du portefeuille ; revend à l'horizon choisi ou au stop −8 %.</p>
      <div className="filters" style={{ alignItems: 'end' }}>
        <div className="field"><label htmlFor="r-score">Score minimum</label><input id="r-score" type="number" min="0" max="100" value={f.min_score} onChange={(e) => setF({ ...f, min_score: e.target.value })} /></div>
        <div className="field"><label htmlFor="r-alloc">Part par ligne (%)</label><input id="r-alloc" type="number" min="0.5" max="20" step="0.5" value={f.alloc_pct} onChange={(e) => setF({ ...f, alloc_pct: e.target.value })} /></div>
        <div className="field"><label htmlFor="r-hold">Horizon (jours)</label><input id="r-hold" type="number" min="5" max="400" value={f.hold_days} onChange={(e) => setF({ ...f, hold_days: e.target.value })} /></div>
        <div className="field"><label htmlFor="r-max">Lignes maximum</label><input id="r-max" type="number" min="1" max="100" value={f.max_positions} onChange={(e) => setF({ ...f, max_positions: e.target.value })} /></div>
      </div>
      <div className="filters">
        {SRC.map(([k, l]) => (
          <label key={k} className="check"><input type="checkbox" checked={f.sources.includes(k)}
            onChange={(e) => setF({ ...f, sources: e.target.checked ? [...f.sources, k] : f.sources.filter((x) => x !== k) })} /> {l}</label>
        ))}
      </div>
      <div className="btn-row">
        <button type="button" className="btn primary" onClick={() => save(true)}>{f.enabled ? 'Enregistrer' : 'Activer la règle'}</button>
        {f.enabled && <button type="button" className="btn danger" onClick={() => save(false)}>Arrêter</button>}
      </div>
      <Notice msg={msg} />
    </section>
  )
}

/* ------------------------------------------------------------ Score v2 */

export function ModelPanel() {
  const [side, setSide] = useState('P')
  const { data } = useApi(`/model?side=${side}`)
  const m = data?.model
  return (
    <section className="panel" aria-label="Score v2">
      <h2>Score v2 <span className="muted small">calibré sur le backtest</span></h2>
      <div className="filters"><Seg label="Source" value={side} onChange={setSide} options={[['P', 'Initiés US'], ['congress', 'Élus'], ['europe', 'Europe']]} /></div>
      {!m ? <p className="muted">{side === 'P' ? 'Pas encore de modèle : il est entraîné avec le premier rapport de backtest complet.' : "Pas encore de modèle : il faut au moins 300 achats dont la performance à 3 mois est connue. L'historique se construit au fil de la collecte."}</p> : (
        <>
          <p>
            {data.in_use ? <span className="badge buy">utilisé</span> : <span className="badge option">non utilisé</span>}{' '}
            <span className="muted small">
              {m.metrics.validated ? 'Validé' : 'Non validé'} sur la période test ({date(m.metrics.test_period[0])} → {date(m.metrics.test_period[1])}, {int(m.metrics.test)} achats jamais vus à l'entraînement) ·
              AUC {String(m.metrics.auc).replace('.', ',')} (0,5 = hasard) · réglage « Score affiché » : {data.version}
            </span>
          </p>
          <div className="stats num">
            <div className="stat"><div className="label">Tous les achats (test)</div><div className="value">{String(m.metrics.test_all.win).replace('.', ',')} %</div><div className="muted small">battent le marché à 3 mois</div></div>
            <div className="stat"><div className="label">20 % les mieux notés</div><div className="value up">{String(m.metrics.test_top20.win).replace('.', ',')} %</div><div className="muted small">médiane {pct(m.metrics.test_top20.median)}</div></div>
            <div className="stat"><div className="label">20 % les moins bien notés</div><div className="value down">{String(m.metrics.test_bottom20.win).replace('.', ',')} %</div><div className="muted small">médiane {pct(m.metrics.test_bottom20.median)}</div></div>
          </div>
          <details>
            <summary className="muted small" style={{ cursor: 'pointer', minHeight: 44, display: 'flex', alignItems: 'center' }}>Poids de chaque critère</summary>
            <div className="table-wrap"><table>
              <thead><tr><th>Critère</th><th className="r">Poids</th></tr></thead>
              <tbody>{m.weights.map((w) => (
                <tr key={w.feature}><td>{w.label}</td><td className={`r num ${tone(w.weight)}`}>{w.weight > 0 ? '+' : ''}{String(w.weight.toFixed(3)).replace('.', ',')}</td></tr>
              ))}</tbody>
            </table></div>
          </details>
          <p className="muted small">Le score v2 = rang centile de la probabilité estimée de battre la référence de marché à 3 mois ({side === 'P' ? 'S&P 500 au-dessus de 2 Md$ de capitalisation, Russell 2000 en dessous' : side === 'congress' ? 'S&P 500' : 'ETF CAC 40'}). Une source sans modèle validé garde le score v1.</p>
        </>
      )}
    </section>
  )
}

/* ------------------------------------------------------------ Track records */

export function TopPeople({ kind }) {
  const [order, setOrder] = useState('best')
  const { data } = useApi(`/tracks?kind=${kind}&order=${order}&min_n=${kind === 'insider' ? 3 : 3}`)
  return (
    <section className="panel" aria-label={kind === 'insider' ? 'Meilleurs initiés' : 'Track record des élus'}>
      <h2>{kind === 'insider' ? 'Track record des initiés' : 'Track record des élus'} <span className="muted small">achats comparés au marché à 3 mois, au moins 3 achats mesurés</span></h2>
      <div className="filters"><Seg label="Classement" value={order} onChange={setOrder} options={[['best', 'Les plus justes'], ['worst', 'Les moins justes'], ['most', 'Les plus actifs']]} /></div>
      {!data?.items?.length ? <p className="muted">Pas encore assez d'historique (il se construit avec le backtest).</p> : (
        <div className="table-wrap"><table>
          <thead><tr><th>{kind === 'insider' ? 'Initié' : 'Élu'}</th><th className="r">Achats</th><th className="r">Battent le marché</th><th className="r">Médiane</th><th className="r">Moyenne</th><th>Dernier</th></tr></thead>
          <tbody>{data.items.slice(0, 30).map((t) => (
            <tr key={t.person_key}><td className="wrap">{t.name}</td><td className="r num">{t.n}</td>
              <td className={`r num ${t.win_rate >= 60 ? 'up' : t.win_rate <= 40 ? 'down' : ''}`}>{String(t.win_rate).replace('.', ',')} %</td>
              <td className={`r num ${tone(Number(t.median_63))}`}>{pct(Number(t.median_63))}</td>
              <td className={`r num ${tone(Number(t.mean_63))}`}>{pct(Number(t.mean_63))}</td>
              <td className="num">{date(t.last_trade)}</td></tr>
          ))}</tbody>
        </table></div>
      )}
    </section>
  )
}

export { Score }
