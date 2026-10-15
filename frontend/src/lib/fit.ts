import { isObj, num, str } from "./util";

/** One-line, honest description of ALDO's fit signal for a style (always derived from provided data). */
export function fitHeadline(sig: unknown): string | undefined {
  if (!isObj(sig)) return undefined;
  const signal = str(sig.signal);
  const share = num(sig.too_small_share);
  const n = num(sig.n_fit);
  if (!signal) return undefined;
  const count = n !== undefined ? ` (n=${n.toLocaleString("en-CA")})` : "";
  if (signal === "runs_small" && share !== undefined) return `Runs small: ${(share * 100).toFixed(1)}% of fit-related returns were too small${count}`;
  if (signal === "runs_large" && share !== undefined) return `Runs large: ${((1 - share) * 100).toFixed(1)}% of fit-related returns were too big${count}`;
  if (signal === "mixed") return `Mixed fit signal${count}`;
  if (signal === "insufficient_data") return `Not enough return data to judge the fit${count}`;
  return `${signal.replace(/_/g, " ")}${count}`;
}
