const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const Module = require('node:module')

const root = process.cwd()
const frontend = path.join(root, '.worktrees/v0.2.0-build20260929.2/app/frontend')
const deps = path.join(frontend, 'node_modules')
const ts = require(path.join(deps, 'typescript'))
require(path.join(deps, 'vue'))
const modules = new Map()
function sourceModule(filename) {
  if (modules.has(filename)) return modules.get(filename).exports
  const item = new Module(filename)
  item.filename = filename
  item.paths = [deps]
  const original = item.require.bind(item)
  item.require = request => request.startsWith('.')
    ? sourceModule(path.resolve(path.dirname(filename), request + '.ts')) : original(request)
  modules.set(filename, item)
  item._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  }).outputText, filename)
  return item.exports
}

let timerId = 0
const timers = new Map()
global.window = {setTimeout(callback, delay) {timers.set(++timerId, {callback, delay}); return timerId}}
global.clearTimeout = id => timers.delete(id)
global.document = {hidden: false}
const state = (work_id, account_epoch, current_account_id, status, operation_id) => ({
  work_id, account_epoch, current_account_id, status, operation_id,
  version: 1, desired: null, value: true, verified: true, fact_account_id: 'account-a',
  cached: true, confirmed_at: '2026-09-29T00:00:00Z', error: null,
})
global.fetch = async () => new Response(JSON.stringify(state(3, 101, 'account-b', 'pending', 'poll')), {
  status: 200, headers: {'content-type': 'application/json'},
})
const bookmarks = sourceModule(path.join(frontend, 'src/state/bookmarks.ts'))

async function main() {
  bookmarks.ingestBookmark(1, state(1, 100, 'account-a', 'pending', 'old'))
  bookmarks.ingestBookmark(2, state(2, 100, 'account-a', 'confirmed', null))
  bookmarks.ingestBookmark(1, state(1, 101, 'account-b', 'pending', 'new'))
  assert.equal(bookmarks.bookmarkState(1).operation_id, 'new')
  assert.equal(bookmarks.bookmarkState(2).current_account_id, 'account-b')
  assert.equal(bookmarks.bookmarkState(2).operation_id, null)
  bookmarks.ingestBookmark(1, state(1, 100, 'account-a', 'confirmed', 'old'))
  assert.equal(bookmarks.bookmarkState(1).operation_id, 'new')
  bookmarks.ingestBookmark(1, state(1, 101, 'account-b', 'confirmed', 'new'))
  bookmarks.ingestBookmark(3, state(3, 101, 'account-b', 'pending', 'poll'))
  const delays = []
  for (let i = 0; i < 6; i++) {
    const [id, timer] = timers.entries().next().value
    timers.delete(id)
    delays.push(timer.delay)
    await timer.callback()
  }
  assert.deepEqual(delays, [900, 1600, 2300, 3000, 3700, 4400])
  const result = {account_switch_kept_new_pending: true, older_response_rejected: true,
    other_work_invalidated: true, pending_poll_delays_ms: delays,
    real_account_used: false, real_library_used: false}
  fs.writeFileSync(path.join(root, '.devtools/test-runs/bookmark-implement-20260929/state-loop.json'),
    JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result))
}
main().catch(error => {console.error(error); process.exitCode = 1})
