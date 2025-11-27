# Docker CHD "Compressed Hunks of Data" Converter  
Compresses GDI, ISO, BIN and CUE files to CHD using **CHDMAN** from MAME Tools.

* Skips existing `.chd` files  
* Does not delete or modify source files  
* Optional: choose between `createcd` (default) or `createdvd`

---

## Quick Start — CD Conversion (Default)

```bash
  docker run \
  --rm \
  -v "$(pwd)/isofiles/:/tmp/images/:rw" \
  -it marctv/chd-converter
```

$(pwd)/images/ is the local path. Could also be:

```bash
  docker run \
  --rm \
  -v "/user/isofiles:/tmp/images/:rw" \
  -it marctv/chd-converter
```

## createdvd (Optional) 

This is important for PlayStation Portable (PSP) CHD files.

 ```bash 
    docker run \
  --rm \
  -e CHDMAN_MODE=createdvd \
  -v "$(pwd)/isofiles/:/tmp/images/:rw" \
  -it marctv/chd-converter
```

## check existing CHD files

 ```bash 
docker run --rm \
  -v "/volume1/base/chdmaker:/tmp/images/:rw" \
  --entrypoint chdman \
  -it marctv/chd-converter \
  info -i "WipEout Pure (USA) (En,Fr,Es) (v2.00)-60fps patch.chd"
 ```