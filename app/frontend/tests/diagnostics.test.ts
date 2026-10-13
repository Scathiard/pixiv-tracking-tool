import { test } from 'node:test'
import assert from 'node:assert/strict'
import { setImmediate as nextTurn } from 'node:timers/promises'
import { BrowserDiagnostics, diagnostics, safeFields, category, errorType, browserError, mediaObservation } from '../src/diagnostics.ts'
import { api } from '../src/api.ts'
import { LatestIntent, ReadLifecycle } from '../src/interaction.ts'

test('diagnostic fields never include arbitrary strings, URLs, messages or stacks', () => {
  assert.deepEqual(safeFields({url: '/secret?q=search', message: 'secret', stack: 'secret', category: 'search',
    operation_id: 'a'.repeat(32), server_trace: 'b'.repeat(12), request_id: 'not an id', count: Infinity,
    bytes: -1, outcome: 'error', error_type: 'TypeError', error_source: 'vue', visible: true}),
  {operation_id: 'a'.repeat(32), server_trace: 'b'.repeat(12), outcome: 'error', error_type: 'TypeError', error_source: 'vue', visible: true})
  assert.equal(category('/api/library?q=private'), 'gallery')
  assert.equal(errorType(new TypeError('private')), 'TypeError')
  assert.equal(errorType({message: 'private', name: 'TypeError'}), 'Other')
})

test('disabled capability adds no uploads and never blocks a business request', async t => {
  const observer = new BrowserDiagnostics()
  const calls: string[] = []
  t.mock.method(globalThis, 'fetch', async (path: string) => { calls.push(path); return new Response(null, {status: 404}) })
  const discovery = observer.start()
  assert.equal(observer.observing, true)
  observer.emit('browser.summary'); await discovery; await observer.flush()
  assert.equal(observer.enabled, false)
  assert.equal(observer.observing, false)
  assert.deepEqual(calls, ['/api/diagnostics'])
})

test('a stalled capability probe is independent of ordinary business requests', async t => {
  const observer = new BrowserDiagnostics()
  let finish!: (response: Response) => void
  t.mock.method(globalThis, 'fetch', async (path: string, options?: RequestInit) => {
    if (path === '/api/diagnostics') return new Promise<Response>(resolve => { finish = resolve })
    assert.equal(new Headers(options?.headers).has('X-Pixiv-Client-Session'), false)
    return Response.json({ok: true})
  })
  const start = observer.start()
  assert.deepEqual(await api('/api/independent'), {ok: true})
  finish(new Response(null, {status: 404})); await start
})

test('bounded batches report overflow and failed uploads without recursive retries', async t => {
  const observer = new BrowserDiagnostics()
  t.after(() => observer.stop())
  const batches: {events: unknown[]; dropped: number; observer_errors: number}[] = []
  t.mock.method(globalThis, 'fetch', async (path: string, options?: RequestInit) => {
    if (path === '/api/diagnostics') return Response.json({enabled: true, run_id: 'a'.repeat(12), build: 'synthetic'})
    const body = String(options?.body)
    assert.ok(new TextEncoder().encode(body).length <= 32768)
    const batch = JSON.parse(body); batches.push(batch)
    assert.ok(batch.events.length <= 32)
    assert.match(batch.session, /^[0-9a-f]{32}$/)
    if (batches.length === 1) throw new TypeError('private upload failure')
    return new Response(null, {status: 204})
  })
  await observer.start()
  for (let count = 0; count < 300; count++) observer.emit('browser.summary', {count})
  await observer.flush()
  await nextTurn()
  assert.equal(batches.length, 1, 'no immediate replay or recursive upload')
  await observer.flush()
  assert.equal(batches[0]!.events.length, 32)
  assert.equal(batches[0]!.dropped, 45)
  assert.equal(batches[1]!.dropped, 77)
  assert.equal(batches[1]!.observer_errors, 1)
  for (let index = 0; index < 10; index++) await observer.flush()
  assert.equal(batches.flatMap(batch => batch.events).length, 256)
})

