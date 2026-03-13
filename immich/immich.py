import json
import logging
import re
from io import BytesIO
from pathlib import Path
from random import choice
import requests

from flask import jsonify, request, current_app
from PIL import Image, ImageColor, ImageDraw, ImageEnhance, ImageFont, ImageOps
from PIL.IptcImagePlugin import getiptcinfo

from blueprints.plugin import plugin_bp
from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session
from utils.image_utils import pad_image_blur, apply_image_enhancement

logger = logging.getLogger(__name__)

FONT_PATH = Path(__file__).parent / "OpenSans-VariableFont_wdth,wght.ttf"
LUT_FILE = Path(__file__).parent / "lut.json"
CAPTION_PATTERN = re.compile(r"\[([^\]]+)\]")

def normalize_base_url(raw: str | None) -> str:
    v = (raw or "").strip()
    if not v:
        return ""
    v = v.rstrip("/")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", v):
        return v
    return "http://" + v

def load_lut_list() -> list[dict]:
    """Load LUT entries from lut.json."""
    try:
        if LUT_FILE.exists():
            with LUT_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Failed to load lut.json: {e}")
    return []

def find_lut_by_name(lut_name: str) -> dict | None:
    """Find a LUT entry by its lut_name."""
    if not lut_name:
        return None
    for entry in load_lut_list():
        if entry.get("lut_name") == lut_name:
            return entry
    return None

def apply_channel_adjust(img: Image.Image, channel_adjust: dict) -> Image.Image:
    """Apply per-channel brightness multipliers (R/G/B)."""
    if img.mode != "RGB":
        img = img.convert("RGB")

    r, g, b = img.split()

    red_mult = channel_adjust.get("red", 1.0)
    green_mult = channel_adjust.get("green", 1.0)
    blue_mult = channel_adjust.get("blue", 1.0)

    if red_mult != 1.0:
        r = ImageEnhance.Brightness(r).enhance(red_mult)
    if green_mult != 1.0:
        g = ImageEnhance.Brightness(g).enhance(green_mult)
    if blue_mult != 1.0:
        b = ImageEnhance.Brightness(b).enhance(blue_mult)

    return Image.merge("RGB", (r, g, b))

def apply_palette_quantize(img: Image.Image, palette: dict) -> Image.Image:
    """Quantize image to a custom color palette using Floyd-Steinberg dithering."""
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Pre-process: boost contrast and saturation for better palette mapping
    img = ImageEnhance.Contrast(img).enhance(1.2)
    img = ImageEnhance.Color(img).enhance(1.3)

    # Build palette data from the color dict
    palette_colors = []
    for color_name in ("black", "white", "red", "yellow", "green", "blue"):
        rgb = palette.get(color_name)
        if rgb and len(rgb) == 3:
            palette_colors.extend(rgb)

    if not palette_colors:
        logger.warning("No valid palette colors found, skipping quantization.")
        return img

    # Pad palette to 256 colors (PIL requirement: 768 bytes)
    while len(palette_colors) < 768:
        palette_colors.extend([0, 0, 0])

    # Create palette image
    palette_img = Image.new("P", (1, 1))
    palette_img.putpalette(palette_colors)

    # Quantize with Floyd-Steinberg dithering
    img = img.quantize(palette=palette_img, dither=Image.Dither.FLOYDSTEINBERG)
    img = img.convert("RGB")

    return img

def apply_lut(img: Image.Image, lut: dict) -> Image.Image:
    """Apply LUT color adjustments to an image."""
    # Step 1: Apply channel adjustments (red/green/blue multipliers)
    channel_adjust = lut.get("channel_adjust")
    if channel_adjust:
        img = apply_channel_adjust(img, channel_adjust)

    # Step 2: Apply palette quantization if enabled
    palette = lut.get("palette")
    quantize = lut.get("quantize", 0)
    if palette and quantize:
        img = apply_palette_quantize(img, palette)

    return img

def extract_iptc_caption_from_bytes(data: bytes) -> str | None:
    try:
        with Image.open(BytesIO(data)) as img:
            iptc = getiptcinfo(img)
            if not iptc:
                return None
            raw = iptc.get((2, 120))
            if not raw:
                return None
            text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
            if text.strip().lower() == "none":
                return None
            match = CAPTION_PATTERN.search(text)
            if match:
                return match.group(1).strip()
    except Exception as e:
        logger.warning(f"Failed to extract IPTC caption: {e}")
    return None

