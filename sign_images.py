#!/usr/bin/env python3
# Copyright (c) 2024 Christian Johnson. MIT License.
"""C2PA image signing pre-commit hook for Hugo sites."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

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
) -> bool:
    """Sign a single image using c2patool.

    Key material is written to temporary files (mode 0o600) and removed
    immediately after c2patool exits, regardless of success or failure.
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
        result = subprocess.run(
            [
                c2patool,
                str(source),
                "-m",
                str(manifest_path),
                "-o",
                str(output),
                "--force",
            ],
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


def process_images(args: argparse.Namespace, c2patool: str) -> bool:
    """Sign all new or changed images found in source_dir.

    Returns True if any images were signed, which causes the commit to be
    blocked so the user can stage the newly created files.
    """
    source_dir = Path(args.source_dir)
    output_dir = Path(args.output_dir)

    if not source_dir.exists():
        return False

    sources = _collect_sources(source_dir)
    if not sources:
        return False

    custom_manifest: dict[str, Any] | None = None
    if args.manifest:
        with Path(args.manifest).open(encoding="utf-8") as f:
            custom_manifest = json.load(f)
        # Strip key material — it is always injected at runtime from the
        # resolved secret source, never read from a committed file.
        custom_manifest.pop("sign_cert", None)
        custom_manifest.pop("private_key", None)

    key_pem = resolve_key("C2PA_1PASSWORD_KEY_REF", "C2PA_PRIVATE_KEY", "private key")
    cert_pem = resolve_key(
        "C2PA_1PASSWORD_CERT_REF",
        "C2PA_CERT_CHAIN",
        "certificate chain",
    )

    signed_any = False
    for source in sources:
        output = output_dir / f"{source.stem}_signed{source.suffix}"
        sidecar = sidecar_path(output)
        source_hash = hash_file(source)

        if output.exists() and sidecar.exists():
            if sidecar.read_text(encoding="utf-8").strip() == source_hash:
                continue  # source unchanged; skip
            is_changed = True
        else:
            is_changed = False

        manifest = _pick_manifest(args, custom_manifest, is_changed=is_changed)
        already_signed = is_c2pa_signed(source, c2patool)
        scenario = "changed" if is_changed else ("chain" if already_signed else "fresh")
        sys.stdout.write(f"Signing [{scenario}]: {source.name} -> {output.name}\n")

        if not sign_image(source, output, manifest, key_pem, cert_pem, c2patool):
            sys.exit(1)

        sidecar.write_text(source_hash, encoding="utf-8")
        signed_any = True

    return signed_any


def main() -> None:
    """Entry point for the pre-commit hook."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    c2patool = check_c2patool()

    if process_images(args, c2patool):
        sys.stdout.write(
            "\nNew images were signed. Stage the signed files and their .sha256 "
            "sidecars, then update your markdown to reference the _signed variants "
            f"in {args.output_dir}/\n",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
