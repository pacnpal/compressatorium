package main

import (
	"bufio"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Wii update partitions removed at shrink time are archived publicly on
// archive.org (the MarioCube NKit recovery collection). The item's file index
// maps each recovery file's name suffix (_<CRC32>) to the CRC stored in the
// NKit header at 0x218, so a removed partition can be fetched and spliced back
// for a bit-exact restore.

const (
	archiveItem        = "MarioCubeLite"
	archiveMetadataURL = "https://archive.org/metadata/" + archiveItem
	recoveryDirPrefix  = "NKit Recovery Partitions/Wii - Individual/Game Partitions/Update - "
)

// recoveryResolver is consulted when an image's update partition was removed
// (header word 0x218 != 0). It may return the partition's original bytes to
// splice at disc offset 0x50000; a nil reader means "zero-fill instead". The
// stream may be shorter than regionSize (the rest of the region is zeros on
// the original disc) but never longer.
type recoveryResolver func(updateCrc uint32, regionSize int64) (io.ReadCloser, int64)

// newRecoveryResolver builds the resolver for a -recovery mode: "none" means
// always zero-fill (nil resolver), "download" fetches without asking, and
// "ask" prompts when stdin is a terminal (otherwise it zero-fills and hints
// at -recovery download). Downloads that fail fall back to zero-filling.
func newRecoveryResolver(mode, tempDir string) recoveryResolver {
	if mode == "none" {
		return nil
	}
	return func(updateCrc uint32, regionSize int64) (io.ReadCloser, int64) {
		if mode == "ask" {
			if !stdinIsTerminal() {
				fmt.Fprintf(os.Stderr, "note: rerun with -recovery download to fetch the removed update partition\n"+
					"      from https://archive.org (item %s) and restore bit-exact.\n", archiveItem)
				return nil, 0
			}
			if !promptRecoveryDownload(updateCrc) {
				return nil, 0
			}
		}
		rc, size, err := fetchRecovery(updateCrc, regionSize, tempDir)
		if err != nil {
			fmt.Fprintf(os.Stderr, "WARNING: recovery download failed (%v);\n"+
				"         continuing with a zero-filled update region instead.\n", err)
			return nil, 0
		}
		return rc, size
	}
}

// promptRecoveryDownload explains both restore modes and asks the user (on a
// terminal) whether to download the removed update partition.
func promptRecoveryDownload(updateCrc uint32) bool {
	fmt.Fprintf(os.Stderr, "\n"+
		"This image was shrunk by removing its Wii update partition; those bytes are\n"+
		"not in the file. A matching recovery file (*_%08X) is publicly archived.\n"+
		"\n"+
		"  [1] Download it from archive.org and restore a bit-exact, redump-verified\n"+
		"      ISO (file index: %s)\n"+
		"  [2] Restore without it: the region is zero-filled, the ISO is full-size\n"+
		"      and plays in Dolphin/USB loaders, but is NOT bit-exact\n"+
		"\n", updateCrc, archiveMetadataURL)
	in := bufio.NewReader(os.Stdin)
	for {
		fmt.Fprint(os.Stderr, "Download the recovery file? [1/2] (default 1): ")
		line, err := in.ReadString('\n')
		if err != nil {
			fmt.Fprintln(os.Stderr)
			return false
		}
		switch strings.ToLower(strings.TrimSpace(line)) {
		case "", "1", "y", "yes":
			return true
		case "2", "n", "no":
			return false
		}
	}
}

func stdinIsTerminal() bool {
	fi, err := os.Stdin.Stat()
	return err == nil && fi.Mode()&os.ModeCharDevice != 0
}

// fetchRecovery looks the recovery file up in the archive.org index, prints
// the exact URLs involved, and downloads it to a temp file in dir.
func fetchRecovery(updateCrc uint32, regionSize int64, dir string) (io.ReadCloser, int64, error) {
	fmt.Fprintf(os.Stderr, "Looking up recovery file *_%08X via %s ...\n", updateCrc, archiveMetadataURL)
	f, err := lookupRecoveryFile(updateCrc)
	if err != nil {
		return nil, 0, err
	}
	if f.size > regionSize {
		return nil, 0, fmt.Errorf("recovery file is larger (%d bytes) than the missing region (%d bytes)", f.size, regionSize)
	}
	fmt.Fprintf(os.Stderr, "Downloading %s (%d MiB)\n", f.url(), (f.size+(1<<20)-1)>>20)
	rc, err := downloadRecoveryFile(f, dir)
	if err != nil {
		return nil, 0, err
	}
	return rc, f.size, nil
}

type recoveryFile struct {
	name string // path within the archive.org item
	size int64
	sha1 string // hex digest from the archive index, for download verification
}

func (f *recoveryFile) url() string {
	u := url.URL{Scheme: "https", Host: "archive.org", Path: "/download/" + archiveItem + "/" + f.name}
	return u.String()
}

func lookupRecoveryFile(updateCrc uint32) (*recoveryFile, error) {
	client := &http.Client{Timeout: 2 * time.Minute}
	resp, err := client.Get(archiveMetadataURL)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("archive.org index: HTTP %s", resp.Status)
	}
	return findRecoveryFile(resp.Body, updateCrc)
}

