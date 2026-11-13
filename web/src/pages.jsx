import { useState } from 'react'
import { api } from './api.js'
import { Notice, Score, Seg, SignalTable, useApi } from './components.jsx'
import Ticket from './Ticket.jsx'
import { date, datetime, int, money, pct, signed, tone, usd } from './format.js'

const DAYS = [['3', '3 j'], ['7', '7 j'], ['30', '30 j'], ['90', '90 j']]

function qs(obj) {
  return '?' + new URLSearchParams(Object.entries(obj).filter(([, v]) => v !== '' && v != null)).toString()
}

/* ------------------------------------------------------------ Flux live */

export function Live({ me }) {
  const [source, setSource] = useState('all')
  const [side, setSide] = useState('buy')
  const [fast, setFast] = useState(false)
  const [days, setDays] = useState('7')
  const [sort, setSort] = useState('score')
  const [ticker, setTicker] = useState('')
  const [selected, setSelected] = useState(null)
  const path = '/signals' + qs({ source, side, fast: fast ? 'true' : '', days, sort, ticker: ticker.trim().toUpperCase(), limit: 150 })
  const { data, error, loading } = useApi(path)

  return (
    <div className="grid-live">
      <section className="panel" aria-label="Flux des signaux">
        <h2>Flux live</h2>
        <div className="filters">
          <Seg label="Source" value={source} onChange={setSource}
            options={[['all', 'Tous'], ['insider', 'Initiés'], ['congress', 'Élus']]} />
          <Seg label="Sens" value={side} onChange={setSide}
            options={[['all', 'Tous'], ['buy', 'Achats'], ['sell', 'Ventes'], ['option', 'Options']]} />
          <Seg label="Période" value={days} onChange={setDays} options={DAYS} />
          <Seg label="Tri" value={sort} onChange={setSort} options={[['score', 'Score'], ['date', 'Date']]} />
          <label className="check"><input type="checkbox" checked={fast} onChange={(e) => setFast(e.target.checked)} /> Déclarés vite</label>
          <div className="inline">
            <label className="inline-label" htmlFor="tk">Ticker</label>
            <input id="tk" type="search" size="8" placeholder="AAPL" value={ticker} onChange={(e) => setTicker(e.target.value)} />
          </div>
        </div>
        {source === 'congress' && !me.congress_enabled && <CongressOff />}
        <Notice msg={error && { type: 'error', text: error }} />
        {loading && !data ? <p className="muted">Chargement…</p> :
          <SignalTable items={data?.items} selected={selected} onSelect={setSelected} />}
        {data && <p className="muted small">{data.items.length} affichés sur {data.total}.</p>}
      </section>
      <div className="side-col">
        <Ticket id={selected} />
        <Convergence onTicker={(t) => { setTicker(t); setSource('all'); setSide('all') }} />
      </div>
    </div>
  )
}

