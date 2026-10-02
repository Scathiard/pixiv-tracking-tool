import { test } from 'node:test'
import assert from 'node:assert/strict'
import { setImmediate as nextTurn } from 'node:timers/promises'
import { api, write } from '../src/api.ts'

test('a plain-text 503 reports a safe HTTP error instead of a JSON parse failure', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response('Service Unavailable', {
    status: 503,
    headers: {'Content-Type': 'text/plain', 'Retry-After': '0'},
  }))

  await assert.rejects(api('/api/archive/gallery'), (error: unknown) => {
    assert.ok(error instanceof Error)
    assert.notEqual(error.name, 'SyntaxError', error.message)
    assert.match(error.message, /503/)
    assert.match(error.message, /服务暂不可用/)
    assert.doesNotMatch(error.message, /Service Unavailable|Unexpected token|valid JSON/)
    return true
  })
})

test('successful JSON still reaches the caller unchanged', async (t) => {
  const value = {ok: true, items: [{id: '123'}]}
  t.mock.method(globalThis, 'fetch', async () => Response.json(value))
  assert.deepEqual(await api('/api/archive/gallery'), value)
})

test('a JSON HTTP error retains the server detail', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({detail: '图库暂不可读'}, {status: 409}))
  await assert.rejects(api('/api/archive/gallery'), {name: 'Error', message: '图库暂不可读'})
})

test('an already cancelled caller remains an AbortError without sending a request', async (t) => {
  const controller = new AbortController()
  controller.abort()
  const fetchMock = t.mock.method(globalThis, 'fetch', async (_path: unknown, options: RequestInit) => {
    assert.equal(options.signal?.aborted, true)
    throw new DOMException('Aborted', 'AbortError')
  })
  await assert.rejects(api('/api/archive/gallery', {signal: controller.signal}), {name: 'AbortError'})
  assert.equal(fetchMock.mock.callCount(), 0)
})

test('a failed POST is sent only once', async (t) => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => Response.json({detail: '暂时无法处理'}, {status: 503}))
  await assert.rejects(write('/api/archive/bookmarks', {id: '123'}), {message: '暂时无法处理'})
  assert.equal(fetchMock.mock.callCount(), 1)
  assert.equal(fetchMock.mock.calls[0]?.arguments[1]?.method, 'POST')
})

test('a GET recovers after a transient 503 and shares one timeout signal', async (t) => {
  const signals: (AbortSignal | null | undefined)[] = []
  const fetchMock = t.mock.method(globalThis, 'fetch', async (_path: unknown, options: RequestInit) => {
    signals.push(options.signal)
    return signals.length === 1
      ? new Response('Service Unavailable', {status: 503, headers: {'Retry-After': '0'}})
      : Response.json({ok: true, items: ['recovered']})
  })
  assert.deepEqual(await api('/api/archive/gallery'), {ok: true, items: ['recovered']})
  assert.equal(fetchMock.mock.callCount(), 2)
  assert.equal(signals[0], signals[1])
})

test('a persistently unavailable GET stops after three requests', async (t) => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response('Service Unavailable', {
    status: 503, headers: {'Retry-After': '0'},
  }))
  await assert.rejects(api('/api/archive/gallery'), {message: '服务暂不可用（503），请稍后重试'})
  assert.equal(fetchMock.mock.callCount(), 3)
})

test('aborting during the retry wait ends the GET without another request', async (t) => {
  const controller = new AbortController()
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response('Service Unavailable', {status: 503}))
  const request = api('/api/archive/gallery', {signal: controller.signal})
  const rejected = assert.rejects(request, {name: 'AbortError'})
  await nextTurn()
  assert.equal(fetchMock.mock.callCount(), 1)
  controller.abort()
  await rejected
  await nextTurn()
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('a plain-text 503 never automatically repeats a write request', async (t) => {
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response('Service Unavailable', {status: 503}))
    await assert.rejects(write('/api/archive/bookmarks', {id: '123'}, method), {
      name: 'Error', message: '服务暂不可用（503），请稍后重试',
    })
    assert.equal(fetchMock.mock.callCount(), 1, method)
    fetchMock.mock.restore()
  }
})

test('a successful response with invalid or missing JSON gives a readable error', async (t) => {
  for (const body of ['not JSON', '']) {
    const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response(body, {status: 200}))
    await assert.rejects(api('/api/archive/gallery'), {name: 'Error', message: '服务返回的数据无法读取，请重试'})
    assert.equal(fetchMock.mock.callCount(), 1)
    fetchMock.mock.restore()
  }
})

