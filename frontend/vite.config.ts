import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev/preview proxy for /api. Default: the local mock API (`npm run mock`, port 3001).
// Point the app at a real deployment with:
//   API_PROXY_TARGET=https://<cloudfront-domain> npm run dev
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.API_PROXY_TARGET || "http://127.0.0.1:3001";
  const proxy = { "/api": { target, changeOrigin: true } };
  return {
    plugins: [react()],
    server: { port: 5173, strictPort: true, proxy },
    preview: { port: 4173, strictPort: true, proxy },
  };
});
