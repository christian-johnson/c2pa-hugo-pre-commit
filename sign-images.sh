#!/bin/bash

MANIFEST="manifest.json"
SOURCE_DIR="assets/img"
OUTPUT_DIR="static/img"

signed_any=false

for file in "$SOURCE_DIR"/*; do
  [ -f "$file" ] || continue

  filename=$(basename "$file")

  [ -f "$OUTPUT_DIR/$filename" ] && continue

  echo "Signing $filename..."
  c2patool "$file" -m "$MANIFEST" -o "$OUTPUT_DIR/$filename"

  signed_any=true
done

if [ "$signed_any" = true ]; then
  echo ""
  echo "ERROR: Signed new images. Update your markdown to reference them in static/img/"
  exit 1
fi

exit 0
