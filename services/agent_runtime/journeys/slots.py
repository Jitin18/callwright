"""Slot definitions shared across journeys.

`description` says what the value is, in plain words. Anything about formats lives in `parse_hint`, which is
internal guidance for filling submit_answer and must never be said to the customer.
"""
from .dsl import Slot

CARD = Slot("card_number", "card_number", "The member's FreshCart Rewards card number.", shared=True, sensitive=True,
            long_input=True, parse_hint="Pass every digit heard (the system completes partial numbers).")
CARD_LOST = Slot("card_lost", "yes_no", "The member says their card is lost or stolen, or they don't have it with them.",
                 required=False, volunteered_only=True,
                 parse_hint="true only if they say the card is lost, stolen or not with them. Not wanting to give the "
                            "number is NOT a lost card: leave this out.")
FULL_NAME = Slot("full_name", "name", "The caller's full name.", shared=True, sensitive=True,
                 parse_hint="First and last name as said.")
DOB = Slot("dob", "dob", "The caller's date of birth.", shared=True, sensitive=True,
           parse_hint="Pass it as heard or as yyyy-mm-dd; UK day-first.")
ADDRESS = Slot("address", "address", "The caller's home address.", shared=True, sensitive=True, long_input=True,
               parse_hint="Everything they said, including the postcode.")
SECURITY_ANSWER = Slot("security_answer", "secret", "The caller's answer to their security question.",
                       shared=True, sensitive=True, secret=True, long_input=True,
                       parse_hint="Verbatim; join spelled-out letters into the word.")
MEMORABLE_WORD = Slot("memorable_word", "secret", "The caller's memorable word.", shared=True, sensitive=True,
                      secret=True, long_input=True, parse_hint="Verbatim; join spelled-out letters into the word.")
EMAIL = Slot("email", "email", "The customer's email address.", shared=True, sensitive=True, required=False,
             parse_hint="'john dot smith at gmail dot com' → john.smith@gmail.com.")
MOBILE = Slot("mobile", "mobile", "The customer's mobile number.", shared=True, sensitive=True, required=False,
              long_input=True, parse_hint="Every digit heard.")

ORDER_NUMBER = Slot("order_number", "order_number", "The FreshCart Online order number.", shared=True,
                    long_input=True, parse_hint="Every digit heard.")
PHONE = Slot("phone", "phone", "The phone number on the online order.", shared=True, sensitive=True, long_input=True,
             parse_hint="Every digit heard.")
POSTCODE = Slot("postcode", "postcode", "The delivery postcode on the online order.", shared=True, sensitive=True,
                long_input=True, parse_hint="As heard; spelled letters are fine.")
