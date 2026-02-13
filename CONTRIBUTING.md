# Contributing to the PeonPing Registry

## Adding a pack

### 1. Create your pack repo

In your own GitHub account, create a repo with:

```
peonping-mypack/
  peonping.json       # CESP v1.0 manifest
  sounds/
    sound1.mp3
    sound2.wav
    ...
  README.md           # Optional
  LICENSE             # Recommended
```

See [peonping.com/create](https://peonping.com/create) for the full manifest format.

### 2. Tag a release

```bash
git tag v1.0.0
git push origin v1.0.0
```

### 3. Compute the manifest hash

```bash
sha256sum peonping.json
# or on macOS:
shasum -a 256 peonping.json
```

### 4. Add your entry to index.json

Fork this repo, then add your pack entry to the `packs` array in `index.json`. Keep entries in alphabetical order by `name`. Set `trust_tier` to `"community"`.

Example entry:

```json
{
  "name": "my-pack",
  "display_name": "My Sound Pack",
  "version": "1.0.0",
  "description": "Short description of your pack",
  "author": { "name": "yourname", "github": "yourname" },
  "trust_tier": "community",
  "categories": ["session.start", "task.complete"],
  "language": "en",
  "license": "MIT",
  "sound_count": 10,
  "total_size_bytes": 500000,
  "source_repo": "yourname/peonping-mypack",
  "source_ref": "v1.0.0",
  "source_path": ".",
  "manifest_sha256": "<sha256 of peonping.json>",
  "tags": ["gaming"],
  "preview_sounds": ["ready.mp3", "done.mp3"],
  "added": "2026-02-12",
  "updated": "2026-02-12"
}
```

### 5. Open a PR

That's it. CI will validate your entry automatically. We'll review and merge. Once merged, your pack becomes available for installation.

## Updating a pack

1. Push a new version to your pack repo
2. Tag it (e.g., `v1.1.0`)
3. Open a PR updating your entry in `index.json` with the new `version`, `source_ref`, `manifest_sha256`, and `updated` date

## Rules

- Pack names must be unique (lowercase, hyphens, underscores)
- Entries in `index.json` must be sorted alphabetically by `name`
- Audio files: WAV, MP3, or OGG only
- Max 1 MB per audio file, 50 MB total per pack
- No offensive content
- Source repo must be publicly accessible
