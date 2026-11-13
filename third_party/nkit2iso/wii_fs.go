package main

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
)

// writeDecryptedFS reconstructs one Wii partition's decrypted filesystem into a
// seekable temp file: it replays the stored boot.bin/apploader/FST and file
// data from the nkit and regenerates junk/gaps (in decrypted space). FST and
// boot offsets are patched in place afterwards. Returns the decrypted length,
// the number of nkit bytes consumed, the partition id, and the size field.
type fsResult struct {
	decSize   int64
	consumed  int64
	bootID    [4]byte
	sizeField uint32 // boot.bin[0x210] -> restored into partition header 0x2bc
	scrubs    []scrubRegion
	flags     []byte // WiiHashStore preserved-group bitmap
}

func writeDecryptedFS(br *bufio.Reader, tmp *os.File, shrunkSize int64) (fsResult, error) {
	var res fsResult
	boot := make([]byte, 0x440)
	if _, err := io.ReadFull(br, boot); err != nil {
		return res, fmt.Errorf("reading boot.bin: %w", err)
	}
	if string(boot[0:4]) == "\x00\x00\x00\x00" {
		return res, errors.New("null-id Wii partition not supported")
	}
	copy(res.bootID[:], boot[0:4])
	res.sizeField = be32(boot, 0x210)
	decSize := hashedLenToData(int64(res.sizeField) * 4)
	res.decSize = decSize

	fstOffset := int64(be32(boot, 0x424)) * 4
	fstSize := int64(be32(boot, 0x428)) * 4
	mainDol := int64(be32(boot, 0x420)) // in /4 units, matched against shrunk file offsets

	hdrToFst := make([]byte, fstOffset-0x440)
	if _, err := io.ReadFull(br, hdrToFst); err != nil {
		return res, fmt.Errorf("reading hdr->fst: %w", err)
	}
	fst := make([]byte, fstSize)
	if _, err := io.ReadFull(br, fst); err != nil {
		return res, fmt.Errorf("reading fst: %w", err)
	}
	flagsLen := wiiFlagsLen(decSize)
	flags := make([]byte, flagsLen)
	if _, err := io.ReadFull(br, flags); err != nil { // WiiHashStore preserved-hash bitmap
		return res, fmt.Errorf("reading hash flags: %w", err)
	}
	res.flags = flags

	for i := 0x200; i < 0x21c; i++ { // clear stashed nkit markers in boot.bin
		boot[i] = 0
	}

	bw := bufio.NewWriterSize(tmp, 1<<20)
	if _, err := bw.Write(boot); err != nil {
		return res, err
	}
	if _, err := bw.Write(hdrToFst); err != nil {
		return res, err
	}
	if _, err := bw.Write(fst); err != nil {
		return res, err
	}

	junk := newJunkStream(res.bootID, boot[6], decSize)
	g := &wiiGap{br: br, out: bw, junk: junk,
		srcPos: fstOffset + fstSize + flagsLen, dstPos: fstOffset + fstSize, nullsPos: fstOffset + fstSize + 0x1c}

	con, cerr := buildConFilesWii(boot, fst, shrunkSize)
	if con == nil {
		if cerr != nil {
			fmt.Fprintf(os.Stderr, "warning: %v; converting partition as raw\n", cerr)
		}
		cf := conFile{f: fstEntry{dataOffset: fstOffset, length: fstSize, offInFst: -1}, gapLength: shrunkSize - g.srcPos}
		if _, err := g.writeGapCore(cf.f.length, cf.gapLength, true); err != nil {
			return res, err
		}
	} else {
		con[0].gapLength -= flagsLen
		firstFile := true
		for i := range con {
			f := &con[i]
			ff := f.f
			if !firstFile {
				if g.srcPos < ff.dataOffset {
					if _, err := io.CopyN(io.Discard, br, ff.dataOffset-g.srcPos); err != nil {
						return res, err
					}
					g.srcPos = ff.dataOffset
				}
				if ff.dataOffset == mainDol {
					putBE32(boot, 0x420, uint32(g.dstPos/4))
				}
				putBE32(fst, ff.offInFst, uint32(g.dstPos/4))
				if err := g.copyFile(f); err != nil {
					return res, err
				}
			}
			if g.dstPos < decSize {
				newLen, err := g.writeGapCore(f.f.length, f.gapLength, i == 0 || i == len(con)-1)
				if err != nil {
					return res, err
				}
				f.f.length = newLen
				if !firstFile {
					putBE32(fst, ff.offInFst+4, uint32(f.f.length))
				}
			}
			firstFile = false
		}
	}

	if g.dstPos != decSize {
		return res, fmt.Errorf("decrypted %d bytes, expected %d", g.dstPos, decSize)
	}
	if err := bw.Flush(); err != nil {
		return res, err
	}
	// patch the FST (and boot) offsets that were rewritten during the loop
	if _, err := tmp.WriteAt(boot, 0); err != nil {
		return res, err
	}
	if _, err := tmp.WriteAt(fst, fstOffset); err != nil {
		return res, err
	}
	res.consumed = g.srcPos
	res.scrubs = g.scrubs
	return res, nil
}

