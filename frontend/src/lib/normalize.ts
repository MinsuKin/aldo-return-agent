import type { CaseWire, MemoryWire, Persona, Provenance, PurchaseWire, QuoteWire, TransactionWire } from "../api/types";
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

/** Badge wording. "synthetic" is only a type label: the UI shows badges for the other kinds, where they add credibility. */
export const PROVENANCE_LABEL: Record<Provenance, string> = {
  synthetic: "synthetic",
  derived: "derived from provided ALDO data",
  provided_csv: "provided ALDO data (CSV)",
  provided_doc: "provided ALDO document",
};

/** Provenance kinds worth a badge in the UI (synthetic is covered by the prototype strip). */
export const credibleProvenance = (kinds: Provenance[]): Provenance[] => kinds.filter((k) => k !== "synthetic");

// ---- Personas (addendum 11.5: tier_label and location_label) --------------------------------

export interface PersonaView {
  customerId: string;
  displayName: string;
  description?: string;
  homeRegion?: string;
  tier: string;
  tierLabel?: string;
  locationLabel?: string;
  /** John and David (tiered demo personas). The regression personas Ava, Ben and Chloe are admin-only. */
  isDemo: boolean;
}

export function normalizePersona(p: Persona): PersonaView {
  const tier = (str(p.tier) ?? "none").toLowerCase();
  const tiered = tier !== "none" && tier !== "";
  return {
    customerId: p.customer_id,
    displayName: p.display_name,
    description: str(p.description),
    homeRegion: str(p.home_region),
    tier,
    tierLabel: str(p.tier_label) ?? (tiered ? `${humanize(tier)} member` : undefined),
    locationLabel: str(p.location_label),
    isDemo: tiered || /(JOHN|DAVID)$/i.test(p.customer_id),
  };
}

// ---- Quote options (contracts.md A.9, A.10; addendum 11.4) ------------------------------------

export const OPTION_TYPE_LABEL: Record<string, string> = {
  exchange_ship_from_dc: "Exchange by mail",
  exchange_ship_from_store: "Exchange by mail from a store",
  exchange_store_pickup: "Exchange in store",
  return_in_store: "Return in store",
  return_mail: "Return by mail",
};

const EVIDENCE_LABEL: Record<string, string> = {
  atp: "Stock at location",
  rate_id: "Rate",
};
const EVIDENCE_HIDDEN = new Set(["inventory_version"]);

export interface OptionLine {
  lineId: string;
  productName: string;
  /** What kind of shoe it is ("boot", "sneaker"), when the server says. */
  kind?: string;
  color?: string;
  size?: string;
  qty: number;
  refundCents?: number;
}

export interface BenefitView {
  label: string;
  /** For example "Platinum perk 1 of 1 this month". */
  note: string;
  savedCents?: number;
}

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
  /** Street address of the store, when the server knows it. */
  locationAddress?: string;
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
  /** Colour exchange: the pair the customer has and the pair they get (same style and size). */
  fromColor?: string;
  toColor?: string;
  fromSize?: string;
  isColorSwap: boolean;
  /** Travel time to the store, when the server knows it (addendum 11.1 D6). */
  minutesAway?: number;
  /** Items of a bundle option (one shipment for several lines of one order, addendum 11.1 D9). */
  lines: OptionLine[];
  benefit?: BenefitView;
  /** One-line ranking reason for the option, when the server provides one. */
  rankingNote?: string;
}

