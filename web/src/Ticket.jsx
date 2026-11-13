import { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import { Notice, Score, SideBadge, useApi } from './components.jsx'
import { date, money, pct, tone, usd } from './format.js'

const STOP = 0.08

export default function Ticket({ id, onChanged }) {
  const { data: s, error, loading, reload } = useApi(id ? `/signals/${id}` : null)
  const paper = useApi('/paper', [id])
  const [alloc, setAlloc] = useState(2)
  const [qty, setQty] = useState('')
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)

  const price = s?.quote?.price ?? s?.current_price ?? null
  const equity = paper.data?.equity ?? 0

  useEffect(() => {
    setMsg(null)
  }, [id])

  useEffect(() => {
    if (price && equity) setQty(String(Math.max(1, Math.floor((equity * alloc) / 100 / price))))
  }, [price, equity, alloc])

  const stop = price ? price * (1 - STOP) : null
  const cost = price && Number(qty) > 0 ? price * Number(qty) : null
  const brokerText = useMemo(() => {
    if (!s?.ticker || !price) return ''
    const verb = s.side === 'sell' ? 'VENTE' : 'ACHAT'
    return `${verb} ${qty || 0} ${s.ticker} au marché (≈ ${usd(price)}) — stop ${usd(stop)} (−8 %) — signal Sillage : ${s.person}, ${s.role}, déclaré le ${date(s.filed_date)}`
  }, [s, price, qty, stop])

  if (!id) {
    return (
      <section className="panel ticket" aria-label="Ticket de copie">
        <h2>Ticket de copie</h2>
        <p className="muted">Choisissez un signal dans la liste pour préparer un ordre fictif ou recopier l'ordre chez votre broker.</p>
      </section>
    )
  }
  if (error && !s) return <section className="panel"><Notice msg={{ type: 'error', text: error }} /></section>
  if (!s) return <section className="panel" aria-busy="true"><p className="muted">Chargement du signal…</p></section>

  async function order(side) {
    setBusy(true)
    setMsg(null)
    try {
      const body = side === 'buy'
        ? { ticker: s.ticker, side, quantity: Number(qty), signal_ref: s.id }
        : { ticker: s.ticker, side }
      const r = await api('/paper/orders', { method: 'POST', body })
      setMsg({
        type: 'ok',
        text: side === 'buy'
          ? `Achat fictif : ${r.quantity} ${r.ticker} à ${usd(r.price)}${r.stale_price ? ' (dernier cours connu)' : ''}.`
          : `Position fictive vendue : ${r.quantity} ${r.ticker} à ${usd(r.price)}, résultat ${r.pnl >= 0 ? '+' : ''}${usd(r.pnl)}.`,
      })
      paper.reload()
      reload()
      onChanged?.()
    } catch (e) {
      setMsg({ type: 'error', text: e.message })
    } finally {
      setBusy(false)
    }
  }

  async function copyBroker() {
    try {
      await navigator.clipboard.writeText(brokerText)
      setMsg({ type: 'ok', text: 'Ordre copié dans le presse-papiers : collez-le chez votre broker.' })
    } catch {
      setMsg({ type: 'error', text: 'Copie impossible ici. Sélectionnez le texte : ' + brokerText })
    }
  }

  async function toggleWatch() {
    try {
      await api(`/watchlist/${encodeURIComponent(s.ticker)}`, { method: s.in_watchlist ? 'DELETE' : 'PUT' })
      reload()
      setMsg({ type: 'ok', text: s.in_watchlist ? `${s.ticker} retiré de la watchlist.` : `${s.ticker} ajouté à la watchlist.` })
    } catch (e) {
      setMsg({ type: 'error', text: e.message })
    }
  }

  return (
    <section className="panel ticket" aria-label={`Ticket ${s.ticker || ''}`} aria-busy={loading}>
      <h2>
        <span className="ticker">{s.ticker || '—'}</span>
        <SideBadge side={s.side} plan={s.plan_10b5_1} />
        <Score value={s.score} />
      </h2>
      <div className="muted small">{s.company || ''}</div>
      <div>{s.person} <span className="muted">· {s.role}</span></div>

      <dl className="kv num">
        <dt>Prix au trade</dt><dd>{s.price ? usd(s.price) : '—'}</dd>
        <dt>Cours actuel</dt>
        <dd>{price ? usd(price) : 'indisponible'}{s.quote?.stale && <span className="muted small"> (ancien)</span>}</dd>
        <dt>Depuis le trade</dt><dd className={tone(s.change_pct)}>{pct(s.change_pct)}</dd>
        <dt>Date du trade</dt><dd>{date(s.trade_date)}</dd>
        <dt>Déclaré le</dt><dd>{date(s.filed_date)}</dd>
        <dt>Délai de déclaration</dt><dd>{s.delay_days != null ? `${s.delay_days} j` : '—'}</dd>
        <dt>Montant</dt><dd>{s.amount_range || money(s.value)}</dd>
        {s.shares != null && <><dt>Actions</dt><dd>{Math.round(s.shares).toLocaleString('fr-FR')}</dd></>}
      </dl>

      <h3>Pourquoi ce score</h3>
      <ul className="reasons">{s.reasons.map((r) => <li key={r}>{r}</li>)}</ul>

      {s.ticker ? (
        <>
          <div className="alloc">
            <div className="seg" role="group" aria-label="Allocation du portefeuille fictif">
              {[1, 2, 5].map((p) => (
                <button key={p} type="button" aria-pressed={alloc === p} onClick={() => setAlloc(p)}>{p} %</button>
              ))}
            </div>
            <div className="field" style={{ flex: 1, minWidth: 110 }}>
              <label htmlFor="qty">Quantité</label>
              <input id="qty" type="number" min="1" step="1" inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value)} />
            </div>
          </div>
          <dl className="kv num">
            <dt>Montant estimé</dt><dd>{usd(cost)}</dd>
            <dt>Stop suggéré (−8 %)</dt><dd className="down">{usd(stop)}</dd>
            <dt>Liquidités fictives</dt><dd>{usd(paper.data?.cash)}</dd>
            {s.position && <><dt>Ma position fictive</dt><dd>{s.position.quantity} @ {usd(s.position.avg_price)}</dd></>}
          </dl>
          <div className="btn-row">
            <button type="button" className="btn primary" disabled={busy || !price || !(Number(qty) > 0)} onClick={() => order('buy')}>Copier en fictif</button>
            <button type="button" className="btn danger" disabled={busy || !s.position} onClick={() => order('sell')}>Vendre ma position fictive</button>
            <button type="button" className="btn" disabled={!brokerText} onClick={copyBroker}>Copier l'ordre broker</button>
            <button type="button" className="btn" aria-pressed={!!s.in_watchlist} onClick={toggleWatch}>{s.in_watchlist ? '− Watchlist' : '+ Watchlist'}</button>
          </div>
        </>
      ) : (
        <p className="muted">Pas de ticker coté pour cet émetteur : copie impossible.</p>
      )}
      <Notice msg={msg} />
      {s.url && <p className="small"><a href={s.url} target="_blank" rel="noopener noreferrer">{s.source === 'insider' ? 'Voir le dépôt sur le site de la SEC ↗' : 'Voir la déclaration (Greffe de la Chambre) ↗'}</a></p>}
    </section>
  )
}
