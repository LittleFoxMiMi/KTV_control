import { computed, onBeforeUnmount, ref, watch } from 'vue'

let workerReady
const getWorker = () => {
  if (!window.isSecureContext || !navigator.serviceWorker) return Promise.resolve(null)
  if (!workerReady) {
    workerReady = (async () => {
      await navigator.serviceWorker.register('/media-buffer-sw.js', { scope: '/', updateViaCache: 'none' })
      if (!navigator.serviceWorker.controller?.scriptURL.endsWith('/media-buffer-sw.js')) {
        await new Promise((resolve, reject) => {
          const finish = () => {
            if (!navigator.serviceWorker.controller?.scriptURL.endsWith('/media-buffer-sw.js')) return
            clearTimeout(timer)
            navigator.serviceWorker.removeEventListener('controllerchange', finish)
            resolve()
          }
          const timer = setTimeout(() => {
            navigator.serviceWorker.removeEventListener('controllerchange', finish)
            reject(new Error('Media cache worker activation timed out'))
          }, 6000)
          navigator.serviceWorker.addEventListener('controllerchange', finish)
          finish()
        })
      }
      return navigator.serviceWorker.controller
    })().catch(error => { console.warn('Full-song cache unavailable', error); return null })
  }
  return workerReady
}

// URLs are assigned only after worker activation, avoiding a second native
// download racing against the cache on the first visit to /player.
export const useSongBuffer = (getSources) => {
  const sources = ref([])
  const progress = ref({})
  const unavailable = ref(false)
  let session
  let worker
  let generation = 0
  let resolveReady
  const ready = new Promise(resolve => { resolveReady = resolve })
  const stop = () => {
    if (session) (navigator.serviceWorker?.controller || worker)?.postMessage({ type: 'ktv-media-stop', session })
    session = null
  }
  const onProgress = event => {
    const data = event.data
    if (data?.type !== 'ktv-media-progress' || data.session !== session) return
    progress.value = { ...progress.value, [data.source]: data }
    if (data.state === 'error') {
      // Keep playback usable on browsers with disabled/full disk storage.
      unavailable.value = true
      const raw = getSources()
      sources.value = sources.value.map((url, i) => raw[i] && new URL(raw[i], location.href).href === data.source ? raw[i] : url)
    }
  }
  navigator.serviceWorker?.addEventListener('message', onProgress)

  const prepare = async raw => {
    const token = ++generation
    stop()
    progress.value = {}
    unavailable.value = false
    sources.value = []
    worker = await getWorker()
    if (token !== generation) return
    if (!worker) {
      unavailable.value = true
      sources.value = raw
      resolveReady()
      return
    }
    session = `${Date.now()}-${crypto.randomUUID()}`
    sources.value = raw.map(source => {
      if (!source) return ''
      const url = new URL(source, location.href)
      if (url.origin !== location.origin || !url.pathname.startsWith('/songs/')) {
        unavailable.value = true
        return source
      }
      const cached = new URL('/__ktv_media__', location.origin)
      cached.searchParams.set('session', session)
      cached.searchParams.set('source', url.href)
      return cached.href
    })
    resolveReady()
  }
  watch(getSources, prepare, { immediate: true })
  // pagehide also covers a closed/reloaded tab, where Vue does not unmount.
  const onPageHide = () => { generation++; stop() }
  const onPageShow = event => { if (event.persisted) void prepare(getSources()) }
  window.addEventListener('pagehide', onPageHide)
  window.addEventListener('pageshow', onPageShow)
  // Weak connections can take longer than one worker event's lifetime. If the
  // browser restarts the worker, resume from disk even while media is paused.
  const heartbeat = setInterval(() => {
    if (!session || unavailable.value) return
    navigator.serviceWorker?.controller?.postMessage({ type: 'ktv-media-keepalive', session,
      sources: [...new Set(getSources().filter(Boolean).map(source => new URL(source, location.href).href))] })
  }, 15000)

  const status = computed(() => {
    if (unavailable.value) return '浏览器缓冲（整曲缓存不可用）'
    const values = Object.values(progress.value)
    const expected = new Set(getSources().filter(Boolean)).size
    if (!values.length) return '正在准备整曲缓存…'
    if (values.length === expected && values.every(p => p.state === 'complete')) return '整曲已缓存'
    if (values.some(p => p.state === 'retrying')) return '网络不稳定，正在重试缓存…'
    const total = values.reduce((n, p) => n + p.total, 0)
    const loaded = values.reduce((n, p) => n + p.loaded, 0)
    return `整曲缓存 ${total ? Math.floor(loaded / total * 100) : 0}%`
  })

  onBeforeUnmount(() => {
    generation++
    stop()
    clearInterval(heartbeat)
    window.removeEventListener('pagehide', onPageHide)
    window.removeEventListener('pageshow', onPageShow)
    navigator.serviceWorker?.removeEventListener('message', onProgress)
  })
  return { sources, status, ready }
}
