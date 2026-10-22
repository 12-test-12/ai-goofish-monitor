import { ref, computed } from 'vue'
import { wsService } from '@/services/websocket'

// Global State
const username = ref<string | null>(null)
const isLoggedIn = ref(false)
let initialized = false
let restoring: Promise<void> | null = null
// Discard the old UI-only login flags; the server session is authoritative.
localStorage.removeItem('auth_username')
localStorage.removeItem('auth_logged_in')

export function useAuth() {
  const isAuthenticated = computed(() => isLoggedIn.value)

  function setAuthenticated(user: string) {
    username.value = user
    isLoggedIn.value = true

    initialized = true

    // 启动 WebSocket 连接
    wsService.start()
  }

  function clearAuthentication() {
    username.value = null
    isLoggedIn.value = false

    // 停止 WebSocket 连接
    wsService.stop()
  }

  async function logout() {
    try {
      const response = await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' })
      if (!response.ok) throw new Error('Logout failed')
    } finally {
      clearAuthentication()
      initialized = true
      window.location.href = '/login'
    }
  }

  async function restoreSession() {
    if (initialized) return
    if (!restoring) {
      restoring = (async () => {
        try {
          const response = await fetch('/auth/status', { credentials: 'same-origin' })
          if (response.ok) {
            const session = await response.json()
            setAuthenticated(session.username)
          } else {
            clearAuthentication()
          }
        } catch {
          clearAuthentication()
        } finally {
          initialized = true
          restoring = null
        }
      })()
    }
    await restoring
  }

  async function login(user: string, pass: string): Promise<boolean> {
    try {
      const response = await fetch('/auth/status', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ username: user, password: pass }),
      })

      if (response.ok) {
        setAuthenticated(user)
        return true
      } else {
        return false
      }
    } catch (e) {
      console.error('Login error', e)
      return false
    }
  }

  return {
    username,
    isAuthenticated,
    login,
    logout,
    restoreSession,
  }
}
