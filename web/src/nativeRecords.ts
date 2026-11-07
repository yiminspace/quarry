import { t } from "./i18n";

type Values = Record<string, string>;
type Document = { values: Values; revision: number; cloudIds: string[] };
type Recovery = Omit<Document, "cloudIds"> & { dirty: true };
type Persistence = "ready" | "unavailable" | "conflict";
export const NATIVE_MODE = typeof document !== "undefined" && document.querySelector('meta[name="quarry-mode"]')?.getAttribute("content") === "native";
const scope = NATIVE_MODE ? document.querySelector('meta[name="quarry-workbench-scope"]')?.getAttribute("content") : null;
const recoveryKey = scope ? `qy_native:${encodeURIComponent(scope)}:recovery` : null;
const STANDALONE_SHARED = NATIVE_MODE && document.querySelector('meta[name="quarry-workbench-entry"]')?.getAttribute("content") === "quarry";
const path = "/api/personal";
let values: Values = {};
let editableCloudIds = new Set<string>();
let revision = 0;
let edits = 0;
let dirty = false;
let saving = false;
let timer: ReturnType<typeof setTimeout> | undefined;
let persistence: Persistence = "ready";
let storageError = "";
let oversized = false;
let ownRecovery: string | null = null;
const listeners = new Set<() => void>();
export const subscribeNativeRecords = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export function nativeRecordsError(): string {
  if (persistence === "conflict") return t("native_records_conflict");
  if (persistence === "unavailable") return t("native_records_unavailable");
  if (oversized) return t("native_records_oversized");
  return storageError;
}
function notify() { listeners.forEach((listener) => listener()); }
function setOversized(value: boolean): void {
  if (oversized === value) return;
  oversized = value;
  notify();
}

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
const DOCUMENT_BYTE_LIMIT = 16 * 1024 * 1024; // PersonalStore::MAX_DOCUMENT_BYTES
const utf8 = new TextEncoder();

function withoutResultSnapshots(snapshot: Values): Values {
  const copy = { ...snapshot };
  delete copy.qy_tabres;
  delete copy.qy_result;
  return copy;
}

function filterCloudRecords(snapshot: Values, visible: (env: string) => boolean): Values {
  const copy = { ...snapshot };
  const allowed = (entry: { env?: string | null } | null) => !entry?.env ||
    ((!entry.env.startsWith("cloud:") && !entry.env.startsWith("remote:")) || visible(entry.env));
  try {
    const tabs = JSON.parse(copy.qy_tabs ?? "[]") as { env?: string }[];
    const results = JSON.parse(copy.qy_tabres ?? "[]") as ({ env?: string } | null)[];
    const keep = tabs.map((tab, index) => allowed(tab) ? index : -1).filter((index) => index !== -1);
    if (Object.hasOwn(copy, "qy_tabs")) {
      copy.qy_ati = String(keep.indexOf(Number(copy.qy_ati ?? 0)));
      copy.qy_tabs = JSON.stringify(keep.map((index) => tabs[index]));
    }
    if (Object.hasOwn(copy, "qy_tabres")) copy.qy_tabres = JSON.stringify(keep.map((index) => allowed(results[index]) ? results[index] : null));
  } catch { delete copy.qy_tabs; delete copy.qy_tabres; delete copy.qy_ati; }
  try {
    if (Object.hasOwn(copy, "qy_hist")) copy.qy_hist = JSON.stringify(JSON.parse(copy.qy_hist).filter(allowed));
  } catch { delete copy.qy_hist; }
  for (const key of ["qy_ui", "qy_result"]) {
    try { if (!allowed(JSON.parse(copy[key] ?? "null"))) delete copy[key]; }
    catch { delete copy[key]; }
  }
  return copy;
}

function saveBody(): string | null {
  let document = JSON.stringify({ values });
  if (utf8.encode(document).length > DOCUMENT_BYTE_LIMIT) {
    document = JSON.stringify({ values: withoutResultSnapshots(values) });
    if (utf8.encode(document).length > DOCUMENT_BYTE_LIMIT) return null;
  }
  return `{"value":${document},"revision":${revision},"cloudIds":${JSON.stringify([...editableCloudIds])}}`;
}

