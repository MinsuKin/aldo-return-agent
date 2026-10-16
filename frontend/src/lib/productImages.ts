/**
 * Real ALDO product photos shipped with the app (public/products/, sources and rights note in docs/design/product-photo-sources.md).
 * File name: <style>-<colour>.jpg, both lower case ("toney-black.jpg"). A product without a photo keeps its colour swatch.
 */
const PHOTOS = new Set<string>([
  "brando-tan",
  "drayson-black",
  "drymos-brown",
  "finespec-grey",
  "meekes-brown",
  "montcourt-black",
  "oscar-white",
  "patten-navy",
  "sosa-black",
  "stark-brown",
  "toney-black",
  "toney-brown",
  "toney-tan",
]);

const slug = (s: string): string =>
  s
    .toLowerCase()
    .replace(/^aldo\s+/, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");

/** The photo URL for a product name ("ALDO Toney", "Toney") and colour name ("BLACK"), if one is shipped. */
export function productPhoto(name: string | undefined, color: string | undefined): string | undefined {
  if (!name || !color) return undefined;
  const key = `${slug(name)}-${color.toLowerCase().replace(/[^a-z]/g, "")}`;
  return PHOTOS.has(key) ? `/products/${key}.jpg` : undefined;
}
