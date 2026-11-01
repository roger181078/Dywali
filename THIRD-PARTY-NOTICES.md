# Third-party notices

This file records known third-party resources and the limits of the provenance checks performed for this public package. It does not assign one license to all images fetched by Dywali.

## Leafy sample wallpapers

The Rosé Pine Wallpapers README credits **Leafy**, **Leafy (Dawn)**, and **Leafy (Moon)** to [`fvrests`](https://github.com/fvrests) in its Illustration section. The repository's root [`LICENSE`](https://github.com/rose-pine/wallpapers/blob/ee39a37b24ab7700cc4d52f50b01a5d13df6a865/LICENSE) is CC0 1.0. The pinned upstream revision is [`ee39a37b24ab7700cc4d52f50b01a5d13df6a865`](https://github.com/rose-pine/wallpapers/commit/ee39a37b24ab7700cc4d52f50b01a5d13df6a865); its [`README.md`](https://github.com/rose-pine/wallpapers/blob/ee39a37b24ab7700cc4d52f50b01a5d13df6a865/README.md) identifies the two variants below. The repository describes separate CC BY-SA terms for a group of photographs; those do not apply to these Illustration entries.

The bundled `assets/Leafy-Day.jpg` visually corresponds to upstream [`illustration/leafy-dawn.png`](https://github.com/rose-pine/wallpapers/blob/ee39a37b24ab7700cc4d52f50b01a5d13df6a865/illustration/leafy-dawn.png); `assets/Leafy-Night.jpg` visually corresponds to [`illustration/leafy-moon.png`](https://github.com/rose-pine/wallpapers/blob/ee39a37b24ab7700cc4d52f50b01a5d13df6a865/illustration/leafy-moon.png). In each pair, the upstream PNG and bundled JPEG have matching 5120 × 3202 dimensions and strongly matching decoded visual content. The formats and SHA-256 hashes differ, and pixel values are not byte-identical, consistent with lossy JPEG encoding. No reproducible record of the exact JPEG conversion process is available; the comparison supports the variant correspondence but does not establish the precise conversion history.

CC0 1.0 reference: <https://creativecommons.org/publicdomain/zero/1.0/>. This attribution and status apply only to the identified Leafy source artwork and its corresponding bundled variants, not to Dywali code or other assets.

## Dywali application icon

`icons/dywali.svg` was generated with AI assistance during Dywali development, as declared in the SVG. The project does not claim exclusive copyright or trademark rights in the generated result. The SVG carries a project notice that it is distributed under GPL-3.0-only; that notice does not establish exclusive ownership of AI-generated elements. No third-party source artwork or separate upstream license has been identified for this SVG in the local materials inspected.

The preview image `icons/dynamic-wallpaper-studio-preview.png` is intentionally not included in the `public-1.0` package.

## NASA APOD and Wikimedia Commons images

NASA APOD and Wikimedia Commons provide images and metadata at runtime. Their photographs can have different authors, credits, and reuse conditions, including third-party rights. Dywali does not relicense those downloaded photographs under GPL or assert a universal license for them. Review the individual provider or Commons file page and its terms before redistributing an image. NASA APOD may identify a copyright holder or credit in its returned metadata; that information is per item.

## Runtime dependencies

GTK 3, PyGObject, Pillow, XFCE/xfconf, XRandR, and systemd are system dependencies rather than vendored libraries in this package. Their respective copyright and licensing terms remain applicable.
