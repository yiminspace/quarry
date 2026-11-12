import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const WORKBENCH_META = new Set([
  "quarry-federated",
  "quarry-mode",
  "quarry-workbench-entry",
  "quarry-workbench-scope",
]);

function localWorkbenchMetadata(target: string): Plugin {
  return {
    name: "quarry-local-workbench-metadata",
    async transformIndexHtml() {
      const response = await fetch(`${target}/quarry/`, {
        redirect: "error",
        signal: AbortSignal.timeout(3000),
      }).catch(() => {
        throw new Error(`Quarry backend is unavailable at ${target}. Start the local Rust workbench.`);
      });
      if (!response.ok) throw new Error(`Quarry backend at ${target} returned HTTP ${response.status}.`);
      const html = await response.text();
      return Array.from(html.matchAll(/<meta name="([\w-]+)" content="([\w.:-]+)">/g))
        .filter(([, name]) => WORKBENCH_META.has(name))
        .map(([, name, content]) => ({ tag: "meta", attrs: { name, content }, injectTo: "head-prepend" }));
    },
  };
}

export default defineConfig(({ command, isPreview, mode }) => {
  const config = {
    plugins: [react()],
    base: "/quarry/",
    build: {
      outDir: "../src/quarry/web_dist",
      emptyOutDir: true,
    },
    test: {
      setupFiles: ["./src/test-setup.ts"],
    },
  };
  if (command !== "serve" || isPreview || mode === "test") return config;

  const { QUARRY_DEV_API_PORT = "8765" } = loadEnv(mode, ".", "QUARRY_DEV_");
  const port = Number(QUARRY_DEV_API_PORT);
  if (!/^\d+$/.test(QUARRY_DEV_API_PORT) || port < 1 || port > 65535) {
    throw new Error("QUARRY_DEV_API_PORT must be a port from 1 to 65535");
  }
  const target = `http://127.0.0.1:${port}`;

  return {
    ...config,
    plugins: [...config.plugins, localWorkbenchMetadata(target)],
    server: {
      host: "127.0.0.1",
      proxy: {
        "^/api(?:/|\\?|$)": {
          target,
          changeOrigin: true,
          bypass(req) {
            const host = `127.0.0.1:${req.socket.localPort}`;
            const origin = `http://${host}`;
            const { origin: requestOrigin, referer } = req.headers;

            // Rust checks both headers against its own loopback port.
            // Translate only requests from the Vite workbench's origin.
            if (
              req.headers.host !== host ||
              (requestOrigin !== undefined && requestOrigin !== origin) ||
              (referer !== undefined && !referer.startsWith(`${origin}/`))
            ) {
              return false;
            }
            if (requestOrigin) req.headers.origin = target;
            if (referer) req.headers.referer = target + referer.slice(origin.length);
          },
        },
      },
    },
  };
});
