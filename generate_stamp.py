#!/usr/bin/env python3
"""Generate a 3D-printable stamp STL from a black-and-white image.

White pixels in the source image become raised geometry on the underside of a
rectangular stamp body. PNG files are supported with the Python standard
library; Pillow is used automatically if installed to support more formats.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


Vec3 = tuple[float, float, float]
Triangle = tuple[Vec3, Vec3, Vec3]


@dataclass(frozen=True)
class ImageData:
    width: int
    height: int
    pixels: bytearray


@dataclass(frozen=True)
class MaskData:
    width: int
    height: int
    pixels: list[bytearray]
    source_width: int
    source_height: int


@dataclass(frozen=True)
class StampConfig:
    config_path: Path
    image_path: Path
    output_path: Path
    stamp_height_mm: float
    base_mode: str
    outline_mm: float
    base_height_mm: float
    relief_height_mm: float
    threshold: int
    max_resolution: int
    sample_mode: str
    invert: bool
    mirror: bool
    ascii_stl: bool


@dataclass(frozen=True)
class PreparedMasks:
    relief: MaskData
    base: MaskData
    outline_cells: int
    actual_outline_mm: float
    white_extents: tuple[int, int, int, int] | None
    artwork_width_cells: int
    artwork_height_cells: int


@dataclass
class Mesh:
    triangles: list[Triangle]

    def add_triangle(self, a: Vec3, b: Vec3, c: Vec3) -> None:
        self.triangles.append((a, b, c))

    def add_quad(self, a: Vec3, b: Vec3, c: Vec3, d: Vec3) -> None:
        self.add_triangle(a, b, c)
        self.add_triangle(a, c, d)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create STL stamp models from per-image JSON config files. "
            "White image regions become the raised stamping surface."
        )
    )
    parser.add_argument(
        "configs",
        type=Path,
        nargs="+",
        help="One or more JSON config files, one per stamp image.",
    )
    return parser.parse_args(argv)


CONFIG_DEFAULTS: Mapping[str, Any] = {
    "stamp_height_mm": 50.0,
    "base_mode": "image",
    "outline_mm": 0.0,
    "base_height_mm": 6.0,
    "relief_height_mm": 2.0,
    "threshold": 245,
    "max_resolution": 256,
    "sample_mode": "majority",
    "invert": False,
    "mirror": True,
    "ascii_stl": False,
}

CONFIG_FIELDS = {"image", "output", *CONFIG_DEFAULTS.keys()}
SAMPLE_MODES = {"majority", "any", "center"}
BASE_MODES = {"image", "extents", "contour"}


def load_stamp_config(config_path: Path) -> StampConfig:
    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"{config_path}: config file does not exist.") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{config_path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}.") from exc

    if not isinstance(raw_config, dict):
        raise SystemExit(f"{config_path}: config must be a JSON object.")

    unknown_fields = sorted(set(raw_config) - CONFIG_FIELDS)
    if unknown_fields:
        names = ", ".join(unknown_fields)
        raise SystemExit(f"{config_path}: unknown config field(s): {names}.")

    image_value = raw_config.get("image")
    if not isinstance(image_value, str) or not image_value:
        raise SystemExit(f"{config_path}: required field `image` must be a non-empty string.")

    config_dir = config_path.resolve().parent
    image_path = resolve_config_path(config_dir, image_value)
    output_value = raw_config.get("output")
    if output_value is None:
        output_path = image_path.with_suffix(".stl")
    elif isinstance(output_value, str) and output_value:
        output_path = resolve_config_path(config_dir, output_value)
    else:
        raise SystemExit(f"{config_path}: field `output` must be a non-empty string when present.")

    sample_mode = raw_config.get("sample_mode", CONFIG_DEFAULTS["sample_mode"])
    if sample_mode not in SAMPLE_MODES:
        allowed = ", ".join(sorted(SAMPLE_MODES))
        raise SystemExit(f"{config_path}: field `sample_mode` must be one of: {allowed}.")

    base_mode = raw_config.get("base_mode", CONFIG_DEFAULTS["base_mode"])
    if base_mode not in BASE_MODES:
        allowed = ", ".join(sorted(BASE_MODES))
        raise SystemExit(f"{config_path}: field `base_mode` must be one of: {allowed}.")

    return StampConfig(
        config_path=config_path,
        image_path=image_path,
        output_path=output_path,
        stamp_height_mm=config_positive_float(config_path, raw_config, "stamp_height_mm"),
        base_mode=str(base_mode),
        outline_mm=config_non_negative_float(config_path, raw_config, "outline_mm"),
        base_height_mm=config_positive_float(config_path, raw_config, "base_height_mm"),
        relief_height_mm=config_positive_float(config_path, raw_config, "relief_height_mm"),
        threshold=config_threshold(config_path, raw_config),
        max_resolution=config_non_negative_int(config_path, raw_config, "max_resolution"),
        sample_mode=str(sample_mode),
        invert=config_bool(config_path, raw_config, "invert"),
        mirror=config_bool(config_path, raw_config, "mirror"),
        ascii_stl=config_bool(config_path, raw_config, "ascii_stl"),
    )


def resolve_config_path(config_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return config_dir / path


def config_positive_float(config_path: Path, config: Mapping[str, Any], field: str) -> float:
    value = config.get(field, CONFIG_DEFAULTS[field])
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{config_path}: field `{field}` must be a number greater than 0.") from exc
    if parsed <= 0:
        raise SystemExit(f"{config_path}: field `{field}` must be greater than 0.")
    return parsed


def config_non_negative_float(config_path: Path, config: Mapping[str, Any], field: str) -> float:
    value = config.get(field, CONFIG_DEFAULTS[field])
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{config_path}: field `{field}` must be a number 0 or greater.") from exc
    if parsed < 0:
        raise SystemExit(f"{config_path}: field `{field}` must be 0 or greater.")
    return parsed


def config_non_negative_int(config_path: Path, config: Mapping[str, Any], field: str) -> int:
    value = config.get(field, CONFIG_DEFAULTS[field])
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{config_path}: field `{field}` must be an integer 0 or greater.") from exc
    if parsed < 0:
        raise SystemExit(f"{config_path}: field `{field}` must be 0 or greater.")
    return parsed


def config_threshold(config_path: Path, config: Mapping[str, Any]) -> int:
    value = config.get("threshold", CONFIG_DEFAULTS["threshold"])
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{config_path}: field `threshold` must be an integer from 0 to 255.") from exc
    if not 0 <= parsed <= 255:
        raise SystemExit(f"{config_path}: field `threshold` must be between 0 and 255.")
    return parsed


def config_bool(config_path: Path, config: Mapping[str, Any], field: str) -> bool:
    value = config.get(field, CONFIG_DEFAULTS[field])
    if not isinstance(value, bool):
        raise SystemExit(f"{config_path}: field `{field}` must be true or false.")
    return value


def load_image(path: Path) -> ImageData:
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        if path.suffix.lower() != ".png":
            raise SystemExit(
                "Only PNG input is supported without Pillow. "
                "Install Pillow with `python3 -m pip install pillow` for more formats."
            )
        return load_png(path)

    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        pixels = bytearray()
        for red, green, blue, alpha in rgba.getdata():
            gray = rgb_to_gray(red, green, blue)
            pixels.append((gray * alpha) // 255)
        return ImageData(rgba.width, rgba.height, pixels)


def load_png(path: Path) -> ImageData:
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise SystemExit(f"{path} is not a PNG file.")

    pos = len(PNG_SIGNATURE)
    width = height = bit_depth = color_type = None
    palette: list[tuple[int, int, int]] = []
    transparency: bytes = b""
    idat_chunks: list[bytes] = []

    while pos < len(data):
        if pos + 8 > len(data):
            raise SystemExit("Invalid PNG: truncated chunk header.")
        length = int.from_bytes(data[pos : pos + 4], "big")
        chunk_type = data[pos + 4 : pos + 8]
        chunk_start = pos + 8
        chunk_end = chunk_start + length
        crc_end = chunk_end + 4
        if crc_end > len(data):
            raise SystemExit("Invalid PNG: truncated chunk data.")
        chunk = data[chunk_start:chunk_end]
        pos = crc_end

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if compression != 0 or filter_method != 0 or interlace != 0:
                raise SystemExit("Unsupported PNG: only non-interlaced standard PNG files are supported.")
            if bit_depth != 8:
                raise SystemExit("Unsupported PNG: only 8-bit PNG files are supported.")
            if color_type not in (0, 2, 3, 4, 6):
                raise SystemExit(f"Unsupported PNG color type: {color_type}.")
        elif chunk_type == b"PLTE":
            palette = [
                (chunk[index], chunk[index + 1], chunk[index + 2])
                for index in range(0, len(chunk) - 2, 3)
            ]
        elif chunk_type == b"tRNS":
            transparency = chunk
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk)
        elif chunk_type == b"IEND":
            break

    if width is None or height is None or bit_depth is None or color_type is None:
        raise SystemExit("Invalid PNG: missing IHDR chunk.")
    if not idat_chunks:
        raise SystemExit("Invalid PNG: missing IDAT data.")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    stride = width * channels

    try:
        raw = zlib.decompress(b"".join(idat_chunks))
    except zlib.error as exc:
        raise SystemExit(f"Invalid PNG compression data: {exc}") from exc

    expected = (stride + 1) * height
    if len(raw) < expected:
        raise SystemExit("Invalid PNG: decompressed data is shorter than expected.")

    gray_pixels = bytearray()
    previous = bytearray(stride)
    raw_pos = 0

    for _row in range(height):
        filter_type = raw[raw_pos]
        raw_pos += 1
        scanline = bytearray(raw[raw_pos : raw_pos + stride])
        raw_pos += stride
        apply_png_filter(filter_type, scanline, previous, channels)
        gray_pixels.extend(scanline_to_gray(scanline, width, color_type, palette, transparency))
        previous = scanline

    return ImageData(width, height, gray_pixels)


def apply_png_filter(filter_type: int, scanline: bytearray, previous: bytearray, bpp: int) -> None:
    if filter_type == 0:
        return

    for index in range(len(scanline)):
        left = scanline[index - bpp] if index >= bpp else 0
        up = previous[index]
        upper_left = previous[index - bpp] if index >= bpp else 0

        if filter_type == 1:
            scanline[index] = (scanline[index] + left) & 0xFF
        elif filter_type == 2:
            scanline[index] = (scanline[index] + up) & 0xFF
        elif filter_type == 3:
            scanline[index] = (scanline[index] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            scanline[index] = (scanline[index] + paeth(left, up, upper_left)) & 0xFF
        else:
            raise SystemExit(f"Unsupported PNG filter type: {filter_type}.")


def paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_upper_left = abs(estimate - upper_left)
    if distance_left <= distance_up and distance_left <= distance_upper_left:
        return left
    if distance_up <= distance_upper_left:
        return up
    return upper_left


def scanline_to_gray(
    scanline: bytearray,
    width: int,
    color_type: int,
    palette: Sequence[tuple[int, int, int]],
    transparency: bytes,
) -> Iterator[int]:
    for col in range(width):
        if color_type == 0:
            yield scanline[col]
        elif color_type == 2:
            offset = col * 3
            yield rgb_to_gray(scanline[offset], scanline[offset + 1], scanline[offset + 2])
        elif color_type == 3:
            palette_index = scanline[col]
            if palette_index >= len(palette):
                raise SystemExit("Invalid PNG: palette index is out of range.")
            red, green, blue = palette[palette_index]
            alpha = transparency[palette_index] if palette_index < len(transparency) else 255
            yield (rgb_to_gray(red, green, blue) * alpha) // 255
        elif color_type == 4:
            offset = col * 2
            gray = scanline[offset]
            alpha = scanline[offset + 1]
            yield (gray * alpha) // 255
        elif color_type == 6:
            offset = col * 4
            gray = rgb_to_gray(scanline[offset], scanline[offset + 1], scanline[offset + 2])
            alpha = scanline[offset + 3]
            yield (gray * alpha) // 255


def rgb_to_gray(red: int, green: int, blue: int) -> int:
    return (299 * red + 587 * green + 114 * blue) // 1000


def build_mask(
    image: ImageData,
    threshold: int,
    max_resolution: int,
    sample_mode: str,
    invert: bool,
    mirror: bool,
) -> MaskData:
    if max_resolution and max(image.width, image.height) > max_resolution:
        scale = max(image.width, image.height) / max_resolution
        mask_width = max(1, round(image.width / scale))
        mask_height = max(1, round(image.height / scale))
    else:
        mask_width = image.width
        mask_height = image.height

    rows: list[bytearray] = []
    for row in range(mask_height):
        source_y0 = (row * image.height) // mask_height
        source_y1 = max(source_y0 + 1, ((row + 1) * image.height) // mask_height)
        mask_row = bytearray()

        for col in range(mask_width):
            source_x0 = (col * image.width) // mask_width
            source_x1 = max(source_x0 + 1, ((col + 1) * image.width) // mask_width)
            is_raised = sample_region(
                image,
                source_x0,
                source_y0,
                min(source_x1, image.width),
                min(source_y1, image.height),
                threshold,
                sample_mode,
            )
            if invert:
                is_raised = not is_raised
            mask_row.append(1 if is_raised else 0)

        if mirror:
            mask_row.reverse()
        rows.append(mask_row)

    return MaskData(mask_width, mask_height, rows, image.width, image.height)


def prepare_masks(mask: MaskData, base_mode: str, outline_mm: float, stamp_height_mm: float) -> PreparedMasks:
    white_extents = find_mask_extents(mask.pixels)
    if white_extents is None:
        raise SystemExit("No raised pixels were detected. Adjust `threshold`, `invert`, or the input image.")

    row0, row1, col0, col1 = white_extents
    artwork_width_cells = col1 - col0
    artwork_height_cells = row1 - row0

    if base_mode == "image":
        if outline_mm > 0:
            raise SystemExit("`outline_mm` only applies when `base_mode` is `extents` or `contour`.")
        return PreparedMasks(
            relief=mask,
            base=filled_mask(mask.width, mask.height, 1, mask),
            outline_cells=0,
            actual_outline_mm=0.0,
            white_extents=white_extents,
            artwork_width_cells=artwork_width_cells,
            artwork_height_cells=artwork_height_cells,
        )

    outline_cells = outline_mm_to_cells(outline_mm, stamp_height_mm, artwork_height_cells)
    relief = extract_mask_with_outline(mask, white_extents, outline_cells)

    if base_mode == "extents":
        base = filled_mask(relief.width, relief.height, 1, relief)
    elif base_mode == "contour":
        base = MaskData(
            relief.width,
            relief.height,
            dilate_mask(relief.pixels, outline_cells),
            relief.source_width,
            relief.source_height,
        )
    else:
        raise SystemExit(f"Unsupported base mode: {base_mode}.")

    mm_per_cell = stamp_height_mm / artwork_height_cells
    actual_outline_mm = outline_cells * mm_per_cell
    return PreparedMasks(
        relief=relief,
        base=base,
        outline_cells=outline_cells,
        actual_outline_mm=actual_outline_mm,
        white_extents=white_extents,
        artwork_width_cells=artwork_width_cells,
        artwork_height_cells=artwork_height_cells,
    )


def filled_mask(width: int, height: int, value: int, source: MaskData) -> MaskData:
    fill = 1 if value else 0
    return MaskData(
        width,
        height,
        [bytearray([fill]) * width for _row in range(height)],
        source.source_width,
        source.source_height,
    )


def find_mask_extents(rows: Sequence[bytearray]) -> tuple[int, int, int, int] | None:
    row0: int | None = None
    row1 = 0
    col0: int | None = None
    col1 = 0

    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            if not value:
                continue
            if row0 is None:
                row0 = row_index
            row1 = row_index + 1
            col0 = col_index if col0 is None else min(col0, col_index)
            col1 = max(col1, col_index + 1)

    if row0 is None or col0 is None:
        return None
    return row0, row1, col0, col1


def outline_mm_to_cells(outline_mm: float, stamp_height_mm: float, artwork_height_cells: int) -> int:
    if outline_mm <= 0:
        return 0

    ideal_cells = outline_mm * artwork_height_cells / stamp_height_mm
    return max(1, round(ideal_cells))


def extract_mask_with_outline(
    mask: MaskData,
    extents: tuple[int, int, int, int],
    outline_cells: int,
) -> MaskData:
    row0, row1, col0, col1 = extents
    output_width = (col1 - col0) + (2 * outline_cells)
    output_height = (row1 - row0) + (2 * outline_cells)
    rows = [bytearray(output_width) for _row in range(output_height)]

    for source_row in range(row0, row1):
        target_row = (source_row - row0) + outline_cells
        for source_col in range(col0, col1):
            if mask.pixels[source_row][source_col]:
                target_col = (source_col - col0) + outline_cells
                rows[target_row][target_col] = 1

    return MaskData(output_width, output_height, rows, mask.source_width, mask.source_height)


def dilate_mask(rows: Sequence[bytearray], radius: int) -> list[bytearray]:
    height = len(rows)
    width = len(rows[0]) if rows else 0
    output = [bytearray(width) for _row in range(height)]

    offsets = [
        (row_offset, col_offset)
        for row_offset in range(-radius, radius + 1)
        for col_offset in range(-radius, radius + 1)
        if (row_offset * row_offset) + (col_offset * col_offset) <= radius * radius
    ]

    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            if not value:
                continue
            for row_offset, col_offset in offsets:
                target_row = row_index + row_offset
                target_col = col_index + col_offset
                if 0 <= target_row < height and 0 <= target_col < width:
                    output[target_row][target_col] = 1

    return output


def sample_region(
    image: ImageData,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    threshold: int,
    sample_mode: str,
) -> bool:
    if sample_mode == "center":
        x = (x0 + x1 - 1) // 2
        y = (y0 + y1 - 1) // 2
        return image.pixels[y * image.width + x] >= threshold

    raised_count = 0
    total = (x1 - x0) * (y1 - y0)
    for y in range(y0, y1):
        offset = y * image.width
        for x in range(x0, x1):
            if image.pixels[offset + x] >= threshold:
                if sample_mode == "any":
                    return True
                raised_count += 1

    return raised_count * 2 >= total


def build_mesh(
    relief_mask: MaskData,
    base_mask: MaskData,
    size_x_mm: float,
    size_y_mm: float,
    base_height_mm: float,
    relief_height_mm: float,
) -> tuple[Mesh, float]:
    if relief_mask.width != base_mask.width or relief_mask.height != base_mask.height:
        raise SystemExit("Internal error: relief mask and base mask dimensions do not match.")

    mesh = Mesh([])
    base_bottom_z = 0.0
    base_top_z = base_height_mm
    relief_top_z = base_height_mm + relief_height_mm

    for row0, row1, col0, col1 in iter_rectangles(base_mask.pixels):
        add_horizontal_rect(
            mesh,
            col0,
            row0,
            col1,
            row1,
            base_mask,
            size_x_mm,
            size_y_mm,
            base_bottom_z,
            up=False,
        )

    exposed_base = subtract_masks(base_mask, relief_mask)
    for row0, row1, col0, col1 in iter_rectangles(exposed_base.pixels):
        add_horizontal_rect(
            mesh,
            col0,
            row0,
            col1,
            row1,
            exposed_base,
            size_x_mm,
            size_y_mm,
            base_top_z,
            up=True,
        )

    for row0, row1, col0, col1 in iter_rectangles(relief_mask.pixels):
        add_horizontal_rect(
            mesh,
            col0,
            row0,
            col1,
            row1,
            relief_mask,
            size_x_mm,
            size_y_mm,
            relief_top_z,
            up=True,
        )

    add_mask_sides(mesh, base_mask, size_x_mm, size_y_mm, base_bottom_z, base_top_z)
    add_mask_sides(mesh, relief_mask, size_x_mm, size_y_mm, base_top_z, relief_top_z)
    return mesh, size_y_mm


def add_horizontal_rect(
    mesh: Mesh,
    col0: int,
    row0: int,
    col1: int,
    row1: int,
    mask: MaskData,
    size_x_mm: float,
    size_y_mm: float,
    z: float,
    up: bool,
) -> None:
    x0 = col0 * size_x_mm / mask.width
    x1 = col1 * size_x_mm / mask.width
    y0 = size_y_mm - (row1 * size_y_mm / mask.height)
    y1 = size_y_mm - (row0 * size_y_mm / mask.height)

    if up:
        mesh.add_quad((x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z))
    else:
        mesh.add_quad((x0, y0, z), (x0, y1, z), (x1, y1, z), (x1, y0, z))


def subtract_masks(base_mask: MaskData, relief_mask: MaskData) -> MaskData:
    rows: list[bytearray] = []
    for base_row, relief_row in zip(base_mask.pixels, relief_mask.pixels):
        rows.append(
            bytearray(
                1 if base_value and not relief_value else 0
                for base_value, relief_value in zip(base_row, relief_row)
            )
        )
    return MaskData(
        base_mask.width,
        base_mask.height,
        rows,
        base_mask.source_width,
        base_mask.source_height,
    )


def iter_rectangles(rows: Sequence[bytearray]) -> Iterator[tuple[int, int, int, int]]:
    active: dict[tuple[int, int], tuple[int, int]] = {}

    for row_index, row in enumerate(rows):
        current_runs = set(find_runs(row))
        next_active: dict[tuple[int, int], tuple[int, int]] = {}

        for run in current_runs:
            if run in active:
                start_row, _end_row = active[run]
                next_active[run] = (start_row, row_index + 1)
            else:
                next_active[run] = (row_index, row_index + 1)

        for run, (start_row, end_row) in active.items():
            if run not in next_active:
                yield start_row, end_row, run[0], run[1]

        active = next_active

    for run, (start_row, end_row) in active.items():
        yield start_row, end_row, run[0], run[1]


def find_runs(row: bytearray) -> Iterator[tuple[int, int]]:
    col = 0
    while col < len(row):
        while col < len(row) and not row[col]:
            col += 1
        start = col
        while col < len(row) and row[col]:
            col += 1
        if start < col:
            yield start, col


def add_mask_sides(
    mesh: Mesh,
    mask: MaskData,
    size_x_mm: float,
    size_y_mm: float,
    z0: float,
    z1: float,
) -> None:
    for row in range(mask.height):
        y_top = size_y_mm - (row * size_y_mm / mask.height)
        y_bottom = size_y_mm - ((row + 1) * size_y_mm / mask.height)

        for col in range(mask.width):
            if not mask.pixels[row][col]:
                continue

            x0 = col * size_x_mm / mask.width
            x1 = (col + 1) * size_x_mm / mask.width

            if col == 0 or not mask.pixels[row][col - 1]:
                mesh.add_quad((x0, y_bottom, z0), (x0, y_bottom, z1), (x0, y_top, z1), (x0, y_top, z0))
            if col == mask.width - 1 or not mask.pixels[row][col + 1]:
                mesh.add_quad((x1, y_bottom, z0), (x1, y_top, z0), (x1, y_top, z1), (x1, y_bottom, z1))
            if row == 0 or not mask.pixels[row - 1][col]:
                mesh.add_quad((x0, y_top, z0), (x0, y_top, z1), (x1, y_top, z1), (x1, y_top, z0))
            if row == mask.height - 1 or not mask.pixels[row + 1][col]:
                mesh.add_quad((x0, y_bottom, z0), (x1, y_bottom, z0), (x1, y_bottom, z1), (x0, y_bottom, z1))


def normal_for(triangle: Triangle) -> Vec3:
    a, b, c = triangle
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length == 0:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


def write_stl(path: Path, mesh: Mesh, ascii_stl: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if ascii_stl:
        write_ascii_stl(path, mesh)
    else:
        write_binary_stl(path, mesh)


def write_binary_stl(path: Path, mesh: Mesh) -> None:
    header = b"Generated by generate_stamp.py".ljust(80, b" ")
    with path.open("wb") as file:
        file.write(header)
        file.write(struct.pack("<I", len(mesh.triangles)))
        for triangle in mesh.triangles:
            normal = normal_for(triangle)
            values = (*normal, *triangle[0], *triangle[1], *triangle[2], 0)
            file.write(struct.pack("<12fH", *values))


def write_ascii_stl(path: Path, mesh: Mesh) -> None:
    with path.open("w", encoding="utf-8") as file:
        file.write("solid stamp\n")
        for triangle in mesh.triangles:
            nx, ny, nz = normal_for(triangle)
            file.write(f"  facet normal {nx:.6g} {ny:.6g} {nz:.6g}\n")
            file.write("    outer loop\n")
            for x, y, z in triangle:
                file.write(f"      vertex {x:.6g} {y:.6g} {z:.6g}\n")
            file.write("    endloop\n")
            file.write("  endfacet\n")
        file.write("endsolid stamp\n")


def generate_stamp(config: StampConfig) -> None:
    image = load_image(config.image_path)
    source_mask = build_mask(
        image=image,
        threshold=config.threshold,
        max_resolution=config.max_resolution,
        sample_mode=config.sample_mode,
        invert=config.invert,
        mirror=config.mirror,
    )
    masks = prepare_masks(
        mask=source_mask,
        base_mode=config.base_mode,
        outline_mm=config.outline_mm,
        stamp_height_mm=config.stamp_height_mm,
    )
    mm_per_cell = config.stamp_height_mm / masks.artwork_height_cells
    size_x_mm = masks.base.width * mm_per_cell
    size_y_mm = masks.base.height * mm_per_cell
    mesh, depth_mm = build_mesh(
        relief_mask=masks.relief,
        base_mask=masks.base,
        size_x_mm=size_x_mm,
        size_y_mm=size_y_mm,
        base_height_mm=config.base_height_mm,
        relief_height_mm=config.relief_height_mm,
    )
    write_stl(config.output_path, mesh, config.ascii_stl)

    raised_cells = sum(sum(row) for row in masks.relief.pixels)
    base_cells = sum(sum(row) for row in masks.base.pixels)
    print(f"Config: {config.config_path}")
    print(f"Input: {config.image_path} ({image.width}x{image.height}px)")
    print(f"Mesh grid: {masks.base.width}x{masks.base.height} cells, {raised_cells} raised, {base_cells} base")
    print(f"Base mode: {config.base_mode}")
    print(f"Stamp figure height: {config.stamp_height_mm:.2f} mm")
    if config.base_mode != "image":
        print(f"Outline: {masks.actual_outline_mm:.2f} mm ({masks.outline_cells} cells)")
    print(
        "Size: "
        f"{size_x_mm:.2f} x {depth_mm:.2f} x "
        f"{config.base_height_mm + config.relief_height_mm:.2f} mm"
    )
    print(f"Triangles: {len(mesh.triangles)}")
    print(f"Wrote: {config.output_path}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    for index, config_path in enumerate(args.configs):
        if index:
            print()
        generate_stamp(load_stamp_config(config_path))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