def draw_caption(img: Image.Image, caption: str) -> Image.Image:
    font_size = max(12, int(img.height * 0.06))
    print(
    f"[Immich Caption] original image size={img.width}x{img.height} | "
    f"display size={img.width}x{img.height} | "
    f"font size={font_size} | "
    f"caption text='{caption}'"
)
    try:
        font = ImageFont.truetype(str(FONT_PATH), font_size)
        font.set_variation_by_axes([600, 100])
    except Exception as e:
        logger.warning(f"Could not load Open-Sans font, falling back to default: {e}")
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(img, "RGBA")

    x_padding = int(img.size[0] * 0.04)
    y_padding = int(img.size[0] * 0.02)
    bbox = draw.textbbox((0, 0), caption, font=font)
    text_h = bbox[3] - bbox[1]

    x = x_padding
    y = img.size[1] - text_h - y_padding * 2

    # Black outline (1px)
    outline = 1
    # Black outline (4 cardinal directions only)
    for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        draw.text((x + dx, y + dy), caption, font=font, fill=(0, 0, 0, 255))

    # White fill
    draw.text((x, y), caption, font=font, fill=(255, 255, 255, 255))

    return img

@plugin_bp.route("/plugin/<plugin_id>/immich_albums", methods=["GET"])
def immich_albums(plugin_id):
    device_config = current_app.config["DEVICE_CONFIG"]

    base_url = normalize_base_url(request.args.get("base_url"))
    if not base_url:
        return jsonify({"ok": False, "error": "Please enter a valid Immich Base URL."}), 400

    key = device_config.load_env_key("IMMICH_KEY")
    if not key:
        return jsonify({"ok": False, "error": "Immich API Key not configured."}), 500

    sess = requests.Session()
    try:
        r = sess.get(
            f"{base_url}/api/albums",
            headers={"x-api-key": key},
            timeout=5
        )
    except (requests.exceptions.SSLError,
            requests.exceptions.MissingSchema,
            requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidURL,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout):
        return jsonify({"ok": False, "error": "Please enter a valid Immich Base URL."}), 400

    if r.status_code in (401, 403):
        return jsonify({"ok": False, "error": "Unauthorized (bad Immich API Key)."}), r.status_code
    if r.status_code == 404:
        return jsonify({"ok": False, "error": "Please enter a valid Immich Base URL."}), 404
    if r.status_code != 200:
        return jsonify({"ok": False, "error": "Failed to retrieve Immich albums."}), 502

    albums = r.json() or []
    out = []
    for a in albums:
        album_id = a.get("id")
        name = a.get("albumName")
        if album_id and name:
            out.append({"id": album_id, "name": name})

    out.sort(key=lambda x: x["name"].lower())
    return jsonify({"ok": True, "albums": out})

@plugin_bp.route("/plugin/<plugin_id>/immich_tags", methods=["GET"])
def immich_tags(plugin_id):
    device_config = current_app.config["DEVICE_CONFIG"]

    base_url = normalize_base_url(request.args.get("base_url"))
    if not base_url:
        return jsonify({"ok": False, "error": "Please enter a valid Immich Base URL."}), 400

    key = device_config.load_env_key("IMMICH_KEY")
    if not key:
        return jsonify({"ok": False, "error": "Immich API Key not configured."}), 500

    sess = requests.Session()
    try:
        r = sess.get(
            f"{base_url}/api/tags",
            headers={"x-api-key": key},
            timeout=5
        )
    except (requests.exceptions.SSLError,
            requests.exceptions.MissingSchema,
            requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidURL,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout):
        return jsonify({"ok": False, "error": "Please enter a valid Immich Base URL."}), 400

    if r.status_code in (401, 403):
        return jsonify({"ok": False, "error": "Unauthorized (bad Immich API Key)."}), r.status_code
    if r.status_code == 404:
        return jsonify({"ok": False, "error": "Please enter a valid Immich Base URL."}), 404
    if r.status_code != 200:
        return jsonify({"ok": False, "error": "Failed to retrieve Immich tags."}), 502

    tags = r.json() or []
    out = sorted({
        (t.get("name") or t.get("value") or "").strip()
        for t in tags
        if (t.get("name") or t.get("value") or "").strip()
    })
    return jsonify({"ok": True, "tags": out})

