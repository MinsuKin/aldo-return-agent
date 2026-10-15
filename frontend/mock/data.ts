// Mock API data. Mirrors contracts.md section (d) fixture values (binding order table, locations,
// stock numbers, ETAs) so the UI can be verified without the backend. Everything is synthetic.

export const DEMO_TODAY = "2026-10-16"; // Friday, DEMO_NOW date in America/Toronto
export const POLICY_VERSION = "2026.10-demo";

export type Variant = "default" | "dc_stockout" | "all_stockout";
export const VARIANTS: Variant[] = ["default", "dc_stockout", "all_stockout"];

export interface MockPersona {
  customer_id: string;
  display_name: string;
  home_region: string;
  description: string;
}

export const PERSONAS: MockPersona[] = [
  {
    customer_id: "DEMO-CUST-AVA",
    display_name: "Ava",
    home_region: "QC",
    description: "Size exchange (hero order DEMO-1001), final-sale items, quantity boundary",
  },
  {
    customer_id: "DEMO-CUST-BEN",
    display_name: "Ben",
    home_region: "ON",
    description: "Return window boundaries (day 30 and 31), refund authority limit, mail-in return",
  },
  {
    customer_id: "DEMO-CUST-CHLOE",
    display_name: "Chloe",
    home_region: "QC",
    description: "Target of cross-customer access attempts, missing delivery date, too-big contrast order",
  },
];

export interface MockLine {
  line_id: string;
  sku: string;
  product_name: string;
  style: string;
  size: string;
  color: string;
  purchased_qty: number;
  returned_or_reserved_qty: number;
  unit_paid_cents: number;
  final_sale: boolean | null;
}

export interface MockOrder {
  order_id: string;
  customer_id: string;
  banner: string;
  currency: string;
  channel: string;
  ordered_at: string;
  delivered_at: string | null;
  source: string;
  lines: MockLine[];
}

interface OrderSeed {
  id: string;
  cust: string;
  product: string;
  token: string;
  color: string;
  size: string;
  qty: number;
  returned: number;
  cents: number;
  finalSale: boolean;
  ordered: string;
  delivered: string | null;
}

const SEEDS: OrderSeed[] = [
  { id: "DEMO-1001", cust: "AVA", product: "Stessy2.0", token: "HERO", color: "BLACK", size: "7.0", qty: 1, returned: 0, cents: 12999, finalSale: false, ordered: "2026-09-30", delivered: "2026-10-04" },
  { id: "DEMO-1002", cust: "AVA", product: "Krios", token: "FINALSALE", color: "TAN", size: "8.0", qty: 1, returned: 0, cents: 5999, finalSale: true, ordered: "2026-10-02", delivered: "2026-10-06" },
  { id: "DEMO-1003", cust: "BEN", product: "Harriot", token: "STD1", color: "BLACK", size: "10.0", qty: 1, returned: 0, cents: 8999, finalSale: false, ordered: "2026-09-10", delivered: "2026-09-16" },
  { id: "DEMO-1004", cust: "BEN", product: "Cherflex", token: "STD2", color: "BROWN", size: "10.0", qty: 1, returned: 0, cents: 8999, finalSale: false, ordered: "2026-09-09", delivered: "2026-09-15" },
  { id: "DEMO-1005", cust: "BEN", product: "Goer", token: "BOOT", color: "BLACK", size: "10.0", qty: 1, returned: 0, cents: 28900, finalSale: false, ordered: "2026-10-05", delivered: "2026-10-09" },
  { id: "DEMO-1006", cust: "CHLOE", product: "Lagoon", token: "STD3", color: "WHITE", size: "7.0", qty: 1, returned: 0, cents: 7999, finalSale: false, ordered: "2026-10-01", delivered: "2026-10-05" },
  { id: "DEMO-1007", cust: "CHLOE", product: "Verity", token: "STD4", color: "NAVY", size: "7.5", qty: 1, returned: 0, cents: 6999, finalSale: false, ordered: "2026-10-12", delivered: null },
  { id: "DEMO-1008", cust: "AVA", product: "Dorenna", token: "STD5", color: "BLACK", size: "8.0", qty: 2, returned: 1, cents: 5999, finalSale: false, ordered: "2026-10-03", delivered: "2026-10-07" },
  { id: "DEMO-1009", cust: "CHLOE", product: "Clementinne", token: "CONTRAST", color: "BLACK", size: "8.0", qty: 1, returned: 0, cents: 11999, finalSale: false, ordered: "2026-10-04", delivered: "2026-10-08" },
  { id: "DEMO-1010", cust: "BEN", product: "Jellyicious", token: "STD6", color: "GREY", size: "10.0", qty: 1, returned: 0, cents: 7499, finalSale: false, ordered: "2026-10-06", delivered: "2026-10-10" },
];