// wiiFlagsLen is the size of the WiiHashStore flags bitmap that precedes the
// file data in the nkit (1 bit per 2 MB group, rounded to 4-byte ints).
func wiiFlagsLen(partitionDataSize int64) int64 {
	size := partitionDataSize / payloadSize * clusterSize
	groups := size / wiiGroupSize
	if size%wiiGroupSize != 0 {
		groups++
	}
	ints := groups / 32
	if groups%32 != 0 {
		ints++
	}
	return ints * 4
}

func buildConFilesWii(boot, fst []byte, shrunkSize int64) ([]conFile, error) {
	fstOffset := int64(be32(boot, 0x424)) * 4
	fstSize := int64(len(fst))

	files, err := parseFst(fst, 4)
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
	gap := shrunkSize - alignUp4(last.dataOffset+last.length)
	if gap >= -3 && gap < 0 {
		gap = 0
	}
	if gap < 0 {
		return nil, fmt.Errorf("negative trailing gap (%d)", gap)
	}
	con = append(con, conFile{f: last, gapLength: gap})
	return con, nil
}

// ---- Wii gap engine (ports NkitReaderWii.writeGap/copyFile) ---------------

type wiiGap struct {
	br       *bufio.Reader
	out      *bufio.Writer
	junk     *junkStream
	srcPos   int64 // physical position in the nkit partition data = bytes read
	dstPos   int64 // decrypted output position
	nullsPos int64
	scrubs   []scrubRegion
	wbuf     [4]byte
	zeros    []byte
}

// scrubRegion marks a run of decrypted space that was scrubbed to a constant
// byte (0x00 or 0xFF). Such clusters encrypt back to that byte, so on restore
// we emit the constant directly rather than hashing/encrypting.
type scrubRegion struct {
	off, end int64
	b        byte
}

// scrubWrite records a scrubbed region and writes placeholder zeros to the temp
// (the group loop emits the constant byte for these clusters, ignoring the temp).
// It does NOT advance dstPos — the caller does, as for every other gap block.
func (g *wiiGap) scrubWrite(size int64, b byte) error {
	if err := g.writeZeros(size); err != nil {
		return err
	}
	if n := len(g.scrubs); n > 0 && g.scrubs[n-1].end == g.dstPos && g.scrubs[n-1].b == b {
		g.scrubs[n-1].end = g.dstPos + size
	} else {
		g.scrubs = append(g.scrubs, scrubRegion{g.dstPos, g.dstPos + size, b})
	}
	return nil
}

func (g *wiiGap) readWord() (uint32, error) {
	if _, err := io.ReadFull(g.br, g.wbuf[:]); err != nil {
		return 0, err
	}
	return be32(g.wbuf[:], 0), nil
}

func (g *wiiGap) copyOut(n int64) error {
	_, err := io.CopyN(g.out, g.br, n)
	return err
}

func (g *wiiGap) writeZeros(n int64) error {
	if g.zeros == nil {
		g.zeros = make([]byte, 0x10000)
	}
	for n > 0 {
		c := int64(len(g.zeros))
		if c > n {
			c = n
		}
		if _, err := g.out.Write(g.zeros[:c]); err != nil {
			return err
		}
		n -= c
	}
	return nil
}

