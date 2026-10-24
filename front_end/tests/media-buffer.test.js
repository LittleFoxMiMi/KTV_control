import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const code = readFileSync(new URL('../public/media-buffer-sw.js', import.meta.url), 'utf8')
const source = 'https://ktv.example/songs/youtube/sample/video.mp4'

const setup = ({ failOnce = false, quotaFailure = false, chunkQuotaFailure = false } = {}) => {
  const bytes = Uint8Array.from({ length: 2 * 1024 * 1024 + 137 }, (_, i) => i % 251)
  const requests = []
  const headRequests = []
  const notifications = []
  const stored = new Map()
  const handlers = {}
  let attempts = 0
  const cache = {
    match: async key => stored.get(key)?.clone(),
    put: async (key, response) => {
      if (quotaFailure || (chunkQuotaFailure && key.includes('__ktv_chunk='))) throw new DOMException('Full disk', 'QuotaExceededError')
      stored.set(key, response)
    }
  }
  const context = vm.createContext({
    URL, Response, ReadableStream, Uint8Array, AbortController, DOMException,
    console, setTimeout: (callback, delay) => setTimeout(callback, delay === 30000 ? delay : 0), clearTimeout,
    caches: { open: async () => cache, delete: async () => { stored.clear(); return true } },
    self: {
      location: { origin: 'https://ktv.example' },
      addEventListener: (name, callback) => { handlers[name] = callback },
      clients: { get: async () => ({ postMessage: data => notifications.push(data) }) }
    },
    fetch: async (url, options) => {
      options.signal?.throwIfAborted()
      if (options.method === 'HEAD') {
        headRequests.push(url)
        return new Response(null, { headers: {
          'Content-Length': String(bytes.length), 'Content-Type': 'video/mp4', ETag: '"sample-v1"'
        } })
      }
      requests.push(options.headers.Range)
      if (failOnce && attempts++ === 0) throw new TypeError('Disconnected')
      assert.equal(options.headers['If-Range'], '"sample-v1"')
      const [, start, end] = /bytes=(\d+)-(\d+)/.exec(options.headers.Range).map(Number)
      return new Response(bytes.slice(start, end + 1), { status: 206,
        headers: { 'Content-Range': `bytes ${start}-${end}/${bytes.length}` } })
    }
  })
  vm.runInContext(`${code}\nglobalThis.api = { MediaFile, parseRange, files };`, context)
  return { ...context.api, bytes, requests, headRequests, notifications, stored, handlers }
}

test('byte ranges include closed, open, suffix and invalid ranges', () => {
  const { parseRange } = setup()
  const range = header => JSON.parse(JSON.stringify(parseRange(header, 100)))
  assert.deepEqual(range(null), { start: 0, end: 99, partial: false })
  assert.deepEqual(range('bytes=10-20'), { start: 10, end: 20, partial: true })
  assert.deepEqual(range('bytes=90-'), { start: 90, end: 99, partial: true })
  assert.deepEqual(range('bytes=-10'), { start: 90, end: 99, partial: true })
  assert.deepEqual(range('bytes=90-500'), { start: 90, end: 99, partial: true })
  for (const value of ['bytes=100-', 'bytes=20-10', 'bytes=-0', 'bytes=-', 'bytes=0-1,2-3']) {
    assert.equal(parseRange(value, 100), null)
  }
})