export const size10 = (size: string | number): string => String(Math.round(Number(size) * 10)).padStart(3, "0");
export const sizeStr = (n: number): string => n.toFixed(1);
export const skuFor = (token: string, color: string, size: string | number): string => `DEMO-SKU-${token}-${color}-${size10(size)}`;

export function baseOrders(): MockOrder[] {
  return SEEDS.map((s) => ({
    order_id: s.id,
    customer_id: `DEMO-CUST-${s.cust}`,
    banner: "ALDO_CA",
    currency: "CAD",
    channel: "web",
    ordered_at: s.ordered,
    delivered_at: s.delivered,
    source: "synthetic",
    lines: [
      {
        line_id: `${s.id}-L1`,
        sku: skuFor(s.token, s.color, s.size),
        product_name: s.product,
        style: `DEMO-STYLE-${s.token}`,
        size: s.size,
        color: s.color,
        purchased_qty: s.qty,
        returned_or_reserved_qty: s.returned,
        unit_paid_cents: s.cents,
        final_sale: s.finalSale,
      },
    ],
  }));
}

export interface MockLocation {
  location_id: string;
  type: "DC" | "STORE";
  name: string;
  city: string;
  region: string;
  pickup_ready_days: number | null;
  ship_days: number;
}

export const LOCATIONS: MockLocation[] = [
  { location_id: "DEMO-DC-QC", type: "DC", name: "ALDO Demo Distribution Centre", city: "Montreal", region: "QC", pickup_ready_days: null, ship_days: 4 },
  { location_id: "DEMO-STORE-LAVAL-01", type: "STORE", name: "ALDO Demo Store Laval", city: "Laval", region: "QC", pickup_ready_days: 1, ship_days: 3 },
  { location_id: "DEMO-STORE-MTL-01", type: "STORE", name: "ALDO Demo Store Montreal", city: "Montreal", region: "QC", pickup_ready_days: 1, ship_days: 3 },
  { location_id: "DEMO-STORE-TOR-01", type: "STORE", name: "ALDO Demo Store Toronto", city: "Toronto", region: "ON", pickup_ready_days: 1, ship_days: 3 },
];
export const locationById = (id: string): MockLocation => LOCATIONS.find((l) => l.location_id === id) ?? LOCATIONS[0];

export const HERO = { product: "Stessy2.0", token: "HERO", style: "DEMO-STYLE-HERO", color: "BLACK", priceCents: 12999 };
export const HERO_SIZES = [6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0];
export const CONTRAST = { product: "Clementinne", token: "CONTRAST", style: "DEMO-STYLE-CONTRAST", color: "BLACK", priceCents: 11999 };
export const CONTRAST_SIZES = [7.0, 7.5, 8.0, 8.5];

