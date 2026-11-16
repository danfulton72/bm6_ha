# BM6 Battery Monitor for Home Assistant

> **Not ready for installation.** The original BM6 custom component has not been supplied yet, so this repository does not contain a working Home Assistant integration. No releases should be published until the integration is imported, reviewed and tested.

This repository will host the `bm6_ha` custom integration for the BM6 Bluetooth battery monitor.

## Planned layout

```
custom_components/
  bm6_ha/
    __init__.py
    manifest.json
    config_flow.py
    sensor.py
```

The final files and supported entities will depend on the original implementation. The repository will use a single integration domain (`bm6_ha`) and the standard `custom_components/<domain>/` layout.

## Development

- HACS / Home Assistant Marketplace validation and hassfest are intended to run on pull requests.
- Python syntax and Ruff checks require the actual integration files.
- Dependency update and release workflows must not be used to publish an empty or placeholder integration.

See [migration notes](docs/MIGRATION_NOTES.md) on the migration branch for remaining tasks.

## Source and licensing

The BM6 component source, any licensing terms and original authorship must be checked before code is imported. Do not add an unsupported license claim until the source is available.
