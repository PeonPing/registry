# PeonPing Registry

The central index of [CESP](https://openpeon.com/spec) sound packs. Metadata only -- no audio files stored here.

Browse packs at [openpeon.com/packs](https://openpeon.com/packs).

## How it works

The file `index.json` at the repo root is the single source of truth for all registered packs. Each pack entry contains flattened metadata pointing to the pack's source repository. There are no per-pack subdirectories; everything lives in `index.json`.

**Index URL:** `https://PeonPing.github.io/registry/index.json`

## Submit a pack

1. Create a GitHub repo with your pack (e.g., `yourname/peonping-mypack`)
2. Include `openpeon.json` manifest + `sounds/` directory
3. Tag a release: `git tag v1.0.0 && git push origin v1.0.0`
4. Fork this repo and add your entry to the `packs` array in `index.json` (keep alphabetical order by `name`)
5. Open a PR -- CI will validate your entry automatically

See the full guide at [openpeon.com/create](https://openpeon.com/create).

## Registry entry format

Each entry in the `packs` array looks like this:

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
  "manifest_sha256": "<sha256 of openpeon.json>",
  "tags": ["gaming"],
  "preview_sounds": ["ready.mp3", "done.mp3"],
  "added": "2026-02-12",
  "updated": "2026-02-12"
}
```

See the [full schema](schema/registry-v1.schema.json) for validation.

## Trust tiers

| Tier | Criteria |
|---|---|
| `official` | Maintained by the PeonPing org |
| `verified` | Established GitHub account (1+ year, 50+ followers or 10+ repos) |
| `community` | Any GitHub account older than 30 days |

## License

MIT