/** ALDO_CA fit numbers (fixtures/derived/fit_insights.json via the data stream's README). */
export const FIT: Record<string, { signal: string; too_small: number; too_big: number }> = {
  "Stessy2.0": { signal: "runs_small", too_small: 465, too_big: 117 },
  Clementinne: { signal: "runs_large", too_small: 63, too_big: 354 },
  Krios: { signal: "mixed", too_small: 41, too_big: 30 },
  Harriot: { signal: "runs_small", too_small: 92, too_big: 40 },
  Cherflex: { signal: "mixed", too_small: 88, too_big: 76 },
  Goer: { signal: "runs_small", too_small: 164, too_big: 69 },
  Lagoon: { signal: "runs_small", too_small: 123, too_big: 23 },
  Verity: { signal: "mixed", too_small: 131, too_big: 91 },
  Dorenna: { signal: "runs_large", too_small: 139, too_big: 312 },
  Jellyicious: { signal: "insufficient_data", too_small: 7, too_big: 19 },
};

/** ATP per `${sku}|${location_id}`. Variants follow contracts.md D.5 exactly. */
export function buildInventory(variant: Variant): Map<string, number> {
  const inv = new Map<string, number>();
  const set = (sku: string, loc: string, atp: number) => inv.set(`${sku}|${loc}`, atp);
  const base = (style: { token: string; color: string }, size: number, dc: number, mtl: number, laval: number, tor: number) => {
    const sku = skuFor(style.token, style.color, size);
    set(sku, "DEMO-DC-QC", dc);
    set(sku, "DEMO-STORE-MTL-01", mtl);
    set(sku, "DEMO-STORE-LAVAL-01", laval);
    set(sku, "DEMO-STORE-TOR-01", tor);
  };
  for (const s of HERO_SIZES) {
    if (s === 7.5 || s === 8.0) {
      if (variant === "default") base(HERO, s, 5, 1, 0, 1);
      else if (variant === "dc_stockout") base(HERO, s, 0, 2, 0, 0);
      else base(HERO, s, 0, 0, 0, 0);
    } else {
      base(HERO, s, 3, 1, 1, 0);
    }
  }
  for (const s of CONTRAST_SIZES) {
    if (s === 7.5) base(CONTRAST, s, 3, 1, 0, 0);
    else base(CONTRAST, s, 2, 0, 0, 0);
  }
  return inv;
}

export interface MemorySeedEntry {
  memory_id: string;
  retailer: string;
  item: string;
  size: string;
  outcome: string;
  source: string;
}

export interface MemorySeed {
  owner_id: string;
  display_name: string;
  size_system: string;
  typical_size: string;
  memory: MemorySeedEntry[];
}

const m = (id: string, retailer: string, item: string, size: string, outcome: string): MemorySeedEntry => ({
  memory_id: id,
  retailer,
  item,
  size,
  outcome,
  source: "synthetic",
});

/** fixtures/personal_agent/memory.json shape from the pivot addendum 5.8. */
export const MEMORY_SEEDS: MemorySeed[] = [
  {
    owner_id: "DEMO-CUST-AVA",
    display_name: "Ava",
    size_system: "US_W",
    typical_size: "7.5",
    memory: [
      m("PM-1", "Retailer A (synthetic)", "running shoe", "7.5", "kept"),
      m("PM-2", "Retailer B (synthetic)", "loafer", "7.5", "kept"),
      m("PM-3", "Retailer C (synthetic)", "sneaker", "8.0", "returned_too_big"),
    ],
  },
  {
    owner_id: "DEMO-CUST-BEN",
    display_name: "Ben",
    size_system: "US_M",
    typical_size: "10.0",
    memory: [m("PM-4", "Retailer A (synthetic)", "oxford", "10.0", "kept")],
  },
  {
    owner_id: "DEMO-CUST-CHLOE",
    display_name: "Chloe",
    size_system: "US_W",
    typical_size: "7.5",
    memory: [m("PM-5", "Retailer B (synthetic)", "boot", "7.5", "kept")],
  },
];

// ---- Date helpers (calendar dates only, UTC arithmetic so there are no timezone shifts) ----

export function addDays(date: string, days: number): string {
  const d = new Date(`${date}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T12:00:00Z`) - Date.parse(`${from}T12:00:00Z`)) / 86_400_000);
}

export function weekdayText(date: string): string {
  return new Intl.DateTimeFormat("en-CA", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" })
    .format(new Date(`${date}T12:00:00Z`))
    .replace(",", "");
}
