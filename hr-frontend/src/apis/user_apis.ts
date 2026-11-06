import request from '@/apis/requests'
import { type User } from '@/stores/user'

export interface LoginData {
  email: string
  password: string
}

interface LoginResponse {
  access_token: string
  user: User
}

export function login(data: LoginData) {
  return request.post<LoginResponse>('/user/login/', data)
}

export interface RegisterData {
  email: string
  invite_code: string
  username: string
  realname: string
  password: string
}

export interface ResponseSchma {
  result: 'success' | 'fail'
}

export function register(data: RegisterData) {
  return request.post<ResponseSchma>('/user/register/', data)
}
