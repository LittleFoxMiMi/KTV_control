import test from 'node:test'
import assert from 'node:assert/strict'
import { createRemoteConnection } from '../src/remoteConnection.js'

const setup = t => {
  const sockets = []
  const statuses = []
  const messages = []
  let opens = 0
  const browser = new EventTarget()
  const page = new EventTarget()
  page.hidden = false

  class MockWebSocket {
    static CONNECTING = 0
    static OPEN = 1
    static CLOSED = 3

    constructor(url) {
      this.url = url
      this.readyState = MockWebSocket.CONNECTING
      this.sent = []
      sockets.push(this)
    }

    open() {
      this.readyState = MockWebSocket.OPEN
      this.onopen?.()
    }

    close() {
      this.readyState = MockWebSocket.CLOSED
      this.onclose?.()
    }

    send(data) {
      if (this.sendError) throw new Error('Disconnected')
      this.sent.push(data)
    }
  }

  t.mock.timers.enable({ apis: ['setTimeout'] })
  const originals = new Map()
  for (const [name, value] of Object.entries({ window: browser, document: page, WebSocket: MockWebSocket })) {
    originals.set(name, Object.getOwnPropertyDescriptor(globalThis, name))
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
  }

  const connection = createRemoteConnection({
    url: 'ws://ktv.test/ws/remote',
    onConnectionChange: connected => statuses.push(connected),
    onOpen: () => { opens++ },
    onMessage: event => messages.push(event.data),
  })
  t.after(() => {
    connection.stop()
    for (const [name, original] of originals) {
      if (original) Object.defineProperty(globalThis, name, original)
      else delete globalThis[name]
    }
  })

  const emit = name => browser.dispatchEvent(new Event(name))
  const visibility = hidden => {
    page.hidden = hidden
    page.dispatchEvent(new Event('visibilitychange'))
  }
  return { connection, sockets, statuses, messages, emit, visibility, get opens() { return opens } }
}

test('connects immediately, delivers messages and sends controls', t => {
  const state = setup(t)
  const { connection, sockets, statuses, messages } = state
  connection.start()
  assert.equal(sockets.length, 1, 'startup must not wait for HTTP requests or a timer')
  assert.equal(sockets[0].url, 'ws://ktv.test/ws/remote')
  assert.equal(connection.send('early'), false)
  sockets[0].open()
  sockets[0].onmessage({ data: 'status' })
  assert.equal(connection.send('pause'), true)
  assert.deepEqual(sockets[0].sent, ['pause'])
  assert.deepEqual(messages, ['status'])
  assert.equal(statuses.at(-1), true)
  assert.equal(state.opens, 1)
  t.mock.timers.tick(3000)
  assert.equal(sockets.length, 1, 'an open socket must not hit the handshake timeout')
})

test('returning from a long suspension immediately replaces even an OPEN stale socket', t => {
  const state = setup(t)
  const { connection, sockets, visibility, emit, statuses } = state
  connection.start()
  const old = sockets[0]
  old.open()
  const lateClose = old.onclose
  const lateMessage = old.onmessage
  // Simulate a browser whose close handshake never completes.
  old.close = () => {}
  visibility(true)
  assert.equal(statuses.at(-1), false)
  assert.equal(connection.send('hidden'), false)
  t.mock.timers.tick(60000)
  assert.equal(sockets.length, 1, 'background tabs must not keep retrying')

  visibility(false)
  assert.equal(sockets.length, 2, 'resume must reconnect synchronously without ticking a timer')
  emit('pageshow')
  emit('focus')
  assert.equal(sockets.length, 2, 'overlapping lifecycle events must share the new handshake')
  sockets[1].open()
  lateClose()
  lateMessage({ data: 'stale status' })
  assert.equal(statuses.at(-1), true)
  assert.equal(state.opens, 2)
  assert.deepEqual(state.messages, [])
  t.mock.timers.tick(3000)
  assert.equal(sockets.length, 2)
})

