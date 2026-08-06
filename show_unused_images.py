#!/usr/bin/python3
import json
from pathlib import Path

# Directories
IMAGES_DIR = Path("cache/images")
NOTES_DIR = Path("cache/notes")

# Collect all referenced image filenames
referenced_images = set()

for json_file in NOTES_DIR.glob("*.json"):
    try:
        with json_file.open("r", encoding="utf-8") as f:
            data = json.load(f)

        # Each top-level value is an entry
        if isinstance(data, dict):
            for entry in data.values():
                if not isinstance(entry, dict):
                    continue

                images = entry.get("images", [])
                if isinstance(images, list):
                    for image in images:
                        if isinstance(image, str):
                            # Store only the filename
                            referenced_images.add(Path(image).name)

    except Exception as e:
        print(f"Error reading {json_file}: {e}")

# All image filenames in the images directory
existing_images = {
    p.name
    for p in IMAGES_DIR.iterdir()
    if p.is_file()
}

# Images not referenced by any note
unused_images = sorted(existing_images - referenced_images)

print(f"Found {len(unused_images)} unused images:\n")

for image in unused_images:
    print(image)

# Optional: save the list
with open("unused_images.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(unused_images))

print("\nList saved to unused_images.txt")
