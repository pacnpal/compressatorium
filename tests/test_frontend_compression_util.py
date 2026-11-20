"""The compression wire format in one place (restructure PR 1, spec §3.1).

``src/lib/tools/compression.js`` turns the pickers' ``{ codecs, level }`` into
the wire string a job is sent ("zlib,lzma", "zstd:19", "none", or null for the
tool's own default) and a Rule's two fields, and back. Evaluated with Node.
"""
from __future__ import annotations

import json

from tests.frontend_tools_harness import run_tools


def _eval(tmp_path, body):
    return run_tools(tmp_path, ["compression.js"], body)


def test_format_writes_each_style(tmp_path):
    got = _eval(tmp_path, """
const f = formatCompression;
process.stdout.write(JSON.stringify({
  chd: f('createcd', { codecs: ['cdlz', 'cdzl'], level: null }),
  // Create DVD takes no CD codecs: only the ones it offers are sent (#471).
  dvd_mixed: f('createdvd', { codecs: ['zlib', 'cdzl'], level: null }),
  dvd_none_offered: f('createdvd', { codecs: ['cdzl'], level: null }),
  none: f('createcd', { codecs: ['none'], level: null }),
  rvz: f('dolphin_rvz', { codecs: ['zstd'], level: 19 }),
  // A level the codec doesn't take is sent as the codec's default.
  rvz_lzma2_19: f('dolphin_rvz', { codecs: ['lzma2'], level: 19 }),
  rvz_blank_level: f('dolphin_rvz', { codecs: ['zstd'], level: null }),
  // WIA refuses zstd: its own default codec runs instead.
  wia_zstd: f('dolphin_wia', { codecs: ['zstd'], level: 19 }),
  extract: f('extractcd', { codecs: ['zlib'], level: null }),
}));
""")
    assert got == {
        "chd": "cdlz,cdzl",
        "dvd_mixed": "zlib",
        "dvd_none_offered": None,
        "none": "none",
        "rvz": "zstd:19",
        "rvz_lzma2_19": "lzma2:5",
        "rvz_blank_level": "zstd:19",
        "wia_zstd": "none",
        "extract": None,
    }


def test_parse_reads_each_wire_form(tmp_path):
    got = _eval(tmp_path, """
const p = parseCompression;
process.stdout.write(JSON.stringify({
  list: p('createcd', 'zlib,lzma'),
  level: p('dolphin_rvz', 'zstd:19'),
  codec_only: p('dolphin_rvz', 'lzma2'),
  none: p('createcd', 'none'),
  blank: p('dolphin_rvz', null),
}));
""")
    assert got == {
        "list": {"codecs": ["zlib", "lzma"], "level": None},
        "level": {"codecs": ["zstd"], "level": 19},
        "codec_only": {"codecs": ["lzma2"], "level": 5},
        "none": {"codecs": ["none"], "level": None},
        "blank": {"codecs": ["zstd"], "level": 19},
    }


def test_format_and_parse_round_trip(tmp_path):
    cases = [
        ["createcd", "cdlz,cdzl,cdfl"], ["createdvd", "zstd"], ["dolphin_rvz", "lzma2:7"],
        ["dolphin_rvz", "zstd:22"], ["nsz_compress", "solid:18"], ["createcd", "none"],
    ]
    got = _eval(tmp_path, f"""
const cases = {json.dumps(cases)};
process.stdout.write(JSON.stringify(cases.map(([m, w]) => formatCompression(m, parseCompression(m, w)))));
""")
    assert got == [w for _, w in cases]


