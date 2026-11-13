package main

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
)

// GameCube NKit v01 -> plain ISO restore. Ported (behaviour, not source text)
// from Nanook/NKitv1: NkitReaderGc.cs, Gaps.cs, NkitFormat.cs, FileSystem.cs.

const gcHeaderSize = 0x440

// gap header types (low 2 bits of the gap word)
const (
	gapAllJunk     = 0
	gapAllScrubbed = 1
	gapMixed       = 2
	gapJunkFile    = 3
)

// gap block types (top 2 bits of each block word)
const (
	blkJunk     = 0
	blkNonJunk  = 1
	blkByteFill = 2
	blkRepeat   = 3
)

const gapBlockSize = 0x100

func be32(b []byte, o int) uint32       { return binary.BigEndian.Uint32(b[o:]) }
func putBE32(b []byte, o int, v uint32) { binary.BigEndian.PutUint32(b[o:], v) }
func alignUp4(n int64) int64            { return (n + 3) &^ 3 }

// sortFst orders FST files by data offset then length (NKit's canonical order).
func sortFst(files []fstEntry) {
	sort.SliceStable(files, func(i, j int) bool {
		if files[i].dataOffset != files[j].dataOffset {
			return files[i].dataOffset < files[j].dataOffset
		}
		return files[i].length < files[j].length
	})
}

type fstEntry struct {
	dataOffset int64
	length     int64
	offInFst   int
}

type conFile struct {
	f         fstEntry
	gapLength int64
}

type restorer struct {
	in        *bufio.Reader
	out       *bufio.Writer
	junk      *junkStream
	srcPos    int64
	dstPos    int64
	nullsPos  int64
	imageSize int64
	hdr       []byte
	fst       []byte
	mainDol   int64
	zeros     []byte
	wbuf      [4]byte
	progress  func(cur, total int64)
}