test('playback starts from the first chunk; suspending playback still downloads to EOF without duplicate requests', async () => {
  const { MediaFile, bytes, requests, notifications } = setup()
  const file = await new MediaFile(source, 'session', 'client').init()
  const background = file.download()
  const response = file.response('bytes=0-')
  assert.equal(response.status, 206)
  assert.equal(response.headers.get('Content-Length'), String(bytes.length))
  const reader = response.body.getReader()
  const first = await reader.read()
  assert.deepEqual(first.value, bytes.slice(0, 1024 * 1024))
  await reader.cancel()
  await background
  assert.equal(file.loaded, bytes.length)
  assert.equal(requests.length, 3)
  assert.equal(new Set(requests).size, 3)
  assert.equal(notifications.at(-1).state, 'complete')
  const count = requests.length
  const offline = file.response('bytes=1048500-1048700')
  assert.deepEqual(new Uint8Array(await offline.arrayBuffer()), bytes.slice(1048500, 1048701))
  assert.equal(requests.length, count, 'cached playback must not need the network')
  assert.equal(file.response('bytes=999999999-').status, 416)
})

test('temporary disconnect retries a chunk and retains completed disk chunks', async () => {
  const { MediaFile, bytes, requests, headRequests, notifications } = setup({ failOnce: true })
  const file = await new MediaFile(source, 'session', 'client').init()
  await file.download()
  assert.equal(file.loaded, bytes.length)
  assert.equal(requests.length, 4)
  assert.equal(requests[0], requests[1])
  assert.ok(notifications.some(n => n.state === 'retrying'))
  const restarted = await new MediaFile(source, 'session', 'client').init()
  await restarted.download()
  assert.equal(restarted.loaded, bytes.length)
  assert.equal(requests.length, 4, 'worker restart reuses completed disk chunks')
  assert.equal(headRequests.length, 1, 'worker restart does not need even a network HEAD')
})

test('disk quota failures do not trigger repeated downloads', async () => {
  const { MediaFile, requests } = setup({ quotaFailure: true })
  await assert.rejects(new MediaFile(source, 'session', 'client').init(), { name: 'QuotaExceededError' })
  assert.equal(requests.length, 0)
  const fullWhileDownloading = setup({ chunkQuotaFailure: true })
  const file = await new fullWhileDownloading.MediaFile(source, 'session', 'client').init()
  await assert.rejects(file.download(), { name: 'QuotaExceededError' })
  assert.equal(fullWhileDownloading.requests.length, 1)
})

test('stopping the song aborts work and deletes its disk cache', async () => {
  const { MediaFile, handlers, files, stored, requests } = setup()
  const file = await new MediaFile(source, 'session', 'client').init()
  await file.chunk(0)
  files.set('session:file', { file, ready: Promise.resolve(file) })
  let cleanup
  handlers.message({ data: { type: 'ktv-media-stop', session: 'session' },
    source: { id: 'client' }, waitUntil: promise => { cleanup = promise } })
  await cleanup
  assert.equal(file.controller.signal.aborted, true)
  assert.equal(files.size, 0)
  assert.equal(stored.size, 0)
  await assert.rejects(file.chunk(1), { name: 'AbortError' })
  assert.equal(requests.length, 1)
})

test('keepalive resumes a suspended worker from disk without fetching the song again', async () => {
  const { MediaFile, handlers, files, requests, headRequests } = setup()
  const file = await new MediaFile(source, 'session', 'client').init()
  await file.download()
  const count = requests.length
  const tasks = []
  handlers.message({ data: { type: 'ktv-media-keepalive', session: 'session', sources: [source] },
    source: { id: 'client' }, waitUntil: task => tasks.push(task) })
  await Promise.all(tasks)
  assert.equal(files.size, 1)
  assert.equal(requests.length, count)
  assert.equal(headRequests.length, 1)
})

test('another player tab cannot stop an active cache session', async () => {
  const { MediaFile, handlers, files, stored } = setup()
  const file = await new MediaFile(source, 'session', 'client').init()
  await file.chunk(0)
  files.set('session:file', { file, ready: Promise.resolve(file) })
  handlers.message({ data: { type: 'ktv-media-stop', session: 'session' },
    source: { id: 'other-client' }, waitUntil: () => assert.fail('must not delete another tab cache') })
  assert.equal(file.controller.signal.aborted, false)
  assert.equal(files.size, 1)
  assert.ok(stored.size > 0)
})
