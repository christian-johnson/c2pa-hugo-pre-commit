#!/usr/bin/env python3
# Copyright (c) 2024 Christian Johnson. MIT License.
"""C2PA image signing pre-commit hook for Hugo sites."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".avif"},
)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the signing hook."""
    parser = argparse.ArgumentParser(
        description="Sign images with C2PA for a Hugo site",
    )
    parser.add_argument(
        "--author-name",
        default="",
        help="Author name for the CreativeWork assertion (optional)",
    )
    parser.add_argument(
        "--claim-generator",
        default="c2pa-hugo-pre-commit/1.0",
        help="Value for the claim_generator field in the C2PA manifest",
    )
    parser.add_argument(
        "--tsa-url",
        default="http://timestamp.digicert.com",
        help="Timestamp authority URL",
    )
    parser.add_argument(
        "--alg",
        default="es256",
        choices=["es256", "es384", "es512", "ps256", "ps384", "ps512", "ed25519"],
        help="Signing algorithm",
    )
    parser.add_argument(
        "--source-dir",
        default="assets/img",
        help="Directory containing source images to sign",
    )
    parser.add_argument(
        "--output-dir",
        default="static/img",
        help="Directory to write signed output images",
    )
    parser.add_argument(
        "--widths",
        default="800,1280",
        help=(
            "Comma-separated list of responsive widths (px) to generate for each "
            "image. Each width smaller than the source is resized and signed as a "
            "C2PA-chained derivative of the full-size signed image. The full-size "
            "image is always emitted. Pass an empty string to disable resizing."
        ),
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=82,
        help="Encoder quality (1-100) for resized JPEG/WebP/AVIF variants",
    )
    parser.add_argument(
        "--data-file",
        default="data/c2pa-images.json",
        help=(
            "Path to the JSON data file describing each image's signed variants. "
            "The companion Hugo render hook reads this to build responsive srcsets "
            "that point at the pre-signed static files."
        ),
    )
    parser.add_argument(
        "--public-prefix",
        default="",
        help=(
            "URL prefix under which the output images are served. When empty it is "
            "derived from --output-dir by stripping a leading 'static/' segment "
            "(e.g. static/img -> /img)."
        ),
    )
    parser.add_argument(
        "--manifest",
        default="",
        help=(
            "Path to a custom manifest JSON file. When provided, assertion args "
            "(--author-name, --claim-generator, etc.) are ignored. Key material "
            "fields (sign_cert, private_key) are stripped and injected at runtime."
        ),
    )
    return parser.parse_args()


def check_c2patool() -> str:
    """Return the absolute path to c2patool, or exit with an error."""
    path = shutil.which("c2patool")
    if path is not None:
        return path
    logger.error(
        "c2patool not found in PATH. "
        "Install from https://github.com/contentauth/c2patool",
    )
    sys.exit(1)


