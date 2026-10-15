import { useSyncExternalStore } from "react";

export type Route = "assistant" | "cases";

function parse(): Route {
  const h = window.location.hash.replace(/^#\/?/, "");
  return h === "cases" ? "cases" : "assistant";
}

function subscribe(fn: () => void): () => void {
  window.addEventListener("hashchange", fn);
  return () => window.removeEventListener("hashchange", fn);
}

/** Hash routing keeps deep links working on S3/CloudFront without server rewrites. */
export function useRoute(): Route {
  return useSyncExternalStore(subscribe, parse, () => "assistant" as Route);
}
