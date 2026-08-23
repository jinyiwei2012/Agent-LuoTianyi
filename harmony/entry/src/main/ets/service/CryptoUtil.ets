import { cryptoFramework } from '@kit.CryptoArchitectureKit';
import { util } from '@kit.ArkTS';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'CryptoUtil';

/** 加密结果（显式 interface，满足 ArkTS 严格模式对象字面量约束） */
export interface EncryptResult {
  ok: boolean;
  encrypted: string;
  reason: string;
}

/**
 * RSA-OAEP + SHA-256 密码加密工具。
 * 严格对齐 app/utils/crypto.ts（node-forge）与服务端 Python padding.OAEP：
 *   - OAEP 主哈希 SHA-256
 *   - MGF1 哈希 SHA-256（cryptoFramework 的 RSA|PKCS1_OAEP|SHA256 MGF1 默认与主摘要一致）
 *   - OAEP label 为空
 */

function pemToBase64(pem: string): string {
  // 去掉 PEM 头尾与换行，只留 base64 内容
  const lines: string[] = [];
  for (const rawLine of pem.split('\n')) {
    const line = rawLine.trim();
    if (line.length === 0) {
      continue;
    }
    if (line.startsWith('-----BEGIN') || line.startsWith('-----END')) {
      continue;
    }
    lines.push(line);
  }
  return lines.join('');
}

function bytesToUtf8String(bytes: Uint8Array): string {
  // Uint8Array -> string（base64 输出字节转字符串）
  let out = '';
  for (let i = 0; i < bytes.length; i++) {
    out += String.fromCharCode(bytes[i]);
  }
  return out;
}

/**
 * 加密密码：RSA-OAEP-SHA256，输出 base64。
 * @param password 明文密码（UTF-8）
 * @param publicKeyPem 服务端 /auth/public_key 返回的 PEM 公钥
 */
export function encryptPasswordWithPem(password: string, publicKeyPem: string): EncryptResult {
  try {
    // 1. PEM -> DER（SPKI X.509 SubjectPublicKeyInfo）
    const base64Helper = new util.Base64Helper();
    const derData: Uint8Array = base64Helper.decodeSync(pemToBase64(publicKeyPem));

    // 2. 导入 RSA 公钥
    const asyKeyGen = cryptoFramework.createAsyKeyGenerator('RSA2048');
    const keyPair = asyKeyGen.convertKeySync({ data: derData }, null);
    const pubKey = keyPair.pubKey;

    // 3. UTF-8 编码密码
    const passwordData: Uint8Array = new util.TextEncoder().encodeInto(password);

    // 4. RSA-OAEP-SHA256 加密（一次性 update + doFinal）
    const cipher = cryptoFramework.createCipher('RSA|PKCS1_OAEP|SHA256');
    cipher.initSync(cryptoFramework.CryptoMode.ENCRYPT_MODE, pubKey, null);
    const encData = cipher.updateSync({ data: passwordData });
    const finalData = cipher.doFinalSync(null);

    const combined = new Uint8Array(encData.data.length + finalData.data.length);
    combined.set(encData.data, 0);
    combined.set(finalData.data, encData.data.length);

    // 5. base64 输出（encodeSync 返回 base64 字符字节）
    const encBytes: Uint8Array = base64Helper.encodeSync(combined);
    const encrypted = bytesToUtf8String(encBytes);
    hilog.info(0x0000, TAG, 'encrypt ok, len=%{public}d', encrypted.length);
    const okResult: EncryptResult = { ok: true, encrypted: encrypted, reason: '' };
    return okResult;
  } catch (e) {
    hilog.error(0x0000, TAG, 'encrypt failed: %{public}s', (e as Error).message);
    const failResult: EncryptResult = { ok: false, encrypted: '', reason: (e as Error).message };
    return failResult;
  }
}
