# Publishing

Releases are automated via GitHub Actions + PyPI Trusted Publishing
(OIDC). No tokens or credentials are stored anywhere.

## Release flow

1. Bump the version in `pyproject.toml` **and** `src/wauldo/__init__.py`
   (`__version__`) — CI rejects a mismatch.
2. Update `CHANGELOG.md`.
3. Tag and push:

   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

The tag triggers `.github/workflows/publish.yml`: test matrix
(Python 3.9–3.12) → version/tag consistency check → build → publish to
PyPI via OIDC → GitHub Release with auto-generated notes.

## Verification

```bash
pip install --upgrade wauldo
python3 -c "import wauldo; print(wauldo.__version__)"
```

## Manual fallback (CI outage only)

```bash
python3 -m build && twine check dist/* && twine upload dist/*
```
