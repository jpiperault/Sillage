import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { SIDE_LABEL, SOURCE_LABEL, date, moneyCur, pct, priceCur, scoreClass, tone } from './format.js'

export function Logo({ size = 30 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="#161B22" stroke="#262D36" />
      <path d="M12 9l9 7-9 7" fill="none" stroke="#4C9AFF" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 13h5M4 19h6" stroke="#6CB6FF" strokeWidth="2" strokeLinecap="round" opacity=".7" />
    </svg>
  )
}

/** Charge une ressource de l'API ; `reload()` relance. */
export function useApi(path, deps = []) {
  const [state, setState] = useState({ data: null, error: null, loading: true })
  const seq = useRef(0)
  const load = useCallback(async () => {
    if (!path) return
    const n = ++seq.current
    setState((s) => ({ ...s, loading: true }))
    try {
      const data = await api(path)
      if (n === seq.current) setState({ data, error: null, loading: false })
    } catch (e) {
      if (n === seq.current) setState((s) => ({ data: s.data, error: e.message, loading: false }))
    }
  }, [path])
  useEffect(() => { load() }, [load, ...deps]) // eslint-disable-line react-hooks/exhaustive-deps
  return { ...state, reload: load }
}

export function Seg({ label, value, options, onChange }) {
  return (
    <div className="inline" role="group" aria-label={label}>
      <span className="inline-label">{label}</span>
      <div className="seg">
        {options.map(([v, l]) => (
          <button key={v} type="button" aria-pressed={value === v} onClick={() => onChange(v)}>{l}</button>
        ))}
      </div>
    </div>
  )
}

export function Score({ value }) {
  return <span className={`score num ${scoreClass(value)}`} title="Score de copiabilité (0-100)">{value}</span>
}

export function SideBadge({ side, plan }) {
  return (
    <>
      <span className={`badge ${side}`}>{SIDE_LABEL[side] || side}</span>
      {plan ? <> <span className="badge plan" title="Transaction programmée par un plan 10b5-1">10b5-1</span></> : null}
    </>
  )
}

export function Notice({ msg }) {
  if (!msg) return null
  return <div className={`notice ${msg.type || ''}`} role={msg.type === 'error' ? 'alert' : 'status'}>{msg.text}</div>
}

const short = (t, n = 70) => (t && t.length > n ? t.slice(0, n - 1) + '…' : t)

/** Nom + rôle ; « X (via HOLDING) » → la holding passe sur la ligne du rôle. */
function Who({ s }) {
  const m = /^(.*?) \(via (.+)\)$/.exec(s.person || '')
  const name = m ? m[1] : s.person
  const via = m ? `via ${short(m[2], 40)}` : null
  const company = s.source === 'amf' ? null : s.company
  return (
    <>
      <div>{name}</div>
      <div className="muted small" title={[s.role, via, s.company].filter(Boolean).join(' · ')}>
        {[short(s.role), via, company].filter(Boolean).join(' · ')}
      </div>
    </>
  )
}

export function SignalTable({ items, selected, onSelect, showSource = true }) {
  if (!items?.length) return <div className="empty">Aucun signal pour ces filtres. La collecte tourne toutes les quelques minutes.</div>
  return (
    <div className="table-wrap">
      <table>
        <caption className="sr-only">Signaux d'initiés et d'élus</caption>
        <thead>
          <tr>
            <th scope="col">Score</th>
            <th scope="col">Ticker</th>
            <th scope="col">Qui</th>
            <th scope="col">Sens</th>
            <th scope="col" className="r">Montant</th>
            <th scope="col" className="r">Prix</th>
            <th scope="col" className="r">Depuis</th>
            <th scope="col">Trade</th>
            <th scope="col">Déclaré</th>
            {showSource && <th scope="col">Source</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((s) => (
            <tr key={s.id} className={`clickable ${selected === s.id ? 'selected' : ''}`} onClick={() => onSelect?.(s.id)}>
              <td><Score value={s.score} /></td>
              <td>
                <button type="button" className="row-btn ticker" aria-label={`Ouvrir le ticket ${s.ticker || ''} ${s.person}`}
                  onClick={(e) => { e.stopPropagation(); onSelect?.(s.id) }}>
                  {s.ticker || (s.source === 'amf' ? <span className="wrap" style={{ display: 'block', maxWidth: 170 }}>{s.company}</span> : '—')}
                </button>
              </td>
              <td className="wrap"><Who s={s} /></td>
              <td><SideBadge side={s.side} plan={s.plan_10b5_1} /></td>
              <td className="r num">{s.amount_range || moneyCur(s.value, s.currency)}</td>
              <td className="r num">{s.price ? priceCur(s.price, s.currency) : '—'}</td>
              <td className={`r num ${tone(s.change_pct)}`}>{pct(s.change_pct)}</td>
              <td className="num">{date(s.trade_date)}</td>
              <td className="num">{date(s.filed_date)}{s.delay_days != null && <span className="muted small"> ({s.delay_days} j)</span>}</td>
              {showSource && <td className="muted small">{SOURCE_LABEL[s.source] || s.source}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
