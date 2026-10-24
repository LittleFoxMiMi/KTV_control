const CONNECT_TIMEOUT_MS = 2000
const RETRY_BASE_MS = 250
const RETRY_MAX_MS = 3000

// Mobile browsers can leave a suspended socket reporting OPEN even after its
// network connection is gone. Retire it on page hide and reconnect on resume.
export const createRemoteConnection = ({ url, onConnectionChange, onOpen, onMessage }) => {
  let socket = null
  let retryTimer = null
  let connectTimer = null
  let retryAttempt = 0
  let stopped = true
  let pageHidden = false

  const clearTimers = () => {
    clearTimeout(retryTimer)
    clearTimeout(connectTimer)
    retryTimer = null
    connectTimer = null
  }

  const retireSocket = () => {
    clearTimers()
    const previous = socket
    socket = null
    if (previous) {
      previous.onopen = previous.onmessage = previous.onerror = previous.onclose = null
      // Start the next socket without waiting for the old close handshake.
      previous.close()
    }
    onConnectionChange(false)
  }

  const canConnect = () => !stopped && !pageHidden && !document.hidden

  const scheduleRetry = () => {
    if (!canConnect() || retryTimer !== null) return
    const delay = retryAttempt === 0 ? 0 : Math.min(RETRY_BASE_MS * 2 ** (retryAttempt - 1), RETRY_MAX_MS)
    retryAttempt++
    retryTimer = setTimeout(() => {
      retryTimer = null
      connect()
    }, delay)
  }

  const connect = () => {
    if (!canConnect() || socket) return
    clearTimers()
    let current
    try {
      current = new WebSocket(url)
    } catch {
      scheduleRetry()
      return
    }
    socket = current

    const failed = () => {
      if (socket !== current || stopped) return
      retireSocket()
      scheduleRetry()
    }
    connectTimer = setTimeout(failed, CONNECT_TIMEOUT_MS)
    current.onopen = () => {
      if (socket !== current || stopped) return
      clearTimers()
      retryAttempt = 0
      onConnectionChange(true)
      onOpen()
    }
    current.onmessage = event => {
      if (socket === current && !stopped) onMessage(event)
    }
    current.onerror = failed
    current.onclose = failed
  }

  const suspend = () => {
    pageHidden = true
    retireSocket()
  }

  const resume = () => {
    if (stopped || document.hidden) return
    pageHidden = false
    retryAttempt = 0
    // Lifecycle events override a pending retry, but share an in-flight socket.
    clearTimeout(retryTimer)
    retryTimer = null
    connect()
  }

  const onVisibilityChange = () => {
    if (document.hidden) suspend()
    else resume()
  }

  const onOnline = () => {
    if (stopped || document.hidden) return
    // A network change can invalidate a socket that still reports OPEN.
    retireSocket()
    resume()
  }

  return {
    start() {
      if (!stopped) return
      stopped = false
      pageHidden = document.hidden
      document.addEventListener('visibilitychange', onVisibilityChange)
      window.addEventListener('pagehide', suspend)
      window.addEventListener('pageshow', resume)
      window.addEventListener('focus', resume)
      window.addEventListener('online', onOnline)
      resume()
    },
    stop() {
      stopped = true
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('pagehide', suspend)
      window.removeEventListener('pageshow', resume)
      window.removeEventListener('focus', resume)
      window.removeEventListener('online', onOnline)
      retireSocket()
    },
    send(data) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return false
      try {
        socket.send(data)
        return true
      } catch {
        retireSocket()
        scheduleRetry()
        return false
      }
    },
  }
}
