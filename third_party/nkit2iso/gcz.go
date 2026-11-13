package main

import (
	"bytes"
	"compress/zlib"
	"encoding/binary"
	"fmt"
	"io"
)

const gczMagic = 0xB10BC001

// gczReader streams the decompressed contents of a Dolphin GCZ container as a
// plain io.Reader. GCZ splits the image into fixed-size blocks, each stored
// either raw or zlib-compressed; we inflate them one at a time so memory stays
// constant regardless of image size.
type gczReader struct {
	src      io.ReaderAt
	ptrs     []uint64 // block offsets in the data section; bit 63 set = stored raw
	dataOff  int64    // file offset where block data begins
	compSize uint64   // total length of the compressed data section
	blockSz  int64
	dataSize int64

	blk    int    // next block index to decode
	buf    []byte // decompressed bytes of the current block
	bufPos int
}

// isGCZ reports whether src starts with the GCZ magic cookie.
func isGCZ(src io.ReaderAt) bool {
	var m [4]byte
	if _, err := src.ReadAt(m[:], 0); err != nil {
		return false
	}
	return binary.LittleEndian.Uint32(m[:]) == gczMagic
}

// newGCZReader parses the GCZ header and returns a sequential decompressing
// reader plus the total decompressed (nkit-stream) length.
func newGCZReader(src io.ReaderAt) (*gczReader, int64, error) {
	var hdr [32]byte
	if _, err := src.ReadAt(hdr[:], 0); err != nil {
		return nil, 0, err
	}
	if binary.LittleEndian.Uint32(hdr[0:]) != gczMagic {
		return nil, 0, fmt.Errorf("not a GCZ file")
	}
	compSize := binary.LittleEndian.Uint64(hdr[8:])
	dataSize := binary.LittleEndian.Uint64(hdr[16:])
	blockSz := binary.LittleEndian.Uint32(hdr[24:])
	numBlocks := binary.LittleEndian.Uint32(hdr[28:])
	if blockSz == 0 || numBlocks == 0 {
		return nil, 0, fmt.Errorf("invalid GCZ header (block_size=%d num_blocks=%d)", blockSz, numBlocks)
	}

	pbuf := make([]byte, 8*int64(numBlocks))
	if _, err := src.ReadAt(pbuf, 32); err != nil {
		return nil, 0, err
	}
	ptrs := make([]uint64, numBlocks)
	for i := range ptrs {
		ptrs[i] = binary.LittleEndian.Uint64(pbuf[i*8:])
	}
	// Block pointers are followed by num_blocks u32 Adler32 hashes (which we do
	// not verify), then the block data itself.
	dataOff := int64(32) + 8*int64(numBlocks) + 4*int64(numBlocks)

	return &gczReader{
		src: src, ptrs: ptrs, dataOff: dataOff,
		compSize: compSize, blockSz: int64(blockSz), dataSize: int64(dataSize),
	}, int64(dataSize), nil
}

// fill decodes the next block into r.buf.
func (r *gczReader) fill() error {
	if r.blk >= len(r.ptrs) {
		return io.EOF
	}
	i := r.blk
	raw := r.ptrs[i]&(1<<63) != 0
	off := r.ptrs[i] &^ (1 << 63)
	end := r.compSize
	if i+1 < len(r.ptrs) {
		end = r.ptrs[i+1] &^ (1 << 63)
	}

	stored := make([]byte, end-off)
	if _, err := r.src.ReadAt(stored, r.dataOff+int64(off)); err != nil {
		return err
	}

	// Every block decompresses to block_size, except the final block which is
	// truncated to whatever is left of the total decompressed size.
	want := r.blockSz
	if rem := r.dataSize - int64(i)*r.blockSz; rem < want {
		want = rem
	}

	if raw {
		if int64(len(stored)) < want {
			return fmt.Errorf("gcz block %d: raw block shorter than expected", i)
		}
		r.buf = stored[:want]
	} else {
		zr, err := zlib.NewReader(bytes.NewReader(stored))
		if err != nil {
			return fmt.Errorf("gcz block %d: %w", i, err)
		}
		out := make([]byte, want)
		if _, err := io.ReadFull(zr, out); err != nil {
			return fmt.Errorf("gcz block %d: %w", i, err)
		}
		zr.Close()
		r.buf = out
	}
	r.bufPos = 0
	r.blk++
	return nil
}

func (r *gczReader) Read(p []byte) (int, error) {
	if r.bufPos >= len(r.buf) {
		if err := r.fill(); err != nil {
			return 0, err
		}
	}
	n := copy(p, r.buf[r.bufPos:])
	r.bufPos += n
	return n, nil
}
