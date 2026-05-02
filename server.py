"""
mcp-compressfile — MCP server for compressfile.pro
Bulk image compression, conversion, resizing, and analysis.
Requires: mcp[server] Pillow pillow-avif-plugin imagehash
"""

import os
import glob
import io
import shutil
from typing import List, Optional

import pillow_avif          # registers AVIF support with Pillow as a side-effect
import imagehash            # perceptual hashing for duplicate detection
from PIL import Image, ImageDraw, ImageFont, ExifTags
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mcp-compressfile")

# ─── Constants ────────────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.tiff', '.bmp', '.avif')
SUPPORTED_FORMATS    = ("avif", "webp", "jpeg", "png")

# EXIF orientation tag id → (clockwise_degrees, flip_horizontal_first)
EXIF_ORIENTATION_MAP = {
    2: (0,   True),
    3: (180, False),
    4: (180, True),
    5: (90,  True),
    6: (90,  False),
    7: (270, True),
    8: (270, False),
}

PHOTO_EXTENSIONS = {'.jpg', '.jpeg'}


# ─── Internal helpers ─────────────────────────────────────────────────────────

def expand_paths(path_patterns: List[str]) -> List[str]:
    """Expand glob patterns into a deduplicated, sorted list of files."""
    expanded = []
    for pattern in path_patterns:
        matches = glob.glob(os.path.expanduser(pattern), recursive=True)
        expanded.extend(matches)
    return sorted(set(f for f in expanded if os.path.isfile(f)))


def normalise_format(raw: str) -> Optional[str]:
    """Lowercase, alias jpg→jpeg, return None if unsupported."""
    ext = raw.lower().strip()
    if ext == "jpg":
        ext = "jpeg"
    return ext if ext in SUPPORTED_FORMATS else None


def build_save_kwargs(target_ext: str, quality: int, lossless: bool) -> dict:
    """Return format-appropriate kwargs for Image.save()."""
    if target_ext == "png":
        return {
            # Linear map: quality 100 → level 0 (fast/larger),
            #             quality 1   → level 9 (slow/smallest)
            "compress_level": round((100 - quality) / 100 * 9),
            "optimize": True,
        }
    if target_ext == "jpeg":
        return {"quality": quality, "optimize": True}
    if target_ext == "webp":
        kwargs: dict = {"lossless": lossless}
        if not lossless:
            kwargs["quality"] = quality
        return kwargs
    if target_ext == "avif":
        return {"quality": quality}
    return {}


def prepare_image(img: Image.Image, target_ext: str) -> Image.Image:
    """
    Convert image mode to one compatible with the target format.
    For JPEG: composites any transparency onto a white background so no pixel
    data is lost and no IndexError is raised on LA images.
    """
    if target_ext != "jpeg" or img.mode == "RGB":
        return img

    # Palette modes may or may not carry a transparency entry.
    if img.mode in ("P", "PA"):
        has_alpha = img.mode == "PA" or "transparency" in img.info
        img = img.convert("RGBA" if has_alpha else "RGB")
        if img.mode == "RGB":
            return img

    # Composite alpha onto white. Normalise to RGBA first so split()[3] is
    # always the alpha band — LA only has 2 bands and would raise IndexError.
    if img.mode in ("RGBA", "LA"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg

    # CMYK, L, YCbCr, HSV, etc.
    return img.convert("RGB")


def apply_exif_rotation(img: Image.Image) -> Image.Image:
    """Return the image rotated/flipped per its EXIF orientation tag."""
    try:
        raw_exif = img._getexif()
    except AttributeError:
        return img
    if not raw_exif:
        return img

    orientation_key = next(
        (k for k, v in ExifTags.TAGS.items() if v == "Orientation"), None
    )
    if orientation_key is None:
        return img

    orientation = raw_exif.get(orientation_key, 1)
    if orientation == 1 or orientation not in EXIF_ORIENTATION_MAP:
        return img

    degrees, do_flip = EXIF_ORIENTATION_MAP[orientation]
    if do_flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if degrees:
        img = img.rotate(degrees, expand=True)
    return img


def size_label(path: str) -> str:
    kb = os.path.getsize(path) / 1024
    return f"{kb:.1f} KB" if kb < 1024 else f"{kb / 1024:.2f} MB"


def image_files_in(directory: str) -> List[str]:
    """Return sorted list of supported image paths inside a directory."""
    return sorted(
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, f))
        and f.lower().endswith(SUPPORTED_EXTENSIONS)
    )


