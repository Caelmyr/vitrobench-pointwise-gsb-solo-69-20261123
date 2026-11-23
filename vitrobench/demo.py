"""Synthetic transmission and raking-light images of workshop glazing panels."""
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .geometry import DEFAULT_CORNERS, canvas_dimensions, homography
from .photos import linear_to_srgb, png_bytes, srgb_to_linear, store_capture
from .store import make_id


DEMO_KEY = "vitrobench.training.v1"
PLANE_SIZE = (688, 514)
PHOTO_SIZE = (832, 646)
DEMO_ROLES = ("diffuse", "parallel", "cross", "rake-left", "rake-right")
POSES = {
    "diffuse": [[0.085, 0.091], [0.912, 0.096], [0.908, 0.889], [0.089, 0.891]],
    "parallel": [[0.112, 0.109], [0.910, 0.075], [0.924, 0.872], [0.107, 0.917]],
    "cross": [[0.079, 0.103], [0.908, 0.118], [0.880, 0.911], [0.083, 0.889]],
    "rake-left": [[0.087, 0.097], [0.895, 0.085], [0.907, 0.902], [0.065, 0.906]],
    "rake-right": [[0.101, 0.095], [0.912, 0.091], [0.926, 0.908], [0.116, 0.894]],
}


@dataclass(frozen=True)
class DemoPanel:
    seed: int
    name: str
    glass: str
    finish: str
    position: str
    tray: str
    palette: tuple


SERIES = (
    DemoPanel(671, "Amber transom · P12", "Antique glass", "Fired enamel", "East transom", "Tray A",
              ((220, 163, 82), (78, 160, 169), (217, 128, 68), (105, 166, 127), (193, 99, 72))),
    DemoPanel(786, "Blue lancet · P26", "Rolled glass", "Grisaille", "North bay", "Tray B",
              ((110, 150, 200), (90, 114, 159), (205, 162, 108), (82, 163, 173), (178, 109, 132))),
    DemoPanel(455, "Green border · P31", "Antique glass", "Silver stain", "West border", "Tray C",
              ((111, 171, 133), (213, 160, 78), (126, 174, 96), (182, 123, 94), (106, 166, 172))),
    DemoPanel(902, "Rose medallion · P08", "Cylinder glass", "Copper enamel", "South aisle", "Tray A",
              ((220, 148, 147), (191, 111, 121), (221, 155, 105), (84, 150, 159), (203, 121, 104))),
)


def smoothed_noise(rng, shape, scale):
    small = rng.normal(size=(int(shape[0] / scale) + 4, int(shape[1] / scale) + 4))
    y, x = np.indices(shape, dtype=np.float32)
    return ndimage.map_coordinates(small, [y / scale + 1, x / scale + 1], order=3).astype(np.float32)


def panel_grid(rng, width, height):
    grid = []
    for row, y in enumerate((.11, .28, .454, .645, .823)):
        vertices = []
        for column, x in enumerate((.11, .268, .433, .608, .752, .889)):
            u = x if column in (0, 5) else x + rng.uniform(-.036, .036)
            v = y if row in (0, 2, 4) else y + rng.uniform(-.036, .036)
            vertices.append((round(u * width), round(v * height)))
        grid.append(vertices)
    return grid


