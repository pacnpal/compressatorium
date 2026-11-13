package main

import "io"

// junkStream reproduces the Nintendo GameCube/Wii disc "junk" padding PRNG
// byte-for-byte. It is a direct port of NKit's JunkStream.cs (Nanook/NKitv1);
// the algorithm — not its source text — is reproduced here (see README credits).
//
// Junk is generated in 0x40000-byte blocks; state is fully reseeded every
// 0x8000 bytes from (4-byte game id, disc number, block index). Byte offset ->
// block index -> byte within the regenerated block.

const junkBlockSize = 0x40000

type junkStream struct {
	id       [4]byte
	disc     byte
	junkLen  int64 // junk beyond this (0x8000-floored length) is zero
	pos      int64
	blockIdx int64 // which block is cached in `block`, -1 = none
	block    []byte
	state    []uint32
}

func newJunkStream(id [4]byte, disc byte, length int64) *junkStream {
	return &junkStream{
		id:       id,
		disc:     disc,
		junkLen:  length &^ 0x7fff, // floor-align to 0x8000
		blockIdx: -1,
		block:    make([]byte, junkBlockSize),
		state:    make([]uint32, 0x824),
	}
}

// a100026e0
func (j *junkStream) temper() {
	b := j.state
	for i := 0; i < 0x20; i++ {
		b[i] ^= b[i+0x1e9]
	}
	for i := 0x20; i < 0x209; i++ {
		b[i] ^= b[i-0x20]
	}
}

// a10002710
func (j *junkStream) reseed(sample uint32) {
	b := j.state
	var num uint32
	for w := 0; w < 0x11; w++ {
		for i := 0; i < 0x20; i++ {
			sample *= 0x5d588b65
			sample++
			num = (num >> 1) | (sample & 0x80000000)
		}
		b[w] = num
	}
	b[0x10] ^= (b[0] >> 9) ^ (b[0x10] << 0x17)
	for w := 1; w < 0x1f9; w++ {
		b[w+0x10] = (b[w-1] << 0x17) ^ (b[w] >> 9) ^ b[w+15]
	}
	j.temper()
	j.temper()
	j.temper()
}

func (j *junkStream) fillBlock(block int64) {
	for i := range j.state {
		j.state[i] = 0
	}
	num2 := 0
	var sample uint32
	blk := (uint32(block) * 8) * 0x1ef29123
	id := j.id
	for i := 0; i < junkBlockSize; i += 4 {
		if i&0x7fff == 0 {
			sample = uint32((((int(id[2])<<8 | int(id[1])) << 0x10) | ((int(id[3]) + int(id[2])) << 8)) | (int(id[0]) + int(id[1])))
			sample = ((sample ^ uint32(j.disc)) * 0x260bcd5) ^ blk
			j.reseed(sample)
			num2 = 520
			blk += 0x1ef29123
		}
		num2++
		if num2 == 0x209 {
			j.temper()
			num2 = 0
		}
		w := j.state[num2]
		j.block[i] = byte(w >> 0x18)
		j.block[i+1] = byte(w >> 0x12)
		j.block[i+2] = byte(w >> 8)
		j.block[i+3] = byte(w)
	}
	// junk past the disc length is zero
	junkSize := j.junkLen - block*junkBlockSize
	if junkSize < 0 {
		junkSize = 0
	} else if junkSize > junkBlockSize {
		junkSize = junkBlockSize
	}
	for i := junkSize; i < junkBlockSize; i++ {
		j.block[i] = 0
	}
	j.blockIdx = block
}

func (j *junkStream) seek(pos int64) { j.pos = pos }

func (j *junkStream) read(p []byte) {
	for len(p) > 0 {
		blk := j.pos / junkBlockSize
		off := int(j.pos % junkBlockSize)
		if blk != j.blockIdx {
			j.fillBlock(blk)
		}
		n := copy(p, j.block[off:])
		p = p[n:]
		j.pos += int64(n)
	}
}

// writeTo writes n junk bytes (from the current position) to w.
func (j *junkStream) writeTo(w io.Writer, n int64) error {
	buf := make([]byte, 0x10000)
	for n > 0 {
		c := int64(len(buf))
		if c > n {
			c = n
		}
		j.read(buf[:c])
		if _, err := w.Write(buf[:c]); err != nil {
			return err
		}
		n -= c
	}
	return nil
}