# ─── Tools: File & Folder ─────────────────────────────────────────────────────

@mcp.tool()
def list_files(directory: str) -> str:
    """
    List all supported image files inside a directory with their sizes.
    Call this first to inspect available images before any other operation.

    Args:
        directory: Path to the directory to inspect (e.g., "/data").
    """
    if not os.path.isdir(directory):
        return f"Directory not found: {directory}"

    files = image_files_in(directory)
    if not files:
        return f"No supported image files found in `{directory}`."

    total_kb = sum(os.path.getsize(f) / 1024 for f in files)
    lines = [
        f"## Images in `{directory}`",
        f"Found **{len(files)}** image(s), {total_kb:.1f} KB total\n",
        *[f"- {os.path.basename(f)}  ({size_label(f)})" for f in files],
    ]
    return "\n".join(lines)


@mcp.tool()
def find_large_images(directory: str, min_size_kb: int = 500) -> str:
    """
    Scan a directory and return images above a given file-size threshold.
    Use this to identify the best candidates for compression.

    Args:
        directory:   Path to scan.
        min_size_kb: Minimum file size in KB to include. Default 500.
    """
    if not os.path.isdir(directory):
        return f"Directory not found: {directory}"

    results = [
        (f, os.path.getsize(f) / 1024)
        for f in image_files_in(directory)
        if os.path.getsize(f) / 1024 >= min_size_kb
    ]
    results.sort(key=lambda x: -x[1])

    if not results:
        return f"No images larger than {min_size_kb} KB found in `{directory}`."

    lines = [
        f"## Large Images in `{directory}`",
        f"Found **{len(results)}** image(s) ≥ {min_size_kb} KB:\n",
        *[f"- {os.path.basename(p)}  ({kb:.1f} KB)" for p, kb in results],
    ]
    return "\n".join(lines)


@mcp.tool()
def compare_folders(source: str, output: str) -> str:
    """
    Diff a source folder against an output folder to show what has and hasn't
    been processed yet. Useful to resume interrupted batch jobs.

    Args:
        source: Original images directory.
        output: Compressed/converted output directory.
    """
    if not os.path.isdir(source):
        return f"Source directory not found: {source}"

    src_names = {
        os.path.splitext(os.path.basename(f))[0]
        for f in image_files_in(source)
    }
    out_names: set = set()
    if os.path.isdir(output):
        out_names = {
            os.path.splitext(os.path.basename(f))[0]
            for f in image_files_in(output)
        }

    done     = sorted(src_names & out_names)
    pending  = sorted(src_names - out_names)
    orphaned = sorted(out_names - src_names)

    lines = [
        "## Folder Comparison",
        f"**Source:** `{source}`  |  **Output:** `{output}`\n",
        f"✅ Processed ({len(done)}): "   + (", ".join(done)     or "none"),
        f"⏳ Pending   ({len(pending)}): " + (", ".join(pending)  or "none"),
        f"👻 Orphaned  ({len(orphaned)}): "+ (", ".join(orphaned) or "none"),
    ]
    return "\n".join(lines)


@mcp.tool()
def undo_last_batch(output_directory: str, confirm: bool = False) -> str:
    """
    Delete an entire output directory to allow retrying with different settings.
    Requires confirm=True as a safety gate — never deletes without explicit confirmation.

    Args:
        output_directory: The directory to remove.
        confirm: Must be True to proceed. Default False (dry-run).
    """
    if not os.path.isdir(output_directory):
        return f"Directory not found: {output_directory}"

    files = image_files_in(output_directory)
    if not confirm:
        return (
            f"Dry-run: would delete `{output_directory}` "
            f"({len(files)} image(s)). Re-run with confirm=True to proceed."
        )

    shutil.rmtree(output_directory)
    return f"✅ Deleted `{output_directory}` ({len(files)} image(s) removed)."


# ─── Tools: Analysis & Metadata ───────────────────────────────────────────────