// restore reads a GameCube .nkit.iso from `in` (already at offset 0) and writes
// the reconstructed plain ISO to `outFile`. It returns the original CRC32 stored
// in the NKit header for the caller to verify against the written file.
// inLen is the total byte length of the input .nkit.iso.
func restore(in io.Reader, outFile *os.File, inLen int64, progress func(cur, total int64)) (nkitCrc uint32, err error) {
	br := bufio.NewReaderSize(in, 1<<20)

	hdr := make([]byte, gcHeaderSize)
	if _, err = io.ReadFull(br, hdr); err != nil {
		return 0, fmt.Errorf("reading header: %w", err)
	}
	if err = detectGC(hdr); err != nil {
		return 0, err
	}

	nkitCrc = be32(hdr, 0x208)
	imageSize := int64(be32(hdr, 0x210))
	mainDol := int64(be32(hdr, 0x420))
	fstOffset := int64(be32(hdr, 0x424))
	fstSize := alignUp4(int64(be32(hdr, 0x428)))

	// junk id: 0x214 override, else the 4-byte game code at 0x00
	var id [4]byte
	if be32(hdr, 0x214) != 0 {
		copy(id[:], hdr[0x214:0x218])
	} else {
		copy(id[:], hdr[0x00:0x04])
	}
	junk := newJunkStream(id, hdr[0x06], imageSize)

	// zero the NKit metadata window (0x200..0x21B) back to original padding
	for i := 0x200; i < 0x21C; i++ {
		hdr[i] = 0
	}

	hdrToFst := make([]byte, fstOffset-gcHeaderSize)
	if _, err = io.ReadFull(br, hdrToFst); err != nil {
		return 0, fmt.Errorf("reading hdr->fst region: %w", err)
	}
	fst := make([]byte, fstSize)
	if _, err = io.ReadFull(br, fst); err != nil {
		return 0, fmt.Errorf("reading fst: %w", err)
	}

	bw := bufio.NewWriterSize(outFile, 1<<20)
	// placeholder write; hdr and fst get patched in memory and rewritten at the end
	if _, err = bw.Write(hdr); err != nil {
		return 0, err
	}
	if _, err = bw.Write(hdrToFst); err != nil {
		return 0, err
	}
	if _, err = bw.Write(fst); err != nil {
		return 0, err
	}

	r := &restorer{
		in: br, out: bw, junk: junk,
		srcPos: fstOffset + fstSize, dstPos: fstOffset + fstSize,
		nullsPos:  fstOffset + fstSize + 0x1c,
		imageSize: imageSize, hdr: hdr, fst: fst, mainDol: mainDol,
		progress: progress,
	}

	con, cerr := buildConFiles(hdr, fst, inLen)
	if con == nil {
		// bad/unparseable FST: treat the whole remainder as one gap
		if cerr != nil {
			fmt.Fprintf(os.Stderr, "warning: %v; converting as raw image\n", cerr)
		}
		cf := conFile{f: fstEntry{dataOffset: fstOffset, length: fstSize, offInFst: -1}, gapLength: inLen - r.srcPos}
		if err = r.writeGap(&cf, true); err != nil {
			return 0, err
		}
	} else {
		firstFile := true
		for i := range con {
			f := &con[i]
			ff := f.f
			if !firstFile {
				if r.srcPos < ff.dataOffset {
					if err = r.discard(ff.dataOffset - r.srcPos); err != nil {
						return 0, err
					}
					r.srcPos = ff.dataOffset
				}
				if ff.dataOffset == r.mainDol {
					putBE32(r.hdr, 0x420, uint32(r.dstPos))
				}
				putBE32(r.fst, ff.offInFst, uint32(r.dstPos))
				if err = r.copyFile(f); err != nil {
					return 0, err
				}
			}
			if r.dstPos < r.imageSize {
				if err = r.writeGap(f, i == 0 || i == len(con)-1); err != nil {
					return 0, err
				}
				if !firstFile {
					putBE32(r.fst, ff.offInFst+4, uint32(f.f.length))
				}
			}
			firstFile = false
		}
	}

	if r.dstPos != imageSize {
		return 0, fmt.Errorf("reconstructed %d bytes, expected %d (corrupt input?)", r.dstPos, imageSize)
	}
	if err = bw.Flush(); err != nil {
		return 0, err
	}
	// rewrite the patched header and FST in place
	if _, err = outFile.WriteAt(hdr, 0); err != nil {
		return 0, err
	}
	if _, err = outFile.WriteAt(fst, fstOffset); err != nil {
		return 0, err
	}
	return nkitCrc, nil
}

func detectGC(hdr []byte) error {
	if be32(hdr, 0x18) == 0x5D1C9EA3 {
		return errors.New("this is a Wii image — only GameCube NKit is supported (Wii support is planned)")
	}
	if be32(hdr, 0x1C) != 0xC2339F3D {
		return errors.New("not a GameCube disc image (magic 0xC2339F3D missing at 0x1C)")
	}
	if string(hdr[0x200:0x208]) != "NKIT v01" {
		return errors.New("not an NKit v01 image (marker missing at 0x200) — is it already a plain ISO?")
	}
	if be32(hdr, 0x218) != 0 {
		return errors.New("Wii NKit image with removed update partition — not supported yet")
	}
	return nil
}