def _try_1password(ref: str, label: str) -> str | None:
    """Attempt to read a secret from 1Password; silently return None on any failure."""
    op = shutil.which("op")
    if op is None:
        return None

    session = subprocess.run([op, "account", "list"], capture_output=True, check=False)
    if session.returncode != 0:
        logger.warning("1Password CLI not signed in; skipping for %s", label)
        return None

    result = subprocess.run(
        [op, "read", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("op read failed for %s: %s", label, result.stderr.strip())
        return None

    return result.stdout


def resolve_key(ref_env: str, pem_env: str, label: str) -> str:
    """Resolve key material from 1Password or an environment variable.

    Resolution order:
      1. Treat the value of ref_env as an op:// URI and read from 1Password.
      2. Fall back to the raw PEM content in pem_env.
      3. Exit with an actionable error message.
    """
    op_ref = os.environ.get(ref_env, "")
    if op_ref:
        value = _try_1password(op_ref, label)
        if value is not None:
            return value

    pem = os.environ.get(pem_env, "")
    if pem:
        return pem

    logger.error(
        "Cannot resolve %s. "
        "Set %s to an op:// reference (e.g. op://Personal/C2PA/private_key), "
        "or set %s to the raw PEM content.",
        label,
        ref_env,
        pem_env,
    )
    sys.exit(1)


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sidecar_path(output_path: Path) -> Path:
    """Return the .sha256 sidecar path for the given signed output."""
    return output_path.with_suffix(output_path.suffix + ".sha256")


def is_c2pa_signed(path: Path, c2patool: str) -> bool:
    """Return True if the file already contains a C2PA manifest."""
    result = subprocess.run(
        [c2patool, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _build_assertions(args: argparse.Namespace, action: str) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    if args.author_name:
        assertions.append(
            {
                "label": "stds.schema-org.CreativeWork",
                "data": {
                    "@context": "https://schema.org",
                    "@type": "CreativeWork",
                    "author": [{"@type": "Person", "name": args.author_name}],
                },
            },
        )
    assertions.append(
        {
            "label": "c2pa.actions",
            "data": {"actions": [{"action": action}]},
        },
    )
    return assertions


def build_fresh_manifest(args: argparse.Namespace) -> dict[str, Any]:
    """Build a manifest for a de novo signing (c2pa.created)."""
    return {
        "claim_generator": args.claim_generator,
        "alg": args.alg,
        "ta_url": args.tsa_url,
        "assertions": _build_assertions(args, "c2pa.created"),
    }


def build_edited_manifest(args: argparse.Namespace) -> dict[str, Any]:
    """Build a manifest for re-signing a changed source image (c2pa.edited)."""
    return {
        "claim_generator": args.claim_generator,
        "alg": args.alg,
        "ta_url": args.tsa_url,
        "assertions": _build_assertions(args, "c2pa.edited"),
    }


def derive_resized_manifest(base: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``base`` describing a resize edit.

    The full-size image is signed as ``c2pa.created``; each smaller variant is a
    derivative of it. We keep every non-action assertion from the base manifest
    (e.g. the author CreativeWork) but replace the ``c2pa.actions`` assertion with
    a single ``c2pa.resized`` action. The parent link to the full-size image is
    added by c2patool at signing time via ``--parent``.
    """
    manifest = copy.deepcopy(base)
    resized = {
        "label": "c2pa.actions",
        "data": {"actions": [{"action": "c2pa.resized"}]},
    }
    assertions = manifest.get("assertions", [])
    for i, assertion in enumerate(assertions):
        if assertion.get("label") == "c2pa.actions":
            assertions[i] = resized
            break
    else:
        assertions.append(resized)
    manifest["assertions"] = assertions
    return manifest


def parse_widths(raw: str) -> list[int]:
    """Parse the --widths argument into a sorted list of positive ints."""
    widths = {int(part) for part in raw.split(",") if part.strip()}
    return sorted(w for w in widths if w > 0)


def _save_image(image: Any, dest: Path, quality: int) -> None:
    """Encode a Pillow image to ``dest``, choosing options by file extension."""
    suffix = dest.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        image.convert("RGB").save(
            dest,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
        )
    elif suffix == ".png":
        image.save(dest, format="PNG", optimize=True)
    elif suffix == ".webp":
        image.save(dest, format="WEBP", quality=quality, method=6)
    elif suffix in {".tif", ".tiff"}:
        image.save(dest, format="TIFF")
    elif suffix == ".avif":
        image.save(dest, format="AVIF", quality=quality)
    else:
        image.save(dest)


def image_dimensions(path: Path) -> tuple[int, int]:
    """Return the (width, height) of ``path`` honouring EXIF orientation."""
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).size


def resize_image(source: Path, dest: Path, width: int, quality: int) -> bool:
    """Resize ``source`` to ``width`` px wide (preserving aspect) into ``dest``.

    EXIF orientation is applied so the pixels are upright (the resized file
    carries no orientation tag of its own). Returns False if the source is not
    wider than the target or the format cannot be processed, in which case no
    variant is written.
    """
    try:
        with Image.open(source) as image:
            upright = ImageOps.exif_transpose(image)
            original_width, original_height = upright.size
            if width >= original_width:
                return False
            height = max(1, round(original_height * width / original_width))
            resized = upright.resize((width, height), Image.LANCZOS)
            dest.parent.mkdir(parents=True, exist_ok=True)
            _save_image(resized, dest, quality)
    except (OSError, ValueError) as exc:
        logger.warning("Could not resize %s to %dpx: %s", source.name, width, exc)
        return False
    return True


def write_temp_file(content: str, suffix: str = ".tmp") -> Path:
    """Write content to a mkstemp file (mode 0o600) and return its path.

    The caller is responsible for unlinking the returned path when done.
    mkstemp creates the file with 0o600 by default, so no explicit chmod is needed.
    """
    fd, path_str = tempfile.mkstemp(suffix=suffix)
    path = Path(path_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def sign_image(
    source: Path,
    output: Path,
    manifest: dict[str, Any],
    key_pem: str,
    cert_pem: str,
    c2patool: str,
    parent: Path | None = None,
) -> bool:
    """Sign a single image using c2patool.

    Key material is written to temporary files (mode 0o600) and removed
    immediately after c2patool exits, regardless of success or failure.
    When ``parent`` is given, it is passed to c2patool as ``--parent`` so the
    signed output carries a ``parentOf`` ingredient linking back to it, forming
    a verifiable provenance chain (full-size signed image -> resized variant).
    Returns True on success, False if c2patool reports an error.
    """
    key_path: Path | None = None
    cert_path: Path | None = None
    manifest_path: Path | None = None
    try:
        key_path = write_temp_file(key_pem, suffix=".key")
        cert_path = write_temp_file(cert_pem, suffix=".pem")
        manifest["sign_cert"] = str(cert_path)
        manifest["private_key"] = str(key_path)
        manifest_path = write_temp_file(json.dumps(manifest), suffix=".json")

        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = [c2patool, str(source), "-m", str(manifest_path)]
        if parent is not None:
            cmd += ["-p", str(parent)]
        cmd += ["-o", str(output), "--force"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error("c2patool failed for %s:\n%s", source.name, result.stderr)
            return False
    finally:
        for p in (key_path, cert_path, manifest_path):
            if p is not None:
                p.unlink(missing_ok=True)
    return True


def _pick_manifest(
    args: argparse.Namespace,
    custom: dict[str, Any] | None,
    *,
    is_changed: bool,
) -> dict[str, Any]:
    if custom is not None:
        return dict(custom)
    return build_edited_manifest(args) if is_changed else build_fresh_manifest(args)


def _collect_sources(source_dir: Path) -> list[Path]:
    return [
        p
        for p in sorted(source_dir.iterdir())
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]


def _load_custom_manifest(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.manifest:
        return None
    with Path(args.manifest).open(encoding="utf-8") as f:
        custom = json.load(f)
    # Strip key material — it is always injected at runtime from the resolved
    # secret source, never read from a committed file.
    custom.pop("sign_cert", None)
    custom.pop("private_key", None)
    return custom


def _public_prefix(args: argparse.Namespace, output_dir: Path) -> str:
    """Derive the URL prefix under which the output images are served."""
    if args.public_prefix:
        return "/" + args.public_prefix.strip("/")
    parts = output_dir.parts
    if parts and parts[0] == "static":
        rest = "/".join(parts[1:])
        return f"/{rest}" if rest else ""
    return "/" + str(output_dir).strip("/")


@dataclass(frozen=True)
class SignContext:
    """Invariant inputs shared across every image processed in one run."""

    args: argparse.Namespace
    output_dir: Path
    widths: list[int]
    prefix: str
    custom_manifest: dict[str, Any] | None
    key_pem: str
    cert_pem: str
    c2patool: str


def _to_url(ctx: SignContext, path: Path) -> str:
    """Map an output file path to its served URL."""
    return f"{ctx.prefix}/{path.name}"


def _variant_specs(
    ctx: SignContext,
    source: Path,
    original_width: int,
) -> list[tuple[int, Path]]:
    """Return (width, output path) pairs for each variant smaller than source."""
    return [
        (w, ctx.output_dir / f"{source.stem}_signed_{w}{source.suffix}")
        for w in ctx.widths
        if w < original_width
    ]


def _sign_variants(
    ctx: SignContext,
    source: Path,
    base_output: Path,
    var_specs: list[tuple[int, Path]],
) -> None:
    """Resize and chain-sign each variant as a derivative of base_output."""
    base = ctx.custom_manifest or build_fresh_manifest(ctx.args)
    for width, var_out in var_specs:
        fd, tmp_str = tempfile.mkstemp(suffix=source.suffix)
        os.close(fd)
        tmp = Path(tmp_str)
        try:
            if not resize_image(source, tmp, width, ctx.args.quality):
                continue
            manifest = derive_resized_manifest(base)
            sys.stdout.write(f"  resized [{width}w]: -> {var_out.name}\n")
            if not sign_image(
                tmp,
                var_out,
                manifest,
                ctx.key_pem,
                ctx.cert_pem,
                ctx.c2patool,
                parent=base_output,
            ):
                sys.exit(1)
            # Downscaling can inflate some formats (e.g. PNG screenshots). If the
            # signed variant is no smaller than the full image, drop it so the
            # srcset never serves a heavier file for a smaller width.
            if var_out.stat().st_size >= base_output.stat().st_size:
                sys.stdout.write(f"  dropped [{width}w]: not smaller than full\n")
                var_out.unlink(missing_ok=True)
        finally:
            tmp.unlink(missing_ok=True)


def _build_entry(
    ctx: SignContext,
    base_output: Path,
    dimensions: tuple[int, int],
    var_specs: list[tuple[int, Path]],
) -> dict[str, Any]:
    """Build the data-file entry describing an image's signed variants."""
    original_width, original_height = dimensions
    variants = [
        {"src": _to_url(ctx, path), "width": w}
        for w, path in var_specs
        if path.exists()
    ]
    variants.append({"src": _to_url(ctx, base_output), "width": original_width})
    variants.sort(key=lambda v: v["width"])
    return {"width": original_width, "height": original_height, "variants": variants}


def _process_one(ctx: SignContext, source: Path, data: dict[str, Any]) -> bool:
    """Sign one source (and its variants) if stale; record its data entry.

    Returns True if any signing happened, so the caller can block the commit.
    """
    base_output = ctx.output_dir / f"{source.stem}_signed{source.suffix}"
    sidecar = sidecar_path(base_output)
    source_hash = hash_file(source)
    dimensions = image_dimensions(source)
    var_specs = _variant_specs(ctx, source, dimensions[0])

    prev = sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else None
    up_to_date = prev == source_hash and base_output.exists()

    signed = False
    if not up_to_date:
        is_changed = base_output.exists() and prev is not None and prev != source_hash
        manifest = _pick_manifest(ctx.args, ctx.custom_manifest, is_changed=is_changed)
        already_signed = is_c2pa_signed(source, ctx.c2patool)
        scenario = "changed" if is_changed else ("chain" if already_signed else "fresh")
        sys.stdout.write(f"Signing [{scenario}]: {source.name} -> {base_output.name}\n")
        if not sign_image(
            source,
            base_output,
            manifest,
            ctx.key_pem,
            ctx.cert_pem,
            ctx.c2patool,
        ):
            sys.exit(1)
        _sign_variants(ctx, source, base_output, var_specs)
        sidecar.write_text(source_hash, encoding="utf-8")
        signed = True

    data[_to_url(ctx, base_output)] = _build_entry(
        ctx,
        base_output,
        dimensions,
        var_specs,
    )
    return signed


def _write_data_file(path: Path, data: dict[str, Any]) -> bool:
    """Write the variant data file. Returns True if its contents changed."""
    serialized = json.dumps(data, indent=2, sort_keys=True) + "\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if existing == serialized:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")
    sys.stdout.write(f"Wrote image variant data: {path}\n")
    return True


def process_images(args: argparse.Namespace, c2patool: str) -> bool:
    """Sign all new or changed images in source_dir and refresh the data file.

    Returns True if anything changed (a signature was (re)written or the data
    file was updated), which causes the commit to be blocked so the user can
    stage the newly created files.
    """
    source_dir = Path(args.source_dir)
    output_dir = Path(args.output_dir)

    if not source_dir.exists():
        return False

    sources = _collect_sources(source_dir)
    if not sources:
        return False

    ctx = SignContext(
        args=args,
        output_dir=output_dir,
        widths=parse_widths(args.widths),
        prefix=_public_prefix(args, output_dir),
        custom_manifest=_load_custom_manifest(args),
        key_pem=resolve_key(
            "C2PA_1PASSWORD_KEY_REF", "C2PA_PRIVATE_KEY", "private key"
        ),
        cert_pem=resolve_key(
            "C2PA_1PASSWORD_CERT_REF",
            "C2PA_CERT_CHAIN",
            "certificate chain",
        ),
        c2patool=c2patool,
    )

    data: dict[str, Any] = {}
    changed = False
    for source in sources:
        if _process_one(ctx, source, data):
            changed = True

    return _write_data_file(Path(args.data_file), data) or changed


def main() -> None:
    """Entry point for the pre-commit hook."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    c2patool = check_c2patool()

    if process_images(args, c2patool):
        sys.stdout.write(
            "\nImages were signed or the variant data file changed. Stage the "
            f"signed files and .sha256 sidecars in {args.output_dir}/, the data "
            f"file ({args.data_file}), then reference the _signed variants in your "
            "markdown and commit again.\n",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
