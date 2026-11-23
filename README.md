# VitroBench

VitroBench is a local glazing conservation workbench. It retains original photographs, registers alternate light views on a measured plane, and records which parts of the surface were visible when a reading was taken.

## Start

Run `docker compose up --build --wait` and open `http://127.0.0.1:19991`. Set `VITROBENCH_PORT` to choose a different local port. The named Compose volume stores original photographs and measurements. For a local Python 3.11 installation with the pinned dependencies, use `./start.sh`.

## Inspect a panel

1. Load the synthetic panels or add a photograph in **Captures**. Enter the surveyed dimensions, mark the four plane corners, and register alternate photographs to those points. In **Sampling regions**, mark the glass field, a clear-glass control and any parts of the photograph to exclude.
2. When a panel bows near the leading and one corner registration cannot follow the painted details, open **Compare registered views → Mark pair alignment**. Mark features visible in both views, optionally split at a middle divider, and set a pairing error limit. Both schemes are checked against that limit: one transform keeps the whole view seamless and stays closest to the outer frame, while the two-sided scheme fits local paint tighter but leaves an unconfirmed middle band empty. Only the selected scheme is kept, and re-registering a capture afterwards does not replace it.
3. Open **Measure**. **Glass pane census** uses dark leading to separate light glass cells and flags cells at the edge of valid support. In **Came width survey**, mark a straight segment where both edges of the leading can be seen. **Relative transmission** compares the photographed glass field with a clear-glass region from the same exposure. **Line scan** also reports the paired capture through a saved pair alignment, marking the stations where no confirmed comparison exists; the measured line length is unchanged. Open **Measurement source** on a saved result to check the photograph and region it used.
4. **Enamel coverage** compares clear and painted control patches, **Pale deposit candidates** measures visible deposits against a reference, and **Fine-line network** identifies dark linework. **Polarized glare** and **Raking light comparison** use paired exposures. **Geometry** retains measured traces and areas. **Panel studies** compares saved measurements. **Evidence** builds an archive from saved results. Open it in **Received evidence** to verify the included digests.

Images may be single-frame PNG, JPEG, WebP or TIFF, up to 24 MiB and 12 megapixels. One panel accepts up to 10 captures and 24 marked regions. Planes are capped at 1,800 pixels on the long side. Widths are measured in the registered plane. Dark paint can resemble leading in the pane census so check the mapped regions on the original. Transmission uses relative intensity in one recorded light view. The demonstration images are synthetic.

VitroBench reuses the local image workbench libraries for capture, registration, photographs and audit evidence. Its pane, came and relative transmission tools and demonstration panels are part of the application.
