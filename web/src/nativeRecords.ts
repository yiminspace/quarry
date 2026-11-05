import { t } from "./i18n";

type Values = Record<string, string>;
type Document = { values: Values; revision: number };
type Recovery = Document & { dirty: true };
type Persistence = "ready" | "unavailable" | "conflict";
export const NATIVE_MODE = typeof document !== "undefined" && document.querySelector('meta[name="quarry-mode"]')?.getAttribute("content") === "native";
const scope = NATIVE_MODE ? document.querySelector('meta[name="quarry-workbench-scope"]')?.getAttribute("content") : null;
const recoveryKey = scope ? `qy_native:${encodeURIComponent(scope)}:recovery` : null;
const path = "/api/personal";
let values: Values = {};
let revision = 0;
let edits = 0;
let dirty = false;
let saving = false;
let timer: ReturnType<typeof setTimeout> | undefined;
let persistence: Persistence = "ready";
let storageError = "";
let ownRecovery: string | null = null;
const listeners = new Set<() => void>();
export const subscribeNativeRecords = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export function nativeRecordsError(): string {
  if (persistence === "conflict") return t("native_records_conflict");
  if (persistence === "unavailable") return t("native_records_unavailable");
  return storageError;
}
function notify() { listeners.forEach((listener) => listener()); }

function validRevision(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
function parseValues(value: unknown): Values | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const entries = Object.entries(value);
  if (!entries.every(([, item]) => typeof item === "string")) return null;
  return Object.fromEntries(entries);
}
function readRecovery(): Recovery | null {
  if (!recoveryKey) return null;
  try {
    const backup = sessionStorage.getItem(recoveryKey) ?? localStorage.getItem(recoveryKey);
    const raw = JSON.parse(backup || "null");
    const recovered = parseValues(raw?.values);
    if (recovered && raw?.dirty === true && validRevision(raw.revision)) {
      ownRecovery = backup;
      return { values: recovered, revision: raw.revision, dirty: true };
    }
  } catch { /* An invalid recovery document cannot replace the SQLite record. */ }
  return null;
}

function clearRecovery(key: string): void {
  // The shared slot can contain an unsaved draft from a different window.
  if (localStorage.getItem(key) === ownRecovery) localStorage.removeItem(key);
  sessionStorage.setItem(key, "null");
  ownRecovery = null;
}

const BROWSER_LIMIT = 2_000_000;
function saveRecovery(): void {
  try {
    if (!recoveryKey) throw new Error("Native workbench storage scope is missing");
    if (dirty) {
      const copy = { ...values };
      const document = () => JSON.stringify({ values: copy, revision, dirty });
      let raw = document();
      if (raw.length > BROWSER_LIMIT) {
        delete copy.qy_tabres;
        raw = document();
      }
      if (raw.length > BROWSER_LIMIT) {
        delete copy.qy_assistant_conversations;
        delete copy.qy_hist;
        raw = document();
      }
      if (raw.length > BROWSER_LIMIT) throw new Error("Native workbench recovery exceeds storage capacity");
      sessionStorage.setItem(recoveryKey, raw);
      const shared = localStorage.getItem(recoveryKey);
      if (shared === null || shared === ownRecovery) localStorage.setItem(recoveryKey, raw);
      ownRecovery = raw;
    } else {
      clearRecovery(recoveryKey);
    }
    if (storageError) { storageError = ""; notify(); }
  } catch {
    storageError = t("native_records_recovery_unavailable");
    notify();
  }
}

async function loadSaved(): Promise<Document> {
  const response = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(3000) });
  if (!response.ok) throw new Error("Cannot load native records");
  const data = await response.json();
  const loaded = data?.value === null ? {} : parseValues(data?.value?.values);
  if (!loaded || !validRevision(data?.revision)) throw new Error("Invalid native records");
  return { values: loaded, revision: data.revision };
}

export async function initializeNativeRecords(): Promise<void> {
  if (!NATIVE_MODE) return;
  const recovered = readRecovery();
  if (recovered) {
    values = recovered.values;
    revision = recovered.revision;
    dirty = true;
  }
  try {
    const saved = await loadSaved();
    if (!recovered) {
      values = saved.values;
      revision = saved.revision;
      return;
    }
    const sameValues = Object.keys(values).length === Object.keys(saved.values).length &&
      Object.entries(values).every(([key, value]) => Object.hasOwn(saved.values, key) && saved.values[key] === value);
    if (sameValues) {
      revision = saved.revision;
      dirty = false;
      saveRecovery();
    } else if (revision === saved.revision) {
      scheduleSave();
    } else {
      persistence = "conflict";
      notify();
    }
  } catch {
    persistence = "unavailable";
    notify();
  }
}

function scheduleSave(): void {
  if (persistence !== "ready" || saving) return;
  clearTimeout(timer);
  timer = setTimeout(() => { void flushNativeRecords(); }, 250);
}
async function flushNativeRecords(): Promise<void> {
  if (!dirty || persistence !== "ready" || saving) return;
  saving = true;
  const version = edits;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({ value: { values }, revision }),
      signal: AbortSignal.timeout(5000),
    });
    if (response.status === 409) {
      persistence = "conflict";
      notify();
      return;
    }
    if (!response.ok) throw new Error("Cannot save native records");
    const saved = await response.json();
    if (saved?.revision !== revision + 1) throw new Error("Invalid saved native records");
    revision = saved.revision;
    dirty = edits !== version;
    saveRecovery();
  } catch {
    persistence = "unavailable";
    notify();
  } finally {
    saving = false;
    if (dirty) scheduleSave();
  }
}
function changed(): void {
  edits++;
  dirty = true;
  saveRecovery();
  scheduleSave();
}
export const nativePersonalStorage = {
  getItem(key: string): string | null { return values[key] ?? null; },
  setItem(key: string, value: string): void {
    if (values[key] === value) return;
    values[key] = value;
    changed();
  },
  removeItem(key: string): void {
    if (!Object.hasOwn(values, key)) return;
    delete values[key];
    changed();
  },
};

export function exportNativeRecovery(): void {
  const blob = new Blob([JSON.stringify({ values }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "quarry-workbench-recovery.json";
  anchor.click();
  URL.revokeObjectURL(url);
}

export async function loadSavedNativeRecords(): Promise<void> {
  // Read before discarding the only recovery copy.
  try {
    await loadSaved();
    if (recoveryKey) clearRecovery(recoveryKey);
    location.reload();
  } catch {
    persistence = "unavailable";
    notify();
  }
}
