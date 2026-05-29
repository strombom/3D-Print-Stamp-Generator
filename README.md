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

1. Provide an input image.
2. Detect the white areas in the image.
3. Convert those white areas into 2D stamp geometry.
4. Create a 3D box to act as the stamp body.
5. Extrude the detected white areas from the bottom of the box.
6. Export the result as a 3D model suitable for printing.

## Goal

The final printed object should work as a physical stamp: the raised geometry on the bottom forms the visible stamped symbol when pressed into ink, clay, or another stampable surface.
