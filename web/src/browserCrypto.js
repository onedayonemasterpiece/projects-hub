// Older Android WebViews expose secure random bytes before crypto.randomUUID.
// Install before App and the shared client load so all request IDs keep using
// the same cryptographic UUID contract, including native bridge acknowledgments.
const cryptoObject = globalThis.crypto;
if (typeof cryptoObject?.randomUUID !== 'function') {
  if (typeof cryptoObject?.getRandomValues !== 'function') {
    throw new Error('Secure random generator unavailable');
  }
  Object.defineProperty(cryptoObject, 'randomUUID', {
    configurable: true,
    writable: true,
    value() {
      const bytes = cryptoObject.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    },
  });
}