@mcp.tool()
def get_image_info(file_path: str) -> str:
    """
    Return full metadata for a single image: dimensions, mode, format, file
    size, DPI, and all readable EXIF fields.
    Call this before processing to make informed decisions about format and quality.

    Args:
        file_path: Absolute path to the image file.
    """
    if not os.path.isfile(file_path):
        return f"File not found: {file_path}"
    if not file_path.lower().endswith(SUPPORTED_EXTENSIONS):
        return f"Unsupported file type: {file_path}"

    try:
        with Image.open(file_path) as img:
            info: dict = {
                "file":       os.path.basename(file_path),
                "size":       size_label(file_path),
                "format":     img.format,
                "mode":       img.mode,
                "width_px":   img.width,
                "height_px":  img.height,
                "megapixels": round(img.width * img.height / 1_000_000, 2),
                "dpi":        img.info.get("dpi"),
                "has_alpha":  (
                    img.mode in ("RGBA", "LA", "PA")
                    or (img.mode == "P" and "transparency" in img.info)
                ),
            }

            exif_data: dict = {}
            try:
                raw_exif = img._getexif() or {}
                for tag_id, value in raw_exif.items():
                    tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                    # Skip binary blobs (MakerNote, thumbnail bytes, etc.)
                    if isinstance(value, (str, int, float, tuple)):
                        exif_data[tag_name] = str(value)
            except AttributeError:
                pass  # not a JPEG or no EXIF

            lines = [f"## Image Info: `{info['file']}`\n"]
            for k, v in info.items():
                lines.append(f"- **{k}:** {v}")
            if exif_data:
                lines.append("\n### EXIF")
                for ek, ev in exif_data.items():
                    lines.append(f"- **{ek}:** {ev}")
            return "\n".join(lines)

    except Exception as e:
        return f"❌ Could not read `{os.path.basename(file_path)}`: {e}"


@mcp.tool()
def strip_metadata(file_patterns: List[str], output_directory: str) -> str:
    """
    Remove all EXIF/IPTC/XMP metadata from images and save clean copies.
    Useful for privacy before publishing images. Output is re-encoded at
    high quality to avoid double-compression artefacts.

    Args:
        file_patterns:    Files or glob patterns to process.
        output_directory: Where to save the metadata-free copies.
    """
    target_files = expand_paths(file_patterns)
    if not target_files:
        return "No valid images found."

    os.makedirs(output_directory, exist_ok=True)
    success_log, error_log = [], []

    for file_path in target_files:
        if not file_path.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        try:
            with Image.open(file_path) as img:
                # Paste into a fresh image to drop all metadata from info dict
                clean = Image.new(img.mode, img.size)
                clean.paste(img)
                ext = os.path.splitext(file_path)[1].lower().lstrip(".")
                if ext == "jpg":
                    ext = "jpeg"
                save_path = os.path.join(output_directory, os.path.basename(file_path))
                clean.save(save_path, format=ext.upper(), quality=95, optimize=True)
                success_log.append(f"✅ {os.path.basename(file_path)} — metadata stripped")
        except Exception as e:
            error_log.append(f"❌ {os.path.basename(file_path)}: {e}")

    return "\n".join([
        "## Strip Metadata Report",
        f"**Status:** {len(success_log)} succeeded, {len(error_log)} failed.",
        f"**Destination:** `{output_directory}`\n",
        *success_log, *error_log,
    ])


