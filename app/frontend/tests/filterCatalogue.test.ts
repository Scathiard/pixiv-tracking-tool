import test from 'node:test'
import assert from 'node:assert/strict'
import { FilterCatalogue } from '../src/state/filterCatalogue.ts'
import { orderedTags, TagOrderPreference } from '../src/state/tagOrder.ts'
import { selectionQuery, toggleSelection } from '../src/state/filterSelection.ts'

test('warm, panel remount and concurrent consumers share completed and in-flight choices', async context => {
  const calls: string[] = []
  const cache = new FilterCatalogue(async kind => {
    calls.push(kind)
    return (kind === 'artists' ? [{id: 1, pixiv_id: '1', name: 'artist', mode: 'paused'}] : {tags: ['tag']}) as never
  })
  context.after(() => cache.dispose())
  const unsubscribe = cache.subscribe(() => {})
  await Promise.all([cache.warm(), cache.warm(), cache.load('tags')])
  const first = cache.snapshot()
  unsubscribe()
  const release = cache.subscribe(() => {})
  await cache.warm()
  assert.deepEqual(calls, ['artists', 'tags'])
  assert.equal(cache.snapshot().tags.value, first.tags.value)
  release()
})

test('catalogue changes discard an old flight and coalesce the replacement', async context => {
  let resolveOld: (value: never) => void = () => {}
  let calls = 0
  const cache = new FilterCatalogue(async kind => {
    if (kind === 'artists') return [] as never
    calls++
    if (calls === 1) return await new Promise<never>(resolve => { resolveOld = resolve })
    return {tags: ['new']} as never
  }, {minimumRefresh: 0, refreshDelay: 100})
  context.after(() => cache.dispose())
  const first = cache.load('tags')
  await Promise.resolve()
  cache.invalidate('tags'); cache.invalidate('tags'); cache.invalidate('tags')
  resolveOld({tags: ['old']} as never)
  await first
  assert.equal(cache.snapshot().tags.value, null)
  await Promise.all([cache.load('tags'), cache.load('tags')])
  assert.equal(calls, 2)
  assert.deepEqual(cache.snapshot().tags.value?.tags, ['new'])
})

test('failed refresh retains known choices and needs an explicit retry', async context => {
  let calls = 0
  const cache = new FilterCatalogue(async () => {
    calls++
    if (calls === 2) throw new Error('synthetic read failure')
    return {tags: [String(calls)]} as never
  }, {minimumRefresh: 0, refreshDelay: 1})
  context.after(() => cache.dispose())
  await cache.load('tags')
  cache.invalidate('tags'); await cache.load('tags')
  assert.deepEqual(cache.snapshot().tags.value?.tags, ['1'])
  assert.match(cache.snapshot().tags.error, /重试/)
  await cache.load('tags')
  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(calls, 2)
  await cache.retry('tags')
  assert.deepEqual(cache.snapshot().tags.value?.tags, ['3'])
  assert.equal(cache.snapshot().tags.error, '')
})

test('artist TTL checks mode changes without refetching tags or polling in the background', async context => {
  let now = 1000, artistCalls = 0, tagCalls = 0
  const cache = new FilterCatalogue(async kind => {
    if (kind === 'tags') { tagCalls++; return {tags: ['tag']} as never }
    artistCalls++
    return [{id: 1, pixiv_id: '1', name: 'artist', mode: artistCalls === 1 ? 'paused' : 'stopped'}] as never
  }, {now: () => now, artistTtl: 30000})
  context.after(() => cache.dispose())
  await cache.warm(); now += 29000; await cache.warm()
  assert.equal(artistCalls, 1)
  now += 1001; await cache.warm()
  assert.equal(artistCalls, 2); assert.equal(tagCalls, 1)
  assert.equal(cache.snapshot().artists.value?.[0]?.mode, 'stopped')
})

