# mcp-compressfile

> Official [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for [compressfile.pro](https://compressfile.pro).  
> Give any MCP-compatible AI agent (Claude, Cursor, etc.) the ability to bulk compress, convert, resize, watermark, and analyse images — entirely on your local machine.

---

## ✨ Features

- **15 AI tools** covering the full image optimisation workflow
- **4 modern formats** — AVIF, WebP, JPEG, PNG
- **Lossy & lossless** compression with per-format tuned parameters
- **Bulk operations** via glob patterns (`/data/**/*.png`)
- **Transparency handling** — alpha composited correctly for every format
- **EXIF auto-rotation** applied before any transformation
- **Perceptual duplicate detection** using average hashing
- **Dry-run estimation** — predict savings before writing any files
- **Privacy first** — your images never leave your machine

---

## 🌐 No Installation? Use the Web Version

Don't need AI integration? **[compressfile.pro](https://compressfile.pro)** offers the same compression and conversion directly in your browser — no upload, no account, no install required.

| | [compressfile.pro](https://compressfile.pro) | mcp-compressfile |
|---|---|---|
| Works in browser | ✅ | — |
| No install needed | ✅ | — |
| AI agent integration | — | ✅ |
| Bulk via glob patterns | — | ✅ |
| Runs on local files | — | ✅ |
| Images leave your machine | ❌ Never | ❌ Never |

---

## 🛠 Installation

### Option A — Python (recommended)

```bash
pip install mcp[server] Pillow pillow-avif-plugin imagehash
```

Clone this repo and point Claude Desktop at `server.py` (see [Configuration](#configuration)).

### Option B — Docker

```bash
git clone https://github.com/yourname/mcp-compressfile
cd mcp-compressfile
docker build -t mcp-compressfile .
```

---

## ⚙️ Configuration

Open Claude Desktop → **Settings → Developer → Edit Config** and add one of the blocks below.

### Python

```json
{
  "mcpServers": {
    "mcp-compressfile": {
      "command": "python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

### Docker

```json
{
  "mcpServers": {
    "mcp-compressfile": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/your/images/folder:/data",
        "mcp-compressfile"
      ]
    }
  }
}
```

> **Windows users:** use forward slashes in paths (`C:/Users/You/Pictures`).  
> With Docker, pass `/data/...` paths to the tools — that is the mount point inside the container.

Restart Claude Desktop after editing the config. A 🔨 hammer icon in the chat input confirms the tools are loaded.

---

## 🤖 How to Use in Claude

Once configured, just describe what you want in plain English. Claude picks the right tools, chains them automatically, and reports back with a summary.

**1. Type a prompt — Claude identifies the files and plans the job**

![Claude identifying JPEG files and planning compression](/1.png)

**2. Claude executes the batch and returns a full summary**

![Claude compression results showing 7 files, 4.4 MB → 657 KB, ~85% savings](/2.png)

> In this example, a single prompt converted 7 JPEGs to WebP at quality 80,  
> reducing total size from **4.4 MB to 657 KB** — an **85% reduction**.

---

## 🧰 Tools

### File & Folder

| Tool | Description |
|---|---|
| `list_files` | List all supported images in a directory with file sizes |
| `find_large_images` | Find images above a KB threshold — ideal starting point for a compression job |
| `compare_folders` | Diff source vs output to see what's been processed and what's still pending |
| `undo_last_batch` | Delete an output directory to retry with different settings (`confirm=True` required) |

### Analysis & Metadata

| Tool | Description |
|---|---|
| `get_image_info` | Full metadata for a single file: dimensions, DPI, mode, and all EXIF fields |
| `strip_metadata` | Remove all EXIF/IPTC/XMP data for privacy before publishing |
| `find_duplicates` | Group near-identical images using perceptual hashing |

### Transformations

| Tool | Description |
|---|---|
| `bulk_compress` | Compress + convert + optional resize; EXIF orientation auto-corrected |
| `bulk_resize` | Resize only, preserving format and aspect ratio |
| `bulk_crop` | Crop a fixed pixel region from a batch; skips files where box exceeds bounds |
| `bulk_rotate` | Rotate by degrees and/or auto-correct EXIF orientation |
| `bulk_watermark` | Stamp text onto images with configurable position, opacity, and font size |

### Reporting & Agent Helpers

| Tool | Description |
|---|---|
| `compression_report` | Aggregate stats after a job: total MB freed, per-format breakdown, per-file log |
| `preview_settings` | Compress one image at multiple quality levels to find the best trade-off |
| `suggest_format` | Analyse images and recommend the best output format with reasoning |
| `estimate_savings` | Dry-run prediction across a batch — no files written to disk |

---

## 💬 Example Prompts

```
List all images in /data and tell me which ones are larger than 1 MB.
```

```
Suggest the best format for all files in /data/photos.
```

```
Estimate how much space I'd save converting /data/*.jpg to AVIF at quality 80.
```

```
Compress everything in /data/photos to WebP at quality 75, max 1920px wide,
and save to /data/output.
```

```
Find duplicate images in /data/vacation.
```

```
Add a "© compressfile.pro" watermark to all PNGs in /data, bottom-right, 50% opacity.
```

```
Show me a compression preview for /data/hero.jpg at quality 60, 75, 85, and 95.
```

```
Generate a full compression report comparing /data/originals and /data/output.
```

---

## 📦 Supported Formats

| Format | Lossy | Lossless | Transparency | Notes |
|---|---|---|---|---|
| AVIF | ✅ | — | ✅ | Best compression ratio for photos |
| WebP | ✅ | ✅ | ✅ | Best universal format |
| JPEG | ✅ | — | — | Alpha composited onto white background |
| PNG | — | ✅ | ✅ | Best for graphics and flat illustrations |

**Supported input types:** `.png` `.jpg` `.jpeg` `.webp` `.tiff` `.bmp` `.avif`

---

## 🔒 Privacy

All processing runs locally via Pillow. No image data is uploaded to any server.  
This MCP server is a local companion to [compressfile.pro](https://compressfile.pro), which provides the same functionality in-browser without any server upload.

---

## 📋 Requirements

```
mcp[server]
Pillow
pillow-avif-plugin
imagehash
```

Python 3.9 or later.

---

## 📄 License

MIT
