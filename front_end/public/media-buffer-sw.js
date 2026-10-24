/* Download current-song media to disk independently of the browser's buffer
 * heuristic. Only explicit /__ktv_media__ URLs use this worker. */
const CACHE_PREFIX = 'ktv-media-v1-'
const CHUNK_SIZE = 1024 * 1024
const files = new Map()
const stoppedSessions = new Set()
const CACHE_MAX_AGE_MS = 24 * 60 * 60 * 1000

self.addEventListener('install', event => event.waitUntil(self.skipWaiting()))
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()))

const pruneOldCaches = async () => {
  for (const name of await caches.keys()) {
    if (!name.startsWith(CACHE_PREFIX)) continue
    const created = Number(name.slice(CACHE_PREFIX.length).split('-')[0])
    if (created && Date.now() - created > CACHE_MAX_AGE_MS) await caches.delete(name)
  }
}

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  if (signal.aborted) return reject(signal.reason)
  const cancel = () => { clearTimeout(timer); reject(signal.reason) }
  const timer = setTimeout(() => {
    signal.removeEventListener('abort', cancel)
    resolve()
  }, ms)
  signal.addEventListener('abort', cancel, { once: true })
})

const parseRange = (value, size) => {
  if (!value) return { start: 0, end: size - 1, partial: false }
  const match = /^bytes=(\d*)-(\d*)$/.exec(value)
  if (!match || (!match[1] && !match[2])) return null
  const start = match[1] ? Number(match[1]) : Math.max(0, size - Number(match[2]))
  const end = match[1] && match[2] ? Math.min(Number(match[2]), size - 1) : size - 1
  return Number.isSafeInteger(start) && Number.isSafeInteger(end) && start <= end && start < size
    ? { start, end, partial: true } : null
}

class MediaFile {
  constructor(source, session, clientId) {
    this.source = source
    this.session = session
    this.clientId = clientId
    this.controller = new AbortController()
    this.pending = new Map()
    this.completed = new Set()
    this.loaded = 0
    this.lastNotification = 0
  }

  async notify(state, force = false) {
    if (!force && Date.now() - this.lastNotification < 300) return
    this.lastNotification = Date.now()
    const client = await self.clients.get(this.clientId)
    client?.postMessage({ type: 'ktv-media-progress', session: this.session,
      source: this.source, loaded: this.loaded, total: this.size, state })
  }

  async init() {
    this.cache = await caches.open(CACHE_PREFIX + this.session)
    const metadataKey = new URL(this.source)
    metadataKey.searchParams.set('__ktv_metadata', '1')
    const cached = await this.cache.match(metadataKey.href)
    if (cached) {
      // A worker can be suspended between browser Range requests. Persist the
      // metadata too, so a fully cached song remains usable without network.
      Object.assign(this, await cached.json())
    } else {
      const response = await fetch(this.source, {
        method: 'HEAD', cache: 'no-store', signal: this.controller.signal
      })
      this.size = Number(response.headers.get('Content-Length'))
      if (!response.ok || !Number.isSafeInteger(this.size) || this.size <= 0) {
        throw new Error('Media length unavailable')
      }
      this.type = response.headers.get('Content-Type') || 'application/octet-stream'
      const etag = response.headers.get('ETag')
      this.validator = etag && !etag.startsWith('W/') ? etag : response.headers.get('Last-Modified')
      await this.cache.put(metadataKey.href, new Response(JSON.stringify({
        size: this.size, type: this.type, validator: this.validator
      })))
    }
    await this.notify('downloading', true)
    return this
  }

