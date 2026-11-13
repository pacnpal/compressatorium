package main

import (
	"bytes"
	"compress/zlib"
	"encoding/binary"
	"hash/adler32"
	"io"
	"testing"
)

// buildGCZ packs data into a minimal GCZ container: block 0 zlib-compressed,
// block 1 stored raw (bit 63 set). Exercises both storage paths and the
// truncated final block.
func buildGCZ(data []byte, blockSz int) []byte {
	nb := (len(data) + blockSz - 1) / blockSz
	var blocks [][]byte
	var ptrs []uint64
	var off uint64
	for i := 0; i < nb; i++ {
		start := i * blockSz
		end := start + blockSz
		if end > len(data) {
			end = len(data)
		}
		plain := data[start:end]
		if i%2 == 0 { // compress even blocks, store odd blocks raw
			var b bytes.Buffer
			zw := zlib.NewWriter(&b)
			zw.Write(plain)
			zw.Close()
			blocks = append(blocks, b.Bytes())
			ptrs = append(ptrs, off)
		} else {
			blocks = append(blocks, plain)
			ptrs = append(ptrs, off|(1<<63))
		}
		off += uint64(len(blocks[i]))
	}

	var out bytes.Buffer
	hdr := make([]byte, 32)
	binary.LittleEndian.PutUint32(hdr[0:], gczMagic)
	binary.LittleEndian.PutUint64(hdr[8:], off) // compressed_data_size
	binary.LittleEndian.PutUint64(hdr[16:], uint64(len(data)))
	binary.LittleEndian.PutUint32(hdr[24:], uint32(blockSz))
	binary.LittleEndian.PutUint32(hdr[28:], uint32(nb))
	out.Write(hdr)
	for _, p := range ptrs {
		binary.Write(&out, binary.LittleEndian, p)
	}
	for _, b := range blocks {
		binary.Write(&out, binary.LittleEndian, adler32.Checksum(b))
	}
	for _, b := range blocks {
		out.Write(b)
	}
	return out.Bytes()
}

func TestGCZRoundTrip(t *testing.T) {
	// Odd length so the last block is truncated.
	data := make([]byte, 4096*3+123)
	for i := range data {
		data[i] = byte(i*31 + 7)
	}
	container := buildGCZ(data, 4096)

	if !isGCZ(bytes.NewReader(container)) {
		t.Fatal("isGCZ did not recognise container")
	}
	r, size, err := newGCZReader(bytes.NewReader(container))
	if err != nil {
		t.Fatal(err)
	}
	if size != int64(len(data)) {
		t.Fatalf("decompressed size = %d, want %d", size, len(data))
	}
	got, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, data) {
		t.Fatalf("round-trip mismatch: got %d bytes", len(got))
	}
}
