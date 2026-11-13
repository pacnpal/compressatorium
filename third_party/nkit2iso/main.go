package main

import (
	"bufio"
	"flag"
	"fmt"
	"hash/crc32"
	"io"
	"os"
	"path/filepath"
	"strings"
)

var version = "dev" // set via -ldflags at release time

func main() {
	in := flag.String("i", "", "input .nkit.iso file (or pass as a positional argument)")
	out := flag.String("o", "", "output .iso file (default: input name with .iso extension)")
	force := flag.Bool("f", false, "overwrite the output file if it already exists")
	recovery := flag.String("recovery", "ask", "removed Wii update partitions: ask, download, or none")
	showVer := flag.Bool("version", false, "print version and exit")
	flag.Parse()

	if *showVer {
		fmt.Println("nkit2iso", version)
		return
	}

	input := *in
	if input == "" && flag.NArg() > 0 {
		input = flag.Arg(0)
	}
	if input == "" {
		fmt.Fprintln(os.Stderr, "usage: nkit2iso -i <in.nkit.iso> [-o <out.iso>] [-f] [-recovery ask|download|none]")
		os.Exit(2)
	}
	switch *recovery {
	case "ask", "download", "none":
	default:
		die("invalid -recovery %q (want ask, download, or none)", *recovery)
	}

	output := *out
	if output == "" {
		output = defaultOutput(input)
	}
	if output == input {
		die("input and output are the same file")
	}
	if !*force {
		if _, err := os.Stat(output); err == nil {
			die("output %q already exists (use -f to overwrite)", output)
		}
	}

	if err := run(input, output, *recovery); err != nil {
		os.Remove(output) // don't leave a half-written file on hard errors
		die("%v", err)
	}
}

func run(input, output, recoveryMode string) error {
	inf, err := os.Open(input)
	if err != nil {
		return err
	}
	defer inf.Close()
	st, err := inf.Stat()
	if err != nil {
		return err
	}

	// A GCZ container (e.g. *.nkit.gcz) holds the nkit stream zlib-compressed;
	// transparently inflate it so the restore sees a plain nkit byte stream.
	var src io.Reader = inf
	srcLen := st.Size()
	var magic [0x20]byte
	if isGCZ(inf) {
		gr, dsize, err := newGCZReader(inf)
		if err != nil {
			return err
		}
		br := bufio.NewReaderSize(gr, 1<<20)
		m, err := br.Peek(len(magic))
		if err != nil {
			return err
		}
		copy(magic[:], m)
		src, srcLen = br, dsize
	} else if _, err := inf.ReadAt(magic[:], 0); err != nil {
		return err
	}

	// Detect GameCube vs Wii by disc magic.
	isWii := be32(magic[:], 0x18) == 0x5D1C9EA3

	outf, err := os.Create(output)
	if err != nil {
		return err
	}

	fmt.Fprintf(os.Stderr, "Restoring %s -> %s\n", input, output)
	var nkitCrc uint32
	exact := true
	if isWii {
		resolve := newRecoveryResolver(recoveryMode, filepath.Dir(outf.Name()))
		nkitCrc, exact, err = restoreWii(src, outf, srcLen, resolve, progressBar())
	} else {
		nkitCrc, err = restore(src, outf, srcLen, progressBar())
	}
	fmt.Fprintln(os.Stderr) // finish the progress line
	if err != nil {
		outf.Close()
		return err
	}
	if err := outf.Close(); err != nil {
		return err
	}

	if !exact {
		fmt.Printf("CRC32 check skipped — output is playable but NOT bit-exact (update partition zero-filled)\n")
		return nil
	}
	gotCrc, err := crc32File(output)
	if err != nil {
		return err
	}
	if gotCrc != nkitCrc {
		return fmt.Errorf("CRC32 MISMATCH: got %08X, expected %08X — output is NOT bit-exact", gotCrc, nkitCrc)
	}
	fmt.Printf("CRC32 %08X  MATCH (redump-verified)\n", gotCrc)
	return nil
}

// defaultOutput turns "foo.nkit.iso" into "foo.iso" (and anything else into
// base + ".iso").
func defaultOutput(input string) string {
	low := strings.ToLower(input)
	for _, suf := range []string{".nkit.iso", ".nkit.gcz", ".gcz"} {
		if strings.HasSuffix(low, suf) {
			return input[:len(input)-len(suf)] + ".iso"
		}
	}
	if i := strings.LastIndexByte(input, '.'); i > strings.LastIndexByte(input, '/') {
		return input[:i] + ".iso"
	}
	return input + ".iso"
}

func crc32File(path string) (uint32, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	h := crc32.NewIEEE()
	if _, err := io.Copy(h, f); err != nil {
		return 0, err
	}
	return h.Sum32(), nil
}

// progressBar returns a callback that prints a throttled percentage to stderr.
func progressBar() func(cur, total int64) {
	last := -1
	return func(cur, total int64) {
		if total <= 0 {
			return
		}
		pct := int(cur * 100 / total)
		if pct != last {
			last = pct
			fmt.Fprintf(os.Stderr, "\r  %3d%%", pct)
		}
	}
}

func die(format string, a ...any) {
	fmt.Fprintf(os.Stderr, "nkit2iso: "+format+"\n", a...)
	os.Exit(1)
}