def draw_panel(panel):
    width, height = PLANE_SIZE
    rng = np.random.default_rng(panel.seed)
    size = (height, width)
    glass = Image.new("RGB", PLANE_SIZE, (91, 104, 105))
    dark = Image.new("L", PLANE_SIZE)
    enamel = Image.new("L", PLANE_SIZE)
    picture = ImageDraw.Draw(glass)
    lead = ImageDraw.Draw(dark)
    paint = ImageDraw.Draw(enamel)
    grid = panel_grid(rng, width, height)
    for row in range(len(grid) - 1):
        for col in range(len(grid[row]) - 1):
            shape = [grid[row][col], grid[row][col + 1], grid[row + 1][col + 1], grid[row + 1][col]]
            color = panel.palette[(row * 2 + col * 3 + row * col) % len(panel.palette)]
            picture.polygon(shape, fill=color)
            gauge = int(rng.integers(7, 11))
            lead.line([*shape, shape[0]], fill=255, width=gauge, joint="curve")
            cx = sum(point[0] for point in shape) / 4
            cy = sum(point[1] for point in shape) / 4
            if (row + col) % 3 == 1:
                paint.arc((cx - 28, cy - 17, cx + 28, cy + 15), 12, 325, fill=145, width=3)
                paint.line((cx - 20, cy + 9, cx + 19, cy - 10), fill=120, width=2)
            if (row * 3 + col) % 4 == 2:
                for dx, dy in ((-13, -8), (-5, 0), (12, 8), (4, 13)):
                    rr = int(rng.integers(2, 5))
                    paint.ellipse((cx + dx - rr, cy + dy - rr, cx + dx + rr, cy + dy + rr), fill=91)
    for row in grid[1:-1]:
        for cx, cy in row[1:-1]:
            lead.ellipse((cx - 5, cy - 4, cx + 5, cy + 5), fill=255)
    # Pale deposit and crack-like markings in the glass interior retain their own color.
    for cx, cy, radius in ((236, 288, 7), (241, 301, 8), (338, 175, 10), (488, 356, 6), (500, 349, 5)):
        picture.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(203, 201, 187))
    paint.line(((326, 187), (336, 197), (340, 213), (358, 227), (362, 232)), fill=183, width=2)
    paint.line(((426, 317), (433, 322), (446, 326), (453, 339), (463, 339)), fill=152, width=2)

    # The small samples are part of the synthetic light-box image and inside the registered plane.
    picture.rectangle((490, 451, 574, 485), fill=(227, 227, 219))
    picture.rectangle((476, 451, 487, 485), fill=(75, 85, 100))
    picture.rectangle((53, 448, 102, 486), fill=(159, 154, 152))
    picture.rectangle((237, 445, 410, 484), fill=(199, 197, 182))
    for x in range(243, 410, 6):
        picture.line((x, 450, x, 468 if (x - 243) % 24 == 0 else 461), fill=(70, 67, 63), width=1)

    y, x = np.indices(size, dtype=np.float32)
    x /= width
    y /= height
    flute = np.sin(2 * np.pi * (x * 42 + y * 4 + .16 * np.sin(y * 32)))
    waves = smoothed_noise(rng, size, 25)
    tooth = smoothed_noise(rng, size, 6)
    grain = rng.normal(size=size).astype(np.float32)
    linear = srgb_to_linear(np.asarray(glass, dtype=np.float32) / 255)
    furnace = np.clip(1 + .06 * waves + .027 * tooth + .021 * flute + .01 * grain, .75, 1.20)
    linear *= furnace[:, :, None]
    paint_mask = np.asarray(enamel, dtype=np.float32) / 255
    linear *= 1 - .59 * paint_mask[:, :, None]
    came = np.asarray(dark, dtype=np.float32) / 255
    sheen = np.clip(ndimage.gaussian_filter(came, 1) - np.roll(came, 2, axis=1), 0, 1)
    came_value = (np.maximum(.007, .018 + .004 * grain) + .044 * sheen)
    linear = linear * (1 - came[:, :, None]) + came_value[:, :, None] * came[:, :, None]
    relief = .18 * ndimage.gaussian_filter(came, .7) + .028 * tooth + .026 * paint_mask
    return linear, relief, came, x, y


def light_field(field, role):
    linear, relief, came, x, y = field
    glow = .96 + .07 * (1 - y) - .015 * np.square(x - .5)
    if role in ("rake-left", "rake-right"):
        sign = -1 if role == "rake-left" else 1
        gy, gx = np.gradient(relief)
        raking = np.clip(.83 + 8 * sign * gx + 3 * gy, .61, 1.25)
        return np.clip(linear * (raking * glow)[:, :, None], 0, 1)
    film = np.exp(-((x - .72 + .18 * (y - .46)) / .20) ** 2 - ((y - .46) / .13) ** 2)
    if role == "cross":
        reflection = .004 + .009 * film
        exposure = .97
    elif role == "parallel":
        reflection = .006 + .21 * film
        exposure = 1.04
    else:
        reflection = .004 + .012 * film
        exposure = 1
    shaded = linear * (glow * exposure)[:, :, None]
    shaded += reflection[:, :, None] * np.array([.68, .82, .92], dtype=np.float32) * (1 - came[:, :, None] * .5)
    return np.clip(shaded, 0, 1)


