import { VOYAGE_APP_DEFAULTS } from "@yiminlab/voyage";
import { VoyageProvider } from "@yiminlab/voyage/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { initializeNativeRecords, initializePlatform } from "./platform";
import HostedEntry from "./HostedEntry";
// Self-hosted (no external CDN dependency — matches the old GUI's icon set
// without its jsdelivr-CDN tech debt; see epic issue for #14).
import "@tabler/icons-webfont/dist/tabler-icons.min.css";
import "@yiminlab/voyage/tokens.css";
import "@yiminlab/voyage/voyage.css";
import "./App.css";

const root = document.getElementById("root")!;
const native = document.querySelector('meta[name="quarry-mode"]')?.getAttribute("content") === "native";
async function openNativeSession(): Promise<boolean> {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const code = fragment.get("quarry_workbench");
  if (code) {
    fragment.delete("quarry_workbench");
    history.replaceState(history.state, "", location.pathname + location.search + (fragment.size ? "#" + fragment.toString() : ""));
  }
  try {
    const response = await fetch("/api/workbench/session", code ? {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
      cache: "no-store",
    } : { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}
if (native && !await openNativeSession()) {
  root.textContent = navigator.language.startsWith("zh")
    ? "请通过 ylab db connect 打开本机 Quarry。"
    : "Open this local Quarry workbench with ylab db connect.";
} else {
  await initializePlatform();
  await initializeNativeRecords();
  const { default: App } = await import("./App.tsx");
  createRoot(root).render(
    <StrictMode>
      <VoyageProvider defaults={VOYAGE_APP_DEFAULTS.quarry}>
        <HostedEntry><App /></HostedEntry>
      </VoyageProvider>
    </StrictMode>,
  );
}
