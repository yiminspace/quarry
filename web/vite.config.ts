import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/quarry/",
  build: {
    outDir: "../src/quarry/web_dist",
    emptyOutDir: true,
  },
  test: {
    setupFiles: ["./src/test-setup.ts"],
  },
});
