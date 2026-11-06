import request from '@/apis/requests'
import {type User} from '@/stores/user'

export interface LoginData {
    email: string
    password: string
}

interface LoginResponse{
    access_token: string
    user:User
}

export function login(data:LoginData){
    return request.post<LoginResponse>('/user/login/',data)
}