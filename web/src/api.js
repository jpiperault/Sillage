export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

export async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  let res
  try {
    res = await fetch('/api/v1' + path, opts)
  } catch {
    throw new ApiError(0, 'Serveur injoignable. Vérifiez votre connexion.')
  }
  let data = null
  try { data = await res.json() } catch { /* réponse vide */ }
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith('/auth/')) window.dispatchEvent(new Event('sillage:logout'))
    const msg = (data && typeof data.detail === 'string' && data.detail) || `Erreur ${res.status}`
    throw new ApiError(res.status, msg)
  }
  return data
}
