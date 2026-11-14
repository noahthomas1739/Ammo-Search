from ammo_finder.parse import parse


def test_9mm_basic():
    a = parse("Magtech 9mm Luger 115gr FMJ Brass Case - 1000 Rounds")
    assert (a.caliber, a.grain, a.case, a.fmj, a.rounds) == ("9mm", 115, "brass", True, 1000)


def test_boxes_of():
    assert parse("Blazer Brass 9mm 124 gr FMJ 20 boxes of 50").rounds == 1000
    assert parse("Blazer Brass 9mm 124 gr FMJ").case == "brass"
    assert parse("CCI Blazer 9mm 115gr FMJ aluminum").case == "aluminum"


def test_box_count_not_confused_with_case_count():
    # one box of 50 for sale; "1000 rds/case" must not be read as the unit count
    assert parse("9mm 115gr FMJ 50 Rounds, 1000 Rds/Case").rounds == 50
    assert parse("9mm 115gr FMJ 50 Rds/Box").rounds is None


def test_steel_and_brass_washed():
    assert parse("Wolf 9mm 115gr FMJ 500 rounds").case == "steel"
    assert parse("Golden Bear 7.62x54R brass coated steel").case == "steel"


def test_not_9mm():
    assert parse("Federal 9mm Makarov 95gr FMJ").caliber is None
    assert parse("Hornady 380 ACP 9mm Kurz 90gr").caliber is None


def test_shotshell():
    a = parse('Fiocchi 12 Gauge 2-3/4" 1-1/4 oz #7.5 Lead 1330 fps - 250 Rounds')
    assert a.caliber == "12ga"
    assert a.shot_oz == 1.25 and a.shell_len == 2.75 and a.velocity == 1330
    assert a.shot_sizes == {"7.5"} and a.rounds == 250


def test_shotshell_variants():
    a = parse("Winchester Super-X 12ga 2 3/4in 3-1/4 Dram 1 1/8oz #8 1255 FPS 25 shells")
    assert a.shot_oz == 1.125 and a.shell_len == 2.75 and a.velocity == 1255 and a.shot_sizes == {"8"}
    assert parse("12 GA 3\" 1-1/4 oz #6").shell_len == 3.0
    assert parse("12 GA 3-1/2\" 1-1/4 oz steel").shell_len == 3.5
    assert parse("12 GA 2-3/4 1¼ oz #7½ 1300 fps").shot_sizes == {"7.5"}
    assert parse("12 GA 2-3/4 1 oz").shot_oz == 1.0


def test_54r_flags():
    a = parse("Bulgarian Surplus 7.62x54R 148gr FMJ Corrosive Steel Core Berdan - 440 Round Spam Can")
    assert a.caliber == "762x54r" and a.surplus and a.corrosive and a.steel_core and a.berdan and a.rounds == 440
    assert parse("PPU 7.62x54R non-corrosive boxer brass").corrosive is False


def test_exclusions():
    assert parse("9mm 115gr FMJ reloaded 1000 rounds").reloaded
    assert parse("9mm snap caps 10 pack").not_ammo
    assert parse("Once fired 9mm brass 1000 ct").not_ammo