// buildConFiles mirrors NkitFormat.GetConvertFstFiles: a synthetic fst.bin file
// followed by every real file (sorted by offset then length), each paired with
// the gap that follows it. inLen is the shrunk .nkit.iso length.
func buildConFiles(hdr, fst []byte, inLen int64) ([]conFile, error) {
	fstOffset := int64(be32(hdr, 0x424))
	fstSize := int64(len(fst))

	files, err := parseFst(fst, 1)
	if err != nil {
		return nil, err
	}
	if len(files) == 0 {
		return nil, errors.New("empty FST")
	}
	sortFst(files)

	con := make([]conFile, 0, len(files)+1)
	synth := fstEntry{dataOffset: fstOffset, length: fstSize, offInFst: -1}
	for i := 0; i < len(files); i++ {
		prev := synth
		if i > 0 {
			prev = files[i-1]
		}
		gap := files[i].dataOffset - alignUp4(prev.dataOffset+prev.length)
		if gap < 0 {
			return nil, fmt.Errorf("negative gap (%d) before file %d", gap, i)
		}
		con = append(con, conFile{f: prev, gapLength: gap})
	}
	last := files[len(files)-1]
	gap := inLen - alignUp4(last.dataOffset+last.length)
	if gap >= -3 && gap < 0 {
		gap = 0
	}
	if gap < 0 {
		return nil, fmt.Errorf("negative trailing gap (%d)", gap)
	}
	con = append(con, conFile{f: last, gapLength: gap})
	return con, nil
}

// parseFst extracts every file entry from a GameCube FST (12-byte records:
// type/name at 0, offset at +4, length at +8; entry 0 is root, its length is
// the total entry count). Directory entries are skipped.
func parseFst(fst []byte, mlt int64) ([]fstEntry, error) {
	if len(fst) < 12 {
		return nil, errors.New("FST too small")
	}
	n := int64(be32(fst, 0x8))
	if 12*n > int64(len(fst)) || n < 1 {
		return nil, fmt.Errorf("FST entry count %d exceeds FST size", n)
	}
	var files []fstEntry
	for i := int64(1); i < n; i++ {
		base := int(12 * i)
		if be32(fst, base)>>24 == 1 {
			continue // directory
		}
		files = append(files, fstEntry{
			dataOffset: int64(be32(fst, base+4)) * mlt, // Wii scales file offsets by 4
			length:     int64(be32(fst, base+8)),
			offInFst:   base + 4,
		})
	}
	return files, nil
}

func (r *restorer) readWord() (uint32, error) {
	if _, err := io.ReadFull(r.in, r.wbuf[:]); err != nil {
		return 0, err
	}
	return binary.BigEndian.Uint32(r.wbuf[:]), nil
}

func (r *restorer) discard(n int64) error {
	_, err := io.CopyN(io.Discard, r.in, n)
	return err
}

func (r *restorer) copyOut(n int64) error {
	_, err := io.CopyN(r.out, r.in, n)
	return err
}

func (r *restorer) writeZeros(n int64) error {
	if r.zeros == nil {
		r.zeros = make([]byte, 0x10000)
	}
	return r.writeRepeat(r.zeros, n)
}

func (r *restorer) writeFill(v byte, n int64) error {
	if v == 0 {
		return r.writeZeros(n)
	}
	buf := make([]byte, 0x1000)
	for i := range buf {
		buf[i] = v
	}
	return r.writeRepeat(buf, n)
}

func (r *restorer) writeRepeat(buf []byte, n int64) error {
	for n > 0 {
		c := int64(len(buf))
		if c > n {
			c = n
		}
		if _, err := r.out.Write(buf[:c]); err != nil {
			return err
		}
		n -= c
	}
	return nil
}

func (r *restorer) writeJunk(at, n int64) error {
	r.junk.seek(at)
	return r.junk.writeTo(r.out, n)
}

func (r *restorer) copyFile(f *conFile) error {
	length := f.f.length
	if length == 0 {
		return nil
	}
	size := alignUp4(length)
	if size > r.imageSize-r.dstPos {
		size = r.imageSize - r.dstPos
	}
	if err := r.copyOut(size); err != nil {
		return err
	}
	r.srcPos += size
	r.dstPos += size
	r.nullsPos = r.dstPos + 0x1c
	if r.progress != nil {
		r.progress(r.dstPos, r.imageSize)
	}
	return nil
}