def test_defaults_and_is_default(tmp_path):
    got = _eval(tmp_path, """
process.stdout.write(JSON.stringify({
  chd: defaultCompression('createcd'),
  rvz: defaultCompression('dolphin_rvz'),
  nsz: defaultCompression('nsz_compress'),
  chd_is: isDefault('createcd', { codecs: ['zlib'], level: null }),
  chd_not: isDefault('createcd', { codecs: ['zlib', 'lzma'], level: null }),
  rvz_is: isDefault('dolphin_rvz', { codecs: ['zstd'], level: 19 }),
  rvz_not: isDefault('dolphin_rvz', { codecs: ['zstd'], level: 3 }),
}));
""")
    assert got["chd"] == {"codecs": ["zlib"], "level": None}
    assert got["rvz"] == {"codecs": ["zstd"], "level": 19}
    assert got["nsz"] == {"codecs": ["solid"], "level": 18}
    assert (got["chd_is"], got["chd_not"], got["rvz_is"], got["rvz_not"]) == (
        True, False, True, False,
    )


def test_a_rules_two_fields_round_trip(tmp_path):
    got = _eval(tmp_path, """
process.stdout.write(JSON.stringify({
  to_level: toRule('dolphin_rvz', { codecs: ['zstd'], level: 12 }),
  to_list: toRule('createcd', { codecs: ['cdlz', 'cdzl'], level: null }),
  to_default: toRule('extractcd', { codecs: ['zlib'], level: null }),
  from_level: fromRule({ mode: 'dolphin_rvz', compression: 'zstd', compression_level: 12 }),
  from_blank: fromRule({ mode: 'dolphin_rvz', compression: null, compression_level: null }),
  from_list: fromRule({ mode: 'createcd', compression: 'cdlz,cdzl' }),
}));
""")
    assert got["to_level"] == {"compression": "zstd", "compression_level": 12}
    assert got["to_list"] == {"compression": "cdlz,cdzl", "compression_level": None}
    assert got["to_default"] == {"compression": None, "compression_level": None}
    assert got["from_level"] == {"codecs": ["zstd"], "level": 12}
    assert got["from_blank"] == {"codecs": ["zstd"], "level": 19}
    assert got["from_list"] == {"codecs": ["cdlz", "cdzl"], "level": None}


def test_the_picker_edits(tmp_path):
    got = _eval(tmp_path, """
const v = { codecs: ['cdlz', 'cdzl', 'cdfl', 'lzma'], level: null };
process.stdout.write(JSON.stringify({
  capped: toggleCodec('createcd', v, 'zlib').codecs,
  removed: toggleCodec('createcd', v, 'cdzl').codecs,
  none: toggleCodec('createcd', v, 'none').codecs,
  // A CD codec kept from Create CD is dropped on Create DVD, not counted.
  dvd: toggleCodec('createdvd', { codecs: ['cdzl'], level: null }, 'zstd').codecs,
  to_lzma2: withCodec('dolphin_rvz', { codecs: ['zstd'], level: 19 }, 'lzma2'),
  clamp: withLevel('dolphin_rvz', { codecs: ['lzma2'], level: 5 }, '19').level,
  blank: withLevel('dolphin_rvz', { codecs: ['zstd'], level: 5 }, '').level,
}));
""")
    assert got["capped"] == ["cdlz", "cdzl", "cdfl", "lzma"]
    assert got["removed"] == ["cdlz", "cdfl", "lzma"]
    assert got["none"] == ["none"]
    assert got["dvd"] == ["zstd"]
    assert got["to_lzma2"] == {"codecs": ["lzma2"], "level": 5}
    assert got["clamp"] == 9
    assert got["blank"] is None


def test_the_picker_reads_only_the_registry_and_compat():
    from pathlib import Path

    picker = (
        Path(__file__).resolve().parents[1]
        / "src/lib/components/panels/CompressionPicker.svelte"
    ).read_text(encoding="utf-8")
    assert "stores/conversion" not in picker
    assert "let { mode, value, onchange, compact = false" in picker
    assert "onchange(defaultCompression(mode))" in picker
    assert "<summary>Choose codecs</summary>" in picker
    assert "Compatible with…" in picker.split("<details", 1)[0], "Compatible with stays visible"