@mcp.tool()
def find_duplicates(directory: str, threshold: int = 5) -> str:
    """
    Find near-identical images using perceptual hashing (average hash).
    Groups visually similar files even if filenames or sizes differ.

    Args:
        directory:  Directory to scan.
        threshold:  Max hash distance to consider two images duplicates.
                    Lower = stricter. 0 = pixel-perfect only. Default 5.
    """
    if not os.path.isdir(directory):
        return f"Directory not found: {directory}"

    files = image_files_in(directory)
    if not files:
        return "No images found."

    hashes: List[tuple] = []
    errors: List[str]   = []

    for f in files:
        try:
            with Image.open(f) as img:
                hashes.append((f, imagehash.average_hash(img)))
        except Exception as e:
            errors.append(f"❌ {os.path.basename(f)}: {e}")

    groups: List[List[str]] = []
    used: set = set()

    for i, (path_a, hash_a) in enumerate(hashes):
        if path_a in used:
            continue
        group = [path_a]
        for path_b, hash_b in hashes[i + 1:]:
            if path_b not in used and abs(hash_a - hash_b) <= threshold:
                group.append(path_b)
                used.add(path_b)
        if len(group) > 1:
            used.add(path_a)
            groups.append(group)

    if not groups:
        return f"No duplicates found in `{directory}` (threshold={threshold})."

    lines = [
        f"## Duplicate Report — `{directory}`",
        f"Found **{len(groups)}** duplicate group(s) (threshold={threshold}):\n",
    ]
    for i, group in enumerate(groups, 1):
        lines.append(f"### Group {i}")
        for f in group:
            lines.append(f"- {os.path.basename(f)}  ({size_label(f)})")
    if errors:
        lines += ["\n### Errors", *errors]

    return "\n".join(lines)


# ─── Tools: Transformations ───────────────────────────────────────────────────

@mcp.tool()
def bulk_compress(
    file_patterns: List[str],
    output_directory: str,
    output_format: str = "webp",
    quality: int = 80,
    lossless: bool = False,
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
) -> str:
    """
    Bulk compress, convert, and optionally resize images using compressfile.pro.
    EXIF orientation is auto-corrected on every file before processing.

    Args:
        file_patterns:    Files or glob patterns (e.g., ["/data/*.png"]).
        output_directory: Where to save the optimized files.
        output_format:    Target format: 'avif', 'webp', 'jpeg', or 'png'. Default 'webp'.
        quality:          Compression quality 1–100 (ignored for lossless). Default 80.
        lossless:         Use lossless compression (WebP/PNG only). Default False.
        max_width:        Resize to this max width in pixels, preserving aspect ratio.
        max_height:       Resize to this max height in pixels, preserving aspect ratio.
    """
    target_ext = normalise_format(output_format)
    if target_ext is None:
        return (
            f"Unsupported format '{output_format}'. "
            f"Choose one of: {', '.join(SUPPORTED_FORMATS)}."
        )

    target_files = expand_paths(file_patterns)
    if not target_files:
        return "No valid images found. Check your paths and volume mounts."

    os.makedirs(output_directory, exist_ok=True)
    save_kwargs   = build_save_kwargs(target_ext, quality, lossless)
    should_resize = max_width is not None or max_height is not None
    resize_box    = (max_width or 99999, max_height or 99999)
    success_log, error_log = [], []

    for file_path in target_files:
        if not file_path.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        try:
            with Image.open(file_path) as img:
                img = apply_exif_rotation(img)
                if should_resize:
                    img.thumbnail(resize_box, Image.LANCZOS)
                proc_img = prepare_image(img, target_ext)

                base_name    = os.path.splitext(os.path.basename(file_path))[0]
                out_filename = f"{base_name}.{target_ext}"
                save_path    = os.path.join(output_directory, out_filename)
                proc_img.save(save_path, format=target_ext.upper(), **save_kwargs)

                orig_kb = os.path.getsize(file_path) / 1024
                new_kb  = os.path.getsize(save_path) / 1024
                pct     = (1 - new_kb / orig_kb) * 100 if orig_kb else 0
                success_log.append(
                    f"✅ {os.path.basename(file_path)} → {out_filename} "
                    f"({orig_kb:.1f} KB → {new_kb:.1f} KB, -{pct:.0f}%)"
                )
        except Exception as e:
            error_log.append(f"❌ {os.path.basename(file_path)}: {e}")

    return "\n".join([
        "## CompressFile.pro Bulk Report",
        f"**Format:** {target_ext.upper()}  |  "
        f"**Quality:** {'lossless' if lossless else quality}  |  "
        f"**Resize:** {f'{max_width}×{max_height}px max' if should_resize else 'none'}",
        f"**Status:** {len(success_log)} succeeded, {len(error_log)} failed.",
        f"**Destination:** `{output_directory}`\n",
        *success_log, *error_log,
    ])


