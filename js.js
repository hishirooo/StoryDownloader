// js.js — tinh gọn cho execjs fallback (Node runtime)
// Cần có: npm i crypto-js
const CryptoJS = require("crypto-js");

/**
 * Giải mã content_comp + key_encrypt giống logic web.
 * @param {string} contentEnc  base64(iv + ciphertext)
 * @param {string} keyEnc      base64 OpenSSL AES (bọc bằng passphrase)
 * @param {string} passphrase  pass giải OpenSSL (bắt buộc!)
 * @returns {string}           HTML nội dung chương
 */
function decryptContent(contentEnc, keyEnc, passphrase) {
  if (!contentEnc) throw new Error("content_comp rỗng");
  if (!keyEnc) throw new Error("key_encrypt rỗng");

  // 1) Giải key_encrypt bằng OpenSSL (CryptoJS hỗ trợ khi dùng chuỗi + pass)
  //    Nếu server trả theo định dạng OpenSSL (Salted__), ta dùng:
  const seedWA = CryptoJS.AES.decrypt(keyEnc, passphrase);
  const seedStr = CryptoJS.enc.Utf8.stringify(seedWA);
  if (!seedStr) throw new Error("Giải key_encrypt thất bại (sai PASS?)");

  // 2) Dẫn xuất key SHA-256(seed)
  const key = CryptoJS.SHA256(seedStr);

  // 3) Giải mã content_comp (base64 của iv + ciphertext)
  const enc = CryptoJS.enc.Base64.parse(contentEnc);
  const iv   = CryptoJS.lib.WordArray.create(enc.words.slice(0, 4), 16);
  const ciph = CryptoJS.lib.WordArray.create(enc.words.slice(4));

  const decWA = CryptoJS.AES.decrypt({ ciphertext: ciph }, key, { iv });
  const out   = CryptoJS.enc.Utf8.stringify(decWA);
  if (!out) throw new Error("Giải content_comp thất bại (padding?)");
  return out;
}

module.exports = { decryptContent };
