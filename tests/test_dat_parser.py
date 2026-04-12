"""Tests for the Logiqx / MAME softlist DAT parser."""

from services.dat_parser import parse_dat


LOGIQX_DATAFILE = """\
<?xml version="1.0"?>
<datafile>
  <header>
    <name>Nintendo - GameCube</name>
    <version>0.285</version>
  </header>
  <game name="Test Game (USA)">
    <rom name="Test Game (USA).iso" size="1459978240"
         sha1="aabbccddaabbccddaabbccddaabbccddaabbccdd"
         md5="aabbccddaabbccddaabbccddaabbccdd"/>
  </game>
</datafile>
"""


# MAME software-list format (used by carts/floppies/tapes: Amiga, Mac,
# PC-88, FM-Towns, etc.). ROMs are nested inside <part><dataarea>.
SOFTLIST_DAT = """\
<?xml version="1.0"?>
<softwarelist name="amiga_cd">
  <software name="game1">
    <description>Cool Amiga Game</description>
    <part name="cdrom" interface="amiga_cdrom">
      <diskarea name="cdrom">
        <disk name="game1" sha1="1111111111111111111111111111111111111111"/>
      </diskarea>
      <dataarea name="rom" size="2048">
        <rom name="game1.bin" size="2048"
             sha1="2222222222222222222222222222222222222222"
             md5="22222222222222222222222222222222"/>
      </dataarea>
    </part>
  </software>
  <software name="game2">
    <description>Another Amiga Game</description>
    <part name="cart" interface="amiga_cart">
      <dataarea name="rom" size="4096">
        <rom name="game2.rom" size="4096"
             sha1="3333333333333333333333333333333333333333"/>
      </dataarea>
    </part>
  </software>
</softwarelist>
"""


def test_parse_logiqx_datafile():
    header, entries = parse_dat(LOGIQX_DATAFILE)
    assert header["name"] == "Nintendo - GameCube"
    assert len(entries) == 1
    assert entries[0]["sha1"] == "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    assert entries[0]["game_name"] == "Test Game (USA)"


def test_parse_mame_softlist_nested_roms_and_disks():
    """Regression: softlist DATs (Amiga, PC-88, Mac, etc.) nest <rom>
    inside <part><dataarea>, and CD-based softlists carry their track
    hashes in <disk> elements under <part><diskarea>. The parser must
    accept both.
    """
    _header, entries = parse_dat(SOFTLIST_DAT)
    # Three hash-carrying entries: one <disk> (1111…) and two <rom>
    # (2222…, 3333…). Prior behaviour ignored <disk>, which zeroed out
    # the entry count for CD-based softlists (Amiga CD, Pippin, etc.).
    assert len(entries) == 3

    by_hash = {e["sha1"]: e for e in entries}
    assert "1111111111111111111111111111111111111111" in by_hash
    assert "2222222222222222222222222222222222222222" in by_hash
    assert "3333333333333333333333333333333333333333" in by_hash

    # <description> should win over the name="" id for game_name.
    assert by_hash["1111111111111111111111111111111111111111"]["game_name"] == "Cool Amiga Game"
    assert by_hash["2222222222222222222222222222222222222222"]["game_name"] == "Cool Amiga Game"
    assert by_hash["3333333333333333333333333333333333333333"]["game_name"] == "Another Amiga Game"


CD_ONLY_SOFTLIST_DAT = """\
<?xml version="1.0"?>
<softwarelist name="pippin">
  <software name="marathon">
    <description>Marathon (Pippin)</description>
    <part name="cdrom" interface="pippin_cdrom">
      <diskarea name="cdrom">
        <disk name="marathon" sha1="4444444444444444444444444444444444444444"/>
      </diskarea>
    </part>
  </software>
  <software name="racing">
    <description>Super Marathon Racing</description>
    <part name="cdrom" interface="pippin_cdrom">
      <diskarea name="cdrom">
        <disk name="racing" sha1="5555555555555555555555555555555555555555"/>
      </diskarea>
    </part>
  </software>
</softwarelist>
"""


def test_parse_cd_only_softlist_populates_entries():
    """Regression for the Entries=0 bug on CD-based MAME softlists
    (Amiga CD, Amiga CD32, Bandai Pippin, Konami FireBeat, etc.):
    when every hash lives in a <disk> element and there are no
    <rom> elements, the parser must still return non-zero entries."""
    _header, entries = parse_dat(CD_ONLY_SOFTLIST_DAT)
    assert len(entries) == 2

    by_hash = {e["sha1"]: e for e in entries}
    assert by_hash["4444444444444444444444444444444444444444"]["game_name"] == "Marathon (Pippin)"
    assert by_hash["5555555555555555555555555555555555555555"]["game_name"] == "Super Marathon Racing"
