import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import 'element-plus/dist/index.css'
import App from './App.vue'
import './styles.css'
// 设计语言（token + Element Plus 主题覆盖）：须在 styles.css 之后引入以覆盖同特异度规则
import './styles/design-tokens.css'

const app = createApp(App)
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
