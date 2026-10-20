// vitest's default "node" environment has no `localStorage` global, but
// modules like i18n.ts read it at import time (`LANG`) — polyfill a minimal
// in-memory Storage so unit tests can import app modules without pulling in
// a full DOM environment (jsdom/happy-dom).
if (typeof globalThis.localStorage === "undefined") {
  const store = new Map<string, string>();
  const storage: Storage = {
    getItem: (key) => store.get(key) ?? null,
    setItem: (key, value) => void store.set(key, String(value)),
    removeItem: (key) => void store.delete(key),
    clear: () => store.clear(),
    key: (index) => Array.from(store.keys())[index] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(globalThis, "localStorage", { value: storage });
}
