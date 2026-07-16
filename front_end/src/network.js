export const API_BASE = window.location.origin

export const getWebSocketUrl = (path) => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  return `${protocol}//${window.location.host}${normalizedPath}`
}
