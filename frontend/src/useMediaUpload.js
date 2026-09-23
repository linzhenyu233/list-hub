// 商品编辑里「选本地文件直接上传」的公共部分：微信、小红书都走同一套流程，
// 差别只在最后一步调哪个平台的接口（由调用方传入的 upload 回调决定）。
//   upload: async ({ filename, contentBase64 }) => 可直接发品的素材地址
export const MAX_IMAGE_BYTES = 20 * 1024 * 1024
export const MAX_VIDEO_BYTES = 50 * 1024 * 1024
// 小红书：视频要 base64 后一次性 POST，而网关请求体上限约 30MB（实测 30MB 通过、32MB 报 413），
// base64 会膨胀约 1/3，所以视频最大只能约 22MB —— 这里取 20MB 留余量。
// 微信是分块上传（每块 1.5MB），不受此限，仍用 MAX_VIDEO_BYTES。
export const MAX_XHS_VIDEO_BYTES = 20 * 1024 * 1024

export function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '')
    reader.onerror = () => reject(new Error('读取本地文件失败'))
    reader.readAsDataURL(file)
  })
}

export async function uploadLocalImage(file, upload) {
  if (!file.type.startsWith('image/')) throw new Error('不是图片文件')
  if (file.size > MAX_IMAGE_BYTES) throw new Error('图片超过 20MB')
  const url = await upload({ filename: file.name, contentBase64: await fileToBase64(file) })
  if (!url) throw new Error('平台未返回图片地址')
  return url
}

export async function uploadLocalVideo(file, upload, maxBytes = MAX_VIDEO_BYTES) {
  if (!file.type.startsWith('video/')) throw new Error('不是视频文件')
  if (file.size > maxBytes) throw new Error(`视频超过 ${Math.round(maxBytes / 1024 / 1024)}MB`)
  const url = await upload({ filename: file.name, contentBase64: await fileToBase64(file) })
  if (!url) throw new Error('平台未返回视频地址')
  return url
}