function saveRecovery(): void {
  try {
    if (!recoveryKey) throw new Error("Native workbench storage scope is missing");
    if (dirty) {
      // Browser recovery spans account changes on this loopback origin. Cloud
      // drafts belong to the SQLite workbench; never put their SQL or rows here.
      let copy = filterCloudRecords(values, () => false);
      const document = () => JSON.stringify({ values: copy, revision, dirty });
      let raw = document();
      if (raw.length > BROWSER_LIMIT) {
        copy = withoutResultSnapshots(copy);
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
  const response = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new Error("Cannot load native records");
  const data = await response.json();
  const loaded = data?.value === null ? {} : parseValues(data?.value?.values);
  if (!loaded || !validRevision(data?.revision)) throw new Error("Invalid native records");
  const cloudIds = Array.isArray(data.cloudIds) ? data.cloudIds.filter((id: unknown): id is string =>
    typeof id === "string" && /^f:[a-f0-9]{64}$/.test(id)) : [];
  return { values: loaded, revision: data.revision, cloudIds };
}

function containsRecovery(saved: Values, recovered: Values): boolean {
  try {
    const readArray = (source: Values, key: string): unknown[] => {
      const value: unknown = JSON.parse(source[key] ?? "[]");
      if (!Array.isArray(value)) throw new Error("Invalid workbench array");
      return value;
    };
    // Newer records can contain additional tabs and history. A stale copy
    // whose tabs still match can differ only in navigation or sidebar state.
    const tabSignature = (tab: unknown): string => {
      if (!tab || typeof tab !== "object" || Array.isArray(tab)) throw new Error("Invalid workbench tab");
      return JSON.stringify(Object.entries(tab).filter(([key]) => key !== "visited").sort(([a], [b]) => a.localeCompare(b)));
    };
    const savedTabs = new Set(readArray(saved, "qy_tabs").map(tabSignature));
    if (!readArray(recovered, "qy_tabs").every((tab) => savedTabs.has(tabSignature(tab)))) return false;
    if (readArray(recovered, "qy_tabres").some((result) => result !== null)) return false;
    if (readArray(recovered, "qy_hist").length && recovered.qy_hist !== saved.qy_hist) return false;
    const checked = new Set(["qy_tabs", "qy_tabres", "qy_hist", "qy_ati", "qy_collapsed"]);
    return Object.entries(recovered).every(([key, value]) => checked.has(key) || saved[key] === value);
  } catch {
    return false;
  }
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
      editableCloudIds = new Set(saved.cloudIds);
      if (STANDALONE_SHARED && revision === 0) {
        // The first federated launch can reuse the drafts from this browser's
        // existing standalone origin when there is no shared document yet.
        const legacy = Object.fromEntries(Object.entries(localStorage).filter(([key]) => key.startsWith("qy_")));
        const imported = filterCloudRecords(legacy, () => false);
        if (Object.keys(imported).length) {
          values = imported;
          changed();
        }
      }
      return;
    }
    const sameValues = Object.keys(values).length === Object.keys(saved.values).length &&
      Object.entries(values).every(([key, value]) => Object.hasOwn(saved.values, key) && saved.values[key] === value);
    if (sameValues || (revision < saved.revision && containsRecovery(saved.values, recovered.values))) {
      values = saved.values;
      revision = saved.revision;
      editableCloudIds = new Set(saved.cloudIds);
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
  const body = saveBody();
  if (body === null) {
    setOversized(true);
    return;
  }
  saving = true;
  const version = edits;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body,
      signal: AbortSignal.timeout(5000),
    });
    if (response.status === 409) {
      persistence = "conflict";
      notify();
      return;
    }
    if (response.status === 413) {
      setOversized(true);
      return;
    }
    if (!response.ok) throw new Error("Cannot save native records");
    const saved = await response.json();
    if (saved?.revision !== revision + 1) throw new Error("Invalid saved native records");
    revision = saved.revision;
    dirty = edits !== version;
    saveRecovery();
    setOversized(false);
  } catch {
    persistence = "unavailable";
    notify();
  } finally {
    saving = false;
    if (dirty && edits !== version) scheduleSave();
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
    if (key === "qy_tabs") {
      try {
        for (const tab of JSON.parse(value)) {
          if (typeof tab.env === "string" && /^cloud:f:[a-f0-9]{64}$/.test(tab.env)) {
            editableCloudIds.add(tab.env.slice("cloud:".length));
          }
        }
      } catch { /* A malformed value cannot claim a cloud scope. */ }
    }
    values[key] = value;
    changed();
  },
  removeItem(key: string): void {
    if (!Object.hasOwn(values, key)) return;
    delete values[key];
    changed();
  },
};

export function exportNativeRecovery(visible: (env: string) => boolean): void {
  const blob = new Blob([JSON.stringify({ values: filterCloudRecords(values, visible) }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "quarry-workbench-recovery.json";
  anchor.click();
  URL.revokeObjectURL(url);
}

export async function restoreNativeScope(): Promise<void> {
  if (!NATIVE_MODE || persistence !== "ready") return;
  for (let i = 0; saving && i < 100; i++) await new Promise((resolve) => setTimeout(resolve, 50));
  await flushNativeRecords();
  if (!dirty && !saving && persistence === "ready") location.reload();
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
