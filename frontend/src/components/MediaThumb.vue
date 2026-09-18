<script setup>
// 图片地址行左侧的缩略图：让运营一眼看出这一行是哪张图（该换哪张、该删哪张）。
// 空地址或加载失败时退化成占位图标，不显示浏览器默认的碎图；
// 有图时点击可放大浏览，previewList 为该组全部有效图片，initialIndex 指明当前这张的位置。
import { Picture } from '@element-plus/icons-vue'

const props = defineProps({
  src: { type: String, default: '' },
  previewList: { type: Array, default: () => [] },
  initialIndex: { type: Number, default: 0 },
})
</script>

<template>
  <div class="media-thumb">
    <el-image
      v-if="props.src"
      class="media-thumb__image"
      :src="props.src"
      :preview-src-list="props.previewList"
      :initial-index="props.initialIndex"
      fit="cover"
      preview-teleported
      referrerpolicy="no-referrer"
    >
      <template #error>
        <div class="media-thumb__mask" title="图片加载失败，请核对地址或重新转存">
          <el-icon><Picture /></el-icon>
        </div>
      </template>
    </el-image>
    <div v-else class="media-thumb__mask" title="尚未填写图片地址">
      <el-icon><Picture /></el-icon>
    </div>
  </div>
</template>
