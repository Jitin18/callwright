"""Natural customer input → validated values (no LLM). Every format from the client feedback meeting."""
import pytest

from services.agent_runtime import normalizers as n


# ---------------------------------------------------------------- dates of birth
@pytest.mark.parametrize("said", [
    "12/04/88", "12-4-1988", "12.04.1988", "12 04 88", "12041988",
    "12th April 88", "12th of April 1988", "the twelfth of April nineteen eighty eight",
    "April twelfth nineteen eighty-eight", "April 12, 1988", "twelve oh four eighty eight",
    "It's Priya Sharma, and my date of birth is the twelfth of April, nineteen eighty eight.",
    "1988-04-12",
])
def test_dob_formats(said):
    assert n.find_dob(said).value == "1988-04-12"


def test_dob_two_digit_year_pivot_and_past_only():
    assert n.find_dob("3/11/79").value == "1979-11-03"
    assert n.find_dob("4 March 05").value == "2005-03-04"     # 21 years ago: an adult
    assert n.find_dob("4 March 20").value == "1920-03-04"     # 2020 would be a child
    assert not n.find_dob("1 January 2090").ok


def test_dob_correction_takes_last_and_partial_is_incomplete():
    assert n.find_dob("the 12th of April, no sorry, the 13th of April 1988").value == "1988-04-13"
    f = n.find_dob("it's the twelfth of April")
    assert f.incomplete and not f.ok


# ---------------------------------------------------------------- postcodes
@pytest.mark.parametrize("said", ["SE1 7PB", "se17pb", "SE1 7PB.", "S E one seven P B", "S E 1, 7 P B",
                                  "S as in Sierra, E, one, seven, P, B", "Sierra Echo one seven Papa Bravo",
                                  "The postcode is S E 1, 7 P B.", "it's se1 7pb thanks"])
def test_postcode_formats(said):
    assert n.find_postcode(said).value == "SE1 7PB"


def test_postcode_other_shapes_and_incomplete():
    assert n.find_postcode("M20 2WS").value == "M20 2WS"
    assert n.find_postcode("B S seven eight double A").value == "BS7 8AA"
    assert n.find_postcode("5 Gloucester Road, Bristol, BS7 8AA").value == "BS7 8AA"
    f = n.find_postcode("it's SE1")
    assert f.incomplete and f.partial == "SE1"


# ---------------------------------------------------------------- phone numbers
@pytest.mark.parametrize("said", ["07700900111", "07700 900 111", "0770-090-0111", "+44 7700 900111",
                                  "oh seven seven double oh nine double oh one one one",
                                  "zero seven seven zero zero nine zero zero one one one",
                                  "oh seven seven oh oh, nine oh oh, triple one"])
def test_phone_formats(said):
    assert n.find_phone(said).value == "07700900111"


def test_phone_incomplete():
    f = n.find_phone("oh seven seven oh oh")
    assert f.incomplete and f.partial == "07700"


# ---------------------------------------------------------------- card and order numbers
@pytest.mark.parametrize("said", ["12345678901", "1234 5678 901", "one two three four five six seven eight nine oh one",
                                  "one two three four, five six seven eight, nine zero one",
                                  "6001 2300 1234 5678 901"])
def test_card_formats(said):
    assert n.find_card(said).value == "6001230012345678901"


def test_card_double_triple_and_correction():
    assert n.find_card("two double oh four five six seven eight nine oh one two").value == "6001230020045678901" or \
        n.find_card("two oh four five six seven eight nine oh one two").value == "6001230020456789012"
    assert n.find_card("one two three, no sorry, it's 20456789012").value == "6001230020456789012"


def test_card_partial_is_incomplete_not_wrong():
    f = n.find_card("1 2 3 4 5 6 7")
    assert f.incomplete and f.partial == "1234567" and not f.ok


@pytest.mark.parametrize("said", ["7004128855", "700 412 8855", "seven double oh four one two double eight five five",
                                  "7 0 0 4 1 2 8 8 5 5"])
def test_order_formats(said):
    assert n.find_order(said).value == "7004128855"


def test_order_incomplete():
    assert n.find_order("seven oh oh four").incomplete


# ---------------------------------------------------------------- yes / no
@pytest.mark.parametrize("said", ["yes", "yeah", "yep", "go on then", "that's right", "correct", "uh-huh",
                                  "Yes please, text me the link.", "yeah that's correct", "sure", "mm-hmm"])
def test_yes(said):
    assert n.normalise_bool(said) is True


@pytest.mark.parametrize("said", ["no", "nope", "not really", "no thanks", "nah", "that's not right",
                                  "No, that's everything, thanks.", "I don't"])
def test_no(said):
    assert n.normalise_bool(said) is False


@pytest.mark.parametrize("said", ["unclear", "maybe", "I don't know", ""])
def test_unclear_yes_no(said):
    assert n.normalise_bool(said) is None


def test_spelled_secret_joined():
    assert n.normalise("secret", "F-A-L-C-O-N")[0] == "FALCON"
    assert n.normalise("secret", "orchard")[0] == "orchard"


def test_hesitations_dont_break_a_number_but_no_does():
    assert n.find_card("uh, it's, um, one two... sorry... three four").partial == "1234"
    assert n.find_card("one two three four, um, five six seven, er, eight nine oh one").value == "6001230012345678901"
    assert n.find_card("1 2 3 4 5, no sorry, it's 1 2 3 4 5 6 7 8 9 0 1.").value == "6001230012345678901"


def test_month_only_dates_for_history_questions():
    from datetime import date
    v = n.find_date("what did I spend my points on in March?").value
    assert v.endswith("-03-01") and v <= date.today().isoformat()
    assert n.find_date("back in May 2019").value == "2019-05-01"
    assert not n.find_dob("in March").ok                       # never for a date of birth