class ImmichProvider:
    def __init__(self, base_url: str, key: str, image_loader):
        self.base_url = normalize_base_url(base_url)
        self.key = key
        self.headers = {"x-api-key": self.key}
        self.image_loader = image_loader
        self.session = get_http_session()

    def get_album_id(self, album: str) -> str:
        r = self.session.get(f"{self.base_url}/api/albums", headers=self.headers, timeout=10)
        r.raise_for_status()
        albums = r.json() or []

        matching_albums = [a for a in albums if a.get("albumName") == album]
        if not matching_albums:
            raise RuntimeError(f"Album '{album}' not found.")

        return matching_albums[0]["id"]

    def get_assets(self, album_id: str) -> list[dict]:
        all_items: list[dict] = []
        page_items = [1]
        page = 1

        while page_items:
            body = {"albumIds": [album_id], "size": 1000, "page": page}
            r2 = self.session.post(
                f"{self.base_url}/api/search/metadata",
                json=body,
                headers=self.headers,
                timeout=20
            )
            r2.raise_for_status()
            assets_data = r2.json() or {}

            page_items = assets_data.get("assets", {}).get("items", []) or []
            all_items.extend(page_items)
            page += 1

        return all_items

    def get_tag_id(self, tag_name: str) -> str | None:
        tag_name = (tag_name or "").strip()
        if not tag_name:
            return None

        r = self.session.get(f"{self.base_url}/api/tags", headers=self.headers, timeout=10)
        r.raise_for_status()
        tags = r.json() or []

        wanted = tag_name.casefold()
        for t in tags:
            name = (t.get("name") or "").strip()
            value = (t.get("value") or "").strip()
            if name.casefold() == wanted or value.casefold() == wanted:
                return t.get("id")
        return None

    def get_assets_by_tag(self, album_id: str, tag_id: str) -> list[dict]:
        # Get all album assets
        album_assets = self.get_assets(album_id)
        album_ids = {a["id"] for a in album_assets}

        # Get all assets with the tag
        tag_assets: list[dict] = []
        page = 1

        while True:
            body = {"tagIds": [tag_id], "size": 1000, "page": page}
            r = self.session.post(
                f"{self.base_url}/api/search/metadata",
                json=body,
                headers=self.headers,
                timeout=20
            )
            r.raise_for_status()
            assets_data = r.json() or {}

            page_items = assets_data.get("assets", {}).get("items", []) or []
            if not page_items:
                break
            tag_assets.extend(page_items)
            page += 1

        # Return only assets that are in both
        return [a for a in tag_assets if a["id"] in album_ids]

    def get_asset_description(self, asset_id: str) -> str | None:
        try:
            r = self.session.get(
                f"{self.base_url}/api/assets/{asset_id}",
                headers=self.headers,
                timeout=10
            )
            r.raise_for_status()
            data = r.json() or {}
            exif = data.get("exifInfo") or {}
            desc = (exif.get("description") or "").strip()
            if not desc:
                return None
            match = CAPTION_PATTERN.search(desc)
            if match:
                return match.group(1).strip()
        except Exception as e:
            logger.warning(f"Failed to get asset description for {asset_id}: {e}")
        return None

    def fetch_raw_bytes(self, asset_url: str) -> bytes | None:
        try:
            r = self.session.get(asset_url, headers=self.headers, timeout=40)
            r.raise_for_status()
            return r.content
        except Exception as e:
            logger.error(f"Failed to fetch raw bytes from {asset_url}: {e}")
            return None

    def get_image(self, album: str, dimensions: tuple[int, int], resize: bool = True, tag_filter: str | None = None, show_captions: bool = False) -> Image.Image | None:
        try:
            album_id = self.get_album_id(album)
        except Exception as e:
            logger.error(f"Error retrieving album id from {self.base_url}: {e}")
            return None

        assets = None

        if tag_filter:
            try:
                tag_id = self.get_tag_id(tag_filter)
                if tag_id:
                    assets = self.get_assets_by_tag(album_id, tag_id)
                    if not assets:
                        logger.warning(f"No assets matched tag '{tag_filter}', falling back to unfiltered.")
                else:
                    logger.warning(f"Tag '{tag_filter}' not found, falling back to unfiltered.")
            except Exception as e:
                logger.error(f"Error applying tag filter '{tag_filter}': {e}")

        if not assets:
            try:
                assets = self.get_assets(album_id)
            except Exception as e:
                logger.error(f"Error retrieving assets from {self.base_url}: {e}")
                return None

        if not assets:
            logger.error(f"No assets found in album '{album}'")
            return None

        selected_asset = choice(assets)
        asset_id = selected_asset["id"]
        asset_url = f"{self.base_url}/api/assets/{asset_id}/original"

        caption = None
        if show_captions:
            # Step 1: Check IPTC metadata in the image file
            raw_bytes = self.fetch_raw_bytes(asset_url)
            if raw_bytes:
                caption = extract_iptc_caption_from_bytes(raw_bytes)

            # Step 2: Check Immich database description (overrides IPTC if found)
            immich_caption = self.get_asset_description(asset_id)
            if immich_caption:
                caption = immich_caption

            # Truncate to 35 characters
            if caption and len(caption) > 35:
                caption = caption[:35] + "..."

            if not caption:
                logger.info(f"No caption found for asset {asset_id}")

        img = self.image_loader.from_url(
            asset_url,
            dimensions,
            timeout_ms=40000,
            resize=resize,
            headers=self.headers
        )

        if not img:
            logger.error(f"Failed to load image {asset_id} from Immich")
            return None

        if caption:
            img = img.convert("RGBA")
            img = draw_caption(img, caption)
            img = img.convert("RGB")

        return img

