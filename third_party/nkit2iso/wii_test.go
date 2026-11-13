package main

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/sha1"
	"encoding/hex"
	"testing"
)

// The retail Wii common key is public and well-known; if the de-obfuscation
// breaks, every Wii partition fails to decrypt.
func TestWiiCommonKey(t *testing.T) {
	got := hex.EncodeToString(wiiCommonKey(2))
	want := "ebe42a225e8593e448d9c5457381aaf7"
	if got != want {
		t.Errorf("retail common key = %s, want %s", got, want)
	}
}

func TestOffsetConversions(t *testing.T) {
	for _, h := range []int64{0x8000, 0x200000, 0x118240000, 0x1FB4E0000} {
		if got := dataToHashedLen(hashedLenToData(h)); got != h {
			t.Errorf("roundtrip hashed %#x -> %#x", h, got)
		}
	}
}

// TestParseWiiPartitionTableRebased checks that the same parser reads both the
// live table at 0x40000 and the original-table backup NKit stores at offset 0
// of the update-partition placeholder (whose entry pointers still reference
// the 0x40000 disc window).
func TestParseWiiPartitionTableRebased(t *testing.T) {
	placeholder := make([]byte, 0x8000)
	putBE32(placeholder, 0, 2)            // group 0: 2 partitions
	putBE32(placeholder, 4, 0x40020/4)    // entry table "at" 0x40020
	putBE32(placeholder, 0x20, 0x50000/4) // update partition
	putBE32(placeholder, 0x24, ptUpdate)
	putBE32(placeholder, 0x28, 0xF800000/4) // data partition
	putBE32(placeholder, 0x2c, ptData)

	parts := parseWiiPartitionTable(placeholder, 0)
	if len(parts) != 2 {
		t.Fatalf("got %d partitions, want 2", len(parts))
	}
	if parts[0].rawOffset != 0x50000 || parts[0].typ != ptUpdate {
		t.Errorf("first = {%#x, %d}, want update at 0x50000", parts[0].rawOffset, parts[0].typ)
	}
	if parts[1].rawOffset != 0xF800000 || parts[1].typ != ptData {
		t.Errorf("second = {%#x, %d}, want data at 0xF800000", parts[1].rawOffset, parts[1].typ)
	}

	hdr := make([]byte, wiiHeaderSize)
	copy(hdr[0x40000:], placeholder[:0x100])
	if got := parseWiiPartitions(hdr); len(got) != 2 || got[1].rawOffset != 0xF800000 {
		t.Errorf("live-table parse mismatch: %+v", got)
	}
}

// TestClusterRoundTrip checks that a freshly hashed+encrypted group decrypts
// back to its payload and that the stored H0 hashes match that payload — i.e.
// the hash-tree layout and per-cluster AES are internally consistent.
func TestClusterRoundTrip(t *testing.T) {
	key, _ := aes.NewCipher(make([]byte, 16))
	buf := make([]byte, wiiGroupSize)
	for i := range buf {
		buf[i] = byte(i*7 + 1)
	}
	for b := 0; b < groupClusters; b++ { // clear hash areas (payload only)
		base := b * clusterSize
		for i := base; i < base+hashBlockSize; i++ {
			buf[i] = 0
		}
	}
	orig := append([]byte(nil), buf...)

	hashAndEncryptGroup(buf, groupClusters, key)

	// decrypt cluster 5 and verify
	base := 5 * clusterSize
	dec := make([]byte, clusterSize)
	iv := make([]byte, 16)
	cipher.NewCBCDecrypter(key, iv).CryptBlocks(dec[:hashBlockSize], buf[base:base+hashBlockSize])
	copy(iv, buf[base+0x3d0:base+0x3e0])
	cipher.NewCBCDecrypter(key, iv).CryptBlocks(dec[hashBlockSize:], buf[base+hashBlockSize:base+clusterSize])

	if !bytes.Equal(dec[hashBlockSize:], orig[base+hashBlockSize:base+clusterSize]) {
		t.Fatal("payload did not survive encrypt/decrypt")
	}
	for i := 0; i < 31; i++ {
		want := sha1.Sum(dec[hashBlockSize+i*hashBlockSize : hashBlockSize+(i+1)*hashBlockSize])
		if !bytes.Equal(dec[i*20:i*20+20], want[:]) {
			t.Fatalf("H0[%d] does not match payload sub-block", i)
		}
	}
}
