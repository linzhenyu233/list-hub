<script setup>
import { computed, ref } from 'vue'
import { ArrowDown, ArrowUp, Refresh, Search } from '@element-plus/icons-vue'

const props = defineProps({
  modelValue: { type: Object, required: true },
  idLabel: { type: String, default: '商品ID' },
  idPlaceholder: { type: String, default: '多个以空格/逗号/分号分隔' },
  codeLabel: { type: String, default: '商家编码' },
  codePlaceholder: { type: String, default: 'SKU/商家编码/条形码，空格/逗号分隔' },
  nameLabel: { type: String, default: '商品名称' },
  namePlaceholder: { type: String, default: '多个词用空格分割' },
  // 是否显示高级筛选（价格区间/库存区间）
  showAdvanced: { type: Boolean, default: true },
})
const emit = defineEmits(['update:modelValue', 'search', 'reset'])

const expanded = ref(false)

// 通过 computed 双向绑定：修改子字段时同步回父组件
const form = computed({
  get: () => props.modelValue,
  set: (val) => emit('update:modelValue', val),
})

function updateField(key, value) {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}

function hasActiveFilter() {
  const f = props.modelValue
  return Boolean(f.ids || f.codes || f.keyword
    || f.minPrice != null || f.maxPrice != null
    || f.minStock != null || f.maxStock != null)
}

function onSearch() { emit('search', props.modelValue) }
function onReset() {
  emit('update:modelValue', { ids: '', codes: '', keyword: '', minPrice: null, maxPrice: null, minStock: null, maxStock: null })
  emit('reset')
}
function toggleExpanded() { expanded.value = !expanded.value }
</script>

<template>
  <section class="product-search-bar">
    <div class="search-bar-main">
      <div class="search-field">
        <label class="search-label">{{ idLabel }}</label>
        <el-input
          :model-value="form.ids"
          :placeholder="idPlaceholder"
          clearable
          class="search-input"
          @update:model-value="(v) => updateField('ids', v)"
          @keyup.enter="onSearch"
        />
      </div>
      <div class="search-field">
        <label class="search-label">{{ codeLabel }}</label>
        <el-input
          :model-value="form.codes"
          :placeholder="codePlaceholder"
          clearable
          class="search-input"
          @update:model-value="(v) => updateField('codes', v)"
          @keyup.enter="onSearch"
        />
      </div>
      <div class="search-field">
        <label class="search-label">{{ nameLabel }}</label>
        <el-input
          :model-value="form.keyword"
          :placeholder="namePlaceholder"
          clearable
          class="search-input"
          @update:model-value="(v) => updateField('keyword', v)"
          @keyup.enter="onSearch"
        />
      </div>
      <div class="search-actions">
        <el-button type="primary" :icon="Search" @click="onSearch">查询</el-button>
        <el-button :icon="Refresh" :disabled="!hasActiveFilter()" @click="onReset">重置</el-button>
        <el-button v-if="showAdvanced" link type="primary" @click="toggleExpanded">
          {{ expanded ? '收起' : '更多筛选' }}
          <el-icon><component :is="expanded ? ArrowUp : ArrowDown" /></el-icon>
        </el-button>
      </div>
    </div>

    <div v-if="showAdvanced && expanded" class="search-bar-advanced">
      <div class="search-field">
        <label class="search-label">售价区间</label>
        <div class="range-input">
          <el-input-number
            :model-value="form.minPrice"
            :min="0"
            :precision="2"
            :controls="false"
            placeholder="最低价"
            class="range-num"
            @update:model-value="(v) => updateField('minPrice', v)"
          />
          <span class="range-sep">–</span>
          <el-input-number
            :model-value="form.maxPrice"
            :min="0"
            :precision="2"
            :controls="false"
            placeholder="最高价"
            class="range-num"
            @update:model-value="(v) => updateField('maxPrice', v)"
          />
          <span class="range-unit">¥</span>
        </div>
      </div>
      <div class="search-field">
        <label class="search-label">库存区间</label>
        <div class="range-input">
          <el-input-number
            :model-value="form.minStock"
            :min="0"
            :precision="0"
            :controls="false"
            placeholder="最小库存"
            class="range-num"
            @update:model-value="(v) => updateField('minStock', v)"
          />
          <span class="range-sep">–</span>
          <el-input-number
            :model-value="form.maxStock"
            :min="0"
            :precision="0"
            :controls="false"
            placeholder="最大库存"
            class="range-num"
            @update:model-value="(v) => updateField('maxStock', v)"
          />
        </div>
      </div>
    </div>
  </section>
</template>
