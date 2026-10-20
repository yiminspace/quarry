import { VOYAGE_APP_DEFAULTS } from "@yiminlab/voyage";
import { VoyageProvider } from "@yiminlab/voyage/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.tsx";
// Self-hosted (no external CDN dependency — matches the old GUI's icon set
// without its jsdelivr-CDN tech debt; see epic issue for #14).
import "@tabler/icons-webfont/dist/tabler-icons.min.css";
import "@yiminlab/voyage/tokens.css";
import "@yiminlab/voyage/voyage.css";
import "./App.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <VoyageProvider defaults={VOYAGE_APP_DEFAULTS.quarry}>
      <App />
    </VoyageProvider>
  </StrictMode>,
);