test('enabled diagnostics correlate retries, sharing, cancellation, discarded results and media safely', async t => {
  t.after(() => diagnostics.stop())
  const events: Record<string, unknown>[] = [], headers: Headers[] = []
  let requests = 0, respond!: (response: Response) => void, confirm!: (response: Response) => void
  t.mock.method(globalThis, 'fetch', async (path: string, options?: RequestInit) => {
    if (path === '/api/diagnostics') return new Promise<Response>(resolve => { confirm = resolve })
    if (path === '/api/diagnostics/events') { events.push(...JSON.parse(String(options?.body)).events); return new Response(null, {status: 204}) }
    headers.push(new Headers(options?.headers)); requests++
    if (path.includes('pending')) return new Promise<Response>(resolve => { respond = resolve })
    return requests === 1 ? new Response('secret proxy body', {status: 503, headers: {'Retry-After': '0'}})
      : Response.json({ok: true}, {headers: {'X-Pixiv-Request-Id': 'b'.repeat(12), 'X-Pixiv-Diagnostic-Run': 'a'.repeat(12)}})
  })
  const discovery = diagnostics.start()
  const first = new AbortController(), second = new AbortController()
  const one = api('/api/library?search=private', {signal: first.signal}), two = api('/api/library?search=private', {signal: second.signal})
  assert.deepEqual(await one, {ok: true}); await two
  assert.equal(requests, 2, 'sharing is unchanged even though each retry has a unique header')
  assert.notEqual(headers[0]!.get('X-Pixiv-Client-Request'), headers[1]!.get('X-Pixiv-Client-Request'))
  assert.equal(headers[0]!.get('X-Pixiv-Client-Session'), diagnostics.session)
  await diagnostics.flush(); assert.equal(events.length, 0, 'pending events are not uploaded before capability confirmation')
  confirm(Response.json({enabled: true, run_id: 'a'.repeat(12), build: 'synthetic'})); await discovery
  const intent = new LatestIntent(), ticket = intent.begin()
  const old = api('/api/pending', {signal: ticket.signal})
  const rejected = assert.rejects(old, {name: 'AbortError'})
  await nextTurn(); intent.cancel(); await rejected; assert.equal(ticket.current(), false)
  respond(Response.json({secret: 'late'})); await nextTurn()
  const lifecycle = new ReadLifecycle()
  let resolveOld!: (value: string) => void
  const stale = lifecycle.run('old', signal => {
    void api('/api/pending-again', {signal}).catch(() => undefined)
    return new Promise<string>(resolve => { resolveOld = resolve })
  }, () => assert.fail('stale committed'), () => {})
  await nextTurn(); lifecycle.dispose(); resolveOld('secret'); await stale
  browserError(new TypeError('secret Vue message'), true)
  const media = mediaObservation('/secret-image.png')
  media?.cancel('unmounted')
  for (let index = 0; index < 8; index++) await diagnostics.flush()
  const begins = events.filter(event => event.event === 'api.begin')
  assert.equal(begins[0]!.operation_id, begins[1]!.operation_id)
  assert.notEqual(begins[0]!.request_id, begins[1]!.request_id)
  assert.ok(events.some(event => event.event === 'api.response' && event.server_trace === 'b'.repeat(12)))
  assert.ok(events.some(event => event.event === 'api.parsed' && event.outcome === 'ok'))
  assert.ok(events.some(event => event.event === 'api.retry' && event.reason === 'http'))
  assert.ok(events.some(event => event.event === 'api.end' && event.outcome === 'cancelled'))
  assert.ok(events.some(event => event.event === 'shared.join' && event.consumer_count === 2))
  assert.ok(events.some(event => event.event === 'shared.abandoned'))
  assert.ok(events.some(event => event.event === 'api.discarded'))
  assert.ok(events.some(event => event.event === 'browser.error' && event.error_source === 'vue'))
  assert.ok(events.some(event => event.event === 'media.cancel' && event.reason === 'unmounted'))
  assert.doesNotMatch(JSON.stringify(events), /private|secret|\/api\/|\/secret-image|stack|message/)
})
