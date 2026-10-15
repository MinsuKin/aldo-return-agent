import type { CaseWire, MemoryWire, Provenance, PurchaseWire, QuoteWire, TransactionWire } from "../api/types";
import { fitHeadline } from "./fit";
import { formatScalar } from "./format";
import { bool, humanize, isObj, num, pick, str, strList } from "./util";

// ---- Provenance ------------------------------------------------------------------------

const KIND_RE = /(provided_csv|provided_doc|synthetic|derived)/;

/** Provenance kinds found in a `provenance` string and/or `source_refs` (strings or objects). */
export function provenanceKinds(...values: unknown[]): Provenance[] {
  const found = new Set<Provenance>();
  const visit = (r: unknown) => {
    if (typeof r === "string") {
      const m = KIND_RE.exec(r);
      if (m) found.add(m[1] as Provenance);
    } else if (isObj(r)) {
      const k = pick(r, ["kind", "provenance", "source", "type"]);
      const m = typeof k === "string" ? KIND_RE.exec(k) : null;
      if (m) found.add(m[1] as Provenance);
    }
  };
  for (const v of values) {
    if (Array.isArray(v)) v.forEach(visit);
    else if (v !== undefined && v !== null) visit(v);
  }
  return [...found];
}

export const PROVENANCE_LABEL: Record<Provenance, string> = {
  synthetic: "synthetic",
  derived: "derived from provided data",
  provided_csv: "provided data (CSV)",
  provided_doc: "provided document",
};

// ---- Quote options (contracts.md A.9, A.10) --------------------------------------------

export const OPTION_TYPE_LABEL: Record<string, string> = {
  exchange_ship_from_dc: "Exchange, ship from distribution centre",
  exchange_ship_from_store: "Exchange, ship from store",
  exchange_store_pickup: "Exchange, pick up in store",
  return_in_store: "Return in store",
  return_mail: "Return by mail",
};

const EVIDENCE_LABEL: Record<string, string> = {
  atp: "Stock at location",
  rate_id: "Rate",
};
const EVIDENCE_HIDDEN = new Set(["inventory_version"]);

export interface OptionView {
  optionId: string;
  type: string;
  typeLabel: string;
  isExchange: boolean;
  qty: number;
  productName?: string;
  replacementSize?: string;
  color?: string;
  sku?: string;
  locationName?: string;
  refundCents: number;
  feeCents: number;
  currency: string;
  etaDate?: string;
  etaText?: string;
  meetsDeadline: boolean | null;
  conditions: string[];
  evidence: { label: string; value: string }[];
  provenance: Provenance[];
  payloadHash: string;
}

export interface LineFallback {
  productName?: string;
  color?: string;
}

function locationName(raw: Record<string, unknown>): string | undefined {
  const loc = pick(raw, ["location_name", "location"]);
  if (isObj(loc)) return str(pick(loc, ["name", "location_name", "location_id", "id"]));
  return str(loc) ?? str(raw.location_id);
}

function evidenceChips(ev: unknown): { label: string; value: string }[] {
  if (Array.isArray(ev)) return ev.map((x) => ({ label: "Evidence", value: formatScalar(x) }));
  if (!isObj(ev)) return [];
  return Object.entries(ev)
    .filter(([k, v]) => !EVIDENCE_HIDDEN.has(k) && v !== null && v !== undefined && !isObj(v))
    .map(([k, v]) => ({ label: EVIDENCE_LABEL[k] ?? humanize(k), value: formatScalar(v) }));
}

export function normalizeOption(raw: Record<string, unknown>, fallbackCurrency: string, line: LineFallback = {}): OptionView {
  const type = str(raw.type) ?? "unknown";
  return {
    optionId: str(pick(raw, ["option_id", "id"])) ?? "OPT-?",
    type,
    typeLabel: OPTION_TYPE_LABEL[type] ?? humanize(type),
    isExchange: type.startsWith("exchange"),
    qty: num(raw.qty) ?? 1,
    productName: str(pick(raw, ["replacement_product_name", "product_name"])) ?? line.productName,
    replacementSize: str(raw.replacement_size),
    color: str(raw.color) ?? line.color,
    sku: str(raw.sku),
    locationName: locationName(raw),
    refundCents: num(raw.refund_cents) ?? 0,
    feeCents: num(raw.fee_cents) ?? 0,
    currency: str(raw.currency) ?? fallbackCurrency,
    etaDate: str(raw.eta_date),
    etaText: str(raw.eta_text),
    meetsDeadline: bool(raw.meets_deadline) ?? null,
    conditions: strList(raw.conditions),
    evidence: evidenceChips(raw.evidence),
    provenance: provenanceKinds(raw.provenance, raw.source_refs),
    payloadHash: str(raw.payload_hash) ?? "",
  };
}

