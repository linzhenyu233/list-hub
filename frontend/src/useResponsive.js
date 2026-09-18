import { onBeforeUnmount, ref } from 'vue'

// 是否移动端。断点与 styles.css 的 620px 媒体查询保持一致，
// 用于在模板里按端切换组件属性（如取消 el-table 固定列）。
export function useIsMobile(breakpoint = 620) {
  const mql = window.matchMedia(`(max-width: ${breakpoint}px)`)
  const isMobile = ref(mql.matches)
  const onChange = (event) => { isMobile.value = event.matches }
  mql.addEventListener('change', onChange)
  onBeforeUnmount(() => mql.removeEventListener('change', onChange))
  return isMobile
}
