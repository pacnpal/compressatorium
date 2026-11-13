package main

import (
	"bufio"
	"crypto/aes"
	"crypto/cipher"
	"crypto/sha1"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Wii NKit v01 -> plain ISO restore. Ported (behaviour, not source text) from
// Nanook/NKitv1: NkitReaderWii.cs, WiiPartition*.cs, WiiHashStore.cs.
//
// A Wii disc is a 0x50000 header + up to 4 encrypted partitions. Each partition
// is AES-128-CBC encrypted in 0x8000 clusters (0x400 SHA-1 hash block + 0x7C00
// payload). NKit stores the decrypted, hash-free, junk-free filesystem; restore
// rebuilds the filesystem (junk regenerated), the H0-H3 hash tree, and the
// encryption. Verified bit-exact against the header CRC32.

const (
	wiiHeaderSize = 0x50000
	clusterSize   = 0x8000
	payloadSize   = 0x7c00
	hashBlockSize = 0x400
	groupClusters = 64
	wiiGroupSize  = clusterSize * groupClusters // 0x200000
)

func dataToHashedLen(d int64) int64 { return d/payloadSize*clusterSize + d%payloadSize }
func hashedLenToData(h int64) int64 { return h/clusterSize*payloadSize + h%clusterSize }

// ---- key derivation ------------------------------------------------------

// The three Wii common keys, obfuscated exactly as NKit stores them (the key
// bytes are interleaved 3-ways in this base64 blob). De-interleaving is the
// source's own method; we reproduce it rather than pasting a raw key constant.
const wiiLame = "oWPrYLjkSisqarQicfReI2GFtU6TKS7krhNIi/LZ7P7FMvtFyLpzFkyB/Juqqn73"

func wiiCommonKey(sel int) []byte {
	lame, _ := base64.StdEncoding.DecodeString(wiiLame)
	key := make([]byte, 16)
	for j, i := 0, sel; j < 16; j, i = j+1, i+3 {
		key[j] = lame[i]
	}
	return key
}

// deriveTitleKey decrypts the partition's title key from its 0x20000 header.
func deriveTitleKey(ph []byte) (cipher.Block, error) {
	issuer := strings.TrimRight(string(ph[0x140:0x180]), "\x00")
	isRvt := issuer == "Root-CA00000002-XS00000006"
	isKorean := !isRvt && ph[0x1f1] == 1
	sel := 2 // retail
	if isRvt {
		sel = 0
	} else if isKorean {
		sel = 1
	}
	common, err := aes.NewCipher(wiiCommonKey(sel))
	if err != nil {
		return nil, err
	}
	titleKey := make([]byte, 16)
	copy(titleKey, ph[0x1bf:0x1cf])
	iv := make([]byte, 16)
	copy(iv, ph[0x1dc:0x1e4]) // title id (8 bytes) + zeros
	cipher.NewCBCDecrypter(common, iv).CryptBlocks(titleKey, titleKey)
	return aes.NewCipher(titleKey)
}

// ---- hash tree + encryption ---------------------------------------------

var blankHash = sha1.Sum(make([]byte, hashBlockSize))

// hashAndEncryptGroup takes a group buffer whose used clusters hold payload at
// [b*0x8000+0x400 .. +0x8000] (hash areas empty), fills the H0/H1/H2 hash tree,
// then AES-CBC encrypts each used cluster in place. `blocks` = used clusters.
func hashAndEncryptGroup(buf []byte, blocks int, key cipher.Block) {
	var h0 [groupClusters][31 * 20]byte
	for b := 0; b < groupClusters; b++ {
		base := b * clusterSize
		for i := 0; i < 31; i++ {
			if b < blocks {
				sum := sha1.Sum(buf[base+(i+1)*hashBlockSize : base+(i+2)*hashBlockSize])
				copy(h0[b][i*20:], sum[:])
			} else {
				copy(h0[b][i*20:], blankHash[:])
			}
		}
	}
	var h1 [8][8 * 20]byte
	var h2 [8 * 20]byte
	for j := 0; j < 8; j++ {
		for k := 0; k < 8; k++ {
			sum := sha1.Sum(h0[8*j+k][:])
			copy(h1[j][k*20:], sum[:])
		}
		sum := sha1.Sum(h1[j][:])
		copy(h2[j*20:], sum[:])
	}
	for b := 0; b < blocks; b++ {
		base := b * clusterSize
		hb := buf[base : base+hashBlockSize]
		for i := range hb {
			hb[i] = 0
		}
		copy(buf[base:base+0x26c], h0[b][:])
		copy(buf[base+0x280:base+0x320], h1[b/8][:])
		copy(buf[base+0x340:base+0x3e0], h2[:])
	}
	for b := 0; b < blocks; b++ {
		encryptCluster(buf, b*clusterSize, key)
	}
}

// encryptCluster AES-CBC encrypts one 0x8000 cluster in place: the 0x400 hash
// block with a zero IV, then the 0x7C00 payload with the IV taken from bytes
// 0x3D0..0x3E0 of the freshly encrypted hash block.
func encryptCluster(buf []byte, base int, key cipher.Block) {
	iv := make([]byte, 16)
	cipher.NewCBCEncrypter(key, iv).CryptBlocks(buf[base:base+hashBlockSize], buf[base:base+hashBlockSize])
	copy(iv, buf[base+0x3d0:base+0x3e0])
	cipher.NewCBCEncrypter(key, iv).CryptBlocks(buf[base+hashBlockSize:base+clusterSize], buf[base+hashBlockSize:base+clusterSize])
}

// ---- partition table -----------------------------------------------------

type wiiPart struct {
	rawOffset   int64 // physical (shrunk) offset in the nkit
	origOffset  int64 // reconstructed original disc offset
	typ         uint32
	tableOffset int // byte offset of this entry's offset field in the disc header
}

func parseWiiPartitions(hdr []byte) []wiiPart {
	return parseWiiPartitionTable(hdr, 0x40000)
}

// parseWiiPartitionTable parses a Wii partition table whose four group headers
// start at b[tableBase]. The entry-table pointers stored in the group headers
// are absolute disc offsets (value*4, normally inside 0x40000..0x40100), so
// they are rebased onto tableBase; that lets the same parser read the
// original-table backup NKit keeps at the start of the update-partition
// placeholder (where the table sits at offset 0).
func parseWiiPartitionTable(b []byte, tableBase int) []wiiPart {
	var parts []wiiPart
	for t := 0; t < 4; t++ {
		count := be32(b, tableBase+t*8)
		if count == 0 {
			continue
		}
		tableOff := int(be32(b, tableBase+t*8+4))*4 - 0x40000 + tableBase
		for i := 0; i < int(count); i++ {
			e := tableOff + i*8
			if e < 0 || e+8 > len(b) {
				break
			}
			parts = append(parts, wiiPart{
				rawOffset:   int64(be32(b, e)) * 4,
				typ:         be32(b, e+4),
				tableOffset: e,
			})
		}
	}
	sort.SliceStable(parts, func(i, j int) bool { return parts[i].rawOffset < parts[j].rawOffset })
	return parts
}

const (
	ptData    = 0
	ptUpdate  = 1
	ptChannel = 2
)

// ---- main restore --------------------------------------------------------

func restoreWii(in io.Reader, outFile *os.File, inLen int64, resolve recoveryResolver, progress func(cur, total int64)) (uint32, bool, error) {
	br := bufio.NewReaderSize(in, 1<<20)
	hdr := make([]byte, wiiHeaderSize)
	if _, err := io.ReadFull(br, hdr); err != nil {
		return 0, false, fmt.Errorf("reading disc header: %w", err)
	}
	if string(hdr[0x200:0x208]) != "NKIT v01" {
		return 0, false, errors.New("not an NKit v01 image (marker missing at 0x200)")
	}
	nkitCrc := be32(hdr, 0x208)
	imageSize := int64(be32(hdr, 0x210)) * 4
	updateCrc := be32(hdr, 0x218)
	discID := [4]byte{hdr[0], hdr[1], hdr[2], hdr[3]}
	discNo := hdr[6]

	// zero the NKit metadata window + the two no-hash/no-crypt flag bytes
	for i := 0x200; i < 0x21c; i++ {
		hdr[i] = 0
	}
	hdr[0x60], hdr[0x61] = 0, 0

	parts := parseWiiPartitions(hdr)
	if len(parts) == 0 {
		return 0, false, errors.New("no partitions found")
	}

	// A non-zero updateCrc means the update partition was removed when this
	// image was shrunk — its bytes are simply not in the file. NKit left a
	// placeholder in its place whose first 0x100 bytes back up the original
	// partition table. The resolver may supply the removed bytes (downloaded
	// recovery file) for a bit-exact restore; otherwise mirror official
	// NKit's missing-recovery behaviour: zero-fill the whole original update
	// region and restore that exact table, which yields a playable but not
	// bit-exact image.
	var origTable []byte
	var zeroFillEnd int64
	var recovery io.ReadCloser
	var recoverySize int64
	if updateCrc != 0 {
		fillLen := parts[0].rawOffset - wiiHeaderSize
		if fillLen <= 0 || fillLen > 1<<20 {
			return 0, false, fmt.Errorf("unexpected update-partition placeholder size 0x%X", fillLen)
		}
		placeholder := make([]byte, fillLen)
		if _, err := io.ReadFull(br, placeholder); err != nil {
			return 0, false, fmt.Errorf("reading update-partition placeholder: %w", err)
		}
		origParts := parseWiiPartitionTable(placeholder, 0)
		var first *wiiPart
		for i := range origParts {
			if origParts[i].typ != ptUpdate && origParts[i].rawOffset >= wiiHeaderSize {
				first = &origParts[i]
				break
			}
		}
		if first == nil {
			return 0, false, errors.New("update-partition placeholder holds no usable partition table")
		}
		origTable = placeholder[:0x100]
		zeroFillEnd = first.rawOffset
		if resolve != nil {
			recovery, recoverySize = resolve(updateCrc, zeroFillEnd-wiiHeaderSize)
		}
		if recovery != nil {
			defer recovery.Close()
		} else {
			fmt.Fprintf(os.Stderr, "WARNING: this image was shrunk by removing its update partition (*_%08X);\n"+
				"         that data is not in the file, so the region is zero-filled instead.\n"+
				"         The ISO works in Dolphin and USB loaders, but is NOT bit-exact\n"+
				"         (not redump-verifiable) and may not boot on an unmodified console.\n", updateCrc)
		}
	}

	bw := bufio.NewWriterSize(outFile, 1<<20)
	if _, err := bw.Write(hdr); err != nil {
		return 0, false, err
	}

	st := &wiiState{br: br, bw: bw, imageSize: imageSize, discID: discID, discNo: discNo,
		srcPos: wiiHeaderSize, dstPos: wiiHeaderSize, progress: progress,
		tempDir: filepath.Dir(outFile.Name())}
	st.lastType = 0xffffffff // "Other"

	if origTable != nil {
		// Splice the recovery bytes (if any) at 0x50000; the rest of the
		// region up to the first surviving partition is zeros on disc.
		if recovery != nil {
			if n, err := io.Copy(bw, recovery); err != nil {
				return 0, false, fmt.Errorf("splicing recovery file: %w", err)
			} else if n != recoverySize {
				return 0, false, fmt.Errorf("splicing recovery file: got %d bytes, expected %d", n, recoverySize)
			}
		}
		if err := writeZeros(bw, zeroFillEnd-wiiHeaderSize-recoverySize); err != nil {
			return 0, false, err
		}
		st.srcPos = parts[0].rawOffset
		st.dstPos = zeroFillEnd
	}

	for i := range parts {
		p := &parts[i]
		if p.rawOffset > st.srcPos {
			if err := st.writeFiller(); err != nil {
				return 0, false, err
			}
			if err := st.discard(p.rawOffset - st.srcPos); err != nil {
				return 0, false, err
			}
			st.srcPos = p.rawOffset
		}
		p.origOffset = st.dstPos
		if err := st.restorePartition(p); err != nil {
			return 0, false, fmt.Errorf("partition at 0x%X: %w", p.rawOffset, err)
		}
	}

	if st.srcPos < inLen {
		if err := st.writeFiller(); err != nil {
			return 0, false, err
		}
	}

	if origTable != nil {
		// restore the exact original partition table (update entry included)
		copy(hdr[0x40000:0x40100], origTable)
	} else {
		// rewrite the partition table offsets to the reconstructed original positions
		for _, p := range parts {
			putBE32(hdr, p.tableOffset, uint32(p.origOffset/4))
		}
	}

	if st.dstPos != imageSize {
		return 0, false, fmt.Errorf("reconstructed %d bytes, expected %d", st.dstPos, imageSize)
	}
	if err := bw.Flush(); err != nil {
		return 0, false, err
	}
	if _, err := outFile.WriteAt(hdr, 0); err != nil {
		return 0, false, err
	}
	return nkitCrc, origTable == nil || recovery != nil, nil
}

// writeZeros streams n zero bytes to w.
func writeZeros(w io.Writer, n int64) error {
	buf := make([]byte, 1<<20)
	for n > 0 {
		c := min64(n, int64(len(buf)))
		if _, err := w.Write(buf[:c]); err != nil {
			return err
		}
		n -= c
	}
	return nil
}

type wiiState struct {
	br        *bufio.Reader
	bw        *bufio.Writer
	imageSize int64
	discID    [4]byte
	discNo    byte
	srcPos    int64
	dstPos    int64
	lastType  uint32
	lastPart  [4]byte
	tempDir   string
	progress  func(cur, total int64)
}

func (s *wiiState) discard(n int64) error {
	_, err := io.CopyN(io.Discard, s.br, n)
	return err
}

// writeFiller regenerates inter-partition junk/gaps in raw disc space.
func (s *wiiState) writeFiller() error {
	id := s.discID
	length := s.imageSize
	if s.lastType == ptData {
		id = s.lastPart
	}
	if s.lastType == ptUpdate {
		length = 0
	}
	junk := newJunkStream(id, s.discNo, length)
	g := &wiiGap{br: s.br, out: s.bw, junk: junk, dstPos: s.dstPos, nullsPos: s.dstPos + 0x1c}
	if _, err := g.writeGapCore(-1, -1, true); err != nil {
		return err
	}
	s.srcPos += g.srcPos
	s.dstPos = g.dstPos
	return nil
}

func (s *wiiState) restorePartition(p *wiiPart) error {
	ph := make([]byte, 0x20000)
	if _, err := io.ReadFull(s.br, ph); err != nil {
		return fmt.Errorf("reading partition header: %w", err)
	}
	s.srcPos += 0x20000
	key, err := deriveTitleKey(ph)
	if err != nil {
		return err
	}
	shrunkSize := int64(be32(ph, 0x2bc)) * 4

	// Reconstruct the decrypted filesystem to a seekable temp file. FST/boot
	// offsets are only known after the file loop, so they are patched in place
	// there before we hash+encrypt (which must see the final bytes).
	tmp, err := os.CreateTemp(s.tempDir, "nkit2iso-dec-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	defer tmp.Close()

	fs, err := writeDecryptedFS(s.br, tmp, shrunkSize)
	if err != nil {
		return err
	}
	s.srcPos += fs.consumed

	putBE32(ph, 0x2bc, fs.sizeField) // restore real partition size
	if _, err := s.bw.Write(ph); err != nil {
		return err
	}
	s.dstPos += 0x20000

	// Read the preserved hash blocks for groups whose hashes can't be
	// recomputed (mixed real+scrubbed groups), keyed by group index.
	hashedLen := dataToHashedLen(fs.decSize)
	preserved := map[int][]byte{}
	for o := int64(0); o < hashedLen; o += wiiGroupSize {
		gi := int(o / wiiGroupSize)
		if flagBit(fs.flags, gi) {
			blocks := int(min64(wiiGroupSize, hashedLen-o)) / clusterSize
			hb := make([]byte, blocks*hashBlockSize)
			if _, err := io.ReadFull(s.br, hb); err != nil {
				return fmt.Errorf("reading preserved hashes: %w", err)
			}
			preserved[gi] = hb
			s.srcPos += int64(blocks * hashBlockSize)
		}
	}

	if _, err := tmp.Seek(0, io.SeekStart); err != nil {
		return err
	}
	tr := bufio.NewReaderSize(tmp, 1<<20)
	buf := make([]byte, wiiGroupSize)
	remaining := fs.decSize
	gi := 0
	for remaining > 0 {
		blocks := int(min64(groupClusters, remaining/payloadSize))
		for b := 0; b < blocks; b++ {
			if _, err := io.ReadFull(tr, buf[b*clusterSize+hashBlockSize:b*clusterSize+clusterSize]); err != nil {
				return fmt.Errorf("reading decrypted partition data: %w", err)
			}
		}
		if blocks < groupClusters {
			for i := blocks * clusterSize; i < len(buf); i++ {
				buf[i] = 0
			}
		}
		if err := encryptGroup(buf, blocks, gi, key, fs.scrubs, preserved[gi]); err != nil {
			return err
		}
		if _, err := s.bw.Write(buf[:blocks*clusterSize]); err != nil {
			return err
		}
		remaining -= int64(blocks) * payloadSize
		s.dstPos += int64(blocks) * clusterSize
		gi++
		if s.progress != nil {
			s.progress(s.dstPos, s.imageSize)
		}
	}

	s.lastType = p.typ
	s.lastPart = fs.bootID
	return nil
}

// flagBit reports whether group gi is flagged as preserved in the bitmap.
func flagBit(flags []byte, gi int) bool {
	byt := gi / 8
	if byt >= len(flags) {
		return false
	}
	return flags[byt]&(1<<(7-(gi%8))) != 0
}

// scrubByteAt returns the scrub byte if the cluster at decrypted data offset
// `off` falls in a scrubbed region. NKit's ScrubManager rounds scrub regions
// out to whole clusters (down at the start, up at the end), so a cluster is
// scrubbed if it overlaps the region at all.
func scrubByteAt(scrubs []scrubRegion, off int64) (byte, bool) {
	for _, r := range scrubs {
		lo := r.off - r.off%payloadSize
		hi := r.end
		if hi%payloadSize != 0 {
			hi += payloadSize - hi%payloadSize
		}
		if off >= lo && off < hi {
			return r.b, true
		}
	}
	return 0, false
}

// encryptGroup turns a group of decrypted payloads into the final encrypted
// bytes: scrubbed clusters become a constant byte, preserved groups reuse their
// stored hash blocks, and everything else gets a freshly computed hash tree.
func encryptGroup(buf []byte, blocks, gi int, key cipher.Block, scrubs []scrubRegion, stored []byte) error {
	var scrubByte [groupClusters]byte
	var isScrub [groupClusters]bool
	anyScrub, anyReal := false, false
	for b := 0; b < blocks; b++ {
		off := int64(gi*groupClusters+b) * payloadSize
		if by, ok := scrubByteAt(scrubs, off); ok {
			isScrub[b], scrubByte[b], anyScrub = true, by, true
		} else {
			anyReal = true
		}
	}

	if !anyScrub && stored == nil { // ordinary group: compute the whole hash tree
		hashAndEncryptGroup(buf, blocks, key)
		return nil
	}
	if anyScrub && anyReal && stored == nil {
		return fmt.Errorf("group %d mixes real and scrubbed clusters but has no preserved hashes", gi)
	}
	for b := 0; b < blocks; b++ {
		base := b * clusterSize
		if isScrub[b] {
			fill(buf[base:base+clusterSize], scrubByte[b])
			continue
		}
		copy(buf[base:base+hashBlockSize], stored[b*hashBlockSize:(b+1)*hashBlockSize])
		encryptCluster(buf, base, key)
	}
	return nil
}

func fill(b []byte, v byte) {
	for i := range b {
		b[i] = v
	}
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}
