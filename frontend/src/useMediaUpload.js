// 商品编辑里「选本地图片直接上传」的公共部分：微信、小红书都走同一套流程，
// 差别只在最后一步调哪个平台的接口（由调用方传入的 upload 回调决定）。
//   upload: async ({ filename, contentBase64 }) => 可直接发品的图片地址
export const MAX_IMAGE_BYTES = 20 * 1024 * 1024

export function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '')
    reader.onerror = () => reject(new Error('读取本地图片失败'))
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