export interface QuoteView {
  quoteId: string;
  status: string;
  expiresAt: number | null;
  policyVersion?: string;
  currency: string;
  orderId?: string;
  lineId?: string;
  options: OptionView[];
  approvedOptionId?: string;
}

export function normalizeQuote(q: QuoteWire, line: LineFallback = {}): QuoteView {
  const currency = str(q.currency) ?? "CAD";
  const exp = q.expires_at ? Date.parse(q.expires_at) : NaN;
  const approval = isObj(q.approval) ? q.approval : undefined;
  return {
    quoteId: q.quote_id,
    status: str(q.status) ?? "open",
    expiresAt: Number.isNaN(exp) ? null : exp,
    policyVersion: str(q.policy_version),
    currency,
    orderId: str(q.order_id),
    lineId: str(q.line_id),
    options: (Array.isArray(q.options) ? q.options : []).filter(isObj).map((o) => normalizeOption(o, currency, line)),
    approvedOptionId: approval ? str(approval.option_id) : undefined,
  };
}

// ---- Transactions and cases (contracts.md A.11, A.12) -----------------------------------

export interface TransactionView {
  transactionId: string;
  kind: "exchange" | "return";
  state: string;
  nextStep?: string;
  createdAt?: string;
  optionLabel?: string;
  locationName?: string;
  replacementSize?: string;
  etaDate?: string;
  orderId?: string;
}

export function transactionKind(id: string, optionType?: string): "exchange" | "return" {
  if (id.includes("-RET-")) return "return";
  if (id.includes("-RMA-")) return "exchange";
  return optionType?.startsWith("return") ? "return" : "exchange";
}

export function normalizeTransaction(t: TransactionWire): TransactionView {
  const type = str(pick(t, ["option_type", "type"]));
  return {
    transactionId: t.transaction_id,
    kind: transactionKind(t.transaction_id, type),
    state: str(t.state) ?? "completed",
    nextStep: str(t.next_step),
    createdAt: str(t.created_at),
    optionLabel: type ? (OPTION_TYPE_LABEL[type] ?? humanize(type)) : undefined,
    locationName: str(t.location_name),
    replacementSize: str(t.replacement_size),
    etaDate: str(t.eta_date),
    orderId: str(t.order_id),
  };
}

export interface CaseView {
  caseId: string;
  reasonCode: string;
  summary: string;
  status: string;
  orderId?: string;
  createdAt?: string;
}

export function normalizeCase(c: CaseWire): CaseView {
  return {
    caseId: c.case_id,
    reasonCode: str(c.reason_code) ?? "",
    summary: str(c.summary) ?? "",
    status: str(c.status) ?? "open",
    orderId: str(c.order_id),
    createdAt: str(c.created_at),
  };
}

// ---- Purchase proposals (pivot addendum 10.1) ----------------------------------------------

export const PURCHASE_OPTION_LABEL: Record<string, string> = {
  store_pickup: "Pick up in store",
  ship_from_store: "Ship from store",
  ship_from_dc: "Ship from distribution centre",
};

export interface PurchaseView {
  sizeRequestId: string;
  /** open (no proposal yet) | proposed | decided | expired */
  status: string;
  productName?: string;
  color?: string;
  size?: string;
  optionId?: string;
  optionType?: string;
  optionLabel?: string;
  locationName?: string;
  etaDate?: string;
  etaText?: string;
  meetsDeadline: boolean | null;
  priceCents?: number;
  currency: string;
  fitHeadline?: string;
  fitProvenance: Provenance[];
  rationalePublic?: string;
  confidence?: string;
  expiresAt: number | null;
  orderId?: string;
  nextStep?: string;
}

