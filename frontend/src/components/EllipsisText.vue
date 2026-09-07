<script setup>
import { nextTick, onMounted, ref, watch } from 'vue'

const props = defineProps({
  // 需要展示的完整文本（超出容器宽度时以省略号截断）
  text: { type: [String, Number], default: '' },
  // 渲染的标签，用于沿用外部按标签命中的样式（如 .product-cell strong / span）
  tag: { type: String, default: 'span' },
  placement: { type: String, default: 'top' },
  // tooltip 主题：统一用 light=白底黑字（与白色背景一致）
  effect: { type: String, default: 'light' },
  // 出现/消失延迟(ms)：尽量快，减少等待
  showAfter: { type: Number, default: 0 },
  hideAfter: { type: Number, default: 0 },
})

const textEl = ref(null)
const truncated = ref(false)

// 仅在文本真正被省略号截断时才启用 tooltip，完整显示时不打扰
function measure() {
  const el = textEl.value
  truncated.value = !!el && el.scrollWidth > el.clientWidth + 1
}

onMounted(() => nextTick(measure))
watch(() => props.text, () => nextTick(measure))
</script>

<template>
  <el-tooltip
    :content="String(text ?? '')"
    :placement="placement"
    :effect="effect"
    :show-after="showAfter"
    :hide-after="hideAfter"
    :disabled="!truncated"
    popper-class="ellipsis-text-tooltip"
  >
    <component :is="tag" ref="textEl" class="ellipsis-text">{{ text }}</component>
  </el-tooltip>
</template>

<style scoped>
.ellipsis-text { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
