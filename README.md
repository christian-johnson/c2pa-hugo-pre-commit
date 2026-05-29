# c2pa-hugo-pre-commit
__AI Disclaimer: I used Claude Code (Sonnet 4.6) to help write the code and documentation for this hook. Every line has been human-reviewed, however.__

This is a [pre-commit](https://pre-commit.com) hook that automatically signs images with [C2PA](https://c2pa.org) provenance metadata when you commit to a Hugo site.

Images in `assets/img/` are processed by Hugo (resize, optimize, etc.), which strips any existing signatures. This hook signs them after the fact and outputs `_signed` variants to `static/img/`, which Hugo serves untouched. It blocks the commit if new signed files were created, prompting you to stage them before re-committing.

__Note: this hook is only sufficient for personal blogs and other small websites. Large organizations should have a production key management service (KMS) in place to perform signing functions.__

## Prerequisites

- [c2patool](https://github.com/contentauth/c2patool) in your `PATH`
- A signing certificate and private key (ES256 / ECDSA P-256 is recommended)
- Python 3.11+
- [pre-commit](https://pre-commit.com) installed

## Quick start

### 1. Get a signing certificate and key

You need an X.509 certificate chain (PEM) and its corresponding PKCS#8 private key (PEM).
For production, it is recommended that you obtain a certificate from a recognized CA that supports the C2PA profile (e.g., [DigiCert](https://www.digicert.com/content-credentials)).
If you want to use a self-signed certificate, you can find some instructions for generating the keys in a C2PA-compliant way [here](https://christianjohnson.xyz/posts/2026-02-12-c2pa/).

### 2. Store your key material securely

Choose one of the two methods below. Do **not** commit key files to your repo.

#### Option A: 1Password (recommended)

Store the PEM contents as secure notes or fields in a 1Password vault:

```bash
# Store private key (paste PEM content when prompted)
op item create --category "Secure Note" --title "C2PA Signing Key" \
  --vault Personal \
  private_key="$(cat signing-pkcs8.key)" \
  cert_chain="$(cat cert-chain.pem)"
```

Then add to your shell profile (`~/.zshrc` or `~/.bash_profile`):

```bash
export C2PA_1PASSWORD_KEY_REF="op://Personal/C2PA Signing Key/private_key"
export C2PA_1PASSWORD_CERT_REF="op://Personal/C2PA Signing Key/cert_chain"
```

The hook uses `op read` to fetch the key at signing time. If 1Password is not signed in, it falls back to the env var method below.

#### Option B: Environment variables

Add the raw PEM content directly to your shell profile:

```bash
export C2PA_PRIVATE_KEY="$(cat signing-pkcs8.key)"
export C2PA_CERT_CHAIN="$(cat cert-chain.pem)"
```

**Security note:** Environment variables are less secure than 1Password — they can appear in crash dumps, process listings, and child process environments.
Option A is preferred for personal use.


### 3. Add the hook to your Hugo repo

In your Hugo site's `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/christian-johnson/c2pa-hugo-pre-commit
    rev: v1.1.0
    hooks:
      - id: sign-hugo-images-c2pa
        args:
          - --author-name=Your Name
```

Run `pre-commit install` once to activate it.

### 4. Add signed outputs to your `.gitignore` exclusions

The hook writes signed files and sidecar hashes to `static/img/`. Make sure that directory is **not** excluded from git — you want to commit both:

- `static/img/photo_signed.jpg` — the signed image, referenced in your markdown
- `static/img/photo_signed.jpg.sha256` — tracks the source hash so unchanged images are skipped on future commits

---

## How it works

On each commit the hook scans `assets/img/` for supported image files (`.jpg`, `.jpeg`, `.png`, `.webp`, `.tiff`, `.tif`, `.avif`) and processes each one:

| Scenario | Detection | Action |
|----------|-----------|--------|
| **fresh** | No signed output in `static/img/` | Sign from scratch; `c2pa.created` action |
| **skip** | Signed output exists and source hash matches `.sha256` sidecar | Do nothing |
| **changed** | Signed output exists but source hash has changed | Re-sign; `c2pa.edited` action |
| **chain** | Source image already contains C2PA metadata (e.g. from a camera) | Sign and chain; `c2pa.created` action, c2patool preserves the prior claim |

If any images were signed during this process the commit is **blocked**. Stage the new `_signed` files and `.sha256` sidecars, update your markdown to reference the `_signed` filenames, then commit again.

---

## Configuration reference

All options are passed via `args:` in `.pre-commit-config.yaml`.

| Argument | Default | Description |
|----------|---------|-------------|
| `--author-name` | _(none)_ | Adds a `stds.schema-org.CreativeWork` assertion with this author name |
| `--claim-generator` | `c2pa-hugo-pre-commit/1.0` | Value for the `claim_generator` field in the C2PA manifest |
| `--alg` | `es256` | Signing algorithm. Choices: `es256`, `es384`, `es512`, `ps256`, `ps384`, `ps512`, `ed25519` |
| `--tsa-url` | `http://timestamp.digicert.com` | Timestamp authority URL for countersigning |
| `--source-dir` | `assets/img` | Directory to scan for source images |
| `--output-dir` | `static/img` | Directory to write signed output images |
| `--manifest` | _(none)_ | Path to a custom manifest JSON file (see below) |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `C2PA_1PASSWORD_KEY_REF` | `op://` URI for the private key in 1Password |
| `C2PA_1PASSWORD_CERT_REF` | `op://` URI for the certificate chain in 1Password |
| `C2PA_PRIVATE_KEY` | Raw PEM content of the private key (fallback if 1Password is unavailable) |
| `C2PA_CERT_CHAIN` | Raw PEM content of the certificate chain (fallback) |

Resolution order for each: 1Password → env var → error.

---

## Use cases

### Minimal setup (no author assertion)

```yaml
hooks:
  - id: sign-hugo-images-c2pa
```

Signs every image in `assets/img/` with default settings and no author metadata. Useful for automated pipelines or when you just want provenance without identity claims.

### Single author blog

```yaml
hooks:
  - id: sign-hugo-images-c2pa
    args:
      - --author-name=Jane Smith
      - --claim-generator=janesmith-blog/1.0
```

Adds a `stds.schema-org.CreativeWork` assertion identifying Jane Smith as the author.

### Non-default image directories

Hugo allows content to live in various places. Adjust if your images are elsewhere:

```yaml
hooks:
  - id: sign-hugo-images-c2pa
    args:
      - --author-name=Jane Smith
      - --source-dir=content/photos
      - --output-dir=static/photos
```

### Custom algorithm and timestamp authority

```yaml
hooks:
  - id: sign-hugo-images-c2pa
    args:
      - --author-name=Jane Smith
      - --alg=es384
      - --tsa-url=http://timestamp.sectigo.com
```

### Custom manifest (advanced)

For full control over the C2PA manifest — multiple authors, custom assertions, different schema.org types — commit a manifest JSON file to your repo and point the hook at it:

```yaml
hooks:
  - id: sign-hugo-images-c2pa
    args:
      - --manifest=c2pa-manifest.json
```

Your `c2pa-manifest.json` should **not** include `sign_cert` or `private_key` fields; the hook strips them if present and injects key material from your environment at runtime. Example:

```json
{
  "claim_generator": "my-blog/2.0",
  "alg": "es256",
  "ta_url": "http://timestamp.digicert.com",
  "assertions": [
    {
      "label": "stds.schema-org.CreativeWork",
      "data": {
        "@context": "https://schema.org",
        "@type": "CreativeWork",
        "author": [
          { "@type": "Person", "name": "Jane Smith", "url": "https://example.com" },
          { "@type": "Person", "name": "John Doe" }
        ]
      }
    },
    {
      "label": "c2pa.actions",
      "data": {
        "actions": [{ "action": "c2pa.created" }]
      }
    }
  ]
}
```

---

## Updating your markdown

After the hook runs and you stage the new files, update any image references in your markdown from the source name to the signed variant:

```markdown
<!-- Before -->
![My photo](../assets/img/photo.jpg)

<!-- After -->
![My photo](/img/photo_signed.jpg)
```

The signed file is in `static/img/`, which Hugo serves at the root path `/img/`.

---

## What gets committed

| File | Commit? | Notes |
|------|---------|-------|
| `assets/img/photo.jpg` | Yes | Source image |
| `static/img/photo_signed.jpg` | Yes | Signed output; reference this in markdown |
| `static/img/photo_signed.jpg.sha256` | Yes | Source hash at signing time; do not edit manually |
| `signing-pkcs8.key` | **No** | Private key — add to `.gitignore` |
| `cert-chain.pem` | Optional | Public certificate — safe to commit if desired |

Add key files to `.gitignore`:

```
signing-pkcs8.key
*.key
```

---

## Troubleshooting

**`c2patool not found in PATH`**
Install c2patool: `cargo install c2patool` or download a release binary from [github.com/contentauth/c2patool](https://github.com/contentauth/c2patool).

**`Cannot resolve private key`**
Set `C2PA_1PASSWORD_KEY_REF` (for 1Password) or `C2PA_PRIVATE_KEY` (for raw PEM). See [Store your key material securely](#2-store-your-key-material-securely).

**`1Password CLI not signed in`**
Run `op signin` before committing, or fall back to the `C2PA_PRIVATE_KEY` env var.

**`c2patool failed` with an algorithm error**
Ensure your key type matches `--alg`. ES256 requires an EC P-256 key; ES384 requires EC P-384. RSA keys use `ps256`, `ps384`, or `ps512`.

**Images keep re-signing on every commit**
The `.sha256` sidecar files are missing or not being committed. Make sure `static/img/*.sha256` is not excluded by your `.gitignore`.

**Hook runs but skips all images**
The source directory (`assets/img` by default) either doesn't exist or contains no supported image files. Check `--source-dir` matches your actual directory.
