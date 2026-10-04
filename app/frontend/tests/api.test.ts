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

test('same-key readers share a fetch; only the last departing consumer aborts it', async (t) => {
  let underlying: AbortSignal | null | undefined
  let respond!: (response: Response) => void
  const fetchMock = t.mock.method(globalThis, 'fetch', (_path: unknown, options: RequestInit) => {
    underlying = options.signal
    return new Promise<Response>(resolve => { respond = resolve })
  })
  const first = new AbortController(), second = new AbortController()
  const one = api('/api/shared', {signal: first.signal}), two = api('/api/shared', {signal: second.signal})
  const rejected = assert.rejects(one, {name: 'AbortError'})
  await nextTurn()
  assert.equal(fetchMock.mock.callCount(), 1)
  first.abort()
  await rejected
  assert.equal(underlying?.aborted, false)
  respond(Response.json({value: 'still owned'}))
  assert.deepEqual(await two, {value: 'still owned'})
  const last = new AbortController()
  const third = api('/api/shared', {signal: last.signal})
  const lastRejected = assert.rejects(third, {name: 'AbortError'})
  await nextTurn()
  last.abort()
  await lastRejected
  assert.equal(underlying?.aborted, true)
  assert.equal(fetchMock.mock.callCount(), 2)
})

test('reopening an abandoned key starts a new read and ignores the late old body', async (t) => {
  const responses: ((response: Response) => void)[] = []
  t.mock.method(globalThis, 'fetch', () => new Promise<Response>(resolve => responses.push(resolve)))
  const controller = new AbortController()
  const old = api('/api/reopen', {signal: controller.signal})
  const rejected = assert.rejects(old, {name: 'AbortError'})
  await nextTurn()
  controller.abort()
  await rejected
  const fresh = api('/api/reopen')
  await nextTurn()
  assert.equal(responses.length, 2)
  responses[1]!(Response.json({value: 'new'}))
  assert.deepEqual(await fresh, {value: 'new'})
  responses[0]!(Response.json({value: 'old'}))
  await nextTurn()
})

test('request identity includes headers and cache generations', async (t) => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => Response.json({ok: true}))
  await Promise.all([
    api('/api/scoped', {readScope: 'before'}), api('/api/scoped', {readScope: 'after'}),
    api('/api/scoped', {headers: {'Accept': 'application/json'}}),
  ])
  assert.equal(fetchMock.mock.callCount(), 3)
})

test('a slow response body has the same deadline and a Chinese timeout message', async (t) => {
  const timeout = new AbortController()
  t.mock.method(AbortSignal, 'timeout', () => timeout.signal)
  let bodyStarted = false
  t.mock.method(globalThis, 'fetch', async () => ({ok: true, status: 200,
    json() { bodyStarted = true; return new Promise(() => {}) },
  }) as Response)
  const rejected = assert.rejects(api('/api/slow-body'), {name: 'TimeoutError', message: '读取超时，请稍后重试'})
  await nextTurn()
  assert.equal(bodyStarted, true)
  timeout.abort(new DOMException('signal timed out', 'TimeoutError'))
  await rejected
})

test('cancellation can release a slow body without waiting for a mocked body to cooperate', async (t) => {
  const controller = new AbortController()
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => ({ok: true, status: 200,
    json() { return new Promise(() => {}) },
  }) as Response)
  const rejected = assert.rejects(api('/api/cancel-body', {signal: controller.signal}), {name: 'AbortError'})
  await nextTurn()
  controller.abort()
  await rejected
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('write timeout and network failures explain uncertain outcome and never replay', async (t) => {
  const timeout = new AbortController()
  t.mock.method(AbortSignal, 'timeout', () => timeout.signal)
  const fetchMock = t.mock.method(globalThis, 'fetch', () => new Promise<Response>(() => {}))
  const rejected = assert.rejects(write('/api/uncertain-write', {}), {name: 'TimeoutError', message: '请求超时，操作结果尚未确认，请查询状态后再试'})
  await nextTurn()
  timeout.abort(new DOMException('signal timed out', 'TimeoutError'))
  await rejected
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('a structured local read deadline reports its safe detail and never repeats the expensive query', async (t) => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => Response.json({detail: {code: 'read_timeout', message: '本地读取超时，请稍后重试'}}, {status: 504}))
  await assert.rejects(api('/api/local-budget'), {name: 'TimeoutError', message: '本地读取超时，请稍后重试', code: 'read_timeout'})
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('structured capacity and cancellation outcomes remain distinct and are not retried', async (t) => {
  for (const [code, name, message] of [['read_busy', 'Error', '本地读取繁忙，请稍后重试'], ['read_cancelled', 'AbortError', '读取已取消']]) {
    const fetchMock = t.mock.method(globalThis, 'fetch', async () => Response.json({detail: {code, message}}, {status: 503}))
    await assert.rejects(api('/api/local-outcome'), {name, message})
    assert.equal(fetchMock.mock.callCount(), 1)
    fetchMock.mock.restore()
  }
})

test('structured proxy errors still retry when they carry no final local read outcome', async (t) => {
  let count = 0
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => ++count === 1
    ? Response.json({detail: '上游暂不可用'}, {status: 504, headers: {'Retry-After': '0'}})
    : Response.json({value: 'recovered'}))
  assert.deepEqual(await api('/api/proxy-read'), {value: 'recovered'})
  assert.equal(fetchMock.mock.callCount(), 2)
})

test('connection loss during a response body remains a network error instead of malformed JSON', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({start(controller) {controller.error(new TypeError('terminated'))}})))
  await assert.rejects(api('/api/broken-body'), {name: 'TypeError', message: '无法连接本地服务，请检查程序是否仍在运行'})
})
