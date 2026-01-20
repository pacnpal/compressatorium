# Copilot Instructions for Docker CHD Converter

## Project Overview

This is a Docker-based CHD (Compressed Hunks of Data) converter that uses MAME's `chdman` tool to compress retro gaming disc images (GDI, ISO, BIN/CUE formats) into the CHD format. The project is minimal by design, focusing on a single Dockerfile with an embedded conversion script.

## Technology Stack

- **Base Image:** Debian Trixie Slim
- **Primary Tool:** MAME Tools (chdman)
- **Shell:** Bash
- **Container Registry:** Docker Hub (`marctv/chd-converter`) and GitHub Container Registry

## Build and Test Commands

### Build Docker Image
```bash
docker build -t marctv/chd-converter .
```

### Test CD Conversion (Default Mode)
```bash
docker run --rm -v "$(pwd)/test:/tmp/images:rw" -it marctv/chd-converter
```

### Test DVD Conversion Mode
```bash
docker run --rm -e CHDMAN_MODE=createdvd -v "$(pwd)/test:/tmp/images:rw" -it marctv/chd-converter
```

### Verify CHD File
```bash
docker run --rm -v "$(pwd)/test:/tmp/images:rw" --entrypoint chdman -it marctv/chd-converter info -i "filename.chd"
```

## Coding Conventions

### Dockerfile Standards
- Use minimal base images (prefer `-slim` variants)
- Clean up package manager caches to reduce image size
- Combine RUN commands to reduce layers
- Use multi-line commands with proper formatting and indentation
- Always include `--no-install-recommends` with `apt-get install`

### Shell Script Standards
- Use Bash for the shell (already set with `SHELL ["/bin/bash", "-c"]`)
- Enable proper error handling where appropriate
- Use `nullglob` for safe file globbing
- Check for file existence before processing
- Provide clear user feedback with `echo` statements

### Environment Variables
- `CHDMAN_MODE`: Controls conversion mode (default: `createcd`, alternative: `createdvd`)
- Support aliases: `cd` → `createcd`, `dvd` → `createdvd`

## Project Constraints and Boundaries

### DO:
- Keep the project minimal and focused on its single purpose
- Maintain backwards compatibility with existing environment variables
- Ensure source files are never modified or deleted (read-only operations only)
- Skip conversion if `.chd` file already exists
- Support all documented file formats: `.gdi`, `.iso`, `.cue`
- Keep the image size as small as possible
- Provide clear error messages

### DO NOT:
- Add complex multi-stage builds unless absolutely necessary
- Introduce additional dependencies beyond mame-tools and bash
- Break the existing ENTRYPOINT script structure
- Remove the skip-existing-CHD logic
- Add file deletion or modification features
- Complicate the simple user experience
- Add web UI or API layers (this is a CLI tool)

## GitHub Actions Workflow

The repository uses GitHub Actions for automated Docker builds:
- **Trigger:** Push to `latest` branch or version tags (`v*`)
- **Platforms:** linux/amd64, linux/arm64
- **Registries:** Docker Hub and GitHub Container Registry (ghcr.io)
- **Secrets Required:**
  - `DOCKER_HUB_USERNAME`
  - `DOCKER_HUB_ACCESS_TOKEN`
  - `GITHUB_TOKEN` (automatically provided)

### Testing Workflow Changes
When modifying `.github/workflows/docker-image.yml`:
- Test locally with `act` if possible
- Verify secret references are correct
- Ensure multi-platform builds are maintained
- Check that both registries receive updates

## Documentation Standards

### README.md
- Keep examples simple and copy-paste ready
- Show both `$(pwd)` and absolute path examples
- Document all supported modes and options
- Include verification commands

### Code Comments
- Minimal inline comments (the script should be self-documenting)
- Document complex case statements and loops
- Explain non-obvious error handling

## Security Considerations

- Never commit Docker Hub credentials or tokens
- Use secrets for sensitive data in GitHub Actions
- Ensure mounted volumes have appropriate permissions (`:rw` only when necessary)
- Run as non-root user if adding user management (not currently implemented)

## Common Tasks

### Adding Support for New File Format
1. Add the extension to the glob pattern in the ENTRYPOINT loop
2. Test with sample files
3. Update README.md with the new format
4. Verify existing formats still work

### Adding New Conversion Mode
1. Add mode validation in the case statement
2. Implement the chdman command with appropriate flags
3. Update README.md with usage example
4. Test with appropriate test files

### Updating Base Image or Dependencies
1. Update the base image tag in Dockerfile
2. Test the build locally
3. Verify chdman functionality with test conversions
4. Check final image size hasn't increased significantly

## Testing Checklist

Before merging changes:
- [ ] Docker image builds successfully
- [ ] CD mode (createcd) works with test ISO/CUE files
- [ ] DVD mode (createdvd) works with test ISO files
- [ ] Existing CHD files are properly skipped
- [ ] Error messages are clear and helpful
- [ ] Multi-platform build works (amd64, arm64)
- [ ] Image size hasn't increased unexpectedly
- [ ] README.md examples are accurate and tested