def warp_to_photo(array, corners, seed):
    width, height = PHOTO_SIZE
    inverse = np.linalg.inv(homography(DEFAULT_CORNERS, corners))
    photo_y, photo_x = np.indices((height, width), dtype=np.float32)
    x = (photo_x + .5) / width
    y = (photo_y + .5) / height
    denominator = inverse[2, 0] * x + inverse[2, 1] * y + inverse[2, 2]
    u = (inverse[0, 0] * x + inverse[0, 1] * y + inverse[0, 2]) / denominator
    v = (inverse[1, 0] * x + inverse[1, 1] * y + inverse[1, 2]) / denominator
    tile_h, tile_w = array.shape[:2]
    coords = (v * tile_h - .5, u * tile_w - .5)
    warped = np.stack([ndimage.map_coordinates(array[:, :, i], coords, order=1, mode="nearest") for i in range(3)], axis=2)
    inside = np.clip(np.minimum(np.minimum(u, 1 - u), np.minimum(v, 1 - v)) * 450 + .5, 0, 1)
    shadow = ndimage.shift(ndimage.gaussian_filter(inside, 8), (5, 7), mode="constant", order=1)
    noise = smoothed_noise(np.random.default_rng(seed + 7), (height, width), 48)
    value = np.clip(.075 + .032 * (1 - y) + .007 * noise, .03, .20)
    desk = value[:, :, None] * np.array([.76, .93, .99])
    desk *= (1 - .38 * shadow)[:, :, None]
    return warped * inside[:, :, None] + desk * (1 - inside[:, :, None])


def render_photo(field, role, panel):
    photo = warp_to_photo(light_field(field, role), POSES[role], panel.seed)
    rgb = np.rint(np.clip(linear_to_srgb(photo), 0, 1) * 255).astype(np.uint8)
    image = Image.fromarray(rgb)
    if role == "rake-right":
        image = image.convert("RGBA")
        ImageDraw.Draw(image).rectangle((596, 362, 632, 430), fill=(0, 0, 0, 0))
    return image


def rectangle(name, purpose, left, top, right, bottom):
    return {"id": make_id(), "name": name, "purpose": purpose, "vertices": [[left, top], [right, top], [right, bottom], [left, bottom]]}


def training_regions():
    return [
        rectangle("Glazing field", "inspection", .104, .103, .897, .831),
        rectangle("Enamel control", "glazed", .304, .526, .347, .568),
        rectangle("Clear glass control", "bare", .743, .895, .811, .928),
        rectangle("Neutral card", "neutral", .085, .881, .145, .941),
        rectangle("Scale strip", "ignore", .361, .878, .585, .932),
        rectangle("Central came strip", "inspection", .112, .402, .889, .502),
    ]


def training_specimen(store, panel):
    field = draw_panel(panel)
    photos = []
    for role in DEMO_ROLES:
        image = render_photo(field, role, panel)
        photo = store_capture(
            store.blobs, png_bytes(image), role,
            f"{panel.name.lower().replace(' ', '-')}-{role}.png",
            "Synthetic transmitted light study of colored glass, surface enamel and lead came.",
            make_id(), synthetic=True,
        )
        photos.append(photo)
    base = photos[0]
    size = canvas_dimensions(base, POSES["diffuse"], 252, 189)
    return {
        "name": panel.name, "batch": panel.tray, "body": panel.glass, "glaze": panel.finish, "firing": panel.position,
        "notes": "Synthetic glazing panel with visible came sections, a clear glass standard and alternate registered light views.",
        "width_mm": 252, "height_mm": 189, "synthetic": True, "photos": photos,
        "plane": {"photo_id": base["id"], "corners": POSES["diffuse"], "size": list(size)},
        "regions": training_regions(),
        "registrations": {
            photo["id"]: {"base_photo_id": base["id"], "corners": POSES[photo["role"]],
                          "support_percent": 97.5 if photo["role"] == "rake-right" else 100}
            for photo in photos[1:]
        },
    }


def load_training(store):
    existing = store.seeded_tiles(DEMO_KEY)
    if existing:
        return existing, False
    return store.seed_tiles(DEMO_KEY, [training_specimen(store, panel) for panel in SERIES]), True


@lru_cache(maxsize=1)
def preview_photo():
    panel = SERIES[0]
    return png_bytes(render_photo(draw_panel(panel), "diffuse", panel))