test('pagehide/pageshow handles the back-forward cache without duplicate connections', t => {
  const { connection, sockets, emit } = setup(t)
  connection.start()
  sockets[0].open()
  emit('pagehide')
  assert.equal(sockets[0].readyState, WebSocket.CLOSED)
  t.mock.timers.tick(10000)
  assert.equal(sockets.length, 1)
  emit('pageshow')
  emit('focus')
  assert.equal(sockets.length, 2)
})

test('an ordinary disconnect retries immediately, then backs off and resets after success', t => {
  const { connection, sockets } = setup(t)
  connection.start()
  sockets[0].open()
  sockets[0].close()
  t.mock.timers.tick(0)
  assert.equal(sockets.length, 2, 'first retry must not wait the old three seconds')
  const delays = [250, 500, 1000, 2000, 3000, 3000]
  for (const delay of delays) {
    sockets.at(-1).close()
    const count = sockets.length
    t.mock.timers.tick(delay - 1)
    assert.equal(sockets.length, count)
    t.mock.timers.tick(1)
    assert.equal(sockets.length, count + 1)
  }
  sockets.at(-1).open()
  const count = sockets.length
  sockets.at(-1).close()
  t.mock.timers.tick(0)
  assert.equal(sockets.length, count + 1, 'successful reconnection resets retry backoff')
})

test('focus and network recovery bypass a pending retry', t => {
  const { connection, sockets, emit } = setup(t)
  connection.start()
  sockets[0].close()
  t.mock.timers.tick(0)
  sockets[1].close()
  emit('focus')
  assert.equal(sockets.length, 3)
  sockets[2].open()
  emit('focus')
  assert.equal(sockets.length, 3, 'focus on a healthy connection must not reconnect')
  emit('online')
  assert.equal(sockets.length, 4, 'network recovery replaces a potentially stale OPEN socket')
  t.mock.timers.tick(250)
  assert.equal(sockets.length, 4, 'the cancelled retry must not create another connection')
})

test('a stalled handshake has a timeout instead of waiting for the browser TCP timeout', t => {
  const { connection, sockets } = setup(t)
  connection.start()
  t.mock.timers.tick(1999)
  assert.equal(sockets.length, 1)
  t.mock.timers.tick(1)
  t.mock.timers.tick(0)
  assert.equal(sockets[0].readyState, WebSocket.CLOSED)
  assert.equal(sockets.length, 2)
})

test('connection errors and send failures recover without waiting for close', t => {
  const { connection, sockets } = setup(t)
  connection.start()
  sockets[0].onerror()
  t.mock.timers.tick(0)
  assert.equal(sockets.length, 2)
  sockets[1].open()
  sockets[1].sendError = true
  assert.equal(connection.send('pause'), false)
  t.mock.timers.tick(0)
  assert.equal(sockets.length, 3)
  assert.deepEqual(sockets[1].sent, [], 'failed controls must not be replayed later')
})

test('starting while hidden waits for visibility, and stop prevents all future reconnects', t => {
  const { connection, sockets, emit, visibility, statuses } = setup(t)
  visibility(true)
  connection.start()
  emit('focus')
  emit('online')
  assert.equal(sockets.length, 0)
  visibility(false)
  assert.equal(sockets.length, 1)
  sockets[0].open()
  const lateClose = sockets[0].onclose
  connection.stop()
  lateClose()
  emit('pageshow')
  emit('focus')
  emit('online')
  visibility(true)
  visibility(false)
  t.mock.timers.tick(60000)
  assert.equal(sockets.length, 1)
  assert.equal(statuses.at(-1), false)
  assert.equal(connection.send('pause'), false)
})

for (const pendingRetry of [false, true]) {
  test(`stop cancels a pending ${pendingRetry ? 'retry' : 'handshake'}`, t => {
    const { connection, sockets } = setup(t)
    connection.start()
    if (pendingRetry) sockets[0].close()
    connection.stop()
    t.mock.timers.tick(60000)
    assert.equal(sockets.length, 1)
  })
}
