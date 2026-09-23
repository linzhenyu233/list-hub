<script setup>
// 单个视频上传卡片（用于「商品视频」这类只有一个地址的字段）。
// 交互与外观对齐同页的图片素材卡（MediaGallery / MediaUploader）：
//   · 有视频时是一张 16:9 圆角封面，悬停右上角浮出「换视频 / 删除」；
//   · 封面中央是圆形播放按钮，点了才播放并显示原生控件（平时画面干净）；
//   · 没有视频时是同尺寸的虚线框。
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, RefreshRight, VideoCamera, VideoPlay } from '@element-plus/icons-vue'
import { MAX_VIDEO_BYTES, uploadLocalVideo } from '../useMediaUpload'

const props = defineProps({
  modelValue: { type: String, default: '' },
  // async ({ filename, contentBase64 }) => 视频地址
  upload: { type: Function, required: true },
  placeholder: { type: String, default: '选择视频' },
  // 平台各自的体积上限（小红书网关限制严，微信是分块上传可以大得多）
  maxBytes: { type: Number, default: MAX_VIDEO_BYTES },
})
const maxMb = computed(() => Math.round(props.maxBytes / 1024 / 1024))
const emit = defineEmits(['update:modelValue'])

const fileInput = ref(null)
const videoRef = ref(null)
const busy = ref(false)
// 播放前只当封面展示(不带控件),"播放"后原生控件才出现
const playing = ref(false)

// 换/删视频后回到封面态,否则新视频一上来就带着旧控件
watch(() => props.modelValue, () => { playing.value = false })

function pick() {
  const input = fileInput.value
  if (!input) return
  input.value = ''       // 清空后才能再次选同一个视频
  input.click()
}

function startPlay() {
  const el = videoRef.value
  if (!el) return
  playing.value = true
  // 等 controls 挂上再播,个别浏览器对刚出现的控件处理不一致
  nextTick(() => { el.play().catch(() => {}) })
}

async function onPicked(event) {
  const file = event?.target?.files?.[0]
  if (!file) return
  busy.value = true
  try {
    emit('update:modelValue', await uploadLocalVideo(file, props.upload, props.maxBytes))
    ElMessage.success('视频已上传')
  } catch (error) {
    ElMessage.error(`${file.name}：${error.message}`)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="video-uploader">
    <div v-if="modelValue" class="video-card" :class="{ 'is-cover': !playing }">
      <video
        ref="videoRef"
        class="video-card__video"
        :src="modelValue"
        :controls="playing"
        preload="metadata"
        playsinline
        referrerpolicy="no-referrer"
        @play="playing = true"
      />
      <button v-if="!playing && !busy" type="button" class="video-card__play" title="播放预览" @click="startPlay">
        <el-icon><VideoPlay /></el-icon>
      </button>
      <div v-if="busy" class="video-card__veil">上传中…</div>
      <div v-else class="video-card__tools">
        <button type="button" title="换视频" @click="pick"><el-icon><RefreshRight /></el-icon><span>换视频</span></button>
        <button type="button" class="is-danger" title="删除" @click="emit('update:modelValue', '')"><el-icon><Delete /></el-icon><span>删除</span></button>
      </div>
    </div>
    <button v-else type="button" class="video-card video-card--add" :disabled="busy" :title="`${placeholder}（最大 ${maxMb}MB）`" @click="pick">
      <el-icon><VideoCamera /></el-icon>
      <span>{{ busy ? '上传中…' : placeholder }}</span>
      <em class="video-card__limit">最大 {{ maxMb }}MB</em>
    </button>
    <input ref="fileInput" type="file" accept="video/*" style="display: none" @change="onPicked" />
  </div>
</template>

<style scoped>
/* 外层表单是 flex,这里必须自己给宽度:否则空态按钮会按内容收缩(很小)、
   有视频时又被撑大,出现"没传时小、传完变大"的跳变。统一成同一个固定尺寸。 */
.video-uploader { display: flex; width: 100%; }
.video-card {
  position: relative;
  flex: 0 0 auto;
  width: 200px;
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: #0d0f12;
}
.video-card__video { display: block; width: 100%; height: 100%; object-fit: contain; background: #0d0f12; }
/* 封面态：叠一层轻微渐变，让圆形播放按钮更清晰 */
.video-card.is-cover::after { content: ''; position: absolute; inset: 0; background: linear-gradient(180deg, rgba(0,0,0,.06), rgba(0,0,0,.28)); pointer-events: none; }
.video-card__play { position: absolute; inset: 0; z-index: 2; display: grid; place-items: center; padding: 0; border: 0; background: transparent; cursor: pointer; }
.video-card__play .el-icon {
  width: 44px; height: 44px; border-radius: 50%;
  display: grid; place-items: center;
  background: rgba(17, 24, 39, .55); color: #fff; font-size: 20px;
  transition: background .15s ease, transform .15s ease;
}
.video-card__play:hover .el-icon { background: var(--brand); transform: scale(1.06); }
/* 空态：与封面同尺寸的虚线框，避免有无视频时布局跳动 */
.video-card--add {
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; padding: 0;
  border-style: dashed; border-color: #ccd5e0; background: #fafcff; color: #667085; font-size: 12px; cursor: pointer;
  transition: border-color .15s ease, color .15s ease, background .15s ease;
}
.video-card--add:hover:not(:disabled) { border-color: var(--brand); background: #f4f9ff; color: var(--brand); }
.video-card--add:disabled { border-color: #e5e9ef; background: #f7f8fa; color: #b9c0cb; cursor: not-allowed; }
.video-card--add .el-icon { font-size: 20px; }
.video-card__limit { color: #98a0ac; font-size: 11px; font-style: normal; }
.video-card__veil { position: absolute; inset: 0; z-index: 3; display: grid; place-items: center; background: rgba(255, 255, 255, .88); color: #98a0ac; font-size: 12px; }
/* 悬停工具：与图片卡的 .media-card__tools button 保持同一套观感（白底圆角小按钮），
   放在右上角而不是整卡浮层，避免遮住中间播放按钮。 */
.video-card__tools { position: absolute; top: 6px; right: 6px; z-index: 3; display: flex; gap: 6px; opacity: 0; pointer-events: none; transition: opacity .15s ease; }
.video-card:hover .video-card__tools { opacity: 1; }
.video-card__tools button { display: inline-flex; align-items: center; gap: 3px; padding: 4px 8px; border: 0; border-radius: 4px; background: rgba(255, 255, 255, .94); color: #303133; font-size: 12px; cursor: pointer; pointer-events: auto; }
.video-card__tools button:hover { color: var(--brand); }
.video-card__tools button.is-danger:hover { color: #d92d20; }
/* 触屏没有 hover：按钮常显，否则运营看不到「换视频/删除」 */
@media (hover: none) {
  .video-card__tools { opacity: 1; }
}
</style>
