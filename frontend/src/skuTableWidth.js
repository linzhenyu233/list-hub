// SKU/规格表格的列宽估算：中文约 16px/字、英文数字约 8px/字。
// 列宽按"表头与内容里最长的那条"算，让每列跟着字段长度走；
// 表格总宽 = 各列之和（容器够宽就右侧留白，不够就横向滚动），
// 这样不会像 el-table 的 min-width 那样把剩余宽度摊给某一列、把短字段的列撑得很空。
export function textWidth(text, min = 76, max = 320, padding = 26) {
  let width = 0
  for (const ch of String(text ?? '')) width += /[\u2e80-\uffef]/.test(ch) ? 16 : 8
  return Math.min(max, Math.max(min, Math.round(width + padding)))
}

export function longestOf(list, fallback = '') {
  return list.reduce(
    (longest, item) => (String(item ?? '').length > String(longest).length ? item : longest),
    fallback,
  )
}
