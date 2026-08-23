import { photoAccessHelper } from '@kit.MediaLibraryKit';
import { fileIo as fs } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'ImagePicker';

/** 选图结果 */
export interface PickedImage {
  uri: string;
  mimeType: string;
  imageBase64: string;
}

function guessMimeType(uri: string): string {
  const lower = uri.toLowerCase();
  if (lower.endsWith('.png')) {
    return 'image/png';
  }
  if (lower.endsWith('.webp')) {
    return 'image/webp';
  }
  if (lower.endsWith('.gif')) {
    return 'image/gif';
  }
  return 'image/jpeg';
}

/**
 * 图片选择 + base64 读取（对齐 app NetworkClient.sendImage）。
 * 返回 base64 已 sanitize（不含 data: 前缀）。
 */
export class ImagePicker {
  static async pickAndRead(): Promise<PickedImage | null> {
    try {
      const picker = new photoAccessHelper.PhotoViewPicker();
      const selectResult = await picker.select({
        MIMEType: photoAccessHelper.PhotoViewMIMETypes.IMAGE_TYPE,
        maxSelectNumber: 1,
      });
      const uris = selectResult.photoUris;
      if (uris === undefined || uris.length === 0) {
        return null;
      }
      const uri = uris[0];
      const mimeType = guessMimeType(uri);
      const file = fs.openSync(uri, fs.OpenMode.READ_ONLY);
      try {
        const stat = fs.statSync(file.fd);
        const buffer = new ArrayBuffer(stat.size);
        const readLen = fs.readSync(file.fd, buffer);
        const bytes = new Uint8Array(buffer, 0, readLen);
        const base64Helper = new util.Base64Helper();
        const encoded = base64Helper.encodeSync(bytes);
        let imageBase64 = '';
        for (let i = 0; i < encoded.length; i++) {
          imageBase64 += String.fromCharCode(encoded[i]);
        }
        hilog.info(0x0000, TAG, 'image picked: %{public}s, %{public}d bytes',
          uri.substring(Math.max(0, uri.length - 24)), stat.size);
        const result: PickedImage = {
          uri: uri,
          mimeType: mimeType,
          imageBase64: imageBase64,
        };
        return result;
      } finally {
        fs.closeSync(file);
      }
    } catch (e) {
      hilog.error(0x0000, TAG, 'pick failed: %{public}s', (e as Error).message);
      return null;
    }
  }
}
