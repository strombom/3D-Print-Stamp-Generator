# 3D Print Stamp Generator

This project aims to take an image as input and generate a 3D model that can be printed as a stamp.

The stamp symbol is defined by the white area or areas in the input image. These white regions are interpreted as the parts of the image that should become raised stamping geometry.

## Concept

The generated stamp model is a 3D box with a defined height. On the bottom face of that box, the white regions from the input image are extruded outward to form the stamp symbol.

In other words:

- Input: a black-and-white image.
- White pixels: the stamp symbol.
- Non-white pixels: background or empty space.
- Output: a 3D printable stamp model.
- Base shape: a rectangular box with a configurable height.
- Stamp face: the white areas from the image extruded from the bottom of the box.

## Intended Workflow

1. Create a JSON config file for the input image.
2. Detect the white areas in the image.
3. Convert those white areas into 2D stamp geometry.
4. Create a 3D box to act as the stamp body.
5. Extrude the detected white areas from the bottom of the box.
6. Export the result as a 3D model suitable for printing.

## Usage

Each stamp image should have its own JSON config file. Paths inside the config
are resolved relative to the JSON file.

```bash
python3 generate_stamp.py stamp_1.json
```

You can also generate several stamps in one run:

```bash
python3 generate_stamp.py stamp_1.json another_stamp.json
```

The script supports PNG files without extra dependencies. If Pillow is
installed, it can also read other image formats supported by Pillow.

## Config File

Example:

```json
{
  "image": "stamp_1.png",
  "output": "stamp_1.stl",
  "width_mm": 50,
  "base_height_mm": 6,
  "relief_height_mm": 2,
  "threshold": 245,
  "max_resolution": 256,
  "sample_mode": "majority",
  "invert": false,
  "mirror": true,
  "ascii_stl": false
}
```

- `image`: input image path.
- `output`: output STL path. If omitted, the image name is reused with `.stl`.
- `width_mm`: physical stamp width.
- `base_height_mm`: height of the rectangular stamp body.
- `relief_height_mm`: height of the raised stamping geometry.
- `threshold`: grayscale value from 0 to 255 treated as white.
- `max_resolution`: maximum mesh grid cells on the longest image side. Use `0` to keep the original image resolution.
- `sample_mode`: `majority`, `any`, or `center` for downsampled pixels.
- `invert`: raise dark regions instead of white regions.
- `mirror`: mirror the model horizontally so the stamped impression matches the input image.
- `ascii_stl`: write text STL instead of binary STL.

## Goal

The final printed object should work as a physical stamp: the raised geometry on the bottom forms the visible stamped symbol when pressed into ink, clay, or another stampable surface.