func (r *restorer) writeGap(f *conFile, firstOrLast bool) error {
	if f.gapLength == 0 {
		if f.f.length == 0 {
			r.nullsPos = r.dstPos + 0x1c
		}
		return nil
	}

	word, err := r.readWord()
	if err != nil {
		return err
	}
	r.srcPos += 4
	size := int64(word &^ 0b11)
	gt := int(word & 0b11)
	if size == 0xFFFFFFFC { // Wii only; never for GC
		w2, err := r.readWord()
		if err != nil {
			return err
		}
		r.srcPos += 4
		size = 0xFFFFFFFC + int64(w2)
	}

	var nulls, junkFileLen int64
	if gt == gapJunkFile {
		if r.nullsPos-r.dstPos < 0 {
			r.nullsPos = r.nullsPos - r.dstPos
		} else {
			r.nullsPos = 0
		}
		nulls = (size & 0xFC) >> 2
		w2, err := r.readWord()
		if err != nil {
			return err
		}
		r.srcPos += 4
		junkFileLen = int64(w2)
		f.f.length = junkFileLen
		junkFileLen = alignUp4(junkFileLen)
		if err := r.writeZeros(nulls); err != nil {
			return err
		}
		if err := r.writeJunk(r.dstPos+nulls, junkFileLen-nulls); err != nil {
			return err
		}
		r.dstPos += junkFileLen
		if f.gapLength <= 8 {
			return nil
		}
		word, err = r.readWord()
		if err != nil {
			return err
		}
		r.srcPos += 4
		size = int64(word &^ 0b11)
		gt = int(word & 0b11)
	} else if f.f.length == 0 {
		r.nullsPos = r.dstPos + 0x1c
	}

	nulls = leadingNulls(size, r.nullsPos-r.dstPos, size, firstOrLast)

	switch gt {
	case gapAllJunk:
		if err := r.writeZeros(nulls); err != nil {
			return err
		}
		if err := r.writeJunk(r.dstPos+nulls, size-nulls); err != nil {
			return err
		}
		r.dstPos += size
	case gapAllScrubbed:
		if err := r.writeZeros(size); err != nil {
			return err
		}
		r.dstPos += size
	default: // mixed
		prg := size
		var btByte byte
		bt := blkJunk
		for prg > 0 {
			blk, err := r.readWord()
			if err != nil {
				return err
			}
			r.srcPos += 4
			btType := int(blk >> 30)
			btRepeat := btType == blkRepeat
			if !btRepeat {
				bt = btType
			}
			cnt := int64(blk & 0x3FFFFFFF)

			var n int64
			switch bt {
			case blkNonJunk:
				n = cnt * gapBlockSize
				if n > prg {
					n = prg
				}
				if err := r.copyOut(n); err != nil {
					return err
				}
				r.srcPos += n
			case blkByteFill:
				if !btRepeat {
					btByte = byte(cnt & 0xFF)
					cnt >>= 8
				}
				n = cnt * gapBlockSize
				if n > prg {
					n = prg
				}
				if err := r.writeFill(btByte, n); err != nil {
					return err
				}
			default: // junk
				n = cnt * gapBlockSize
				if n > prg {
					n = prg
				}
				bn := leadingNulls(prg, r.nullsPos-r.dstPos, n, firstOrLast)
				if err := r.writeZeros(bn); err != nil {
					return err
				}
				if err := r.writeJunk(r.dstPos+bn, n-bn); err != nil {
					return err
				}
			}
			prg -= n
			r.dstPos += n
		}
	}
	if r.progress != nil {
		r.progress(r.dstPos, r.imageSize)
	}
	return nil
}

// leadingNulls reproduces the small run of literal 00 bytes at the start of a
// junk region. `avail` is what's compared against maxNulls (the whole gap size,
// or remaining bytes inside a mixed gap); `chunk` is the bytes being emitted.
func leadingNulls(avail, maxNulls, chunk int64, firstOrLast bool) int64 {
	if maxNulls < 0 {
		maxNulls = 0
	}
	if avail < maxNulls {
		return chunk
	}
	if chunk >= 0x40000 && !firstOrLast {
		return 0
	}
	return maxNulls
}
