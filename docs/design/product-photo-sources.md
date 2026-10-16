# Product photos

Real ALDO product photos used by the demo UI (`src/lib/productImages.ts`). Retrieved 2026-10-15 from aldoshoes.com and ALDO's own CDN, one request per photo, then cropped to the shoe (4:3, 360x270 JPEG) with `scripts/crop_product_photos.py`.

**Rights.** The photos and the style names are ALDO's. They are here for an internal prototype demo only; do not redistribute them or publish them outside the demo without ALDO's permission.

| File | ALDO style | Colourway used | Product page | Image source | Used for |
| --- | --- | --- | --- | --- | --- |
| `toney-black.jpg` | Toney | BLACK (Black) | https://www.aldoshoes.com/en-ca/products/toney-black | https://cdn.shopify.com/s/files/1/0911/8842/9099/files/product_13892077_toney_black_001-001-043_main_sq_gy.jpg | John's boot |
| `toney-brown.jpg` | Toney | BROWN (Brown) | https://www.aldoshoes.com/en-ca/products/toney-brown | https://cdn.shopify.com/s/files/1/0911/8842/9099/files/product_13892074_toney_brown_200-001-043_main_sq_gy.jpg | John's boot |
| `toney-tan.jpg` | Toney | TAN (real colourway Cognac; ALDO has no tan Toney) | https://www.aldoshoes.com/en-ca/products/toney-cognac | https://cdn.shopify.com/s/files/1/0911/8842/9099/files/product_13892076_toney_brown_220-001-043_main_sq_gy.jpg | John's boot |
| `drayson-black.jpg` | Drayson | BLACK (Black) | https://www.aldoshoes.com/products/drayson-black | http://www.aldoshoes.com/cdn/shop/files/product_13814445_drayson_black_001-001-043_main_sq_gy.jpg | David L1 (was Lededanten, derby) |
| `brando-tan.jpg` | Brando | TAN (Cognac) | https://www.aldoshoes.com/products/brando-cognac-14161941 | http://www.aldoshoes.com/cdn/shop/files/product_14161941_brando_brown_238-001-046_main_sq_gy.jpg | David L2 (was Aahliyah, boot) |
| `meekes-brown.jpg` | Meekes | BROWN (Dark Brown) | https://www.aldoshoes.com/products/meekes-dark-brown | http://www.aldoshoes.com/cdn/shop/files/product_13813935_meekes_brown_201-001-043_main_sq_gy.jpg | David L3 (was Joselie, oxford) |
| `sosa-black.jpg` | Sosa | BLACK (Black) | https://www.aldoshoes.com/products/sosa-black | http://www.aldoshoes.com/cdn/shop/files/product_14051656_sosa_black_001-001-043_main_sq_gy.jpg | David L4 (was Tanerdee, chelsea boot) |
| `oscar-white.jpg` | Oscar | WHITE (White) | https://www.aldoshoes.com/products/oscar-white | http://www.aldoshoes.com/cdn/shop/files/product_13710852_oscar_white_100-002-043_main_sq_gy.jpg | David L5 (was Alanie, sneaker) |
| `stark-brown.jpg` | Stark | BROWN (Brown) | https://www.aldoshoes.com/products/stark-brown | http://www.aldoshoes.com/cdn/shop/files/product_14401006_stark_brown_200-001-046_main_sq_gy.jpg | David L6 (was Mazey, boat shoe) |
| `montcourt-black.jpg` | Montcourt | BLACK (Black) | https://www.aldoshoes.com/products/montcourt-black | http://www.aldoshoes.com/cdn/shop/files/product_13963664_montcourt_black_001-001-034_main_sq_gy.jpg | David L7 (was Caraelia, monk strap) |
| `patten-navy.jpg` | Patten | NAVY (Blue (Navy)) | https://www.aldoshoes.com/products/patten-navy | http://www.aldoshoes.com/cdn/shop/files/product_14488258_patten_blue_410-001-046_main_sq_gy.jpg | David L8 (was Valegyn, loafer) |
| `drymos-brown.jpg` | Drymos | BROWN (Brown) | https://www.aldoshoes.com/products/drymos-brown | http://www.aldoshoes.com/cdn/shop/files/product_14493410_drymos_brown_227-002-029_main_sq_gy.jpg | David L9 (was Krishelle, brogue) |
| `finespec-grey.jpg` | Finespec | GREY (Light grey) | https://www.aldoshoes.com/products/finespec-light-grey-14014415 | http://www.aldoshoes.com/cdn/shop/files/product_14014415_finespec_grey_050-002-017_main_sq_gy.jpg | David L10 (was Oneameth, sneaker) |

Notes:
- `ALDO Men 001` in the old fixtures is a placeholder for the real men's style **Toney** (lace-up ankle boot, smooth leather); the demo price (CAD 149.99) is simulated, ALDO's list price is CAD 190.
- ALDO's men's catalogue has no boat-shoe, monk-strap or brogue category, so David's lines L6, L7 and L9 use the closest real styles and the generic words `slip-on`, `dress shoe` and `casual lace-up`.
- Photo lookup is by `<style>-<colour>.jpg` (lower case); a product without a photo keeps its colour swatch.
