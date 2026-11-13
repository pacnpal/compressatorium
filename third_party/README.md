# Vendored third-party source

Code here is copied verbatim from an upstream project because the Docker image
can no longer fetch it at build time. Each entry records where the copy came
from and how it was verified, so anyone can repeat the checks.

Rules:

- **Never edit vendored files.** Each directory is the upstream release byte for
  byte, and a test pins its hash. To change the code, re-vendor a new upstream
  version, update the pinned hash, and record the new provenance here.
- Keep the upstream `LICENSE` in the directory.
- `.gitattributes` marks `third_party/**` as `-text` so git never rewrites line
  endings, which would break the hash.
- Renovate and Codacy skip this tree (`renovate.json` `ignorePaths`,
  `.codacy.yml` `exclude_paths`). Upstream's own go.mod and workflows are not
  ours to bump or lint.

## nkit2iso

| | |
|---|---|
| Upstream | `github.com/DonMikone/nkit2iso` (deleted or made private by 2026-11; the repo returns 404) |
| License | MIT, Copyright (c) 2026 Mike Kremer (`nkit2iso/LICENSE`) |
| Commit | `b97129c157edf704da5bbf65e6d9e642c843b922` (2026-07-18T12:24:50Z, "Offer recovery-file download for removed Wii update partitions") |
| Go module version | `github.com/DonMikone/nkit2iso@v0.1.1-0.20260718122450-b97129c157ed` |
| Module hash (`h1:`) | `h1:FDV/0eL7S5mpYiTNIh3yKmW5nS9U/8GijNONpQdn+Ro=` |
| go.mod hash | `h1:ciLwrn6PJoYvh6bcH5BaMRu2FPmBFldbEJXf+TcSKYE=` |
| Used by | `Dockerfile` stage `nkit2iso-builder` (`NKIT2ISO_REF` is the version stamp) |
| Guarded by | `tests/test_vendored_nkit2iso.py` |

This is the same commit the image pinned (`NKIT2ISO_REF`) before upstream
disappeared, and the one every released image up to 4.5.0 shipped.

**Where the bytes came from.** The tree is the Go module zip for that pseudo-version,
fetched from `proxy.golang.org` with
`go mod download -json github.com/DonMikone/nkit2iso@v0.1.1-0.20260718122450-b97129c157ed`
inside the image's pinned `golang:1.26-trixie` base. The Go proxy caches module
versions permanently, so it still served this version after the GitHub
repository went away. `go mod download` verifies the zip against
`sum.golang.org` before unpacking it.

**How it was verified.** Four independent checks agree:

1. **Go checksum database.** `sum.golang.org` (a public, append-only
   transparency log) records the `h1:` hash above for this version. The record
   is at tree size 58502047. Check it yourself:
   ```bash
   curl 'https://sum.golang.org/lookup/github.com/!don!mikone/nkit2iso@v0.1.1-0.20260718122450-b97129c157ed'
   ```
   The test recomputes Go's `dirhash.Hash1` over this directory and requires
   the same value.
2. **Reproducible build against the shipped binary.** The build info embedded in
   `nkit2iso` from the published `pacnpal/compressatorium:sha-32bc5bd` image
   (index `sha256:9d2a28ac588cb5fccbaabf61c6c0935b5a2d868b7c5fe209d79af845920da286`)
   names `vcs.revision=b97129c…` and the pseudo-version above. This source was
   rebuilt for amd64 and arm64 in that base image with the Dockerfile's exact
   flags. The build ran inside a scratch git repo committed at the same time,
   so the stamped build info has the same length. Every ELF section came out
   identical to the shipped binary (`.text`, `.data`, `.gopclntab`, …). The
   only differing bytes are the commit hash in the build-info string (48 bytes
   ×2) and the build IDs.
3. **Wayback Machine.** A 2026-08-09 capture of the repository
   (`web.archive.org/web/20260809133639/https://github.com/DonMikone/nkit2iso`)
   shows `master`'s latest commit as `b97129c157ed…`, dated
   2026-07-18T14:24:50+02:00. The eight raw files captured from `master` that
   day (`main.go`, `nkit.go`, `wii.go`, `recovery.go`, `gcz_test.go`,
   `CONTEXT.md`, `LICENSE`, `.gitignore`) are byte-identical to the vendored
   copies.
4. **Earlier release.** A GitHub source archive of the `v0.1.0` tag (commit
   `ba3901c`) matches `sum.golang.org`'s `v0.1.0` hash
   (`h1:v9FrVhD55y/NUWpNhv4P09EYIQW61mqTQY8JLdfNElc=`). The vendored commit
   builds on it and adds `recovery.go` (the `-recovery` flag that
   `NKIT2ISO_RECOVERY` drives). `v0.1.0` alone would not work, because the app
   needs that flag.

### Prebuilt binaries (`nkit2iso-bin/`)

Upstream's releases page went down with its repository, so the binaries are
hosted here too. These are the exact files shipped at `/usr/local/bin/nkit2iso`
in `pacnpal/compressatorium:sha-32bc5bd`, copied out of that image by its index
digest (`sha256:9d2a28ac…0da286`):

| File | Platform manifest | SHA-256 |
|---|---|---|
| `nkit2iso-bin/linux_amd64/nkit2iso` | `sha256:a970bee4c18aebf19be39dddc1c6fb3cabe99772c43dcc6a96fccdd609642c36` | `35886ce962e87ca6f78a498e3b401d963109eee9403cc2e627962554525c8bd7` |
| `nkit2iso-bin/linux_arm64/nkit2iso` | `sha256:f0c422d4ce65174f45aca6b758309c3f9f280d5f5d2fc41fd9359dadf06ca048` | `8be81072c6d544e5ddfc3cce76c74e5e56ba6c81064b3bfd4c4ca2f712330675` |

Both are static (`CGO_ENABLED=0`), built by Go 1.26.5, and stamped with
`-version` = `b97129c…`. Their embedded build info names
`vcs.revision=b97129c157edf704da5bbf65e6d9e642c843b922` and the module version
above. Check 2 shows they rebuild from the vendored source. The test checks them
against `nkit2iso-bin/SHA256SUMS` and checks their build info. Verify a
download with:

```bash
cd third_party/nkit2iso-bin && shasum -a 256 -c SHA256SUMS
```

An image built from this tree ships a binary whose SHA-256 differs from these.
Built without a `.git` directory, its Go build info carries no `vcs.*` lines,
and the shorter string shifts the binary's layout. The code is the same
(check 2), and `-version` still prints `b97129c…`. The Dockerfile build is
deterministic: the 2026-11-13 builds produced
`73580476830299711ed5ef9bcd4236b5a4845e954ebb68a8bdd116fc839f6600` (amd64) and
`f8932cab772e559f1971357113b04391d29bec99b71404cb7c11ed28a8d1f69e` (arm64), the
same as a plain `go build` of this tree with the Dockerfile's flags.

The hosted binaries are for running nkit2iso outside the image, for example in local
development with `NKIT2ISO_PATH=/abs/path/to/third_party/nkit2iso-bin/linux_amd64/nkit2iso`
on a Linux host. The Docker image does not use them: it compiles the vendored
source, and `.dockerignore` keeps this directory out of the build context. If
the vendored source is ever re-vendored, replace these with binaries built from
the new source, and update `SHA256SUMS` and the test.

`github.com/Tosserreddwarfstar408/nkit2iso` appears in GitHub search. It is not
a fork, it does not contain commit `b97129c`, and it was not used.
