import axios, { type AxiosInstance, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { useUserStore } from '@/stores/user'
import router from '@/router'

class HttpRequest {
  private instance: AxiosInstance
  public baseURL: string
  private isRefreshing = false
  private waitQueue: Array<(token: string) => void> = []

  constructor() {
    const baseURL = import.meta.env.VITE_API_BASE_URL
    this.baseURL = baseURL

    this.instance = axios.create({
      baseURL,
      timeout: 300000,
    })

    this.initializeInterceptors()
  }

  private initializeInterceptors() {
    // 请求拦截器：附加 accessToken
    this.instance.interceptors.request.use(
      (config: InternalAxiosRequestConfig) => {
        const userStore = useUserStore()
        const token = userStore.accessToken
        if (token) {
          config.headers.Authorization = `Bearer ${token}`
        }
        return config
      },
      (error) => Promise.reject(error),
    )

    // 响应拦截器：401 时自动刷新 token
    this.instance.interceptors.response.use(
      (response) => response.data,
      async (error) => {
        const originalRequest = error.config as AxiosRequestConfig & { _retry?: boolean }

        // 401 且未重试过 → 尝试刷新
        if (error.response?.status === 401 && !originalRequest._retry) {
          originalRequest._retry = true

          try {
            const newToken = await this.refreshAccessToken()
            if (originalRequest.headers) {
              originalRequest.headers.Authorization = `Bearer ${newToken}`
            }
            return this.instance.request(originalRequest)
          } catch (refreshError) {
            // 刷新失败 → 登出并跳转登录页
            const userStore = useUserStore()
            userStore.logout()
            router.push('/login')
            return Promise.reject(refreshError)
          }
        }

        return Promise.reject(error)
      },
    )
  }

  /**
   * 使用 refreshToken 获取新的 accessToken
   */
  private async refreshAccessToken(): Promise<string> {
    const userStore = useUserStore()
    const refreshToken = userStore.refreshToken

    if (!refreshToken) {
      throw new Error('No refresh token')
    }

    // 如果正在刷新，等待结果
    if (this.isRefreshing) {
      return new Promise((resolve) => {
        this.waitQueue.push((token: string) => resolve(token))
      })
    }

    this.isRefreshing = true

    try {
      const response = await axios.post(`${this.baseURL}/user/refresh`, {
        refresh_token: refreshToken,
      })

      const { access_token, refresh_token } = response.data
      userStore.updateTokens(access_token, refresh_token)

      // 通知等待中的请求
      this.waitQueue.forEach((callback) => callback(access_token))
      this.waitQueue = []

      return access_token
    } finally {
      this.isRefreshing = false
    }
  }

  public request<T = any>(config: AxiosRequestConfig): Promise<T> {
    return this.instance.request(config) as unknown as Promise<T>
  }

  public get<T = any>(url: string, params?: object, config?: AxiosRequestConfig): Promise<T> {
    return this.request({ ...config, url, method: 'GET', params })
  }

  public post<T = any>(url: string, data?: object, config?: AxiosRequestConfig): Promise<T> {
    return this.request({ ...config, url, method: 'POST', data })
  }

  public put<T = any>(url: string, data?: object, config?: AxiosRequestConfig): Promise<T> {
    return this.request({ ...config, url, method: 'PUT', data })
  }

  public delete<T = any>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return this.request({ ...config, url, method: 'DELETE' })
  }

  public patch<T = any>(url: string, data?: object, config?: AxiosRequestConfig): Promise<T> {
    return this.request({ ...config, url, method: 'PATCH', data })
  }

  public upload<T = any>(url: string, formData: FormData, config?: AxiosRequestConfig): Promise<T> {
    return this.request({
      ...config,
      url,
      method: 'POST',
      data: formData,
      headers: {
        ...config?.headers,
        'Content-Type': 'multipart/form-data',
      },
    })
  }
}

export default new HttpRequest()