test('a JSON business failure keeps its error and is not retried', async (t) => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => Response.json({ok: false, error: '筛选条件不可用'}))
  await assert.rejects(api('/api/archive/gallery'), {message: '筛选条件不可用'})
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('default transient retries wait 350ms then 1000ms and stop at three requests', async (t) => {
  t.mock.timers.enable({apis: ['setTimeout']})
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response('Service Unavailable', {status: 503}))
  const rejected = assert.rejects(api('/api/archive/gallery'), {message: '服务暂不可用（503），请稍后重试'})
  await nextTurn()
  t.mock.timers.tick(349)
  await nextTurn()
  assert.equal(fetchMock.mock.callCount(), 1)
  t.mock.timers.tick(1)
  await nextTurn()
  assert.equal(fetchMock.mock.callCount(), 2)
  t.mock.timers.tick(999)
  await nextTurn()
  assert.equal(fetchMock.mock.callCount(), 2)
  t.mock.timers.tick(1)
  await rejected
  assert.equal(fetchMock.mock.callCount(), 3)
})

test('Retry-After is respected but capped at three seconds', async (t) => {
  t.mock.timers.enable({apis: ['setTimeout']})
  let calls = 0
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => ++calls === 1
    ? new Response('Service Unavailable', {status: 503, headers: {'Retry-After': '99'}})
    : Response.json({ok: true}))
  const request = api('/api/archive/gallery')
  await nextTurn()
  t.mock.timers.tick(2999)
  await nextTurn()
  assert.equal(fetchMock.mock.callCount(), 1)
  t.mock.timers.tick(1)
  assert.deepEqual(await request, {ok: true})
  assert.equal(fetchMock.mock.callCount(), 2)
})

test('a network failure retries a GET but never repeats a write', async (t) => {
  t.mock.timers.enable({apis: ['setTimeout']})
  let calls = 0
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => {
    if (++calls === 1) throw new TypeError('Failed to fetch')
    return Response.json({ok: true})
  })
  const request = api('/api/archive/gallery')
  await nextTurn()
  t.mock.timers.tick(350)
  assert.deepEqual(await request, {ok: true})
  assert.equal(fetchMock.mock.callCount(), 2)
  fetchMock.mock.restore()
  const writeFetch = t.mock.method(globalThis, 'fetch', async () => { throw new TypeError('Failed to fetch') })
  await assert.rejects(write('/api/archive/bookmarks', {id: '123'}), {name: 'TypeError'})
  assert.equal(writeFetch.mock.callCount(), 1)
})

test('a GET also retries transient 502 and 504 responses', async (t) => {
  let calls = 0
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => {
    const status = [502, 504, 200][calls++]!
    return status === 200 ? Response.json({ok: true})
      : new Response('Temporary failure', {status, headers: {'Retry-After': '0'}})
  })
  assert.deepEqual(await api('/api/archive/gallery'), {ok: true})
  assert.equal(fetchMock.mock.callCount(), 3)
})

test('the shared timeout ends a retry wait instead of starting another request', async (t) => {
  const controller = new AbortController()
  const timeoutMock = t.mock.method(AbortSignal, 'timeout', (duration: number) => {
    assert.equal(duration, 30000)
    return controller.signal
  })
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response('Service Unavailable', {status: 503}))
  const rejected = assert.rejects(api('/api/archive/gallery'), {name: 'TimeoutError'})
  await nextTurn()
  controller.abort(new DOMException('Timed out', 'TimeoutError'))
  await rejected
  assert.equal(fetchMock.mock.callCount(), 1)
  assert.equal(timeoutMock.mock.callCount(), 1)
})

test('an errored 503 response body cannot stop retries or replace the HTTP error', async (t) => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({
    start(controller) { controller.error(new TypeError('terminated')) },
  }), {status: 503, headers: {'Retry-After': '0'}}))
  await assert.rejects(api('/api/archive/gallery'), {name: 'Error', message: '服务暂不可用（503），请稍后重试'})
  assert.equal(fetchMock.mock.callCount(), 3)
})

test('caller cancellation remains authoritative when response cleanup also fails', async (t) => {
  const controller = new AbortController()
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({
    cancel() { controller.abort(); throw new TypeError('cleanup failed') },
  }), {status: 503}))
  await assert.rejects(api('/api/archive/gallery', {signal: controller.signal}), {name: 'AbortError'})
  assert.equal(fetchMock.mock.callCount(), 1)
})