class Immich(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["api_key"] = {
            "required": True,
            "service": "Immich",
            "expected_key": "IMMICH_KEY",
        }
        # Pass current system image settings to the template
        from flask import current_app
        device_config = current_app.config.get("DEVICE_CONFIG")
        if device_config:
            img_settings = device_config.get_config("image_settings") or {}
            template_params["system_image_settings"] = {
                "saturation": img_settings.get("saturation", 1.0),
                "brightness": img_settings.get("brightness", 1.0),
                "contrast": img_settings.get("contrast", 1.0),
                "sharpness": img_settings.get("sharpness", 1.0),
            }
        else:
            template_params["system_image_settings"] = {
                "saturation": 1.0, "brightness": 1.0,
                "contrast": 1.0, "sharpness": 1.0,
            }

        # Pass LUT options and data to the template
        lut_list = load_lut_list()
        template_params["lut_options"] = [
            {"value": entry.get("lut_name", ""), "label": entry.get("display name", entry.get("lut_name", ""))}
            for entry in lut_list
            if entry.get("lut_name")
        ]
        template_params["lut_data"] = {
            entry["lut_name"]: entry
            for entry in lut_list
            if entry.get("lut_name")
        }

        return template_params

    def generate_image(self, settings, device_config):
        orientation = device_config.get_config("orientation")
        dimensions = device_config.get_resolution()
        if orientation == "vertical":
            dimensions = dimensions[::-1]

        album_provider = settings.get("albumProvider") or "Immich"

        use_padding = settings.get("padImage") == "true"
        background_option = settings.get("backgroundOption", "blur")
        show_captions = settings.get("showCaptions") == "true"

        match album_provider:
            case "Immich":
                key = device_config.load_env_key("IMMICH_KEY")
                if not key:
                    raise RuntimeError("Immich API Key not configured.")

                url = normalize_base_url(settings.get("url"))
                if not url:
                    raise RuntimeError("Immich URL is required.")

                album = settings.get("album")
                if not album:
                    raise RuntimeError("Album name is required.")

                tag_filter = (settings.get("tagFilter") or "").strip() or None

                provider = ImmichProvider(url, key, self.image_loader)
                img = provider.get_image(
                    album,
                    dimensions,
                    resize=not use_padding,
                    tag_filter=tag_filter,
                    show_captions=show_captions
                )
                if not img:
                    raise RuntimeError("Failed to load image, please check logs.")
            case _:
                raise RuntimeError(f"Unsupported album provider: {album_provider}")

        if use_padding:
            if background_option == "blur":
                img = pad_image_blur(img, dimensions)
            else:
                background_color = ImageColor.getcolor(settings.get("backgroundColor") or "white", img.mode)
                img = ImageOps.pad(img, dimensions, color=background_color, method=Image.Resampling.LANCZOS)

        # Apply LUT color adjustments (channel adjust, palette quantize)
        lut = None
        lut_name = (settings.get("lut") or "").strip()
        if lut_name:
            lut = find_lut_by_name(lut_name)
            if lut:
                img = apply_lut(img, lut)
                logger.info(f"Applied LUT '{lut_name}'")
            else:
                logger.warning(f"LUT '{lut_name}' not found in lut.json")

        # Merge slider values:
        # defaults -> LUT sliders -> page/UI settings
        enhancement_settings = {
            "saturation": 1.0,
            "brightness": 1.0,
            "contrast": 1.0,
            "sharpness": 1.0,
        }

        lut_sliders = (lut or {}).get("sliders", {})
        for k in ("saturation", "brightness", "contrast", "sharpness"):
            if k in lut_sliders and lut_sliders[k] not in (None, ""):
                enhancement_settings[k] = float(lut_sliders[k])

        for k in ("saturation", "brightness", "contrast", "sharpness"):
            if k in settings and settings[k] not in (None, ""):
                enhancement_settings[k] = float(settings[k])

        img = apply_image_enhancement(img, enhancement_settings)

        # Update system image_settings in memory to match what was actually applied
        current_settings = (device_config.get_config("image_settings") or {}).copy()
        changed = False
        for k in ("saturation", "brightness", "contrast", "sharpness"):
            new_val = enhancement_settings[k]
            if new_val != current_settings.get(k):
                current_settings[k] = new_val
                changed = True

        if changed:
            device_config.update_value("image_settings", current_settings)

        return img