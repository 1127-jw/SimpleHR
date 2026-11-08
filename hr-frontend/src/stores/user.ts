import { defineStore } from "pinia"

export interface Department {
    id: string
    name: string
}

export interface User {
    id: string
    username: string
    email: string
    realname: string
    phone_number: string | null
    avatar: string | null
    department: Department | null
    status: 'ACTIVE' | 'BLOCKED' | 'RESIGNED'
    is_superuser: boolean
    is_hr: boolean
    created_at: string
    managed_departments?: Department[]
}

export interface UserState {
    user: User | null
    accessToken: string | null
    refreshToken: string | null
}

export const useUserStore = defineStore('user', {
    state: (): UserState => {
        const user = localStorage.getItem('user')
        const accessToken = localStorage.getItem('accessToken')
        const refreshToken = localStorage.getItem('refreshToken')
        return {
            user: user ? JSON.parse(user) : null,
            accessToken: accessToken || null,
            refreshToken: refreshToken || null,
        }
    },

    getters: {
        isLoggedIn(): boolean {
            return !!this.accessToken
        },

        getUserInfo(): User | null {
            return this.user
        },

        getAccessToken(): string | null {
            return this.accessToken
        },

        getRefreshToken(): string | null {
            return this.refreshToken
        },

        isHr(): boolean {
            return this.user?.is_hr || false
        },

        isSuperUser(): boolean {
            return this.user?.is_superuser || false
        },

        isActive(): boolean {
            return this.user?.status === 'ACTIVE'
        }
    },

    actions: {
        login(user: User, accessToken: string, refreshToken: string) {
            this.user = user
            this.accessToken = accessToken
            this.refreshToken = refreshToken
            localStorage.setItem('user', JSON.stringify(user))
            localStorage.setItem('accessToken', accessToken)
            localStorage.setItem('refreshToken', refreshToken)
        },

        logout() {
            this.user = null
            this.accessToken = null
            this.refreshToken = null
            localStorage.removeItem('user')
            localStorage.removeItem('accessToken')
            localStorage.removeItem('refreshToken')
        },

        updateTokens(accessToken: string, refreshToken: string) {
            this.accessToken = accessToken
            this.refreshToken = refreshToken
            localStorage.setItem('accessToken', accessToken)
            localStorage.setItem('refreshToken', refreshToken)
        }
    }
})