@mcp.tool()
def bulk_resize(
    file_patterns: List[str],
    output_directory: str,
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
) -> str:
    """
    Resize images without changing format or applying lossy compression.
    Aspect ratio is always preserved. EXIF orientation is auto-corrected first.

    Args:
        file_patterns:    Files or glob patterns to process.
        output_directory: Where to save resized files.
        max_width:        Maximum output width in pixels.
        max_height:       Maximum output height in pixels.
    """
    if max_width is None and max_height is None:
        return "Provide at least one of max_width or max_height."

    target_files = expand_paths(file_patterns)
    if not target_files:
        return "No valid images found."

    os.makedirs(output_directory, exist_ok=True)
    resize_box = (max_width or 99999, max_height or 99999)
    success_log, error_log = [], []

    for file_path in target_files:
        if not file_path.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        try:
            with Image.open(file_path) as img:
                img = apply_exif_rotation(img)
                orig_w, orig_h = img.size
                img.thumbnail(resize_box, Image.LANCZOS)
                new_w, new_h = img.size
                out_name  = os.path.basename(file_path)
                save_path = os.path.join(output_directory, out_name)
                fmt = img.format or os.path.splitext(file_path)[1].lstrip(".").upper()
                img.save(save_path, format=fmt)
                success_log.append(
                    f"✅ {out_name}  {orig_w}×{orig_h} → {new_w}×{new_h}"
                )
        except Exception as e:
            error_log.append(f"❌ {os.path.basename(file_path)}: {e}")

    return "\n".join([
        "## Bulk Resize Report",
        f"**Bound:** {max_width or '∞'}×{max_height or '∞'} px",
        f"**Status:** {len(success_log)} succeeded, {len(error_log)} failed.",
        f"**Destination:** `{output_directory}`\n",
        *success_log, *error_log,
    ])


@mcp.tool()
def bulk_crop(
    file_patterns: List[str],
    output_directory: str,
    x: int,
    y: int,
    width: int,
    height: int,
) -> str:
    """
    Crop a rectangular region from each image in a batch.
    Coordinates are in pixels from the top-left corner of the image.
    Files where the crop box exceeds the image bounds are skipped with a warning.

    Args:
        file_patterns:    Files or glob patterns to process.
        output_directory: Where to save cropped files.
        x:      Left edge of the crop box (pixels from left).
        y:      Top edge of the crop box (pixels from top).
        width:  Width of the crop region in pixels.
        height: Height of the crop region in pixels.
    """
    target_files = expand_paths(file_patterns)
    if not target_files:
        return "No valid images found."

    os.makedirs(output_directory, exist_ok=True)
    box = (x, y, x + width, y + height)
    success_log, error_log = [], []

    for file_path in target_files:
        if not file_path.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        try:
            with Image.open(file_path) as img:
                img = apply_exif_rotation(img)
                if x + width > img.width or y + height > img.height:
                    error_log.append(
                        f"⚠️ {os.path.basename(file_path)}: crop box "
                        f"({x},{y}→{x+width},{y+height}) exceeds image "
                        f"{img.width}×{img.height} — skipped"
                    )
                    continue
                cropped   = img.crop(box)
                out_name  = os.path.basename(file_path)
                save_path = os.path.join(output_directory, out_name)
                fmt = img.format or os.path.splitext(file_path)[1].lstrip(".").upper()
                cropped.save(save_path, format=fmt)
                success_log.append(f"✅ {out_name}  cropped to {width}×{height}")
        except Exception as e:
            error_log.append(f"❌ {os.path.basename(file_path)}: {e}")

    return "\n".join([
        "## Bulk Crop Report",
        f"**Box:** x={x}, y={y}, {width}×{height} px",
        f"**Status:** {len(success_log)} succeeded, {len(error_log)} failed.",
        f"**Destination:** `{output_directory}`\n",
        *success_log, *error_log,
    ])


