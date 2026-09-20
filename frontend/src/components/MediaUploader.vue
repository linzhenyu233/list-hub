<script setup>
// 单张图片上传卡片（用于 SKU 规格图这类只有一个地址的字段）：
// 空的时候是一个虚线「选择图片」方块，有图时显示缩略图，悬停可换图/删除，点击可放大。
// 与素材网格共用 useMediaUpload 的上传流程，只传一张。
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, Picture, Plus, RefreshRight } from '@element-plus/icons-vue'
import { uploadLocalImage } from '../useMediaUpload'

const props = defineProps({
  modelValue: { type: String, default: '' },
  // async ({ filename, contentBase64 }) => 图片地址
  upload: { type: Function, required: true },
  placeholder: { type: String, default: '选择图片' },
  // 紧凑模式：给表格单元格用（56px，空态只显示一个 +）
  compact: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const fileInput = ref(null)
const busy = ref(false)

function pick() {
  const input = fileInput.value
  if (!input) return
  input.value = ''       // 清空后才能再次选同一张图
  input.click()
}

async function onPicked(event) {
  const file = event?.target?.files?.[0]
  if (!file) return
  busy.value = true
  try {
    emit('update:modelValue', await uploadLocalImage(file, props.upload))
    ElMessage.success('图片已上传')
  } catch (error) {
    ElMessage.error(`${file.name}：${error.message}`)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="media-gallery media-gallery--small" :class="{ 'media-gallery--cell': props.compact }">
    <div v-if="modelValue" class="media-card media-card--small" :title="'点击可放大'">
      <el-image
        class="media-card__img"
        :src="modelValue"
        :preview-src-list="[modelValue]"
        :initial-index="0"
        fit="cover"
        preview-teleported
        referrerpolicy="no-referrer"
      >
        <template #error>
          <div class="media-card__fallback" title="图片加载失败，请重新选一张"><el-icon><Picture /></el-icon></div>
        </template>
      </el-image>
      <div v-if="busy" class="media-card__veil">上传中…</div>
      <div v-else class="media-card__tools">
        <button type="button" :title="'换图'" @click="pick"><el-icon><RefreshRight /></el-icon><span>换图</span></button>
        <button type="button" class="is-danger" :title="'删除'" @click="emit('update:modelValue', '')"><el-icon><Delete /></el-icon><span>删除</span></button>
      </div>
    </div>
    <button v-else type="button" class="media-card media-card--small media-card--add" :disabled="busy" :title="placeholder" @click="pick">
      <el-icon><Plus /></el-icon>
      <span>{{ busy ? '上传中…' : placeholder }}</span>
    </button>
    <input ref="fileInput" type="file" accept="image/*" style="display: none" @change="onPicked" />
  </div>
</template>
