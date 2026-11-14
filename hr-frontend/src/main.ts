import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
// 注意：暗色变量表必须在本项目 index.css 之前导入，
// 这样 index.css 里的 html.dark 品牌色 / 玻璃化覆盖才能生效。
import 'element-plus/theme-chalk/dark/css-vars.css'
import './index.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import App from './App.vue'
import router from './router'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus)

// 添加icon
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.mount('#app')
