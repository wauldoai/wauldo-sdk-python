# SDK publish checklist

Run from the repo root. These commands are user actions (they require
credentials that live on your workstation, not in CI).

## 1. Python — PyPI 0.8.2 → 0.9.0

```bash
cd sdk-python
pip install --user --break-system-packages build twine
python3 -m build                 # builds wheel + sdist under dist/
twine check dist/*               # metadata validation
twine upload dist/*              # prompts for PyPI token
```

Verification:

```bash
pip install --upgrade wauldo
python3 -c "import wauldo; print(wauldo.__version__)"   # → 0.9.0
```

## 2. TypeScript — npm 0.7.2 → 0.8.0

```bash
cd sdk-typescript
npm run build                    # tsup → dist/
npm pack --dry-run               # sanity check tarball contents
npm publish --access public      # needs `npm login` done once
```

Verification:

```bash
npm view wauldo@latest version   # → 0.8.0
```

## 3. Rust — crates.io 0.7.0 → 0.8.0

```bash
cd sdk-rust
cargo publish --dry-run          # full build + metadata check
cargo publish                    # credentials in ~/.cargo/credentials.toml
```

Verification:

```bash
curl -sS -H 'User-Agent: sanity' https://crates.io/api/v1/crates/wauldo \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['crate']['max_version'])"
# → 0.8.0
```

## 4. Sync public repos (blocked while GitHub account suspended)

Each SDK lives in a public mirror repo. Re-sync after publish so
`github.com/wauldo/wauldo-sdk-*` stays in lockstep with the registry:

- `wauldo/wauldo-sdk-python` — copy `sdk-python/` over, commit, push.
- `wauldo/wauldo-sdk-js` — same with `sdk-typescript/`.
- `wauldo/wauldo-sdk-rust` — same with `sdk-rust/`.

Current blocker: the GitHub account is suspended (see MEMORY.md).
Unblocks once the appeal is resolved.
