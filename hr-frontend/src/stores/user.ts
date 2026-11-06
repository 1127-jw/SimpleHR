import { defineStore } from "pinia"
import type { n } from "vue-router/dist/index-u4XkZVDG.js"

export interface Department {
    id:string
    name:string
}

export interface User {
    id:string
    username:string
    email:string
    phone_number:string|null
    realname:string
    // avatar:string|null
    department:Department|null
    is_active:boolean
    is_superuser:boolean
    is_hr:boolean
    created_at:string
    // 在 TypeScript 中，undefined 和 null 是两个不同的概念。? 实际上等价于 | undefined。
    managed_departments?:Department[]
}

export interface UserState {
    user:User|null
    accessToken:string|null
}

export const useUserStore = defineStore('user',{
    state: (): UserState => {
        const user = localStorage.getItem('user')
        const accessToken = localStorage.getItem('accessToken')
    return{
        user:user?JSON.parse(user):null,
        accessToken: accessToken||null
    }
    },

    getters:{
        isLoggedIn(): boolean{
            return !!this.accessToken
        },

        getUserInfo():User|null{
            return this.user
        },

        getAccessToken():string|null{
        return this.accessToken
        },

        isHr():boolean{
            return this.user?.is_hr || false
        },

        isSuperUser():boolean{
            return this.user?.is_superuser || false
        },

        isActive():boolean{
            return this.user?.is_active || false
        }
    },

    actions:{
        login(user:User, accessToken:string){
            this.user=user
            this.accessToken=accessToken
            localStorage.setItem('user', JSON.stringify(user))
            localStorage.setItem('accessToken', accessToken)
        },
        logout(){
            this.user=null
            this.accessToken=null
            localStorage.removeItem('user')
            localStorage.removeItem('accessToken')
        },
    }

})