export interface LineFallback {
  productName?: string;
  color?: string;
  size?: string;
  /** Look up an order line by id (bundle options may list only line ids). */
  lookup?: (lineId: string) => { productName?: string; color?: string; size?: string } | undefined;
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

/** DEMO-SKU-MEN001-BROWN-095 -> BROWN. */
export function colorFromSku(sku: string | undefined): string | undefined {
  return sku ? /-([A-Z]+)-\d{3}$/.exec(sku)?.[1] : undefined;
}

function normalizeLines(raw: unknown, line: LineFallback): OptionLine[] {
  if (!Array.isArray(raw)) return [];
  const out: OptionLine[] = [];
  for (const item of raw) {
    if (typeof item === "string") {
      const found = line.lookup?.(item);
      out.push({ lineId: item, productName: found?.productName ?? item, color: found?.color, size: found?.size, qty: 1 });
    } else if (isObj(item)) {
      const lineId = str(pick(item, ["line_id", "id"])) ?? `line-${out.length + 1}`;
      const found = line.lookup?.(lineId);
      out.push({
        lineId,
        productName: str(pick(item, ["product_name", "name"])) ?? found?.productName ?? lineId,
        kind: str(pick(item, ["category", "kind", "product_type"])),
        color: str(item.color) ?? found?.color,
        size: str(item.size) ?? found?.size,
        qty: num(item.qty) ?? 1,
        refundCents: num(pick(item, ["refund_cents", "unit_paid_cents"])),
      });
    }
  }
  return out;
}

function normalizeBenefit(raw: unknown, fallbackTier?: string): BenefitView | undefined {
  if (!isObj(raw)) return undefined;
  const label = str(pick(raw, ["label", "title", "name"])) ?? "Free return shipping";
  const used = num(raw.used);
  // The option note of the real API (benefits.benefit_note) says `allowed`; the A2A benefit_applied event says `per_month`.
  const perMonth = num(pick(raw, ["per_month", "allowed"]));
  // That note has no tier field either: take it from the option's evidence, else from the label ("Platinum free return shipping").
  const tier = str(pick(raw, ["tier_label", "tier"])) ?? fallbackTier ?? label.match(/^(\w+) free return shipping$/i)?.[1];
  const prefix = tier ? humanize(tier.replace(/ member$/i, "")) : "Member";
  const note =
    str(pick(raw, ["note", "text", "description"])) ??
    (used !== undefined && perMonth !== undefined ? `${prefix} perk ${used} of ${perMonth} this month` : label);
  return { label, note, savedCents: num(pick(raw, ["saved_cents", "saved"])) };
}

export function normalizeOption(raw: Record<string, unknown>, fallbackCurrency: string, line: LineFallback = {}): OptionView {
  const type = str(raw.type) ?? "unknown";
  const isExchange = type.startsWith("exchange");
  const lines = normalizeLines(raw.lines ?? raw.line_ids, line);
  const toColor = isExchange ? (str(pick(raw, ["replacement_color", "color"])) ?? colorFromSku(str(raw.sku)) ?? line.color) : undefined;
  const fromColor = isExchange ? (str(raw.original_color) ?? line.color) : undefined;
  return {
    optionId: str(pick(raw, ["option_id", "id"])) ?? "OPT-?",
    type,
    typeLabel: OPTION_TYPE_LABEL[type] ?? humanize(type),
    isExchange,
    qty: num(raw.qty) ?? 1,
    productName: str(pick(raw, ["replacement_product_name", "product_name"])) ?? line.productName,
    replacementSize: str(raw.replacement_size),
    color: str(pick(raw, ["replacement_color", "color"])) ?? line.color,
    sku: str(raw.sku),
    locationName: locationName(raw),
    locationAddress: str(raw.location_address),
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
    fromColor,
    toColor,
    fromSize: str(raw.original_size) ?? line.size,
    isColorSwap: isExchange && !!fromColor && !!toColor && fromColor.toLowerCase() !== toColor.toLowerCase(),
    minutesAway: num(pick(raw, ["minutes_away", "travel_minutes", "eta_minutes"])),
    lines,
    benefit: normalizeBenefit(raw.benefit, isObj(raw.evidence) ? str(raw.evidence.tier) : undefined),
    rankingNote: str(pick(raw, ["ranking_note", "rank_reason", "ranking_reason"])),
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
  /** Conversation the quote belongs to; needed to ask for fresh options after expiry. */
  convId?: string;
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
    convId: str(q.conv_id),
    options: (Array.isArray(q.options) ? q.options : []).filter(isObj).map((o) => normalizeOption(o, currency, line)),
    approvedOptionId: approval ? str(approval.option_id) : undefined,
  };
}

// ---- Transactions and cases (contracts.md A.11, A.12; addendum 11.4) ----------------------------

export interface StoreNoticeView {
  storeName?: string;
  etaMinutes?: number;
  summary?: string;
}

export interface TransactionView {
  transactionId: string;
  kind: "exchange" | "return";
  state: string;
  nextStep?: string;
  createdAt?: string;
  optionType?: string;
  optionLabel?: string;
  locationName?: string;
  replacementSize?: string;
  replacementColor?: string;
  etaDate?: string;
  orderId?: string;
  refundCents?: number;
  feeCents?: number;
  currency: string;
  lines: OptionLine[];
  benefit?: BenefitView;
  /** The store notice created in the same transaction (addendum 11.1 D7). */
  storeNotice?: StoreNoticeView;
}

export function transactionKind(id: string, optionType?: string): "exchange" | "return" {
  if (id.includes("-RET-")) return "return";
  if (id.includes("-RMA-")) return "exchange";
  return optionType?.startsWith("return") ? "return" : "exchange";
}

function normalizeStoreNotice(raw: unknown): StoreNoticeView | undefined {
  if (!isObj(raw)) return undefined;
  const notice = {
    storeName: str(pick(raw, ["store_name", "store", "location_name"])),
    etaMinutes: num(pick(raw, ["eta_minutes", "minutes_away"])),
    summary: str(pick(raw, ["summary", "text", "message"])),
  };
  return notice.storeName || notice.summary ? notice : undefined;
}

export function normalizeTransaction(t: TransactionWire, line: LineFallback = {}): TransactionView {
  const type = str(pick(t, ["option_type", "type"]));
  const currency = str(t.currency) ?? "CAD";
  return {
    transactionId: t.transaction_id,
    kind: transactionKind(t.transaction_id, type),
    state: str(t.state) ?? "completed",
    nextStep: str(t.next_step),
    createdAt: str(t.created_at),
    optionType: type,
    optionLabel: type ? (OPTION_TYPE_LABEL[type] ?? humanize(type)) : undefined,
    locationName: str(t.location_name),
    replacementSize: str(t.replacement_size),
    replacementColor: str(pick(t, ["replacement_color", "color"])) ?? colorFromSku(str(t.sku)),
    etaDate: str(t.eta_date),
    orderId: str(t.order_id),
    refundCents: num(t.refund_cents),
    feeCents: num(t.fee_cents),
    currency,
    lines: normalizeLines(t.lines, line),
    benefit: normalizeBenefit(t.benefit),
    storeNotice: normalizeStoreNotice(t.store_notice),
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
  ship_from_store: "Delivery from a store",
  ship_from_dc: "Delivery from our distribution centre",
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

// ---- Personal-agent memory (pivot addendum 5.4, 5.5, 5.8; 11.1 D11) -------------------------------

export interface MemoryEntry {
  id: string;
  retailer: string;
  item: string;
  size: string;
  outcome: string;
  /** Optional colour of the item (addendum 12.3): lets the personal agent offer colours the owner usually keeps. */
  color?: string;
}

export interface MemoryUpdateView {
  id: string;
  product: string;
  color?: string;
  fromSize?: string;
  toSize?: string;
  outcome: string;
  provisional: boolean;
  /** "size_ok": the owner kept the same size when exchanging colour (addendum 11.1 D11). */
  fitFeedback?: string;
  reason?: string;
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

/** Retailer labels come as "Retailer A (synthetic)"; the prototype strip already says the data is simulated. */
export const cleanLabel = (s: string): string => s.replace(/\s*\((synthetic|simulated)\)\s*$/i, "");

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
      color: str(m.color),
    })),
    updates: listAt(data, ["updates", "aldo_updates", "writebacks", "write_backs"]).map((u, i) => ({
      id: str(pick(u, ["update_id", "id"])) ?? `upd-${i}`,
      product: str(pick(u, ["product_name", "style", "item"])) ?? "ALDO item",
      color: str(u.color),
      fromSize: str(pick(u, ["original_size", "from_size"])),
      toSize: str(pick(u, ["new_size", "to_size"])),
      outcome: str(u.outcome) ?? "exchange_created",
      provisional: bool(u.provisional) ?? true,
      fitFeedback: str(u.fit_feedback),
      reason: str(u.reason),
    })),
  };
}
