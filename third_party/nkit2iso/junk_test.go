package main

import (
	"bytes"
	"encoding/hex"
	"strings"
	"testing"
)

// Known-answer vectors for the GameCube junk PRNG, generated from a faithful
// port of NKit's JunkStream.cs and cross-checked. If this test breaks, the
// generator diverged and restored ISOs will not be bit-exact.
func TestJunkVectors(t *testing.T) {
	cases := []struct {
		id     string
		disc   byte
		offset int64
		want   string
	}{
		{"GALE", 0, 0x0, "94215ADA27F15C2D8BC834ABFA884B9DD56682FE0D7D25264C2BDCE8DD7C856B"},
		{"GALE", 0, 0x8000, "73F4FFC12957635E6805ACFAEBDE52D3"},
		{"GALE", 0, 0x40000, "C1590A52C7F16EFE6D7E042A683B574F"},
		{"GALE", 0, 0x1234, "954D1727D7F7B68B56863FA23EB4F1FE"},
		{"G8ME", 0, 0x0, "2BF2B36254365F4ED698DCD7EEBA2DC3"},
		{"GALE", 1, 0x0, "E80C7641382C1CEC3C2804599685201B"},
	}
	for _, c := range cases {
		var id [4]byte
		copy(id[:], c.id)
		j := newJunkStream(id, c.disc, 0x57058000)
		j.seek(c.offset)
		want, _ := hex.DecodeString(strings.ToUpper(c.want))
		got := make([]byte, len(want))
		j.read(got)
		if !bytes.Equal(got, want) {
			t.Errorf("id=%s disc=%d off=%#x:\n got %X\nwant %X", c.id, c.disc, c.offset, got, want)
		}
	}
}