function Convergence({ onTicker }) {
  const { data } = useApi('/convergence')
  return (
    <section className="panel" aria-label="Convergence">
      <h2>Convergence <span className="muted small">30 j</span></h2>
      <p className="muted small">Tickers où plusieurs sources se rejoignent : initiés acheteurs, élus acheteurs, 13D.</p>
      {!data?.items?.length ? <p className="muted">Pas encore de convergence.</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Ticker</th><th className="r">Initiés</th><th className="r">Élus</th><th className="r">13D</th><th className="r">Score</th></tr></thead>
            <tbody>
              {data.items.slice(0, 10).map((c) => (
                <tr key={c.ticker}>
                  <td><button type="button" className="row-btn ticker" onClick={() => onTicker(c.ticker)} title={c.name || ''}>{c.ticker}</button></td>
                  <td className="r num">{c.insiders}</td><td className="r num">{c.congress}</td>
                  <td className="r num">{c.ownership}</td><td className="r"><Score value={c.score} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

/* ------------------------------------------------------------ Entraînement */

export function Training() {
  const { data: p, error, reload } = useApi('/paper')
  const [msg, setMsg] = useState(null)

  async function sell(ticker) {
    try {
      const r = await api('/paper/orders', { method: 'POST', body: { ticker, side: 'sell' } })
      setMsg({ type: 'ok', text: `${r.ticker} vendu à ${usd(r.price)} : ${signed(r.pnl)}.` })
      reload()
    } catch (e) { setMsg({ type: 'error', text: e.message }) }
  }
  async function reset() {
    if (!window.confirm('Remettre le portefeuille fictif à zéro ? Positions et journal seront effacés.')) return
    try { await api('/paper/reset', { method: 'POST' }); setMsg({ type: 'ok', text: 'Portefeuille réinitialisé.' }); reload() }
    catch (e) { setMsg({ type: 'error', text: e.message }) }
  }

  if (error && !p) return <Notice msg={{ type: 'error', text: error }} />
  if (!p) return <p className="muted">Chargement…</p>
  return (
    <>
      <div className="stats num">
        <Stat label="Valeur totale" value={usd(p.equity)} />
        <Stat label="Performance" value={pct(p.total_pct)} cls={tone(p.total_pct)} />
        <Stat label="Liquidités" value={usd(p.cash)} />
        <Stat label="Latent" value={signed(p.unrealized)} cls={tone(p.unrealized)} />
        <Stat label="Réalisé" value={signed(p.realized)} cls={tone(p.realized)} />
        <Stat label="Gagnants" value={p.closed ? `${p.wins}/${p.closed} (${Math.round(p.win_rate)} %)` : '—'} />
      </div>
      <Notice msg={msg} />
      <section className="panel" aria-label="Positions fictives">
        <h2>Positions <span className="muted small">capital de départ {usd(p.start_cash)} · stop automatique −8 %</span></h2>
        {!p.positions.length ? <p className="empty">Aucune position. Copiez un signal en fictif depuis le Flux live.</p> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Ticker</th><th className="r">Quantité</th><th className="r">Prix moyen</th><th className="r">Cours</th>
                <th className="r">Valeur</th><th className="r">P&amp;L</th><th className="r">Stop</th><th>Ouverte</th><th></th></tr></thead>
              <tbody>
                {p.positions.map((x) => (
                  <tr key={x.ticker}>
                    <td className="ticker">{x.ticker}</td>
                    <td className="r num">{int(x.quantity)}</td>
                    <td className="r num">{usd(x.avg_price)}</td>
                    <td className="r num">{usd(x.last_price)}</td>
                    <td className="r num">{usd(x.value)}</td>
                    <td className={`r num ${tone(x.pnl)}`}>{signed(x.pnl)} ({pct(x.pnl_pct)})</td>
                    <td className="r num down">{usd(x.stop_price)}</td>
                    <td className="num">{date(x.opened_at)}</td>
                    <td><button type="button" className="btn danger" onClick={() => sell(x.ticker)}>Vendre</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="panel" aria-label="Journal des ordres">
        <h2>Journal</h2>
        {!p.orders.length ? <p className="muted">Aucun ordre.</p> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Date</th><th>Ticker</th><th>Sens</th><th className="r">Quantité</th><th className="r">Prix</th><th className="r">P&amp;L</th><th>Motif</th></tr></thead>
              <tbody>
                {p.orders.map((o) => (
                  <tr key={o.id}>
                    <td className="num">{datetime(o.created_at)}</td>
                    <td className="ticker">{o.ticker}</td>
                    <td className={o.side === 'buy' ? 'up' : 'sell'}>{o.side === 'buy' ? 'Achat' : 'Vente'}</td>
                    <td className="r num">{int(o.quantity)}</td>
                    <td className="r num">{usd(o.price)}</td>
                    <td className={`r num ${tone(o.pnl)}`}>{o.pnl == null ? '' : signed(o.pnl)}</td>
                    <td className="muted">{o.reason === 'stop' ? 'Stop −8 %' : 'Manuel'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="btn-row" style={{ marginTop: 16 }}>
          <button type="button" className="btn danger" onClick={reset}>Réinitialiser le portefeuille fictif</button>
        </div>
      </section>
    </>
  )
}

function Stat({ label, value, cls = '' }) {
  return <div className="stat"><div className="label">{label}</div><div className={`value ${cls}`}>{value}</div></div>
}

/* ------------------------------------------------------------ Insiders */

export function Insiders() {
  const [side, setSide] = useState('all')
  const [days, setDays] = useState('30')
  const [selected, setSelected] = useState(null)
  const { data, error } = useApi('/signals' + qs({ source: 'insider', side, days, sort: 'date', limit: 300 }))
  return (
    <div className="grid-live">
      <section className="panel" aria-label="Transactions d'initiés">
        <h2>Insiders <span className="muted small">dirigeants, administrateurs, actionnaires &gt; 10 % (SEC Form 4)</span></h2>
        <div className="filters">
          <Seg label="Sens" value={side} onChange={setSide}
            options={[['all', 'Tous'], ['buy', 'Achats'], ['sell', 'Ventes'], ['option', 'Options']]} />
          <Seg label="Période" value={days} onChange={setDays} options={DAYS} />
        </div>
        <Notice msg={error && { type: 'error', text: error }} />
        <SignalTable items={data?.items} selected={selected} onSelect={setSelected} showSource={false} />
      </section>
      <div className="side-col"><Ticket id={selected} /></div>
    </div>
  )
}

/* ------------------------------------------------------------ Élus */

function CongressOff() {
  return (
    <div className="notice">
      La source Congrès est inactive. L'administrateur doit ajouter la variable <code>QUIVER_API_KEY</code> (clé
      Quiver Quantitative) pour suivre les transactions des parlementaires américains.
    </div>
  )
}

export function Congress({ me }) {
  const ranking = useApi('/congress/ranking')
  const [side, setSide] = useState('all')
  const [selected, setSelected] = useState(null)
  const feed = useApi(me.congress_enabled ? '/signals' + qs({ source: 'congress', side, days: 90, sort: 'date', limit: 200 }) : null)
  if (!me.congress_enabled) return <section className="panel"><h2>Élus</h2><CongressOff /></section>
  return (
    <div className="grid-live">
      <div>
        <section className="panel" aria-label="Classement des élus">
          <h2>Classement <span className="muted small">12 derniers mois</span></h2>
          {!ranking.data?.items?.length ? <p className="muted">Pas encore de données.</p> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Élu</th><th>Parti</th><th>Chambre</th><th className="r">Trades</th><th className="r">Achats</th>
                  <th className="r">Ventes</th><th className="r">Volume min.</th><th className="r">Délai moyen</th><th>Dernière</th></tr></thead>
                <tbody>
                  {ranking.data.items.map((r) => (
                    <tr key={r.politician}>
                      <td>{r.politician}</td><td>{r.party || '—'}</td><td>{r.chamber || '—'}</td>
                      <td className="r num">{r.trades}</td><td className="r num up">{r.buys}</td><td className="r num sell">{r.sells}</td>
                      <td className="r num">{money(r.volume_min)}</td>
                      <td className="r num">{r.avg_delay != null ? `${Math.round(r.avg_delay)} j` : '—'}</td>
                      <td className="num">{date(r.last_report)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <section className="panel" aria-label="Flux des élus">
          <h2>Flux <span className="muted small">90 j</span></h2>
          <div className="filters">
            <Seg label="Sens" value={side} onChange={setSide} options={[['all', 'Tous'], ['buy', 'Achats'], ['sell', 'Ventes']]} />
          </div>
          <SignalTable items={feed.data?.items} selected={selected} onSelect={setSelected} showSource={false} />
        </section>
      </div>
      <div className="side-col"><Ticket id={selected} /></div>
    </div>
  )
}

/* ------------------------------------------------------------ Fonds */

export function Funds() {
  const [form, setForm] = useState('all')
  const [days, setDays] = useState('30')
  const { data, error } = useApi('/funds' + qs({ form, days }))
  return (
    <section className="panel" aria-label="Déclarations de fonds">
      <h2>Fonds <span className="muted small">franchissements de 5 % (13D actif, 13G passif)</span></h2>
      <div className="filters">
        <Seg label="Formulaire" value={form} onChange={setForm} options={[['all', 'Tous'], ['13D', '13D'], ['13G', '13G']]} />
        <Seg label="Période" value={days} onChange={setDays} options={DAYS} />
      </div>
      <Notice msg={error && { type: 'error', text: error }} />
      {!data?.items?.length ? <p className="empty">Aucune déclaration sur la période.</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Déposé</th><th>Formulaire</th><th>Ticker</th><th>Société visée</th><th>Déclarant</th><th></th></tr></thead>
            <tbody>
              {data.items.map((f) => (
                <tr key={f.accession}>
                  <td className="num">{date(f.filed_date)}</td>
                  <td><span className={`badge ${f.form_type.startsWith('13D') ? 'buy' : 'option'}`}>{f.form_type}</span></td>
                  <td className="ticker">{f.ticker || '—'}</td>
                  <td className="wrap">{f.subject_name || '—'}</td>
                  <td className="wrap">{f.filer_name || '—'}</td>
                  <td>{f.url && <a href={f.url} target="_blank" rel="noopener noreferrer">SEC ↗</a>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

/* ------------------------------------------------------------ Guide */

export function Guide() {
  return (
    <div className="grid-2 guide">
      <section className="panel">
        <h2>Les signaux</h2>
        <dl>
          <dt>Form 4 — initiés</dt>
          <dd>Dirigeants, administrateurs et actionnaires de plus de 10 % doivent déclarer leurs transactions sous 2 jours ouvrés. Sillage garde les achats sur le marché (P), les ventes (S) et les levées d'options (M).</dd>
          <dt>Achat sur le marché (P)</dt>
          <dd>Le signal le plus fort : l'initié met son propre argent. Les ventes ont mille raisons (impôts, diversification), elles pèsent moins.</dd>
          <dt>Plan 10b5-1</dt>
          <dd>Transaction programmée des mois à l'avance : elle ne dit rien de l'information actuelle de l'initié. Pénalité de −20.</dd>
          <dt>Cluster</dt>
          <dd>Plusieurs initiés différents qui achètent la même action en 30 jours : signal nettement plus fiable.</dd>
          <dt>13D / 13G</dt>
          <dd>Un investisseur franchit 5 % du capital. 13D = intention active (activisme, prise de contrôle), 13G = passif.</dd>
          <dt>Élus (Congrès)</dt>
          <dd>Les parlementaires américains déclarent leurs trades jusqu'à 45 jours après, par tranches de montant. Source Quiver Quantitative.</dd>
        </dl>
      </section>
      <section className="panel">
        <h2>Le score de copiabilité</h2>
        <p>Note de 0 à 100 qui résume l'intérêt de recopier un signal. Le détail des points est affiché dans chaque ticket.</p>
        <ul>
          <li>Base : achat 40, vente 12, options 5 (élus : achat 35, vente 15).</li>
          <li>Rôle : CEO, CFO, président, fondateur +15 ; autre dirigeant ou administrateur +8 ; actionnaire &gt; 10 % +5.</li>
          <li>Montant : ≥ 1 M$ +15, ≥ 250 K$ +10, ≥ 50 K$ +5.</li>
          <li>Délai de déclaration d'un achat : ≤ 2 j +10, ≤ 5 j +5.</li>
          <li>Cluster sur 30 j : 3 initiés ou plus +15, 2 +8.</li>
          <li>Plan 10b5-1 −20 ; cours déjà monté de plus de 10 % −10.</li>
        </ul>
        <h3>Copier manuellement</h3>
        <ol>
          <li>Entraînez-vous d'abord en fictif (onglet Entraînement) pour juger la méthode.</li>
          <li>Limitez chaque ligne à 1-5 % du portefeuille et posez un stop (−8 % suggéré).</li>
          <li>« Copier l'ordre broker » met l'ordre en texte dans le presse-papiers : à passer vous-même chez votre broker.</li>
        </ol>
        <p className="small">Sillage n'est pas un conseil en investissement. Les données publiques arrivent avec retard et les performances passées ne préjugent pas des performances futures.</p>
      </section>
    </div>
  )
}

/* ------------------------------------------------------------ Compte */

export function Account({ me, onLogout }) {
  const watch = useApi('/watchlist')
  const status = useApi('/status')
  const [pw, setPw] = useState({ current: '', next: '' })
  const [msg, setMsg] = useState(null)
  const [adminMsg, setAdminMsg] = useState(null)

  async function changePw(e) {
    e.preventDefault()
    try {
      const r = await api('/auth/password', { method: 'POST', body: { current_password: pw.current, new_password: pw.next } })
      setMsg({ type: 'ok', text: r.message }); setPw({ current: '', next: '' })
    } catch (err) { setMsg({ type: 'error', text: err.message }) }
  }
  async function removeWatch(t) {
    try { await api(`/watchlist/${encodeURIComponent(t)}`, { method: 'DELETE' }); watch.reload() } catch { /* */ }
  }
  async function ingest() {
    try { const r = await api('/admin/ingest', { method: 'POST' }); setAdminMsg({ type: r.ok ? 'ok' : '', text: r.message }); setTimeout(status.reload, 3000) }
    catch (err) { setAdminMsg({ type: 'error', text: err.message }) }
  }

  const st = status.data
  return (
    <div className="grid-2">
      <div>
        <section className="panel" aria-label="Mon compte">
          <h2>Mon compte</h2>
          <p>{me.user.email}{me.user.is_admin && <span className="badge buy" style={{ marginLeft: 8 }}>admin</span>}</p>
          <form onSubmit={changePw} style={{ display: 'grid', gap: 12, maxWidth: 380 }}>
            <div className="field"><label htmlFor="pw-cur">Mot de passe actuel</label>
              <input id="pw-cur" type="password" autoComplete="current-password" required value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} /></div>
            <div className="field"><label htmlFor="pw-new">Nouveau mot de passe (10 caractères minimum)</label>
              <input id="pw-new" type="password" autoComplete="new-password" minLength={10} required value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} /></div>
            <div className="btn-row"><button className="btn primary" type="submit">Changer le mot de passe</button>
              <button className="btn" type="button" onClick={onLogout}>Se déconnecter</button></div>
          </form>
          <Notice msg={msg} />
        </section>
        <section className="panel" aria-label="Watchlist">
          <h2>Watchlist</h2>
          {!watch.data?.items?.length ? <p className="muted">Vide. Ajoutez un ticker depuis un ticket de copie.</p> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Ticker</th><th>Société</th><th className="r">Cours</th><th>Dernier Form 4</th><th></th></tr></thead>
                <tbody>
                  {watch.data.items.map((w) => (
                    <tr key={w.ticker}>
                      <td className="ticker">{w.ticker}</td><td className="wrap">{w.company || '—'}</td>
                      <td className="r num">{usd(w.price)}</td><td className="num">{date(w.last_insider)}</td>
                      <td><button type="button" className="btn" onClick={() => removeWatch(w.ticker)} aria-label={`Retirer ${w.ticker}`}>Retirer</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
      <section className="panel" aria-label="État de la collecte">
        <h2>État de la collecte</h2>
        {!st ? <p className="muted">Chargement…</p> : (
          <>
            <p className="muted small">
              Collecte {st.ingest_enabled ? `toutes les ${st.interval_min} min` : 'désactivée'}
              {st.ingest_running && ' · en cours'} · Congrès {st.congress_enabled ? 'actif' : 'inactif'}
            </p>
            <div className="stats num">
              <div className="stat"><div className="label">Transactions</div><div className="value">{int(st.counts.insider_trades)}</div></div>
              <div className="stat"><div className="label">Dépôts en file</div><div className="value">{int(st.counts.filings_pending)}</div></div>
              <div className="stat"><div className="label">13D/13G</div><div className="value">{int(st.counts.ownership_filings)}</div></div>
              <div className="stat"><div className="label">Tickers connus</div><div className="value">{int(st.counts.tickers)}</div></div>
            </div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Étape</th><th>Dernier succès</th><th>Dernière erreur</th></tr></thead>
                <tbody>
                  {st.jobs.map((j) => (
                    <tr key={j.name}>
                      <td>{j.name}</td>
                      <td className="num">{datetime(j.last_ok_at)}</td>
                      <td className="wrap small">{j.last_error ? <span className="down">{datetime(j.last_error_at)} — {j.last_error}</span> : <span className="muted">aucune</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted small">Dernier dépôt lu : {date(st.counts.last_filed)} · dépôts en erreur : {st.counts.filings_error}</p>
          </>
        )}
        <div className="btn-row" style={{ marginTop: 12 }}>
          <button type="button" className="btn" onClick={status.reload}>Actualiser</button>
          {me.user.is_admin && <button type="button" className="btn primary" onClick={ingest}>Relancer la collecte</button>}
        </div>
        <Notice msg={adminMsg} />
      </section>
    </div>
  )
}
