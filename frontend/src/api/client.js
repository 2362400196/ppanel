import axios from 'axios'

export const api = axios.create({ baseURL: '/api', timeout: 120000 })

api.interceptors.request.use(cfg => {
  const token = localStorage.getItem('ppanel_token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

api.interceptors.response.use(
  r => r,
  err => {
    if (err.response?.status === 401 && !location.pathname.startsWith('/login')) {
      localStorage.removeItem('ppanel_token')
      location.href = '/login'
    }
    return Promise.reject(err)
  }
)

export const errText = e =>
  e?.response?.data?.detail || e?.message || '请求失败'

export function wsUrl(path, params = {}) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const qs = new URLSearchParams({
    token: localStorage.getItem('ppanel_token') || '',
    ...params
  })
  return `${proto}://${location.host}${path}?${qs}`
}
