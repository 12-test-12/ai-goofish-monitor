import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { i18n } from './i18n'
import './assets/main.css'

if (new URLSearchParams(window.location.search).get('desktop') === '1') {
  document.documentElement.classList.add('desktop-shell')
}
window.addEventListener('pywebviewready', () => {
  document.documentElement.classList.add('desktop-shell')
})

const app = createApp(App)

app.use(router)
app.use(i18n)

app.mount('#app')
