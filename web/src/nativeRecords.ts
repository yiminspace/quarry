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
/** The saved document at `revision`: the common ancestor for cross-window merges. */
let base: Values = {};
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
const replaceListeners = new Set<(keys: Set<string>) => void>();
/** Called after records saved by another window were merged into this window. */
export const subscribeNativeRecordsReplaced = (listener: (keys: Set<string>) => void) => { replaceListeners.add(listener); return () => { replaceListeners.delete(listener); }; };
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

function saveBody(): { body: string; sent: Values } | null {
  let sent = { ...values };
  let document = JSON.stringify({ values: sent });
  if (utf8.encode(document).length > DOCUMENT_BYTE_LIMIT) {
    sent = withoutResultSnapshots(values);
    document = JSON.stringify({ values: sent });
    if (utf8.encode(document).length > DOCUMENT_BYTE_LIMIT) return null;
  }
  return { body: `{"value":${document},"revision":${revision},"cloudIds":${JSON.stringify([...editableCloudIds])}}`, sent };
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

type StoredTab = Record<string, unknown> & { id?: unknown };
const MAX_HISTORY = 1000; // useSqlHistory MAX_ENTRIES
/** Per-window bookkeeping: never a reason to report a conflict. */
const TAB_COUNTERS = new Set(["visited", "sqlRevision"]);

function sameValues(a: Values, b: Values): boolean {
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((key) => Object.hasOwn(b, key) && a[key] === b[key]);
}

function parseArray(raw: string | undefined): unknown[] {
  if (raw === undefined) return [];
  const value: unknown = JSON.parse(raw);
  if (!Array.isArray(value)) throw new Error("Invalid workbench array");
  return value;
}

class MergeConflict extends Error {}

function tabId(tab: unknown): string {
  if (!tab || typeof tab !== "object" || Array.isArray(tab) || typeof (tab as StoredTab).id !== "string") throw new Error("Invalid workbench tab");
  return (tab as StoredTab).id as string;
}
const tabContent = (tab: StoredTab): string =>
  JSON.stringify(Object.entries(tab).filter(([key, value]) => !TAB_COUNTERS.has(key) && value !== undefined && value !== null)
    .sort(([a], [b]) => a.localeCompare(b)));

/** Field-wise three-way merge of one tab present in both windows. */
function mergeTab(baseTab: StoredTab | undefined, local: StoredTab, remote: StoredTab): StoredTab {
  if (!baseTab && tabContent(local) !== tabContent(remote)) throw new MergeConflict();
  const merged: StoredTab = {};
  for (const key of new Set([...Object.keys(remote), ...Object.keys(local)])) {
    const l = JSON.stringify(local[key] ?? null), r = JSON.stringify(remote[key] ?? null), b = JSON.stringify(baseTab?.[key] ?? null);
    if (TAB_COUNTERS.has(key)) {
      const numbers = [local[key], remote[key]].filter((value): value is number => typeof value === "number");
      if (numbers.length) merged[key] = Math.max(...numbers);
    } else if (l === r || r === b) merged[key] = local[key];
    else if (l === b) merged[key] = remote[key];
    else throw new MergeConflict(); // Both windows edited this field differently.
  }
  return merged;
}

/** Union of tabs by id. Tabs are kept unless one side removed an unchanged tab;
 * an edited tab survives the other window closing it. */
function mergeTabs(base: Values, local: Values, remote: Values, merged: Values): void {
  const read = (source: Values) => {
    const tabs = parseArray(source.qy_tabs) as StoredTab[];
    const results = parseArray(source.qy_tabres);
    const ids = tabs.map(tabId);
    const ati = Number(source.qy_ati ?? 0);
    return { tabs, ids, byId: new Map(ids.map((id, index) => [id, tabs[index]])), result: (id: string) => {
      const index = ids.indexOf(id);
      return index === -1 ? null : results[index] ?? null;
    }, active: Number.isInteger(ati) ? ids[ati] : undefined };
  };
  const b = read(base), l = read(local), r = read(remote);
  // Keep this window's arrangement unless it is still the saved one.
  const localOrderChanged = JSON.stringify(l.ids.filter((id) => b.byId.has(id))) !== JSON.stringify(b.ids.filter((id) => l.byId.has(id)));
  const used = new Set<string>([...b.ids, ...l.ids, ...r.ids]);
  const nextId = () => {
    let n = Math.max(0, ...[...used].map((id) => Number(id.slice(1))).filter(Number.isFinite)) + 1;
    while (used.has(`t${n}`)) n++;
    used.add(`t${n}`);
    return `t${n}`;
  };
  const out: { tab: StoredTab; result: unknown }[] = [];
  const remap = new Map<string, string>();
  const changedFromBase = (tab: StoredTab, id: string) => !b.byId.has(id) || tabContent(b.byId.get(id)!) !== tabContent(tab);
  const pickResult = (id: string) => JSON.stringify(l.result(id)) !== JSON.stringify(b.result(id)) ? l.result(id) : r.result(id);
  // Two windows that opened the same connection independently get one tab.
  const remoteNewEmpty = r.ids.filter((id) => !b.byId.has(id) && !l.byId.has(id) && r.byId.get(id)!.sql === "" && !r.byId.get(id)!.title);
  const takeLocal = (id: string) => {
    const tab = l.byId.get(id)!;
    const remoteTab = r.byId.get(id);
    if (remoteTab) {
      try {
        out.push({ tab: mergeTab(b.byId.get(id), tab, remoteTab), result: pickResult(id) });
      } catch (error) {
        if (b.byId.has(id) || !(error instanceof MergeConflict)) throw error;
        // Both windows created a different tab with the same counter value.
        const renamed = nextId();
        remap.set(id, renamed);
        out.push({ tab: remoteTab, result: r.result(id) }, { tab: { ...tab, id: renamed }, result: l.result(id) });
      }
      return;
    }
    if (b.byId.has(id) && !changedFromBase(tab, id)) return; // Closed in another window.
    if (!b.byId.has(id) && tab.sql === "" && !tab.title) {
      const twin = remoteNewEmpty.find((other) => tabContent({ ...r.byId.get(other)!, id }) === tabContent(tab));
      if (twin) { remoteNewEmpty.splice(remoteNewEmpty.indexOf(twin), 1); remap.set(id, twin); return; }
    }
    out.push({ tab, result: l.result(id) });
  };
  const takeRemote = (id: string) => {
    const tab = r.byId.get(id)!;
    if (b.byId.has(id) && !changedFromBase(tab, id)) return; // Closed in this window.
    out.push({ tab, result: r.result(id) });
  };
  if (localOrderChanged) {
    l.ids.forEach(takeLocal);
    r.ids.filter((id) => !l.byId.has(id)).forEach(takeRemote);
  } else {
    for (const id of r.ids) if (l.byId.has(id)) takeLocal(id); else takeRemote(id);
    l.ids.filter((id) => !r.byId.has(id)).forEach(takeLocal);
  }
  const ids = out.map(({ tab }) => tab.id);
  const localActive = l.active === undefined ? undefined : remap.get(l.active) ?? l.active;
  const preferred = local.qy_ati !== base.qy_ati || l.active !== b.active ? localActive : r.active;
  const index = [preferred, localActive, r.active].map((id) => id === undefined ? -1 : ids.indexOf(id)).find((i) => i !== -1);
  merged.qy_tabs = JSON.stringify(out.map(({ tab }) => tab));
  merged.qy_ati = String(index ?? Math.min(Number(local.qy_ati ?? 0) || 0, out.length - 1));
  if (Object.hasOwn(local, "qy_tabres") || Object.hasOwn(remote, "qy_tabres")) merged.qy_tabres = JSON.stringify(out.map(({ result }) => result));
}

function mergeHistory(local: string, remote: string): string {
  const seen = new Set<string>();
  const entries = [...parseArray(local), ...parseArray(remote)].filter((entry) => {
    const key = JSON.stringify(entry);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const ts = (entry: unknown) => typeof entry === "object" && entry && typeof (entry as { ts?: unknown }).ts === "number" ? (entry as { ts: number }).ts : 0;
  return JSON.stringify(entries.sort((a, b) => ts(b) - ts(a)).slice(0, MAX_HISTORY));
}

function mergeObject(base: string | undefined, local: string, remote: string): string {
  const parse = (raw: string | undefined): Record<string, unknown> => {
    const value: unknown = JSON.parse(raw ?? "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Not an object");
    return value as Record<string, unknown>;
  };
  const b = parse(base), l = parse(local), r = parse(remote);
  const out: Record<string, unknown> = { ...r };
  for (const key of new Set([...Object.keys(l), ...Object.keys(b)])) {
    const lv = JSON.stringify(l[key]), bv = JSON.stringify(b[key]);
    if (lv === bv) continue; // Unchanged here: the remote value (or removal) stands.
    if (Object.hasOwn(l, key)) out[key] = l[key]; else delete out[key];
  }
  return JSON.stringify(out);
}

/** Three-way merge of this window's records with a newer saved document.
 * Returns null only for divergent edits of the same tab. Preferences changed in
 * both windows keep this window's value. */
export function mergeNativeRecords(baseValues: Values, local: Values, remote: Values): Values | null {
  const merged: Values = {};
  const tabKeys = ["qy_tabs", "qy_tabres", "qy_ati"];
  try {
    for (const key of new Set([...Object.keys(remote), ...Object.keys(local), ...Object.keys(baseValues)])) {
      if (tabKeys.includes(key)) continue;
      const l = local[key], r = remote[key], b = baseValues[key];
      let value: string | undefined;
      if (l === r || r === b) value = l;
      else if (l === b) value = r;
      else if (l === undefined || r === undefined) value = l ?? r; // Removed in one window, edited in the other.
      else if (key === "qy_hist") value = mergeHistory(l, r);
      else {
        try { value = mergeObject(b, l, r); }
        catch { value = l; }
      }
      if (value !== undefined) merged[key] = value;
    }
    const same = (a: Values, b: Values) => tabKeys.every((key) => a[key] === b[key]);
    const source = same(local, baseValues) ? remote : same(remote, baseValues) || same(local, remote) ? local : null;
    if (source) {
      for (const key of tabKeys) if (Object.hasOwn(source, key)) merged[key] = source[key];
    } else {
      mergeTabs(baseValues, local, remote, merged);
    }
    return merged;
  } catch {
    return null;
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
    base = { ...saved.values };
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
    if (sameValues(values, saved.values) || (revision < saved.revision && containsRecovery(saved.values, recovered.values))) {
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
  } finally {
    listenForOtherWindows();
  }
}

// Windows on this origin and workbench scope announce each saved revision.
const channelName = scope ? `qy_native:${encodeURIComponent(scope)}:saved` : null;
let channel: BroadcastChannel | null = null;
function announceSaved(): void {
  if (!channelName) return;
  try {
    if (channel) channel.postMessage({ revision });
    else localStorage.setItem(channelName, JSON.stringify({ revision, at: Date.now() }));
  } catch { /* Other windows still merge on their next save. */ }
}
function heardRevision(data: unknown): void {
  const announced = (data as { revision?: unknown } | null)?.revision;
  if (validRevision(announced) && announced > revision) requestSync("announced");
}
function listenForOtherWindows(): void {
  if (!channelName || channel) return;
  if (typeof BroadcastChannel === "function") {
    channel = new BroadcastChannel(channelName);
    channel.onmessage = (event) => heardRevision(event.data);
  } else {
    window.addEventListener("storage", (event) => {
      if (event.key !== channelName || !event.newValue) return;
      try { heardRevision(JSON.parse(event.newValue)); } catch { /* ignore malformed announcements */ }
    });
  }
}

let pendingSync: "announced" | "rejected" | null = null;
function requestSync(reason: "announced" | "rejected"): void {
  if (persistence !== "ready") return;
  if (saving) {
    if (pendingSync !== "rejected") pendingSync = reason;
    return;
  }
  void syncFromSaved(reason);
}

/** Merge a document another window saved into this window's records. */
async function syncFromSaved(reason: "announced" | "rejected"): Promise<void> {
  if (persistence !== "ready" || saving) return;
  saving = true;
  clearTimeout(timer);
  try {
    const saved = await loadSaved();
    if (saved.revision <= revision) {
      if (reason === "rejected") { persistence = "conflict"; notify(); }
      return;
    }
    // Merge against the latest local values: edits made during the read are kept.
    const merged = mergeNativeRecords(base, values, saved.values);
    if (!merged) {
      persistence = "conflict";
      notify();
      return;
    }
    const before = values;
    values = merged;
    base = { ...saved.values };
    revision = saved.revision;
    saved.cloudIds.forEach((id) => editableCloudIds.add(id));
    dirty = !sameValues(merged, saved.values);
    if (dirty) edits++;
    saveRecovery();
    const keys = new Set([...Object.keys(before), ...Object.keys(merged)].filter((key) => before[key] !== merged[key]));
    if (keys.size) replaceListeners.forEach((listener) => listener(keys));
  } catch {
    persistence = "unavailable";
    notify();
  } finally {
    saving = false;
    afterBusy();
  }
}

function afterBusy(): void {
  const reason = pendingSync;
  pendingSync = null;
  if (reason) requestSync(reason);
  else if (dirty) scheduleSave();
}

function scheduleSave(): void {
  if (persistence !== "ready" || saving) return;
  clearTimeout(timer);
  timer = setTimeout(() => { void flushNativeRecords(); }, 250);
}
async function flushNativeRecords(): Promise<void> {
  if (!dirty || persistence !== "ready" || saving) return;
  const prepared = saveBody();
  if (prepared === null) {
    setOversized(true);
    return;
  }
  const { body, sent } = prepared;
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
      // Another window saved first: merge its records, then save again.
      pendingSync = "rejected";
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
    base = sent;
    dirty = edits !== version;
    saveRecovery();
    setOversized(false);
    announceSaved();
  } catch {
    persistence = "unavailable";
    notify();
  } finally {
    saving = false;
    if (pendingSync) afterBusy();
    else if (dirty && edits !== version) scheduleSave();
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
