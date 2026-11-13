package main

import (
	"strings"
	"testing"
)

// A trimmed archive.org metadata document: sizes are JSON strings, the
// "files" array sits behind other top-level keys, and unrelated files
// (including a GameCube recovery) must be skipped.
const sampleMetadata = `{
  "created": 1780971850,
  "dir": "/6/items/MarioCubeLite",
  "files": [
    {"name": "DS Demos/something.nds", "size": "123", "sha1": "aa"},
    {"name": "NKit Recovery Partitions/GameCube - Individual/whatever_BA6000BA", "size": "99", "sha1": "bb"},
    {"name": "NKit Recovery Partitions/Wii - Individual/Game Partitions/Update - Normal Games/143E53D5B93CC106ADD1E6184DC8E9AEA0BAD2BD_N_BA6000BA", "size": "152076288", "sha1": "b117bf96dc7902d7244ffc10ba784b450d49bf0b"},
    {"name": "NKit Recovery Partitions/Wii - Individual/Game Partitions/Update - Korean Games/0F8727864C77F6280B12DAE8EC5BD454F6B55B8E_K_8DC858C5", "size": "2162688", "sha1": "cc"}
  ],
  "item_last_updated": 1610019693
}`

func TestFindRecoveryFile(t *testing.T) {
	f, err := findRecoveryFile(strings.NewReader(sampleMetadata), 0xBA6000BA)
	if err != nil {
		t.Fatal(err)
	}
	wantName := "NKit Recovery Partitions/Wii - Individual/Game Partitions/Update - Normal Games/143E53D5B93CC106ADD1E6184DC8E9AEA0BAD2BD_N_BA6000BA"
	if f.name != wantName {
		t.Errorf("name = %q, want %q", f.name, wantName)
	}
	if f.size != 152076288 {
		t.Errorf("size = %d, want 152076288", f.size)
	}
	if f.sha1 != "b117bf96dc7902d7244ffc10ba784b450d49bf0b" {
		t.Errorf("sha1 = %q", f.sha1)
	}
	wantURL := "https://archive.org/download/MarioCubeLite/NKit%20Recovery%20Partitions/Wii%20-%20Individual/Game%20Partitions/Update%20-%20Normal%20Games/143E53D5B93CC106ADD1E6184DC8E9AEA0BAD2BD_N_BA6000BA"
	if got := f.url(); got != wantURL {
		t.Errorf("url = %q, want %q", got, wantURL)
	}
}

func TestFindRecoveryFileKorean(t *testing.T) {
	f, err := findRecoveryFile(strings.NewReader(sampleMetadata), 0x8DC858C5)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(f.name, "_K_8DC858C5") {
		t.Errorf("picked %q, want the Korean recovery file", f.name)
	}
}

func TestFindRecoveryFileMissing(t *testing.T) {
	if _, err := findRecoveryFile(strings.NewReader(sampleMetadata), 0xDEADBEEF); err == nil {
		t.Fatal("expected an error for an unknown CRC")
	}
}
