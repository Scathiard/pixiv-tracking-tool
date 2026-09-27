import { createApp } from 'vue'
import App from './App.vue'
import { createRouter, createWebHistory } from 'vue-router'
import './style.css'
const router = createRouter({history: createWebHistory(), routes: [
  {path: '/', component: App}, {path: '/flow', component: App}, {path: '/illust/:id', component: App},
  {path: '/artists', component: App}, {path: '/artist/:id', component: App}, {path: '/settings', component: App},
]})
createApp(App).use(router).mount('#app')
