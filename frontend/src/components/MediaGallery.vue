<script setup>
// 素材网格：缩略图卡片（悬停可换图/删除，点击可放大）+ 虚线「添加图片」方块。
// 图片直接从本地选，上传由外部传入的 upload 回调负责（微信=微信素材库、小红书=小红书素材库），
// 回调返回可直接发品的图片地址；数组里的空位（回填时预留的占位）不渲染成卡片。
// 第 1 张即首图，序号标在缩略图左下角。
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, Picture, Plus, RefreshRight } from '@element-plus/icons-vue'
import { uploadLocalImage } from '../useMediaUpload'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  max: { type: Number, default: 9 },
  // async ({ filename, contentBase64 }) => 图片地址
  upload: { type: Function, required: true },
})
const emit = defineEmits(['update:modelValue'])

const BUSY_BATCH = -2

const rowInput = ref(null)      // 单张换图
const batchInput = ref(null)    // 一次选多张
const replacing = ref(-1)       // 正在换图的卡片下标
const busyIndex = ref(-1)       // 上传中的卡片下标；-2 表示批量添加中
const progress = ref('')

const items = computed(() => props.modelValue
  .map((url, index) => ({ url, index }))
  .filter((item) => item.url)
  .map((item, pos) => ({ ...item, pos })))
const urls = computed(() => items.value.map((item) => item.url))
const full = computed(() => items.value.length >= props.max)

function update(list) {
  emit('update:modelValue', list)
}

function uploadOne(file) {
  return uploadLocalImage(file, props.upload)
}

function pickReplace(index) {
  replacing.value = index
  const input = rowInput.value
  if (!input) return
  input.value = ''       // 清空后才能再次选同一张图
  input.click()
}

async function onReplacePicked(event) {
  const file = event?.target?.files?.[0]
  const index = replacing.value
  replacing.value = -1
  if (!file || index < 0) return
  busyIndex.value = index
  try {
    const next = [...props.modelValue]
    next[index] = await uploadOne(file)
    update(next)
    ElMessage.success('图片已上传')
  } catch (error) {
    ElMessage.error(`${file.name}：${error.message}`)
  } finally {
    busyIndex.value = -1
  }
}

function pickAdd() {
  if (full.value) return
  const input = batchInput.value
  if (!input) return
  input.value = ''
  input.click()
}

// 一次选多张：先补空位，空位填满了再往后追加；顺序即选择顺序
async function onBatchPicked(event) {
  const files = Array.from(event?.target?.files || [])
  if (!files.length) return
  const next = [...props.modelValue]
  busyIndex.value = BUSY_BATCH
  let done = 0
  const failed = []
  for (const [i, file] of files.entries()) {
    if (next.filter(Boolean).length >= props.max) {
      failed.push(`${file.name}：最多 ${props.max} 张`)
      continue
    }
    progress.value = `${i + 1}/${files.length}`
    try {
      const url = await uploadOne(file)
      const empty = next.findIndex((item) => !item)
      if (empty >= 0) next[empty] = url
      else next.push(url)
      done += 1
    } catch (error) {
      failed.push(`${file.name}：${error.message}`)
    }
  }
  progress.value = ''
  busyIndex.value = -1
  if (done) {
    update(next)
    ElMessage.success(`已上传 ${done} 张图片`)
  }
  if (failed.length) {
    ElMessage.warning(`${failed.length} 张没传成功：${failed.slice(0, 3).join('；')}${failed.length > 3 ? ' …' : ''}`)
  }
}

function remove(index) {
  const next = [...props.modelValue]
  next.splice(index, 1)
  update(next)
}
</script>

<template>
  <div class="media-gallery">
    <div v-for="item in items" :key="`${item.index}-${item.url}`" class="media-card">
      <el-image
        class="media-card__img"
        :src="item.url"
        :preview-src-list="urls"
        :initial-index="item.pos"
        fit="cover"
        preview-teleported
        referrerpolicy="no-referrer"
      >
        <template #error>
          <div class="media-card__fallback"><el-icon><Picture /></el-icon><span>加载失败</span></div>
        </template>
      </el-image>
      <span class="media-card__index">{{ item.pos + 1 }}</span>
      <div v-if="busyIndex === item.index" class="media-card__veil">上传中…</div>
      <div v-else class="media-card__tools">
        <button type="button" @click="pickReplace(item.index)"><el-icon><RefreshRight /></el-icon><span>换图</span></button>
        <button type="button" class="is-danger" @click="remove(item.index)"><el-icon><Delete /></el-icon><span>删除</span></button>
      </div>
    </div>
    <button type="button" class="media-card media-card--add" :disabled="full" @click="pickAdd">
      <el-icon><Plus /></el-icon>
      <span>{{ full ? `最多 ${max} 张` : '添加图片' }}</span>
      <span v-if="busyIndex === BUSY_BATCH" class="media-card__progress">上传中 {{ progress }}</span>
    </button>
    <input ref="rowInput" type="file" accept="image/*" style="display: none" @change="onReplacePicked" />
    <input ref="batchInput" type="file" accept="image/*" multiple style="display: none" @change="onBatchPicked" />
  </div>
</template>
