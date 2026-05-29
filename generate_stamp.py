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
    width_mm: float
    base_height_mm: float
    relief_height_mm: float
    threshold: int
    max_resolution: int
    sample_mode: str
    invert: bool
    mirror: bool
    ascii_stl: bool


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
    "width_mm": 50.0,
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

    return StampConfig(
        config_path=config_path,
        image_path=image_path,
        output_path=output_path,
        width_mm=config_positive_float(config_path, raw_config, "width_mm"),
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
    mask: MaskData,
    width_mm: float,
    base_height_mm: float,
    relief_height_mm: float,
) -> tuple[Mesh, float]:
    mesh = Mesh([])
    height_mm = width_mm * (mask.height / mask.width)
    top_z = base_height_mm + relief_height_mm
    base_bottom_z = relief_height_mm
    relief_bottom_z = 0.0

    add_horizontal_rect(mesh, 0, 0, mask.width, mask.height, mask, width_mm, height_mm, top_z, up=True)
    add_base_sides(mesh, width_mm, height_mm, base_bottom_z, top_z)

    background = [bytearray(1 - value for value in row) for row in mask.pixels]
    background_mask = MaskData(mask.width, mask.height, background, mask.source_width, mask.source_height)

    for row0, row1, col0, col1 in iter_rectangles(background_mask.pixels):
        add_horizontal_rect(
            mesh,
            col0,
            row0,
            col1,
            row1,
            background_mask,
            width_mm,
            height_mm,
            base_bottom_z,
            up=False,
        )

    for row0, row1, col0, col1 in iter_rectangles(mask.pixels):
        add_horizontal_rect(
            mesh,
            col0,
            row0,
            col1,
            row1,
            mask,
            width_mm,
            height_mm,
            relief_bottom_z,
            up=False,
        )

    add_relief_sides(mesh, mask, width_mm, height_mm, relief_bottom_z, base_bottom_z)
    return mesh, height_mm


def add_horizontal_rect(
    mesh: Mesh,
    col0: int,
    row0: int,
    col1: int,
    row1: int,
    mask: MaskData,
    width_mm: float,
    height_mm: float,
    z: float,
    up: bool,
) -> None:
    x0 = col0 * width_mm / mask.width
    x1 = col1 * width_mm / mask.width
    y0 = height_mm - (row1 * height_mm / mask.height)
    y1 = height_mm - (row0 * height_mm / mask.height)

    if up:
        mesh.add_quad((x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z))
    else:
        mesh.add_quad((x0, y0, z), (x0, y1, z), (x1, y1, z), (x1, y0, z))


def add_base_sides(mesh: Mesh, width_mm: float, height_mm: float, z0: float, z1: float) -> None:
    mesh.add_quad((0.0, 0.0, z0), (0.0, 0.0, z1), (0.0, height_mm, z1), (0.0, height_mm, z0))
    mesh.add_quad(
        (width_mm, 0.0, z0),
        (width_mm, height_mm, z0),
        (width_mm, height_mm, z1),
        (width_mm, 0.0, z1),
    )
    mesh.add_quad((0.0, 0.0, z0), (width_mm, 0.0, z0), (width_mm, 0.0, z1), (0.0, 0.0, z1))
    mesh.add_quad(
        (0.0, height_mm, z0),
        (0.0, height_mm, z1),
        (width_mm, height_mm, z1),
        (width_mm, height_mm, z0),
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


def add_relief_sides(
    mesh: Mesh,
    mask: MaskData,
    width_mm: float,
    height_mm: float,
    z0: float,
    z1: float,
) -> None:
    for row in range(mask.height):
        y_top = height_mm - (row * height_mm / mask.height)
        y_bottom = height_mm - ((row + 1) * height_mm / mask.height)

        for col in range(mask.width):
            if not mask.pixels[row][col]:
                continue

            x0 = col * width_mm / mask.width
            x1 = (col + 1) * width_mm / mask.width

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
    mask = build_mask(
        image=image,
        threshold=config.threshold,
        max_resolution=config.max_resolution,
        sample_mode=config.sample_mode,
        invert=config.invert,
        mirror=config.mirror,
    )
    mesh, depth_mm = build_mesh(
        mask=mask,
        width_mm=config.width_mm,
        base_height_mm=config.base_height_mm,
        relief_height_mm=config.relief_height_mm,
    )
    write_stl(config.output_path, mesh, config.ascii_stl)

    raised_cells = sum(sum(row) for row in mask.pixels)
    print(f"Config: {config.config_path}")
    print(f"Input: {config.image_path} ({image.width}x{image.height}px)")
    print(f"Mesh grid: {mask.width}x{mask.height} cells, {raised_cells} raised")
    print(
        "Size: "
        f"{config.width_mm:.2f} x {depth_mm:.2f} x "
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
