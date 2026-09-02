import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';

// Shared AES-256-GCM envelope used to store OAuth tokens at rest. Both
// feishu-user-oauth.mjs and baidu-pan-storage.mjs previously carried
// byte-for-byte copies of this pair; the key derivation (which differs by
// secret/domain) still lives in each caller.
export function encryptSecret(value, key) {
	const iv = randomBytes(12);
	const cipher = createCipheriv('aes-256-gcm', key, iv);
	const ciphertext = Buffer.concat([cipher.update(String(value), 'utf8'), cipher.final()]);
	return `v1.${iv.toString('base64url')}.${cipher.getAuthTag().toString('base64url')}.${ciphertext.toString('base64url')}`;
}

export function decryptSecret(value, key, invalidMessage = '保存的凭据格式无效，请重新授权') {
	const [version, iv, tag, ciphertext] = String(value ?? '').split('.');
	if (version !== 'v1' || !iv || !tag || !ciphertext) throw new Error(invalidMessage);
	const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(iv, 'base64url'));
	decipher.setAuthTag(Buffer.from(tag, 'base64url'));
	return Buffer.concat([decipher.update(Buffer.from(ciphertext, 'base64url')), decipher.final()]).toString('utf8');
}