  async fetchChunk(start, end) {
    const signal = this.controller.signal
    const attempt = new AbortController()
    const cancel = () => attempt.abort(signal.reason)
    signal.addEventListener('abort', cancel, { once: true })
    if (signal.aborted) cancel()
    let timer
    const resetTimeout = () => {
      clearTimeout(timer)
      timer = setTimeout(() => attempt.abort(new Error('Media download stalled')), 30000)
    }
    try {
      resetTimeout()
      const headers = { Range: `bytes=${start}-${end}` }
      if (this.validator) headers['If-Range'] = this.validator
      const response = await fetch(this.source, { headers, cache: 'no-store', signal: attempt.signal })
      if (response.status !== 206 || response.headers.get('Content-Range') !== `bytes ${start}-${end}/${this.size}`) {
        const error = new Error(`Media Range failed (${response.status})`)
        error.fatal = response.status < 500 && ![408, 429].includes(response.status)
        throw error
      }
      const data = new Uint8Array(end - start + 1)
      let offset = 0
      const reader = response.body.getReader()
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        resetTimeout()
        if (offset + value.length > data.length) throw new Error('Oversized media chunk')
        data.set(value, offset)
        offset += value.length
      }
      if (offset !== data.length) throw new Error('Incomplete media chunk')
      return data
    } finally {
      clearTimeout(timer)
      signal.removeEventListener('abort', cancel)
      attempt.abort()
    }
  }

  chunkKey(index) {
    const key = new URL(this.source)
    key.searchParams.set('__ktv_chunk', index)
    // A restarted worker must not combine chunks from different file versions.
    key.searchParams.set('__ktv_version', `${this.validator || ''}:${this.size}`)
    return key.href
  }

  chunk(index) {
    if (this.pending.has(index)) return this.pending.get(index)
    const task = this.readChunk(index).finally(() => this.pending.delete(index))
    this.pending.set(index, task)
    return task
  }

  async readChunk(index) {
    const signal = this.controller.signal
    signal.throwIfAborted()
    const key = this.chunkKey(index)
    const cached = await this.cache.match(key)
    if (cached) {
      const data = new Uint8Array(await cached.arrayBuffer())
      this.recordChunk(index, data.length)
      return data
    }
    const start = index * CHUNK_SIZE
    const end = Math.min(start + CHUNK_SIZE, this.size) - 1
    let attempt = 0
    let data
    // A temporary disconnect must not throw away already downloaded bytes.
    while (!signal.aborted) {
      try {
        data = await this.fetchChunk(start, end)
        break
      } catch (error) {
        if (signal.aborted || error.fatal) throw error
        await this.notify('retrying', true)
        await sleep(Math.min(1000 * 2 ** Math.min(attempt++, 4), 15000), signal)
      }
    }
    signal.throwIfAborted()
    // Cache writes are outside network retries: disk/quota failures fall back
    // to native playback instead of retrying the same download indefinitely.
    await this.cache.put(key, new Response(data))
    this.recordChunk(index, data.length)
    return data
  }

  recordChunk(index, size) {
    if (this.completed.has(index)) return
    this.completed.add(index)
    this.loaded += size
    void this.notify(this.loaded === this.size ? 'complete' : 'downloading', this.loaded === this.size)
  }

  download() {
    if (!this.downloadTask) {
      this.downloadTask = (async () => {
        for (let i = 0; i < Math.ceil(this.size / CHUNK_SIZE); i++) await this.chunk(i)
      })()
    }
    return this.downloadTask
  }

  response(rangeHeader) {
    const range = parseRange(rangeHeader, this.size)
    if (!range) return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${this.size}` } })
    let offset = range.start
    const stream = new ReadableStream({
      pull: async controller => {
        try {
          const index = Math.floor(offset / CHUNK_SIZE)
          const data = await this.chunk(index)
          const from = offset - index * CHUNK_SIZE
          const to = Math.min(data.length, range.end - index * CHUNK_SIZE + 1)
          controller.enqueue(data.subarray(from, to))
          offset += to - from
          if (offset > range.end) controller.close()
        } catch (error) { controller.error(error) }
      }
      // A browser suspending its own media buffer does not cancel downloading.
    })
    const headers = { 'Content-Type': this.type, 'Accept-Ranges': 'bytes',
      'Content-Length': String(range.end - range.start + 1), 'Cache-Control': 'no-store' }
    if (range.partial) headers['Content-Range'] = `bytes ${range.start}-${range.end}/${this.size}`
    return new Response(stream, { status: range.partial ? 206 : 200, headers })
  }
}

const getEntry = (session, source, clientId) => {
  const key = `${session}:${source}`
  if (!files.has(key)) {
    const file = new MediaFile(source, session, clientId)
    files.set(key, { file, ready: file.init() })
  }
  return files.get(key)
}

const keepDownloading = ({ file, ready }) => ready.then(() => file.download()).catch(error => {
  if (!file.controller.signal.aborted) {
    console.warn('Full-song cache unavailable', error)
    file.controller.abort(error)
    return file.notify('error', true)
  }
})

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url)
  if (url.origin !== self.location.origin || url.pathname !== '/__ktv_media__' || event.request.method !== 'GET') return
  const session = url.searchParams.get('session')
  let source
  try { source = new URL(url.searchParams.get('source')) } catch { return }
  if (source.origin !== self.location.origin || !source.pathname.startsWith('/songs/') || !/^[a-zA-Z0-9-]+$/.test(session || '')) return
  if (stoppedSessions.has(session)) {
    event.respondWith(new Response(null, { status: 410 }))
    return
  }
  const entry = getEntry(session, source.href, event.clientId)
  const { file, ready } = entry
  event.waitUntil(keepDownloading(entry))
  event.respondWith(ready.then(() => file.response(event.request.headers.get('Range'))).catch(() =>
    fetch(source.href, { headers: event.request.headers, signal: file.controller.signal })
  ))
})

self.addEventListener('message', event => {
  if (!['ktv-media-stop', 'ktv-media-keepalive'].includes(event.data?.type)) return
  const session = event.data.session
  if (!/^[a-zA-Z0-9-]+$/.test(session || '')) return
  if (event.data.type === 'ktv-media-keepalive') {
    if (stoppedSessions.has(session)) return
    const tasks = []
    for (const value of (event.data.sources || []).slice(0, 3)) {
      let source
      try { source = new URL(value) } catch { continue }
      if (source.origin !== self.location.origin || !source.pathname.startsWith('/songs/')) continue
      tasks.push(keepDownloading(getEntry(session, source.href, event.source.id)))
    }
    event.waitUntil(Promise.allSettled(tasks))
    event.waitUntil(pruneOldCaches().catch(() => {}))
    return
  }
  if ([...files.values()].some(entry => entry.file.session === session && entry.file.clientId !== event.source?.id)) return
  stoppedSessions.add(session)
  // Keep this bounded even when a player has been open for days.
  if (stoppedSessions.size > 100) stoppedSessions.delete(stoppedSessions.values().next().value)
  const active = []
  for (const [key, entry] of files) {
    if (entry.file.session !== session) continue
    // One tab must not cancel another tab's playback.
    if (entry.file.clientId !== event.source?.id) continue
    entry.file.controller.abort(new DOMException('Song changed', 'AbortError'))
    active.push(entry.ready, entry.file.downloadTask, ...entry.file.pending.values())
    files.delete(key)
  }
  event.waitUntil(Promise.allSettled(active).then(() => caches.delete(CACHE_PREFIX + session)))
})
