import { defineStore } from 'pinia'
import { api } from '../api/client'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('ppanel_token') || '',
    user: null
  }),
  getters: {
    isAdmin: s => s.user?.role === 'admin'
  },
  actions: {
    async login(username, password) {
      const { data } = await api.post('/auth/login', { username, password })
      this.token = data.token
      this.user = data.user
      localStorage.setItem('ppanel_token', data.token)
    },
    async fetchMe() {
      const { data } = await api.get('/me')
      this.user = data
    },
    logout() {
      this.token = ''
      this.user = null
      localStorage.removeItem('ppanel_token')
    }
  }
})