func (g *wiiGap) writeJunk(at, n int64) error {
	g.junk.seek(at)
	return g.junk.writeTo(g.out, n)
}

func (g *wiiGap) copyFile(f *conFile) error {
	length := f.f.length
	if length == 0 {
		return nil
	}
	size := alignUp4(length)
	if err := g.copyOut(size); err != nil {
		return err
	}
	g.srcPos += size
	g.dstPos += size
	g.nullsPos = g.dstPos + 0x1c
	return nil
}

// writeGapCore ports NkitReaderWii.writeGap. Returns the (possibly junk-file
// updated) file length. gapLen0 == -1 and fileLen0 == -1 for inter-file filler.
func (g *wiiGap) writeGapCore(fileLen0, gapLen0 int64, firstOrLast bool) (int64, error) {
	fileLength := fileLen0
	if gapLen0 == 0 {
		if fileLength == 0 {
			g.nullsPos = g.dstPos + 0x1c
		}
		return fileLength, nil
	}
	srcLen := gapLen0

	word, err := g.readWord()
	if err != nil {
		return fileLength, err
	}
	g.srcPos += 4
	size := int64(word &^ 3)
	gt := int(word & 3)
	if size == 0xFFFFFFFC { // Wii huge-gap continuation
		w2, err := g.readWord()
		if err != nil {
			return fileLength, err
		}
		g.srcPos += 4
		size = 0xFFFFFFFC + int64(w2)
	}

	var nulls, junkFileLen int64
	if gt == gapJunkFile {
		if g.nullsPos-g.dstPos < 0 {
			g.nullsPos = g.nullsPos - g.dstPos
		} else {
			g.nullsPos = 0
		}
		nulls = (size & 0xFC) >> 2
		w2, err := g.readWord()
		if err != nil {
			return fileLength, err
		}
		g.srcPos += 4
		junkFileLen = int64(w2)
		fileLength = junkFileLen
		junkFileLen = alignUp4(junkFileLen)
		if err := g.writeZeros(nulls); err != nil {
			return fileLength, err
		}
		if err := g.writeJunk(g.dstPos+nulls, junkFileLen-nulls); err != nil {
			return fileLength, err
		}
		g.dstPos += junkFileLen
		if srcLen <= 8 {
			return fileLength, nil
		}
		word, err = g.readWord()
		if err != nil {
			return fileLength, err
		}
		g.srcPos += 4
		size = int64(word &^ 3)
		gt = int(word & 3)
	} else if fileLength == 0 {
		g.nullsPos = g.dstPos + 0x1c
	}

	nulls = leadingNulls(size, g.nullsPos-g.dstPos, size, firstOrLast)
	g.nullsPos = g.dstPos + nulls

	switch gt {
	case gapAllJunk:
		if err := g.writeZeros(nulls); err != nil {
			return fileLength, err
		}
		if err := g.writeJunk(g.dstPos+nulls, size-nulls); err != nil {
			return fileLength, err
		}
		g.dstPos += size
	case gapAllScrubbed:
		if err := g.scrubWrite(size, 0); err != nil {
			return fileLength, err
		}
		g.dstPos += size
	default: // mixed
		prg := size
		bt := blkJunk
		var btByte byte
		for prg > 0 {
			blk, err := g.readWord()
			if err != nil {
				return fileLength, err
			}
			g.srcPos += 4
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
				if err := g.copyOut(n); err != nil {
					return fileLength, err
				}
				g.srcPos += n
			case blkByteFill:
				if !btRepeat {
					btByte = byte(cnt & 0xFF)
					cnt >>= 8
				}
				n = cnt * gapBlockSize
				if n > prg {
					n = prg
				}
				if err := g.scrubWrite(n, btByte); err != nil {
					return fileLength, err
				}
			default: // junk
				n = cnt * gapBlockSize
				if n > prg {
					n = prg
				}
				bn := leadingNulls(prg, g.nullsPos-g.dstPos, n, firstOrLast)
				if err := g.writeZeros(bn); err != nil {
					return fileLength, err
				}
				if err := g.writeJunk(g.dstPos+bn, n-bn); err != nil {
					return fileLength, err
				}
			}
			prg -= n
			g.dstPos += n
		}
	}
	return fileLength, nil
}
