import { createApp } from 'vue'
import App from './App.vue'
import { createRouter, createWebHistory } from 'vue-router'
import './style.css'
import { diagnostics, installBrowserObservers, browserError, category } from './diagnostics'
const router = createRouter({history: createWebHistory(), routes: [
  {path: '/', component: App}, {path: '/flow', component: App}, {path: '/illust/:id', component: App},
  {path: '/artists', component: App}, {path: '/artist/:id', component: App}, {path: '/settings', component: App},
]})
// Capability discovery runs independently; mounting never waits for diagnostics.
const discovery = diagnostics.start()
let stopObservers: (() => void) | undefined
try { stopObservers = installBrowserObservers() } catch { diagnostics.failure() }
void discovery.then(() => { if (!diagnostics.enabled) stopObservers?.() }).catch(() => diagnostics.failure())
router.afterEach((to, _from, failure) => diagnostics.emit('browser.navigation', {category: category(to.path), outcome: failure ? 'error' : 'ok'}))
router.onError(error => browserError(error))
const app = createApp(App).use(router)
// Vue forwards errors to this handler instead of the browser ErrorEvent.
app.config.errorHandler = error => { browserError(error, true); console.error(error) }
app.mount('#app')