export function normalizePurchase(id: string, w: PurchaseWire): PurchaseView {
  const optionType = str(pick(w, ["option_type", "type", "proposed_option_type"]));
  const exp = str(w.expires_at) ? Date.parse(str(w.expires_at) as string) : NaN;
  const fit = w.fit_signal;
  const location = pick(w, ["location_name", "location"]);
  return {
    sizeRequestId: str(w.size_request_id) ?? id,
    status: str(w.status) ?? "proposed",
    productName: str(w.product_name),
    color: str(w.color),
    size: str(pick(w, ["size", "chosen_size", "proposed_size"])),
    optionId: str(pick(w, ["option_id", "proposed_option_id", "chosen_option_id"])),
    optionType,
    optionLabel: optionType ? (PURCHASE_OPTION_LABEL[optionType] ?? humanize(optionType)) : undefined,
    locationName: isObj(location) ? str(location.name) : str(location),
    etaDate: str(w.eta_date),
    etaText: str(w.eta_text),
    meetsDeadline: bool(w.meets_deadline) ?? null,
    priceCents: num(w.price_cents),
    currency: str(w.currency) ?? "CAD",
    fitHeadline: str(pick(w, ["fit_signal_headline", "fit_headline"])) ?? fitHeadline(fit),
    fitProvenance: provenanceKinds(isObj(fit) ? fit.provenance : undefined, "derived"),
    rationalePublic: str(w.rationale_public),
    confidence: str(w.confidence),
    expiresAt: Number.isNaN(exp) ? null : exp,
    orderId: str(w.order_id),
    nextStep: str(w.next_step),
  };
}

// ---- Personal-agent memory (pivot addendum 5.4, 5.5, 5.8) -------------------------------

export interface MemoryEntry {
  id: string;
  retailer: string;
  item: string;
  size: string;
  outcome: string;
}

export interface MemoryUpdateView {
  id: string;
  product: string;
  color?: string;
  fromSize?: string;
  toSize?: string;
  outcome: string;
  provisional: boolean;
}

export interface MemoryView {
  displayName?: string;
  typicalSize?: string;
  sizeSystem?: string;
  entries: MemoryEntry[];
  updates: MemoryUpdateView[];
}

const OUTCOME_LABEL: Record<string, string> = {
  kept: "kept",
  returned_too_big: "returned, too big",
  returned_too_small: "returned, too small",
  exchange_created: "exchange created",
  exchange_completed_kept: "exchange kept",
};

export const outcomeLabel = (o: string): string => OUTCOME_LABEL[o] ?? humanize(o).toLowerCase();

function listAt(data: unknown, keys: string[]): Record<string, unknown>[] {
  const v = pick(data, keys);
  return Array.isArray(v) ? v.filter(isObj) : [];
}

export function normalizeMemory(data: MemoryWire): MemoryView {
  const owner = isObj(data.owner) ? data.owner : data;
  return {
    displayName: str(pick(owner, ["display_name", "name"])),
    typicalSize: str(pick(owner, ["typical_size"])),
    sizeSystem: str(pick(owner, ["size_system"])),
    entries: listAt(data, ["memory", "entries", "items"]).map((m, i) => ({
      id: str(pick(m, ["memory_id", "id"])) ?? `mem-${i}`,
      retailer: str(m.retailer) ?? "Retailer",
      item: str(m.item) ?? "item",
      size: str(m.size) ?? "?",
      outcome: str(m.outcome) ?? "kept",
    })),
    updates: listAt(data, ["updates", "aldo_updates", "writebacks", "write_backs"]).map((u, i) => ({
      id: str(pick(u, ["update_id", "id"])) ?? `upd-${i}`,
      product: str(pick(u, ["product_name", "style", "item"])) ?? "ALDO item",
      color: str(u.color),
      fromSize: str(pick(u, ["original_size", "from_size"])),
      toSize: str(pick(u, ["new_size", "to_size"])),
      outcome: str(u.outcome) ?? "exchange_created",
      provisional: bool(u.provisional) ?? true,
    })),
  };
}