@mcp.tool()
def bulk_rotate(
    file_patterns: List[str],
    output_directory: str,
    degrees: Optional[int] = None,
    auto_exif: bool = True,
) -> str:
    """
    Rotate images by a fixed number of degrees and/or auto-correct their EXIF
    orientation tag. EXIF correction is applied first when both are requested.

    Args:
        file_patterns:    Files or glob patterns to process.
        output_directory: Where to save rotated files.
        degrees:   Clockwise rotation in degrees (e.g. 90, 180, 270).
                   None = no manual rotation (use with auto_exif=True).
        auto_exif: Auto-correct orientation from EXIF tag. Default True.
    """
    if degrees is None and not auto_exif:
        return "Provide degrees and/or set auto_exif=True."

    target_files = expand_paths(file_patterns)
    if not target_files:
        return "No valid images found."

    os.makedirs(output_directory, exist_ok=True)
    success_log, error_log = [], []

    for file_path in target_files:
        if not file_path.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        try:
            with Image.open(file_path) as img:
                if auto_exif:
                    img = apply_exif_rotation(img)
                if degrees:
                    # PIL rotates counter-clockwise; negate to get clockwise
                    img = img.rotate(-degrees, expand=True)
                out_name  = os.path.basename(file_path)
                save_path = os.path.join(output_directory, out_name)
                fmt = img.format or os.path.splitext(file_path)[1].lstrip(".").upper()
                img.save(save_path, format=fmt)
                action = []
                if auto_exif:
                    action.append("EXIF corrected")
                if degrees:
                    action.append(f"rotated {degrees}°CW")
                success_log.append(f"✅ {out_name}  ({', '.join(action)})")
        except Exception as e:
            error_log.append(f"❌ {os.path.basename(file_path)}: {e}")

    return "\n".join([
        "## Bulk Rotate Report",
        f"**Status:** {len(success_log)} succeeded, {len(error_log)} failed.",
        f"**Destination:** `{output_directory}`\n",
        *success_log, *error_log,
    ])