// findRecoveryFile stream-decodes the archive.org item metadata (a large JSON
// object whose "files" array lists every archived file) and picks the update
// partition recovery file whose name ends in the wanted CRC32.
func findRecoveryFile(r io.Reader, updateCrc uint32) (*recoveryFile, error) {
	suffix := fmt.Sprintf("_%08X", updateCrc)
	dec := json.NewDecoder(bufio.NewReaderSize(r, 1<<20))
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return nil, fmt.Errorf("malformed archive index (%v)", err)
	}
	for dec.More() {
		keyTok, err := dec.Token()
		if err != nil {
			return nil, err
		}
		if key, _ := keyTok.(string); key != "files" {
			var skip json.RawMessage
			if err := dec.Decode(&skip); err != nil {
				return nil, err
			}
			continue
		}
		if t, err := dec.Token(); err != nil || t != json.Delim('[') {
			return nil, fmt.Errorf("malformed archive index (%v)", err)
		}
		for dec.More() {
			var f struct {
				Name string `json:"name"`
				Size string `json:"size"`
				SHA1 string `json:"sha1"`
			}
			if err := dec.Decode(&f); err != nil {
				return nil, err
			}
			if !strings.HasPrefix(f.Name, recoveryDirPrefix) || !strings.HasSuffix(f.Name, suffix) {
				continue
			}
			size, err := strconv.ParseInt(f.Size, 10, 64)
			if err != nil || size <= 0 {
				return nil, fmt.Errorf("archive index has no valid size for %s", f.Name)
			}
			return &recoveryFile{name: f.Name, size: size, sha1: f.SHA1}, nil
		}
		return nil, fmt.Errorf("no recovery file *%s in the archive index", suffix)
	}
	return nil, fmt.Errorf("malformed archive index (no files list)")
}

// downloadRecoveryFile streams the file to a temp file in dir, verifying its
// size and SHA-1 against the archive index. The returned ReadCloser removes
// the temp file when closed.
func downloadRecoveryFile(f *recoveryFile, dir string) (io.ReadCloser, error) {
	resp, err := http.Get(f.url())
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %s", resp.Status)
	}

	tmp, err := os.CreateTemp(dir, "nkit2iso-recovery-*")
	if err != nil {
		return nil, err
	}
	ok := false
	defer func() {
		if !ok {
			tmp.Close()
			os.Remove(tmp.Name())
		}
	}()

	h := sha1.New()
	buf := make([]byte, 1<<20)
	var n int64
	last := -1
	for {
		c, rerr := resp.Body.Read(buf)
		if c > 0 {
			if _, err := tmp.Write(buf[:c]); err != nil {
				return nil, err
			}
			h.Write(buf[:c])
			n += int64(c)
			if pct := int(n * 100 / f.size); pct != last {
				last = pct
				fmt.Fprintf(os.Stderr, "\r  %3d%%", pct)
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			return nil, rerr
		}
	}
	fmt.Fprintln(os.Stderr)

	if n != f.size {
		return nil, fmt.Errorf("download truncated: got %d of %d bytes", n, f.size)
	}
	if got := hex.EncodeToString(h.Sum(nil)); f.sha1 != "" && got != f.sha1 {
		return nil, fmt.Errorf("download corrupt: SHA-1 %s, expected %s", got, f.sha1)
	}
	if _, err := tmp.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	ok = true
	return &tempFileReader{tmp}, nil
}

// tempFileReader removes the underlying temp file when closed.
type tempFileReader struct {
	*os.File
}

func (t *tempFileReader) Close() error {
	err := t.File.Close()
	os.Remove(t.File.Name())
	return err
}