test('frequent catalogue events merge into one background refresh after the quiet budget', async context => {
  context.mock.timers.enable({apis: ['setTimeout']})
  let now = 1000, calls = 0
  const cache = new FilterCatalogue(async () => { calls++; return {tags: [String(calls)]} as never }, {now: () => now})
  context.after(() => cache.dispose())
  await cache.load('tags')
  for (let index = 0; index < 20; index++) cache.invalidate('tags')
  now += 59999; context.mock.timers.tick(59999)
  await Promise.resolve()
  assert.equal(calls, 1)
  now++; context.mock.timers.tick(1)
  await cache.load('tags')
  assert.equal(calls, 2)
  assert.deepEqual(cache.snapshot().tags.value?.tags, ['2'])
})

test('dispose aborts ownership and prevents late responses from publishing', async () => {
  let signal: AbortSignal | undefined, resolveRead: (value: never) => void = () => {}
  const cache = new FilterCatalogue(async (_kind, owned) => {
    signal = owned
    return await new Promise<never>(resolve => { resolveRead = resolve })
  })
  let publications = 0
  cache.subscribe(() => { publications++ })
  const flight = cache.load('tags'); await Promise.resolve()
  cache.dispose(); const before = publications
  resolveRead({tags: ['late']} as never); await flight
  assert.equal(signal?.aborted, true)
  assert.equal(publications, before)
  assert.equal(cache.snapshot().tags.value, null)
})

test('tag order switches cached names without new reads, mutation or losing selection and translations', async context => {
  let calls = 0
  const source = {tags: ['B', 'A', 'C'], tag_counts: {A: 2, B: 1, C: 2}, tag_details: [
    {name: 'A', translated_name: '中文译名', source: 'pixiv', language: 'zh-cn'}]}
  const cache = new FilterCatalogue(async () => { calls++; return source as never })
  context.after(() => cache.dispose())
  await cache.load('tags')
  const catalogue = cache.snapshot().tags.value!
  const selected = toggleSelection({all: true, values: []}, 'C', catalogue.tags)
  assert.deepEqual(orderedTags(catalogue.tags, catalogue.tag_counts, 'count'), ['A', 'C', 'B'])
  assert.deepEqual(orderedTags(catalogue.tags, catalogue.tag_counts, 'name'), ['A', 'B', 'C'])
  assert.deepEqual(selectionQuery(selected, orderedTags(catalogue.tags, catalogue.tag_counts, 'count')), {values: ['C'], excluded: false})
  assert.deepEqual(catalogue.tags, ['B', 'A', 'C'])
  assert.equal(catalogue.tag_details?.[0]?.translated_name, '中文译名')
  assert.equal(calls, 1)
  assert.deepEqual(orderedTags(['B', 'A'], undefined, 'count'), ['A', 'B'])
})

test('tag preference defaults to work count, survives reload, and tolerates blocked browser storage', context => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  context.after(() => {
    if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor)
    else Reflect.deleteProperty(globalThis, 'localStorage')
  })
  const storage = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {configurable: true, value: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value) },
  }})
  const preference = new TagOrderPreference(), events: string[] = []
  assert.equal(preference.get(), 'count')
  const unsubscribe = preference.subscribe(value => events.push(value))
  assert.equal(preference.set('name'), true)
  assert.equal(new TagOrderPreference().get(), 'name')
  assert.equal(preference.set('count'), true)
  assert.deepEqual(events, ['count', 'name', 'count'])
  unsubscribe(); preference.set('name')
  assert.deepEqual(events, ['count', 'name', 'count'])
  preference.set('unknown')
  assert.equal(new TagOrderPreference().get(), 'count')
  Object.defineProperty(globalThis, 'localStorage', {configurable: true, get() { throw new Error('blocked') }})
  const blocked = new TagOrderPreference()
  assert.equal(blocked.get(), 'count')
  assert.equal(blocked.set('name'), false)
  assert.equal(blocked.get(), 'name')
})