@mcp.tool()
def bulk_watermark(
    file_patterns: List[str],
    output_directory: str,
    text: str,
    position: str = "bottom-right",
    opacity: int = 128,
    font_size: int = 36,
) -> str:
    """
    Stamp a text watermark onto each image in a batch.

    Args:
        file_patterns:    Files or glob patterns to process.
        output_directory: Where to save watermarked files.
        text:      Watermark text (e.g., "© compressfile.pro").
        position:  One of: top-left, top-right, bottom-left, bottom-right, center.
        opacity:   Watermark opacity 0 (invisible) – 255 (solid). Default 128.
        font_size: Font size in points. Default 36.
    """
    POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right", "center"}
    if position not in POSITIONS:
        return f"Invalid position '{position}'. Choose from: {', '.join(sorted(POSITIONS))}."

    target_files = expand_paths(file_patterns)
    if not target_files:
        return "No valid images found."

    os.makedirs(output_directory, exist_ok=True)
    success_log, error_log = [], []

    for file_path in target_files:
        if not file_path.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        try:
            with Image.open(file_path) as img:
                img = apply_exif_rotation(img)
                base  = img.convert("RGBA")
                layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
                draw  = ImageDraw.Draw(layer)

                try:
                    font = ImageFont.truetype("arial.ttf", font_size)
                except (IOError, OSError):
                    font = ImageFont.load_default()

                bbox   = draw.textbbox((0, 0), text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                margin = 10
                iw, ih = base.size

                coords = {
                    "top-left":     (margin,          margin),
                    "top-right":    (iw - tw - margin, margin),
                    "bottom-left":  (margin,           ih - th - margin),
                    "bottom-right": (iw - tw - margin, ih - th - margin),
                    "center":       ((iw - tw) // 2,   (ih - th) // 2),
                }[position]

                draw.text(coords, text, font=font, fill=(255, 255, 255, opacity))
                composite = Image.alpha_composite(base, layer)

                out_name  = os.path.basename(file_path)
                save_path = os.path.join(output_directory, out_name)
                if img.mode != "RGBA":
                    composite = composite.convert(img.mode)
                fmt = img.format or os.path.splitext(file_path)[1].lstrip(".").upper()
                composite.save(save_path, format=fmt)
                success_log.append(f"✅ {out_name}")
        except Exception as e:
            error_log.append(f"❌ {os.path.basename(file_path)}: {e}")

    return "\n".join([
        "## Bulk Watermark Report",
        f"**Text:** \"{text}\"  |  **Position:** {position}  |  **Opacity:** {opacity}/255",
        f"**Status:** {len(success_log)} succeeded, {len(error_log)} failed.",
        f"**Destination:** `{output_directory}`\n",
        *success_log, *error_log,
    ])


# ─── Tools: Reporting & Agent Helpers ─────────────────────────────────────────

@mcp.tool()
def compression_report(source: str, output: str) -> str:
    """
    Aggregate compression statistics across two folders: total bytes saved,
    average reduction percentage, and a per-format breakdown.
    Run this after a bulk_compress job to see overall results.

    Args:
        source: Original images directory.
        output: Compressed images directory.
    """
    if not os.path.isdir(source):
        return f"Source directory not found: {source}"
    if not os.path.isdir(output):
        return f"Output directory not found: {output}"

    src_map = {
        os.path.splitext(os.path.basename(f))[0]: f
        for f in image_files_in(source)
    }
    out_map = {
        os.path.splitext(os.path.basename(f))[0]: f
        for f in image_files_in(output)
    }
    matched = [(src_map[k], out_map[k]) for k in src_map if k in out_map]
    if not matched:
        return "No matching files found between source and output."

    total_orig = total_new = 0.0
    by_format: dict = {}
    rows: List[str] = []

    for src, out in matched:
        orig_kb = os.path.getsize(src) / 1024
        new_kb  = os.path.getsize(out) / 1024
        pct     = (1 - new_kb / orig_kb) * 100 if orig_kb else 0
        total_orig += orig_kb
        total_new  += new_kb
        ext = os.path.splitext(out)[1].lower()
        d = by_format.setdefault(ext, {"count": 0, "orig": 0.0, "new": 0.0})
        d["count"] += 1
        d["orig"]  += orig_kb
        d["new"]   += new_kb
        rows.append(
            f"- {os.path.basename(out)}: {orig_kb:.1f} → {new_kb:.1f} KB ({pct:.0f}% saved)"
        )

    total_pct   = (1 - total_new / total_orig) * 100 if total_orig else 0
    total_saved = (total_orig - total_new) / 1024

    lines = [
        "## Compression Report",
        f"**Files matched:** {len(matched)}",
        f"**Total:** {total_orig:.1f} KB → {total_new:.1f} KB  "
        f"(**{total_pct:.1f}% saved**, {total_saved:.2f} MB freed)\n",
        "### By Format",
    ]
    for ext, d in by_format.items():
        fp = (1 - d["new"] / d["orig"]) * 100 if d["orig"] else 0
        lines.append(f"- **{ext}** ({d['count']} files): {fp:.1f}% avg reduction")
    lines += ["\n### Per-File", *rows]
    return "\n".join(lines)


@mcp.tool()
def preview_settings(
    file_path: str,
    output_directory: str,
    output_format: str = "webp",
    quality_levels: Optional[List[int]] = None,
) -> str:
    """
    Compress a single image at multiple quality levels so you can compare
    size vs quality before committing to a full bulk job.

    Args:
        file_path:        Single image to test.
        output_directory: Where to save the preview variants.
        output_format:    Target format. Default 'webp'.
        quality_levels:   List of quality values to test. Default [60, 75, 85, 95].
    """
    if quality_levels is None:
        quality_levels = [60, 75, 85, 95]

    if not os.path.isfile(file_path):
        return f"File not found: {file_path}"

    target_ext = normalise_format(output_format)
    if target_ext is None:
        return f"Unsupported format '{output_format}'."

    os.makedirs(output_directory, exist_ok=True)
    orig_kb = os.path.getsize(file_path) / 1024
    base    = os.path.splitext(os.path.basename(file_path))[0]
    rows    = [f"**Original:** {orig_kb:.1f} KB\n"]

    with Image.open(file_path) as img:
        img      = apply_exif_rotation(img)
        proc_img = prepare_image(img, target_ext)

        for q in quality_levels:
            kwargs    = build_save_kwargs(target_ext, q, False)
            out_name  = f"{base}_q{q}.{target_ext}"
            save_path = os.path.join(output_directory, out_name)
            proc_img.save(save_path, format=target_ext.upper(), **kwargs)
            new_kb = os.path.getsize(save_path) / 1024
            pct    = (1 - new_kb / orig_kb) * 100 if orig_kb else 0
            rows.append(f"- **q={q}:** {new_kb:.1f} KB  (-{pct:.0f}%)")

    return "\n".join([
        f"## Preview Settings: `{os.path.basename(file_path)}`",
        f"**Format:** {target_ext.upper()}  |  **Destination:** `{output_directory}`\n",
        *rows,
    ])


@mcp.tool()
def suggest_format(file_patterns: List[str]) -> str:
    """
    Analyse a set of images and recommend the best output format for each,
    based on content type (photo vs graphic), transparency, and current format.
    Call this before bulk_compress when unsure which format to pick.

    Args:
        file_patterns: Files or glob patterns to analyse.
    """
    target_files = expand_paths(file_patterns)
    if not target_files:
        return "No valid images found."

    lines = ["## Format Suggestions\n"]

    for file_path in target_files:
        if not file_path.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        try:
            with Image.open(file_path) as img:
                has_alpha = (
                    img.mode in ("RGBA", "LA", "PA")
                    or (img.mode == "P" and "transparency" in img.info)
                )
                is_photo = (
                    os.path.splitext(file_path)[1].lower() in PHOTO_EXTENSIONS
                    or img.format in ("JPEG",)
                )
                # Cheap heuristic: try to quantise to ≤256 colours
                try:
                    colours = img.convert("RGB").quantize(colors=256).getcolors()
                    is_graphic = colours is not None and len(colours) <= 256
                except Exception:
                    is_graphic = False

                if has_alpha:
                    rec    = "avif"
                    reason = "has transparency — AVIF gives best size with alpha support"
                elif is_photo:
                    rec    = "avif"
                    reason = "photographic content — AVIF gives best compression ratio"
                elif is_graphic:
                    rec    = "png"
                    reason = "flat graphic / few colours — PNG lossless preserves quality"
                else:
                    rec    = "webp"
                    reason = "mixed content — WebP is a safe universal choice"

                lines.append(
                    f"- **{os.path.basename(file_path)}** → `{rec}`  _{reason}_"
                )
        except Exception as e:
            lines.append(f"- ❌ {os.path.basename(file_path)}: {e}")

    return "\n".join(lines)


@mcp.tool()
def estimate_savings(
    file_patterns: List[str],
    output_format: str = "webp",
    quality: int = 80,
) -> str:
    """
    Dry-run: compress images into memory and predict output sizes and savings
    without writing any files to disk.

    Args:
        file_patterns: Files or glob patterns to analyse.
        output_format: Target format for the estimate. Default 'webp'.
        quality:       Quality level for the estimate. Default 80.
    """
    target_ext = normalise_format(output_format)
    if target_ext is None:
        return f"Unsupported format '{output_format}'."

    target_files = expand_paths(file_patterns)
    if not target_files:
        return "No valid images found."

    save_kwargs = build_save_kwargs(target_ext, quality, False)
    total_orig = total_est = 0.0
    rows, errors = [], []

    for file_path in target_files:
        if not file_path.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        try:
            orig_kb = os.path.getsize(file_path) / 1024
            with Image.open(file_path) as img:
                img  = apply_exif_rotation(img)
                proc = prepare_image(img, target_ext)
                buf  = io.BytesIO()
                proc.save(buf, format=target_ext.upper(), **save_kwargs)
                est_kb = buf.tell() / 1024

            pct = (1 - est_kb / orig_kb) * 100 if orig_kb else 0
            total_orig += orig_kb
            total_est  += est_kb
            rows.append(
                f"- {os.path.basename(file_path)}: "
                f"{orig_kb:.1f} KB → ~{est_kb:.1f} KB  (-{pct:.0f}%)"
            )
        except Exception as e:
            errors.append(f"❌ {os.path.basename(file_path)}: {e}")

    total_pct   = (1 - total_est / total_orig) * 100 if total_orig else 0
    total_saved = (total_orig - total_est) / 1024

    return "\n".join([
        "## Estimated Savings  (dry-run — no files written)",
        f"**Format:** {target_ext.upper()}  |  **Quality:** {quality}",
        f"**Total:** {total_orig:.1f} KB → ~{total_est:.1f} KB  "
        f"(**~{total_pct:.1f}% saved**, ~{total_saved:.2f} MB freed)\n",
        *rows, *errors,
    ])


if __name__ == "__main__":
    mcp.run()
