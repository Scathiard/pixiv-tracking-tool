// Isolated audit: executes current api.ts and extracted SettingsView script logic.
// Vue lifecycle/ref hooks and HTTP are substitutes; no account, server, or real data.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import { stripTypeScriptTypes } from 'node:module';
import { api } from '../../../app/frontend/src/api.ts';

const output = {};
const originalFetch = globalThis.fetch;
const nativeTimeout = AbortSignal.timeout;
const originalWindow = globalThis.window;
try {
  AbortSignal.timeout = () => nativeTimeout(1);
  globalThis.fetch = async (_path, {signal}) => new Promise((_resolve, reject) => {
    if (signal.aborted) reject(signal.reason);
    else signal.addEventListener('abort', () => reject(signal.reason), {once: true});
    // Keep Node alive for the native unref'ed timeout.
    setTimeout(() => {}, 5);
  });
  try { await api('/api/logs'); assert.fail('Expected timeout'); }
  catch (error) {
    assert.equal(error.name, 'TimeoutError');
    output.timeout = {name: error.name, raw_native_message: error.message,
      classification: 'No product message normalization; Node native wording differs from Chromium.'};
  }
  AbortSignal.timeout = nativeTimeout;

  const source = fs.readFileSync(new URL('../../../app/frontend/src/views/SettingsView.vue', import.meta.url), 'utf8');
  const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
    .replace(/^import .*$/gm, '');
  const code = stripTypeScriptTypes(script, {mode: 'transform'}) + '\nreturn {loadLogs, toggleLogs, load, pollStatus, logs, logsLoading, settings, error};';
  const pending = [];
  const unmount = [];
  const scheduled = [];
  globalThis.window = {setTimeout: (fn, delay) => {scheduled.push({fn, delay}); return 0;}};
  globalThis.fetch = async (path, options) => new Promise(resolve => pending.push({path, signal: options.signal, resolve}));
  const create = new Function('ref', 'nextTick', 'onMounted', 'onBeforeUnmount', 'api', 'write', 'defineProps', 'defineEmits', code);
  const view = create(value => ({value}), fn => Promise.resolve().then(fn), () => {}, fn => unmount.push(fn), api, () => {throw new Error('Writes disabled');}, () => {}, () => () => {});
  view.toggleLogs();
  view.toggleLogs();
  view.toggleLogs();
  assert.equal(pending.length, 2);
  pending[1].resolve(Response.json({lines: ['new'], truncated: false}));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(view.logsLoading.value, false);
  assert.equal(pending[0].signal.aborted, false);
  output.reopen_logs = {parallel_requests: pending.length, loading_false_while_old_request_active: true};
  pending[0].resolve(Response.json({lines: ['old'], truncated: false}));
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(view.logs.value.lines, ['old']);
  output.reopen_logs.old_response_overwrites_new_response = true;

  const first = view.load();
  const second = view.load();
  const setting = port => ({next_port: port, check_interval_minutes: 60});
  pending[3].resolve(Response.json(setting(9002)));
  await second;
  pending[2].resolve(Response.json(setting(9001)));
  await first;
  assert.equal(view.settings.value.next_port, 9001);
  output.concurrent_settings = {old_response_overwrites_new_response: true};

  const late = view.loadLogs();
  unmount.forEach(fn => fn());
  assert.equal(pending[4].signal.aborted, false);
  pending[4].resolve(Response.json({lines: ['after-unmount'], truncated: false}));
  await late;
  assert.deepEqual(view.logs.value.lines, ['after-unmount']);
  output.unmount = {http_remains_active: true, response_still_updates_retired_view_state: true};
  console.log(JSON.stringify(output, null, 2));
} finally {
  globalThis.fetch = originalFetch;
  AbortSignal.timeout = nativeTimeout;
  globalThis.window = originalWindow;
}
