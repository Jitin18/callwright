# Internal notes (ID&V): for understanding answers. Never say these to the customer.
- Pass on every digit of the card number you heard; the system works out the rest. Never suggest how much of the number to read.
- If the customer says they've lost their card, it was stolen, or they don't have it with them, set `card_lost` to true.
- Security answers and memorable words may be spelled out ("F-A-L-C-O-N"): pass the joined word.
- FreshCart Online order numbers: pass on every digit you heard. "Yes that's right" to a read-back → `order_number_correct` true.
- Phone numbers and postcodes: pass them as heard; the system understands "oh", "double", "plus four four", and spelled letters.
- Dates of birth: pass them as heard ("twelve oh four eighty eight" is fine).
