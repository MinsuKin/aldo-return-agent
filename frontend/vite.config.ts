import { defineConfig, loadEnv, type Connect, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Same-origin /api proxy for `vite` and `vite preview`.
 *
 * Vite's built-in `server.proxy` (http-proxy) stalls every request on Node 26.10 with Vite 8.3.4
 * (verified: a proxy entry makes even `GET /` hang), so this forwards /api with fetch instead.
 * Target: API_PROXY_TARGET, default the local mock API (`npm run mock`, port 3001). Point the app at
 * a real deployment with:   API_PROXY_TARGET=https://<cloudfront-domain> npm run dev
 */
function apiProxy(target: string): Plugin {
  const base = target.replace(/\/+$/, "");
  const skip = new Set(["host", "connection", "content-length", "accept-encoding", "transfer-encoding"]);
  const handler: Connect.NextHandleFunction = async (req, res, next) => {
    const url = req.url ?? "";
    if (!(url === "/api" || url.startsWith("/api/") || url.startsWith("/api?"))) return next();
    try {
      const chunks: Buffer[] = [];
      for await (const chunk of req) chunks.push(chunk as Buffer);
      const method = req.method ?? "GET";
      const headers = new Headers();
      for (const [k, v] of Object.entries(req.headers)) {
        if (v !== undefined && !skip.has(k)) headers.set(k, Array.isArray(v) ? v.join(", ") : v);
      }
      const upstream = await fetch(base + url, {
        method,
        headers,
        body: chunks.length && method !== "GET" && method !== "HEAD" ? Buffer.concat(chunks) : undefined,
        redirect: "manual",
      });
      res.statusCode = upstream.status;
      upstream.headers.forEach((value, key) => {
        if (!skip.has(key) && key !== "content-encoding") res.setHeader(key, value);
      });
      res.end(Buffer.from(await upstream.arrayBuffer()));
    } catch {
      res.statusCode = 502;
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ error: { code: "UPSTREAM_TIMEOUT", message: `The dev proxy could not reach ${base}.`, retryable: true } }));
    }
  };
  return {
    name: "api-proxy",
    configureServer(server) {
      server.middlewares.use(handler);
    },
    configurePreviewServer(server) {
      server.middlewares.use(handler);
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [react(), apiProxy(env.API_PROXY_TARGET || "http://127.0.0.1:3001")],
    server: { port: 5173, strictPort: true },
    preview: { port: 4173, strictPort: true },
  };
});
