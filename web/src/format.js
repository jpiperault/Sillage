const nf2 = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const nf0 = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 0 })
const df = new Intl.DateTimeFormat('fr-FR', { day: '2-digit', month: 'short', year: 'numeric' })
const dtf = new Intl.DateTimeFormat('fr-FR', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })

export const usd = (v) => (v == null || isNaN(v) ? '—' : nf2.format(v) + ' $')
export const int = (v) => (v == null || isNaN(v) ? '—' : nf0.format(v))

export function money(v) {
  if (v == null || isNaN(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1e9) return nf2.format(v / 1e9) + ' Md$'
  if (a >= 1e6) return nf2.format(v / 1e6) + ' M$'
  if (a >= 1e3) return nf0.format(v / 1e3) + ' K$'
  return nf0.format(v) + ' $'
}

export function pct(v, sign = true) {
  if (v == null || isNaN(v)) return '—'
  return (sign && v > 0 ? '+' : '') + nf2.format(v) + ' %'
}

export function signed(v) {
  if (v == null || isNaN(v)) return '—'
  return (v > 0 ? '+' : '') + nf2.format(v) + ' $'
}

export function date(v) {
  if (!v) return '—'
  const d = new Date(v.length === 10 ? v + 'T12:00:00' : v)
  return isNaN(d) ? v : df.format(d)
}

export function datetime(v) {
  if (!v) return '—'
  const d = new Date(v)
  return isNaN(d) ? v : dtf.format(d)
}

export const tone = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : '')

export const SIDE_LABEL = { buy: 'Achat', sell: 'Vente', option: 'Options' }

export function scoreClass(s) {
  return s >= 70 ? 'hi' : s >= 45 ? 'mid' : 'lo'
}

const nfe = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
/** Montant dans la devise du signal (dollars par défaut, euros pour l'AMF). */
export function moneyCur(v, cur) {
  if (cur !== 'EUR') return money(v)
  if (v == null || isNaN(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1e6) return nfe.format(v / 1e6) + ' M€'
  if (a >= 1e3) return nf0.format(v / 1e3) + ' K€'
  return nf0.format(v) + ' €'
}
export const priceCur = (v, cur) => (cur === 'EUR' ? (v == null || isNaN(v) ? '—' : nfe.format(v) + ' €') : usd(v))

export const SOURCE_LABEL = { insider: 'SEC Form 4', congress: 'Congrès', amf: 'AMF' }
