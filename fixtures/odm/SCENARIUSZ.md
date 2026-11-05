# Scenariusz danych testowych ODM - październik 2026

> Plik generowany przez `fixtures/generate.py` - nie edytuj ręcznie. Wszystkie firmy, osoby, NIP-y, IBAN-y,
> telefony i numery KSeF są wymyślone (sumy kontrolne poprawne). Prawda referencyjna: `ground_truth.json`.

Firma: **ODM Partners sp. z o.o.**, NIP 5342650778. Okres: 01.10-31.10.2026 plus faktury wrześniowe płacone w październiku i październikowe płatne w listopadzie (należności mają wiek). Stan na 31.10.2026.

## Liczby

| Co | Ile |
| --- | --- |
| Transakcje zaksięgowane (BOOK), 3 rachunki | 83 |
| Transakcje oczekujące (PDNG) | 4 |
| Faktury KSeF - sprzedaż / zakup | 13 / 29 |
| PDF-y z maili | 6 |
| Wiersze arkusza PM | 20 |
| Wiersze JPK_V7M 09/2026 (sprzedaż / zakup) | 5 / 18 |
| Wiersze eksportu biura 10/2025 | 23 |
| Eventy | 5 |

## Rachunki i bilans

| Rachunek | IBAN | Saldo otwarcia | Wpływy | Wypływy | Saldo zamknięcia | BOOK | PDNG | Dostępne |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mBank Biznes - rachunek bieżący PLN | `PL40114020040000350277812345` | 142 386,52 | 187 167,50 | -190 733,67 | 138 820,35 | 66 | 3 | 137 184,55 |
| mBank Biznes - rachunek VAT | `PL67114020040000310277812346` | 6 912,00 | 15 180,00 | -17 627,00 | 4 465,00 | 5 | 0 | 4 465,00 |
| Millennium - rachunek bieżący PLN | `PL80116022020000000412906733` | 38 450,19 | 40 383,12 | -61 717,40 | 17 115,91 | 12 | 1 | 17 083,41 |

Saldo otwarcia + wszystkie operacje BOOK = saldo zamknięcia co do grosza (asercja w skrypcie, także po ponownym odczycie camt.053). Blokady kartowe (PDNG) są w osobnych plikach `transactions_pending.json` i zmniejszają tylko saldo dostępne (ITAV).

## Eventy i marża memoriałowa netto

| Id | Event | Klient | Daty | Miasto | Przychód | Koszt | Marża | Cash contribution |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EV-01 | Gala ACME 2026 | ACME Polska sp. z o.o. | 2026-10-08 - 2026-10-10 | Kraków | 90 000,00 | 73 036,40 | 16 963,60 | 27 859,20 |
| EV-02 | Roadshow Nordis Gdańsk | Nordis Retail sp. z o.o. | 2026-10-03 - 2026-10-03 | Gdańsk | 16 504,06 | 10 458,54 | 6 045,52 | -180,00 |
| EV-03 | Roadshow Nordis Wrocław | Nordis Retail sp. z o.o. | 2026-10-17 - 2026-10-17 | Wrocław | 21 000,00 | 16 358,54 | 4 641,46 | -7 965,00 |
| EV-04 | Roadshow Nordis Łódź | Nordis Retail sp. z o.o. | 2026-10-24 - 2026-10-24 | Łódź | 25 000,00 | 18 577,20 | 6 422,80 | -10 833,20 |
| EV-05 | Helios Partner Summit 2026 | Helios Software GmbH | 2026-10-15 - 2026-10-16 | Warszawa | 53 085,00 | 39 908,00 | 13 177,00 | 29 779,50 |

Marża liczona z `final_allocation` (po decyzjach człowieka): faktura zbiorcza 20/30/50, projekt Kreska po 1/3, hotel i sala Odra wg sugestii, paragon za paliwo -> Łódź. Zaliczka nie jest przychodem, ROZ = całe zamówienie, korekta = różnica. Wiersze arkusza PM to koszt (jeden raz, nawet gdy jest też PDF i przelew). ZUS/PIT od umów cywilnoprawnych -> UNASSIGNED (D9).

Raport kasowy października: suma eventów 38 660,50 + UNASSIGNED -61 207,95 + niesparowane -4 800,00 = zmiana sald -27 347,45; różnica kontrolna 0,00.

## Pułapki w banku

### Split payment (MPP) i przeksięgowanie z rachunku VAT

ACME płaci faktury FV 3/10/2026 (rozliczeniowa, BEZ adnotacji MPP) i FV 7/10/2026 (z adnotacją P_18A=1) podzieloną płatnością: kwota netto wpływa na rachunek bieżący mBank, kwota VAT na rachunek VAT, obie nogi mają ten sam tytuł `/VAT/…/IDC/5342650778/INV/…/TXT/…`. Dwie nogi to jedno zdarzenie dopasowane do faktury na kwotę brutto. ODM sam płaci splitem fakturę Scena Pro SP/2026/10/087 (netto z bieżącego, VAT z rachunku VAT). Przeksięgowanie z rachunku VAT na bieżący (postanowienie naczelnika US) to transfer własny. Kontrola MPP: suma bloków /VAT/ we wpływach = suma wpływów na rachunek VAT. Uwaga: numer FV 7/10/2026 ma też faktura czynszowa Biura Centrum - rozstrzyga NIP (IDC) i kierunek.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610234002647079` | mbank_biezacy | 2026-10-23 | 56 000,00 | ACME Polska sp. z o.o. | /VAT/12880,00/IDC/5342650778/INV/FV 3/10/2026/TXT/rozliczenie Gala ACME | matched | auto | FV 3/10/2026 (56 000,00) |
| `202610232774376085` | mbank_vat | 2026-10-23 | 12 880,00 | ACME Polska sp. z o.o. | /VAT/12880,00/IDC/5342650778/INV/FV 3/10/2026/TXT/rozliczenie Gala ACME | matched | auto | FV 3/10/2026 (12 880,00) |
| `202610275984818978` | mbank_biezacy | 2026-10-27 | 10 000,00 | ACME Polska sp. z o.o. | /VAT/2300,00/IDC/5342650778/INV/FV 7/10/2026/TXT/atrakcje Gala ACME | matched | auto | FV 7/10/2026 (10 000,00) |
| `202610277768618204` | mbank_vat | 2026-10-27 | 2 300,00 | ACME Polska sp. z o.o. | /VAT/2300,00/IDC/5342650778/INV/FV 7/10/2026/TXT/atrakcje Gala ACME | matched | auto | FV 7/10/2026 (2 300,00) |
| `202610143186203462` | mbank_biezacy | 2026-10-14 | -25 000,00 | Scena Pro sp. z o.o. | /VAT/5750,00/IDC/9516127975/INV/SP/2026/10/087/TXT/technika Gala ACME | matched | auto | SP/2026/10/087 (25 000,00) |
| `202610141002142076` | mbank_vat | 2026-10-14 | -5 750,00 | Scena Pro sp. z o.o. | /VAT/5750,00/IDC/9516127975/INV/SP/2026/10/087/TXT/technika Gala ACME | matched | auto | SP/2026/10/087 (5 750,00) |
| `202610288152441704` | mbank_biezacy | 2026-10-28 | 3 000,00 | ODM Partners sp. z o.o. | Przeksiegowanie z rachunku VAT - postanowienie NUS 1471-SPV.4012.37.2026 z 21.10.2026 | own_transfer | auto | para `202610286656245067` |
| `202610286656245067` | mbank_vat | 2026-10-28 | -3 000,00 | ODM Partners sp. z o.o. | Przeksiegowanie z rachunku VAT - postanowienie NUS 1471-SPV.4012.37.2026 z 21.10.2026 | own_transfer | auto | para `202610288152441704` |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV 3/10/2026 | ROZ | sprzedaż | ACME Polska sp. z o.o. | 2026-10-12 | 68 880,00 PLN | `5342650778-20261012-32A55F0B257F-45` |
| FV 7/10/2026 | VAT | sprzedaż | ACME Polska sp. z o.o. | 2026-10-21 | 12 300,00 PLN | `5342650778-20261021-1D7921E70E4A-35` |
| SP/2026/10/087 | VAT | zakup | Scena Pro sp. z o.o. | 2026-10-12 | 30 750,00 PLN | `9516127975-20261012-2AA68213FEA1-23` |

### Jeden przelew za trzy faktury

ODM płaci Scena Pro jednym przelewem za trzy faktury roadshow Nordis; tytuł zawiera trzy numery, kwota = suma co do grosza. Oczekiwane: trzy dopasowania z kwotami w jednej grupie.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610289276403991` | mbank_biezacy | 2026-10-28 | -16 236,00 | Scena Pro sp. z o.o. | Zaplata za FV SP/2026/10/081, SP/2026/10/093, SP/2026/10/101 | matched | auto | SP/2026/10/081 (4 920,00); SP/2026/10/093 (4 920,00); SP/2026/10/101 (6 396,00) |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| SP/2026/10/081 | VAT | zakup | Scena Pro sp. z o.o. | 2026-10-05 | 4 920,00 PLN | `9516127975-20261005-CB4C8C08EEC5-62` |
| SP/2026/10/093 | VAT | zakup | Scena Pro sp. z o.o. | 2026-10-19 | 4 920,00 PLN | `9516127975-20261019-035132680149-DD` |
| SP/2026/10/101 | VAT | zakup | Scena Pro sp. z o.o. | 2026-10-26 | 6 396,00 PLN | `9516127975-20261026-D4AC3D8591B2-11` |

### Korekta zmniejszająca: 10 000 - 2 000 = 8 000

Faktura FV 2/10/2026 dla Nordis na 10 000,00 brutto, korekta KOR 1/10/2026 na -2 000,00 (rabat, wiersze StanPrzed/po, VAT liczony od różnicy). Klient płaci 8 000,00 z tytułem zawierającym tylko numer faktury pierwotnej. Saldo liczone z łańcucha faktura + korekta; jeden przelew spina oba dokumenty.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ML2610205610950` | millennium_biezacy | 2026-10-20 | 8 000,00 | Nordis Retail sp. z o.o. | Zaplata FV 2/10/2026 po korekcie | matched | auto | FV 2/10/2026 (10 000,00); KOR 1/10/2026 (-2 000,00) |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV 2/10/2026 | VAT | sprzedaż | Nordis Retail sp. z o.o. | 2026-10-05 | 10 000,00 PLN | `5342650778-20261005-F3887A61DE66-1E` |
| KOR 1/10/2026 | KOR | sprzedaż | Nordis Retail sp. z o.o. | 2026-10-14 | -2 000,00 PLN | `5342650778-20261014-3E16B3750463-E8` |

### Wpływ od „Revolut” z klientem w tytule

Nadawcą jest Revolut Bank UAB (IBAN LT), a nie klient; klient (Kwadrat Development) jest tylko w tytule, bez numeru faktury. Kwota równa jedynej otwartej fakturze Kwadrat. Oczekiwane: kolejka z jedną propozycją; po akceptacji reguła „ten IBAN LT = Kwadrat”.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610233423350565` | mbank_biezacy | 2026-10-23 | 14 760,00 | Revolut Bank UAB | KWADRAT DEVELOPMENT SP. Z O.O. - wynajem naglosnienia | matched | queue | FV 4/10/2026 (14 760,00) |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV 4/10/2026 | VAT | sprzedaż | Kwadrat Development sp. z o.o. | 2026-10-14 | 14 760,00 PLN | `5342650778-20261014-FB42A5AC4510-90` |

### Karta za zagraniczny SaaS (różnica kursowa) i paragon

Google Workspace (99 EUR), Anthropic (150 USD), Notion (72 USD) - faktury przychodzą PDF-em, płatność kartą w PLN po kursie banku, 1-2 dni po dacie faktury. Faktura trzyma walutę; PLN z kursu NBP z dnia poprzedniego (reference/nbp_rates.json). Różnica ma nazwę „kurs”. Paragon za paliwo (Łódź, 24.10) - dowód PDF, przypisanie do eventu tylko jako podpowiedź (miejsce + data).

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610166224040945` | mbank_biezacy | 2026-10-16 | -437,12 | GOOGLE *WORKSPACE ODMPAR | GOOGLE *Workspace odmpar G.CO/HELPPAY# IE (+2) | matched | queue | EM-01 5384029117 (437,12); różnica „kurs” 16,25 |
| `202610021670611934` | mbank_biezacy | 2026-10-02 | -586,35 | ANTHROPIC* CLAUDE TEAM | ANTHROPIC* CLAUDE TEAM SAN FRANCISCO US (+2) | matched | queue | EM-02 INV-2610-0437 (586,35); różnica „kurs” 10,05 |
| `202610221759883865` | mbank_biezacy | 2026-10-22 | -281,95 | NOTION LABS INC | NOTION LABS, INC. SAN FRANCISCO US (+2) | matched | queue | EM-03 8F2C41A0-0007 (281,95); różnica „kurs” 6,37 |
| `202610264228948107` | mbank_biezacy | 2026-10-26 | -377,20 | STACJA PETROVA 214 LODZ | STACJA PETROVA 214 LODZ PL (+1) | matched | queue | EM-04 028417 (377,20) |

### BLIK dla hostess na telefon + arkusz PM

16 przelewów BLIK na telefon (500-xxx-xxx w tytule, bez IBAN-u) ma odpowiedniki w arkuszu PM (internal/arkusz_pm_2026-10.*; nazwisko w formacie „Nazwisko Imię”, data pracy wcześniejsza niż przelew). Arkusz to dokument wewnętrzny, ale nie twardy dowód (decyzja D2) - kolejka. Dodatkowo: zwrot kosztów dla PM i umowa o dzieło (przelewy z dokumentem w arkuszu), JEDEN wiersz bez odpowiednika w banku (R20, Sadowska - do wypłaty) i JEDEN BLIK bez wiersza (Michalak, 300,00 - do wyjaśnienia).

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610053264356611` | mbank_biezacy | 2026-10-05 | -400,00 | MAJA LEWANDOWICZ | Nordis Gdansk hostessa 03.10 (+1) | matched | queue | PM R01 (400,00) |
| `202610055288071238` | mbank_biezacy | 2026-10-05 | -400,00 | HANNA DROZDOWSKA | Nordis Gdansk hostessa 03.10 (+1) | matched | queue | PM R02 (400,00) |
| `202610129033812558` | mbank_biezacy | 2026-10-12 | -520,00 | NATALIA WÓJCICKA | Gala ACME koordynacja hostess 09.10 (+1) | matched | queue | PM R04 (520,00) |
| `202610126851950665` | mbank_biezacy | 2026-10-12 | -450,00 | JULIA SOBIERAJ | Gala ACME hostessa 09.10 (+1) | matched | queue | PM R05 (450,00) |
| `202610124185038206` | mbank_biezacy | 2026-10-12 | -450,00 | OLIWIA KARCZMAREK | Gala ACME hostessa 09.10 (+1) | matched | queue | PM R06 (450,00) |
| `202610121722469427` | mbank_biezacy | 2026-10-12 | -450,00 | ZUZANNA PIETRASIK | Gala ACME hostessa 09.10 (+1) | matched | queue | PM R07 (450,00) |
| `202610191974988946` | mbank_biezacy | 2026-10-19 | -400,00 | AMELIA ZAWILSKA | Nordis Wroclaw hostessa 17.10 (+1) | matched | queue | PM R13 (400,00) |
| `202610197443800288` | mbank_biezacy | 2026-10-19 | -400,00 | LENA KUCZYŃSKA | Nordis Wroclaw hostessa 17.10 (+1) | matched | queue | PM R14 (400,00) |
| `202610199643433857` | mbank_biezacy | 2026-10-19 | -400,00 | WIKTORIA BARTOSZEK | Nordis Wroclaw hostessa 17.10 (+1) | matched | queue | PM R15 (400,00) |
| `202610195324026909` | mbank_biezacy | 2026-10-19 | -900,00 | KINGA RUTKOWSKA | Helios Summit hostessa 15-16.10 (+1) | matched | queue | PM R09 (900,00) |
| `202610191274866862` | mbank_biezacy | 2026-10-19 | -900,00 | PAULA WESOŁOWSKA | Helios Summit hostessa 15-16.10 (+1) | matched | queue | PM R10 (900,00) |
| `202610192377596813` | mbank_biezacy | 2026-10-19 | -900,00 | EWA JABŁONOWSKA | Helios Summit hostessa 15-16.10 (+1) | matched | queue | PM R11 (900,00) |
| `202610266449035591` | mbank_biezacy | 2026-10-26 | -400,00 | ALICJA GRABARCZYK | Nordis Lodz hostessa 24.10 (+1) | matched | queue | PM R16 (400,00) |
| `202610267505245647` | mbank_biezacy | 2026-10-26 | -400,00 | LAURA MIKULSKA | Nordis Lodz hostessa 24.10 (+1) | matched | queue | PM R17 (400,00) |
| `202610268440070535` | mbank_biezacy | 2026-10-26 | -400,00 | NIKOLA SZAFRAŃSKA | Nordis Lodz hostessa 24.10 (+1) | matched | queue | PM R18 (400,00) |
| `202610269739519729` | mbank_biezacy | 2026-10-26 | -400,00 | MARTYNA OLEJNICZAK | Nordis Lodz hostessa 24.10 (+1) | matched | queue | PM R19 (400,00) |
| `202610131625662595` | mbank_biezacy | 2026-10-13 | -300,00 | DAWID MICHALAK | pomoc przy demontazu (+1) | to_clarify | queue | - |
| `202610128715345043` | mbank_biezacy | 2026-10-12 | -286,40 | Anna Mazur | Zwrot kosztow - dekoracje kwiatowe Gala ACME | matched | queue | PM R03 (286,40) |
| `202610205635891886` | mbank_biezacy | 2026-10-20 | -1 808,00 | Jakub Wrona | Umowa o dzielo 07/2026 - projekt scenografii Helios Summit | matched | queue | PM R12 (1 808,00) |

### ZUS, US, prowizje, wynagrodzenia - bez dokumentów

Składki ZUS, PIT-4R, zaliczka CIT, VAT-7 (z rachunku VAT; kwota = P_51 z JPK_V7M za wrzesień), prowizje i opłaty bankowe, odsetki, wynagrodzenia. To osobne hipotezy z kategoriami, nie „nie znaleźliśmy”. Tytuły podatkowe w formacie `/TI/N…/OKR/26M09/SFP/…/TXT/…`.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610157109429897` | mbank_biezacy | 2026-10-15 | -14 382,47 | Zakład Ubezpieczeń Społecznych | Skladki ZUS za 09/2026 | no_document (ZUS) | any | - |
| `202610208183657650` | mbank_biezacy | 2026-10-20 | -3 214,00 | Urząd Skarbowy - mikrorachunek podatkowy | /TI/N5342650778/OKR/26M09/SFP/PIT4R/TXT/zaliczki PIT 09/2026 | no_document (Podatki (US)) | any | - |
| `202610201635845675` | mbank_biezacy | 2026-10-20 | -6 450,00 | Urząd Skarbowy - mikrorachunek podatkowy | /TI/N5342650778/OKR/26M09/SFP/CIT8/TXT/zaliczka CIT 09/2026 | no_document (Podatki (US)) | any | - |
| `202610266071538229` | mbank_vat | 2026-10-26 | -8 877,00 | Urząd Skarbowy - mikrorachunek podatkowy | /TI/N5342650778/OKR/26M09/SFP/VAT7/TXT/VAT za 09/2026 | no_document (Podatki (US)) | any | - |
| `202610281026795668` | mbank_biezacy | 2026-10-28 | -25,00 | - | Prowizja - przelew zagraniczny przychodzacy SEPA | no_document (Prowizje bankowe) | any | - |
| `202610302699472862` | mbank_biezacy | 2026-10-30 | -89,00 | - | Oplata za prowadzenie rachunku 10/2026 | no_document (Prowizje bankowe) | any | - |
| `ML2610196097579` | millennium_biezacy | 2026-10-19 | -5,00 | - | Prowizja za przelew Express Elixir | no_document (Prowizje bankowe) | any | - |
| `ML2610305496545` | millennium_biezacy | 2026-10-30 | -39,00 | - | Oplata miesieczna za prowadzenie rachunku | no_document (Prowizje bankowe) | any | - |
| `ML2610309084734` | millennium_biezacy | 2026-10-30 | 3,12 | - | Kapitalizacja odsetek 10/2026 | no_document (Odsetki) | any | - |
| `202610097692101583` | mbank_biezacy | 2026-10-09 | -7 240,18 | Anna Mazur | Wynagrodzenie 09/2026 | no_document (Wynagrodzenia) | any | - |
| `202610099337938270` | mbank_biezacy | 2026-10-09 | -6 512,40 | Bartłomiej Sowiński | Wynagrodzenie 09/2026 | no_document (Wynagrodzenia) | any | - |
| `202610093419345457` | mbank_biezacy | 2026-10-09 | -5 980,77 | Karolina Wierzchowska | Wynagrodzenie 09/2026 | no_document (Wynagrodzenia) | any | - |
| `202610095435590927` | mbank_biezacy | 2026-10-09 | -4 105,33 | Tomasz Grabowiecki | Wynagrodzenie 09/2026 | no_document (Wynagrodzenia) | any | - |

### Zaliczki: przychodząca przed fakturą i wychodząca bez faktury

ACME wpłaca 29 520,00 zaliczki 01.10 bez numeru faktury; faktura zaliczkowa FV 1/10/2026 (RodzajFaktury ZAL, P_6 = data wpłaty, Zamowienie z wartością zamówienia) powstaje 02.10, a rozliczeniowa FV 3/10/2026 (ROZ) po evencie. Zaliczka nie jest przychodem - przychodem jest całe zamówienie 80 000 netto w dacie sprzedaży ROZ. ODM wpłaca 5 000,00 zaliczki na poczet faktury Dworowi Kalina (rezerwacja na listopad) - w październiku brak dokumentu: no_document/Zaliczki, czeka do 180 dni.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610017411739420` | mbank_biezacy | 2026-10-01 | 29 520,00 | ACME Polska sp. z o.o. | Zaliczka 30% - Gala ACME 2026 - umowa 14/2026 | matched | any | FV 1/10/2026 (29 520,00) |
| `202610076358867832` | mbank_biezacy | 2026-10-07 | -5 000,00 | Dwór Kalina Event sp. z o.o. | Zaliczka na poczet faktury - rezerwacja Dwor Kalina, termin 14.11.2026 | no_document (Zaliczki) | queue | - |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV 1/10/2026 | ZAL | sprzedaż | ACME Polska sp. z o.o. | 2026-10-02 | 29 520,00 PLN | `5342650778-20261002-BE973AA732BF-1B` |
| FV 3/10/2026 | ROZ | sprzedaż | ACME Polska sp. z o.o. | 2026-10-12 | 68 880,00 PLN | `5342650778-20261012-32A55F0B257F-45` |

### Nadpłata (duplikat wpłaty) i zwrot

Orbita Media płaci FV 5/09/2026 dwa razy (13.10 i 14.10, ten sam tytuł i kwota). Druga wpłata = nadpłata z flagą „możliwy duplikat” (zawsze kolejka); zwrot 21.10 wiąże się z drugą wpłatą, nic nie znika. Dopasowania faktury nie przekraczają jej kwoty.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610136613771164` | mbank_biezacy | 2026-10-13 | 3 075,00 | Orbita Media sp. z o.o. | Zaplata za FV 5/09/2026 | matched | auto | FV 5/09/2026 (3 075,00) |
| `202610143818524718` | mbank_biezacy | 2026-10-14 | 3 075,00 | Orbita Media sp. z o.o. | Zaplata za FV 5/09/2026 | matched | queue | przelew `202610219260141951` (refunded_by); różnica „nadpłata (możliwy duplikat)” 3 075,00 |
| `202610219260141951` | mbank_biezacy | 2026-10-21 | -3 075,00 | Orbita Media sp. z o.o. | Zwrot nadplaty - podwojna wplata z 14.10.2026 za FV 5/09/2026 | matched | queue | przelew `202610143818524718` (refund_of); różnica „zwrot nadpłaty” -3 075,00 |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV 5/09/2026 | VAT | sprzedaż | Orbita Media sp. z o.o. | 2026-09-28 | 3 075,00 PLN | `5342650778-20260928-11D840C7317D-FE` |

### Remis: dwie faktury po 4 500 zł i przelew „zaplata”

Kadr Studio wystawiło dwie faktury po 4 500,00 (fotorelacje Gdańsk i Wrocław). ODM wysyła 4 500,00 z tytułem „zaplata”. Remis = zawsze człowiek; do rozstrzygnięcia obie faktury pozostają otwarte w zobowiązaniach.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610277149922608` | mbank_biezacy | 2026-10-27 | -4 500,00 | Kadr Studio Paweł Wierzbicki | zaplata | to_clarify | queue | remis: FV 14/2026 / FV 15/2026 |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV 14/2026 | VAT | zakup | Kadr Studio Paweł Wierzbicki | 2026-10-05 | 4 500,00 PLN | `5837377627-20261006-AF50F2319AFC-01` |
| FV 15/2026 | VAT | zakup | Kadr Studio Paweł Wierzbicki | 2026-10-19 | 4 500,00 PLN | `5837377627-20261019-8449FC1F191F-84` |

### Numer faktury w tytule, ale niezgodna kwota (częściowa zapłata)

Agencja Fala płaci 5 000,00 tytułem „FV 4/09/2026 czesc 1” przy fakturze 9 225,00. Twardy identyfikator zostaje, ale kwota idzie do warstwy 3: dopasowanie częściowe z różnicą „niedopłata”; reszta 4 225,00 jest należnością przeterminowaną (kubełek 31-60).

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ML2610084922077` | millennium_biezacy | 2026-10-08 | 5 000,00 | Agencja Fala sp. z o.o. | FV 4/09/2026 czesc 1 | matched | queue | FV 4/09/2026 (5 000,00); różnica „niedopłata” -4 225,00 |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV 4/09/2026 | VAT | sprzedaż | Agencja Fala sp. z o.o. | 2026-09-14 | 9 225,00 PLN | `5342650778-20260914-D18FC534C88C-71` |

### Przelew walutowy EUR na rachunek PLN

Helios Software GmbH płaci 12 500,00 EUR za fakturę FV 5/10/2026 (EUR, np II, odwrotne obciążenie). Bank przewalutowuje po 4,2190 i księguje 52 737,50 PLN (exchange_rate w JSON, AmtDtls/CcyXchg w camt). Faktura w PLN po NBP 4,2468 = 53 085,00; różnica „kurs” -347,50. Osobna prowizja za przelew SEPA bez dokumentu.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610280050568790` | mbank_biezacy | 2026-10-28 | 52 737,50 | Helios Software GmbH | INV FV 5/10/2026 HELIOS PARTNER SUMMIT 2026 | matched | auto | FV 5/10/2026 (52 737,50); różnica „kurs” -347,50 |
| `202610281026795668` | mbank_biezacy | 2026-10-28 | -25,00 | - | Prowizja - przelew zagraniczny przychodzacy SEPA | no_document (Prowizje bankowe) | any | - |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV 5/10/2026 | VAT | sprzedaż | Helios Software GmbH | 2026-10-16 | 12 500,00 EUR | `5342650778-20261016-BCF879831158-F7` |

### Przelewy własne mBank <-> Millennium

Zasilenie Millennium z mBanku (ten sam dzień) i zwrot z Millennium do mBanku (obciążenie 29.10, uznanie 30.10 - parowanie z przesunięciem daty). Status own_transfer, obie nogi.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610065634798198` | mbank_biezacy | 2026-10-06 | -20 000,00 | ODM Partners sp. z o.o. | Przelew wlasny - zasilenie rachunku Millennium | own_transfer | auto | para `ML2610068440316` |
| `ML2610068440316` | millennium_biezacy | 2026-10-06 | 20 000,00 | ODM Partners sp. z o.o. | Przelew wlasny - zasilenie rachunku Millennium | own_transfer | auto | para `202610065634798198` |
| `ML2610299971452` | millennium_biezacy | 2026-10-29 | -15 000,00 | ODM Partners sp. z o.o. | Przelew wlasny | own_transfer | auto | para `202610309011341667` |
| `202610309011341667` | mbank_biezacy | 2026-10-30 | 15 000,00 | ODM Partners sp. z o.o. | Przelew wlasny | own_transfer | auto | para `ML2610299971452` |

### Tytuł z nazwiskiem i słowem wrażliwym (komornik)

Przelew do komornika z tytułem zawierającym słowo „Komornik”, sygnaturę i nazwisko dłużnika (pracownika). Bramka pseudonimizacji: tytuł omija model AI w ogóle, trafia do kolejki, kategorię nadaje człowiek. Pełna lista danych osobowych z fixtures jest w ground_truth.json -> pii.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610096232801350` | mbank_biezacy | 2026-10-09 | -1 250,00 | Komornik Sądowy przy Sądzie Rejonowym dl | Komornik KM 1187/26 - zajecie wynagrodzenia dluznika Tomasz Grabowiecki za 09/2026 | no_document (Zajęcie komornicze) | queue | - |

### Ten sam numer faktury u innego NIP-u

Czynsz Biura Centrum ma numer FV 7/10/2026 - taki sam jak nasza sprzedaż dla ACME; w JPK za wrzesień Biuro Centrum ma FV 12/09/2026 - taki sam numer jak faktura Studia Kreska w KSeF. Ten sam numer u innego NIP-u to nigdy nie duplikat.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610092920619715` | mbank_biezacy | 2026-10-09 | -9 840,00 | Biuro Centrum sp. z o.o. | Czynsz pazdziernik 2026 FV 7/10/2026 | matched | auto | FV 7/10/2026 (9 840,00) |
| `ML2610095382734` | millennium_biezacy | 2026-10-09 | -7 380,00 | Studio Graficzne Kreska sp. z o.o. | FV 12/09/2026 projekt graficzny Roadshow Nordis | matched | auto | FV 12/09/2026 (7 380,00) |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV 7/10/2026 | VAT | zakup | Biuro Centrum sp. z o.o. | 2026-10-01 | 9 840,00 PLN | `5265275853-20261001-24101F269640-D0` |
| FV 7/10/2026 | VAT | sprzedaż | ACME Polska sp. z o.o. | 2026-10-21 | 12 300,00 PLN | `5342650778-20261021-1D7921E70E4A-35` |
| FV 12/09/2026 | VAT | zakup | Studio Graficzne Kreska sp. z o.o. | 2026-09-25 | 7 380,00 PLN | `8970418075-20260925-2D289E0A071E-A5` |

### Jeden koszt, trzy dowody

Konferansjer Marek Zawadzki: rachunek PDF z maila (EM-05), wiersz arkusza PM R08 i przelew 1 800,00. Do marży Gali ACME liczony raz.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610160785476080` | mbank_biezacy | 2026-10-16 | -1 800,00 | Marek Zawadzki | Rachunek 4/2026 - konferansjerka Gala ACME 09.10 | matched | any | EM-05 4/2026 (1 800,00) |

### Deduplikacja: ta sama faktura z kilku kanałów

Faktura Hotelu Wawel Grand jest w KSeF i jako PDF w mailu (EM-06, bez numeru KSeF na wydruku). Wrześniowe faktury (Fala, Orbita, Kreska, Drukarnia) są w KSeF i w JPK_V7M (jedna z numerem zapisanym „FV/5/09/2026”). Wszystkie transakcje mBanku bieżącego są w JSON EnableBanking i w camt.053 (ten sam entry_reference).

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ML2610224165347` | millennium_biezacy | 2026-10-22 | -14 078,40 | Hotel Wawel Grand sp. z o.o. | FV/2026/10/0412 | matched | auto | FV/2026/10/0412 (14 078,40) |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV/2026/10/0412 | VAT | zakup | Hotel Wawel Grand sp. z o.o. | 2026-10-10 | 14 078,40 PLN | `6768253927-20261010-035D037CD68A-E7` |
| FV 4/09/2026 | VAT | sprzedaż | Agencja Fala sp. z o.o. | 2026-09-14 | 9 225,00 PLN | `5342650778-20260914-D18FC534C88C-71` |
| FV 5/09/2026 | VAT | sprzedaż | Orbita Media sp. z o.o. | 2026-09-28 | 3 075,00 PLN | `5342650778-20260928-11D840C7317D-FE` |
| FV 12/09/2026 | VAT | zakup | Studio Graficzne Kreska sp. z o.o. | 2026-09-25 | 7 380,00 PLN | `8970418075-20260925-2D289E0A071E-A5` |
| 214/09/2026 | VAT | zakup | Drukarnia Format sp. z o.o. | 2026-09-29 | 2 952,00 PLN | `7254098012-20260929-E6F9BD7A2BC6-6F` |

### 12 drobnych faktur Orange (jednorodna paczka)

12 faktur Orange na łącznie 4 380,00 zł, każda z własnym rachunkiem wirtualnym i numerem w tytule przelewu. Kandydat na zbiorczą akceptację paczki; koszt ogólny (UNASSIGNED).

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610232074877344` | mbank_biezacy | 2026-10-23 | -464,25 | Orange Polska S.A. | F/40020041/10/26 | matched | auto | F/40020041/10/26 (464,25) |
| `202610238895930439` | mbank_biezacy | 2026-10-23 | -349,41 | Orange Polska S.A. | F/40020215/10/26 | matched | auto | F/40020215/10/26 (349,41) |
| `202610234005550231` | mbank_biezacy | 2026-10-23 | -413,42 | Orange Polska S.A. | F/40020401/10/26 | matched | auto | F/40020401/10/26 (413,42) |
| `202610231207327033` | mbank_biezacy | 2026-10-23 | -404,76 | Orange Polska S.A. | F/40021164/10/26 | matched | auto | F/40021164/10/26 (404,76) |
| `202610234436463076` | mbank_biezacy | 2026-10-23 | -309,91 | Orange Polska S.A. | F/40021770/10/26 | matched | auto | F/40021770/10/26 (309,91) |
| `202610235181437466` | mbank_biezacy | 2026-10-23 | -390,23 | Orange Polska S.A. | F/40021998/10/26 | matched | auto | F/40021998/10/26 (390,23) |
| `202610232954944150` | mbank_biezacy | 2026-10-23 | -392,15 | Orange Polska S.A. | F/40022271/10/26 | matched | auto | F/40022271/10/26 (392,15) |
| `202610232283937056` | mbank_biezacy | 2026-10-23 | -369,62 | Orange Polska S.A. | F/40022282/10/26 | matched | auto | F/40022282/10/26 (369,62) |
| `202610236171165512` | mbank_biezacy | 2026-10-23 | -420,64 | Orange Polska S.A. | F/40022318/10/26 | matched | auto | F/40022318/10/26 (420,64) |
| `202610239443481757` | mbank_biezacy | 2026-10-23 | -152,52 | Orange Polska S.A. | F/40022346/10/26 | matched | auto | F/40022346/10/26 (152,52) |
| `202610236037395553` | mbank_biezacy | 2026-10-23 | -280,78 | Orange Polska S.A. | F/40022509/10/26 | matched | auto | F/40022509/10/26 (280,78) |
| `202610233396451346` | mbank_biezacy | 2026-10-23 | -432,31 | Orange Polska S.A. | F/40022585/10/26 | matched | auto | F/40022585/10/26 (432,31) |

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| F/40020041/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 464,25 PLN | `5263451926-20261005-6F6E70E45848-68` |
| F/40020215/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 349,41 PLN | `5263451926-20261005-45E1752BBD60-AA` |
| F/40020401/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 413,42 PLN | `5263451926-20261005-B13DFC6D17F2-14` |
| F/40021164/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 404,76 PLN | `5263451926-20261005-792C342B09D1-B5` |
| F/40021770/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 309,91 PLN | `5263451926-20261005-70485A0B91FB-DB` |
| F/40021998/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 390,23 PLN | `5263451926-20261005-D257A5B41974-70` |
| F/40022271/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 392,15 PLN | `5263451926-20261005-B87580461C26-B7` |
| F/40022282/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 369,62 PLN | `5263451926-20261005-2C2BBEC6EA01-C6` |
| F/40022318/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 420,64 PLN | `5263451926-20261005-6D932D567AF4-54` |
| F/40022346/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 152,52 PLN | `5263451926-20261005-2B95899197CE-B0` |
| F/40022509/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 280,78 PLN | `5263451926-20261005-5B6A4ADA4E9C-F8` |
| F/40022585/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 432,31 PLN | `5263451926-20261005-0BFB0C8C18A3-46` |

### Ścieżka podstawowa

Przelewy z numerem faktury, zgodnym kontrahentem i kwotą co do grosza - automat może zamknąć sam.

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610086622571743` | mbank_biezacy | 2026-10-08 | -2 952,00 | Drukarnia Format sp. z o.o. | Faktura 214/09/2026 | matched | auto | 214/09/2026 (2 952,00) |
| `202610120329141687` | mbank_biezacy | 2026-10-12 | -1 845,00 | Biuro Rachunkowe Bilans sp. z o.o. | FV/2026/10/033 uslugi ksiegowe 09/2026 | matched | auto | FV/2026/10/033 (1 845,00) |
| `202610202756598854` | mbank_biezacy | 2026-10-20 | -31 104,00 | Smaki Wisły Catering sp. z o.o. | Faktura 2026/10/0193 catering Gala ACME | matched | auto | 2026/10/0193 (31 104,00) |
| `202610266785844198` | mbank_biezacy | 2026-10-26 | -3 505,50 | Mobilis Leasing sp. z o.o. | Polecenie zaplaty L/10/2026/0815 rata 10/36 | matched | auto | L/10/2026/0815 (3 505,50) |
| `ML2610095382734` | millennium_biezacy | 2026-10-09 | -7 380,00 | Studio Graficzne Kreska sp. z o.o. | FV 12/09/2026 projekt graficzny Roadshow Nordis | matched | auto | FV 12/09/2026 (7 380,00) |
| `ML2610197665994` | millennium_biezacy | 2026-10-19 | -18 450,00 | Loft Praga sp. z o.o. | LP/118/2026 Helios Partner Summit | matched | auto | LP/118/2026 (18 450,00) |
| `ML2610210025139` | millennium_biezacy | 2026-10-21 | -6 765,00 | Przestrzeń Odra sp. z o.o. | Faktura 2026/10/27 | matched | auto | 2026/10/27 (6 765,00) |
| `ML2610301580065` | millennium_biezacy | 2026-10-30 | 7 380,00 | Nordis Retail sp. z o.o. | FV 8/10/2026 | matched | auto | FV 8/10/2026 (7 380,00) |

## Pułapki w dokumentach

- **Faktura zbiorcza za trzy eventy** - FV 10/10/2026 (`5342650778-20261031-9DF88F833A08-29`): Jedna pozycja „Obsługa eventów - październik 2026” na 50 000 netto za trzy roadshow Nordis. Faktura zbiorcza nigdy nie jest kotwicą eventu. Brak ofert -> podział wg osobodni z arkusza PM (Gdańsk 2, Wrocław 3, Łódź 5 = 20/30/50) jako SUGESTIA w kolejce. Wystawiona 31.10, płatna 30.11 - należność niewymagalna.
- **Okres usługi zamiast daty sprzedaży (przez dwa miesiące)** - FV 4/10/2026 (`5342650778-20261014-FB42A5AC4510-90`): OkresFa 15.09-14.10.2026 zamiast P_6: przychód 12 000 netto rozkłada się po dniach - 6 400 na wrzesień, 5 600 na październik.
- **Faktura bez terminu płatności** - FV 9/10/2026 (`5342650778-20261026-BACC87CCF7B6-76`): Brak TerminPlatnosci (pole opcjonalne w FA(3)). Brak terminu nie znaczy „płatne od ręki”: wiek należności liczony od daty wystawienia i oznaczony.
- **Faktura w EUR z kursem w pozycji** - FV 5/10/2026 (`5342650778-20261016-BCF879831158-F7`): KodWaluty EUR, P_13_9 (np II), P_18=1 (odwrotne obciążenie), KursWaluty w FaWiersz. Kwota główna w EUR, PLN pochodna.
- **Hotel z okresem pobytu (sygnał B)** - FV/2026/10/0412 (`6768253927-20261010-035D037CD68A-E7`): OkresFa 08-10.10.2026, dwie stawki VAT (8% nocleg, 23% parking), w pozycjach brak nazwy eventu. Kontrahent + okres + miasto wskazują jedyny event w oknie (Gala ACME) - sugestia, nie automat.
- **Sala bez nazwy eventu (sygnał B)** - 2026/10/27 (`8989012005-20261019-0E02469CDA8D-1F`): Wynajem sali we Wrocławiu 17.10 bez nazwy eventu - sugestia Roadshow Nordis Wrocław, kolejka.
- **Projekt na trzy eventy** - FV 12/09/2026 (`8970418075-20260925-2D289E0A071E-A5`): Pozycja wymienia trzy miasta roadshow - podział równy tylko decyzją człowieka (kolejka).
- **KSeF offline** - FV 14/2026 (`5837377627-20261006-AF50F2319AFC-01`): Faktura wystawiona 05.10, przyjęta w KSeF 06.10 - data w numerze KSeF różni się od daty wystawienia.
- **Brak P_6 = data sprzedaży jak data wystawienia** - LP/118/2026 (`5245573056-20261016-4CE9D6D72B6F-6E`): Gdy data sprzedaży jest równa dacie wystawienia, FA(3) nie zawiera P_6 - parser musi przyjąć P_1.

## Należności otwarte na 31.10.2026

| Faktura | Kontrahent | Wystawiona | Termin | Brutto | Zapłacono | Otwarte | Dni po terminie* | Kubełek |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| FV 4/09/2026 | Agencja Fala sp. z o.o. | 2026-09-14 | 2026-09-28 | 9 225,00 | 5 000,00 | 4 225,00 | 33 | 31-60 |
| FV 6/10/2026 | Agencja Fala sp. z o.o. | 2026-10-19 | 2026-11-02 | 6 150,00 | 0,00 | 6 150,00 | -2 | nie_wymagalne |
| FV 9/10/2026 | Orbita Media sp. z o.o. | 2026-10-26 | brak | 3 690,00 | 0,00 | 3 690,00 | 5 | 1-30 |
| FV 10/10/2026 | Nordis Retail sp. z o.o. | 2026-10-31 | 2026-11-30 | 61 500,00 | 0,00 | 61 500,00 | -30 | nie_wymagalne |

Razem: 75 565,00 zł. *Ujemne = dni do terminu; przy braku terminu - dni od wystawienia (flaga `due_date_missing`).

## Zobowiązania otwarte na 31.10.2026

| Faktura | Kontrahent | Termin | Otwarte | Kubełek | Nierozstrzygnięta płatność |
| --- | --- | --- | --- | --- | --- |
| FV 14/2026 | Kadr Studio Paweł Wierzbicki | 2026-10-19 | 4 500,00 | 1-30 | `202610277149922608` |
| BM/1016/2026 | Bufet Mazowsze sp. z o.o. | 2026-11-15 | 22 032,00 | nie_wymagalne | - |
| FV 15/2026 | Kadr Studio Paweł Wierzbicki | 2026-11-02 | 4 500,00 | nie_wymagalne | `202610277149922608` |
| HEL/2026/10/0057 | Hala Expo Łódź sp. z o.o. | 2026-11-09 | 11 070,00 | nie_wymagalne | - |

Razem: 42 102,00 zł.

## Faktury KSeF

| Faktura | Rodzaj | Kierunek | Kontrahent | Wystawiona | Brutto | Numer KSeF |
| --- | --- | --- | --- | --- | --- | --- |
| FV 4/09/2026 | VAT | sprzedaż | Agencja Fala sp. z o.o. | 2026-09-14 | 9 225,00 PLN | `5342650778-20260914-D18FC534C88C-71` |
| FV 5/09/2026 | VAT | sprzedaż | Orbita Media sp. z o.o. | 2026-09-28 | 3 075,00 PLN | `5342650778-20260928-11D840C7317D-FE` |
| FV 1/10/2026 | ZAL | sprzedaż | ACME Polska sp. z o.o. | 2026-10-02 | 29 520,00 PLN | `5342650778-20261002-BE973AA732BF-1B` |
| FV 2/10/2026 | VAT | sprzedaż | Nordis Retail sp. z o.o. | 2026-10-05 | 10 000,00 PLN | `5342650778-20261005-F3887A61DE66-1E` |
| FV 3/10/2026 | ROZ | sprzedaż | ACME Polska sp. z o.o. | 2026-10-12 | 68 880,00 PLN | `5342650778-20261012-32A55F0B257F-45` |
| FV 4/10/2026 | VAT | sprzedaż | Kwadrat Development sp. z o.o. | 2026-10-14 | 14 760,00 PLN | `5342650778-20261014-FB42A5AC4510-90` |
| KOR 1/10/2026 | KOR | sprzedaż | Nordis Retail sp. z o.o. | 2026-10-14 | -2 000,00 PLN | `5342650778-20261014-3E16B3750463-E8` |
| FV 5/10/2026 | VAT | sprzedaż | Helios Software GmbH | 2026-10-16 | 12 500,00 EUR | `5342650778-20261016-BCF879831158-F7` |
| FV 6/10/2026 | VAT | sprzedaż | Agencja Fala sp. z o.o. | 2026-10-19 | 6 150,00 PLN | `5342650778-20261019-B0634B728BCD-87` |
| FV 7/10/2026 | VAT | sprzedaż | ACME Polska sp. z o.o. | 2026-10-21 | 12 300,00 PLN | `5342650778-20261021-1D7921E70E4A-35` |
| FV 8/10/2026 | VAT | sprzedaż | Nordis Retail sp. z o.o. | 2026-10-22 | 7 380,00 PLN | `5342650778-20261022-30E229DAA4F2-95` |
| FV 9/10/2026 | VAT | sprzedaż | Orbita Media sp. z o.o. | 2026-10-26 | 3 690,00 PLN | `5342650778-20261026-BACC87CCF7B6-76` |
| FV 10/10/2026 | VAT | sprzedaż | Nordis Retail sp. z o.o. | 2026-10-31 | 61 500,00 PLN | `5342650778-20261031-9DF88F833A08-29` |
| FV 12/09/2026 | VAT | zakup | Studio Graficzne Kreska sp. z o.o. | 2026-09-25 | 7 380,00 PLN | `8970418075-20260925-2D289E0A071E-A5` |
| 214/09/2026 | VAT | zakup | Drukarnia Format sp. z o.o. | 2026-09-29 | 2 952,00 PLN | `7254098012-20260929-E6F9BD7A2BC6-6F` |
| FV 7/10/2026 | VAT | zakup | Biuro Centrum sp. z o.o. | 2026-10-01 | 9 840,00 PLN | `5265275853-20261001-24101F269640-D0` |
| FV/2026/10/033 | VAT | zakup | Biuro Rachunkowe Bilans sp. z o.o. | 2026-10-05 | 1 845,00 PLN | `5344047247-20261005-5C2356F8CE14-E8` |
| FV 14/2026 | VAT | zakup | Kadr Studio Paweł Wierzbicki | 2026-10-05 | 4 500,00 PLN | `5837377627-20261006-AF50F2319AFC-01` |
| F/40020041/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 464,25 PLN | `5263451926-20261005-6F6E70E45848-68` |
| F/40020215/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 349,41 PLN | `5263451926-20261005-45E1752BBD60-AA` |
| F/40020401/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 413,42 PLN | `5263451926-20261005-B13DFC6D17F2-14` |
| F/40021164/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 404,76 PLN | `5263451926-20261005-792C342B09D1-B5` |
| F/40021770/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 309,91 PLN | `5263451926-20261005-70485A0B91FB-DB` |
| F/40021998/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 390,23 PLN | `5263451926-20261005-D257A5B41974-70` |
| F/40022271/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 392,15 PLN | `5263451926-20261005-B87580461C26-B7` |
| F/40022282/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 369,62 PLN | `5263451926-20261005-2C2BBEC6EA01-C6` |
| F/40022318/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 420,64 PLN | `5263451926-20261005-6D932D567AF4-54` |
| F/40022346/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 152,52 PLN | `5263451926-20261005-2B95899197CE-B0` |
| F/40022509/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 280,78 PLN | `5263451926-20261005-5B6A4ADA4E9C-F8` |
| F/40022585/10/26 | VAT | zakup | Orange Polska S.A. | 2026-10-05 | 432,31 PLN | `5263451926-20261005-0BFB0C8C18A3-46` |
| SP/2026/10/081 | VAT | zakup | Scena Pro sp. z o.o. | 2026-10-05 | 4 920,00 PLN | `9516127975-20261005-CB4C8C08EEC5-62` |
| FV/2026/10/0412 | VAT | zakup | Hotel Wawel Grand sp. z o.o. | 2026-10-10 | 14 078,40 PLN | `6768253927-20261010-035D037CD68A-E7` |
| L/10/2026/0815 | VAT | zakup | Mobilis Leasing sp. z o.o. | 2026-10-12 | 3 505,50 PLN | `5271149722-20261012-371F4A871366-F6` |
| SP/2026/10/087 | VAT | zakup | Scena Pro sp. z o.o. | 2026-10-12 | 30 750,00 PLN | `9516127975-20261012-2AA68213FEA1-23` |
| 2026/10/0193 | VAT | zakup | Smaki Wisły Catering sp. z o.o. | 2026-10-12 | 31 104,00 PLN | `6798361490-20261012-24469C9E459D-8F` |
| BM/1016/2026 | VAT | zakup | Bufet Mazowsze sp. z o.o. | 2026-10-16 | 22 032,00 PLN | `1138672933-20261016-CB6C0DE0B17D-0F` |
| LP/118/2026 | VAT | zakup | Loft Praga sp. z o.o. | 2026-10-16 | 18 450,00 PLN | `5245573056-20261016-4CE9D6D72B6F-6E` |
| FV 15/2026 | VAT | zakup | Kadr Studio Paweł Wierzbicki | 2026-10-19 | 4 500,00 PLN | `5837377627-20261019-8449FC1F191F-84` |
| 2026/10/27 | VAT | zakup | Przestrzeń Odra sp. z o.o. | 2026-10-19 | 6 765,00 PLN | `8989012005-20261019-0E02469CDA8D-1F` |
| SP/2026/10/093 | VAT | zakup | Scena Pro sp. z o.o. | 2026-10-19 | 4 920,00 PLN | `9516127975-20261019-035132680149-DD` |
| HEL/2026/10/0057 | VAT | zakup | Hala Expo Łódź sp. z o.o. | 2026-10-26 | 11 070,00 PLN | `7270138396-20261026-64A40E785BD0-47` |
| SP/2026/10/101 | VAT | zakup | Scena Pro sp. z o.o. | 2026-10-26 | 6 396,00 PLN | `9516127975-20261026-D4AC3D8591B2-11` |

## Wszystkie transakcje zaksięgowane

| entry_reference | Rachunek | Data | Kwota | Kontrahent | Tytuł | Status | Ścieżka | Dokumenty / różnica |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `202610017411739420` | mbank_biezacy | 2026-10-01 | 29 520,00 | ACME Polska sp. z o.o. | Zaliczka 30% - Gala ACME 2026 - umowa 14/2026 | matched | any | FV 1/10/2026 (29 520,00) |
| `202610021670611934` | mbank_biezacy | 2026-10-02 | -586,35 | ANTHROPIC* CLAUDE TEAM | ANTHROPIC* CLAUDE TEAM SAN FRANCISCO US (+2) | matched | queue | EM-02 INV-2610-0437 (586,35); różnica „kurs” 10,05 |
| `202610053264356611` | mbank_biezacy | 2026-10-05 | -400,00 | MAJA LEWANDOWICZ | Nordis Gdansk hostessa 03.10 (+1) | matched | queue | PM R01 (400,00) |
| `202610055288071238` | mbank_biezacy | 2026-10-05 | -400,00 | HANNA DROZDOWSKA | Nordis Gdansk hostessa 03.10 (+1) | matched | queue | PM R02 (400,00) |
| `202610065634798198` | mbank_biezacy | 2026-10-06 | -20 000,00 | ODM Partners sp. z o.o. | Przelew wlasny - zasilenie rachunku Millennium | own_transfer | auto | para `ML2610068440316` |
| `ML2610068440316` | millennium_biezacy | 2026-10-06 | 20 000,00 | ODM Partners sp. z o.o. | Przelew wlasny - zasilenie rachunku Millennium | own_transfer | auto | para `202610065634798198` |
| `202610076358867832` | mbank_biezacy | 2026-10-07 | -5 000,00 | Dwór Kalina Event sp. z o.o. | Zaliczka na poczet faktury - rezerwacja Dwor Kalina, termin 14.11.2026 | no_document (Zaliczki) | queue | - |
| `202610086622571743` | mbank_biezacy | 2026-10-08 | -2 952,00 | Drukarnia Format sp. z o.o. | Faktura 214/09/2026 | matched | auto | 214/09/2026 (2 952,00) |
| `ML2610084922077` | millennium_biezacy | 2026-10-08 | 5 000,00 | Agencja Fala sp. z o.o. | FV 4/09/2026 czesc 1 | matched | queue | FV 4/09/2026 (5 000,00); różnica „niedopłata” -4 225,00 |
| `202610092920619715` | mbank_biezacy | 2026-10-09 | -9 840,00 | Biuro Centrum sp. z o.o. | Czynsz pazdziernik 2026 FV 7/10/2026 | matched | auto | FV 7/10/2026 (9 840,00) |
| `202610097692101583` | mbank_biezacy | 2026-10-09 | -7 240,18 | Anna Mazur | Wynagrodzenie 09/2026 | no_document (Wynagrodzenia) | any | - |
| `202610099337938270` | mbank_biezacy | 2026-10-09 | -6 512,40 | Bartłomiej Sowiński | Wynagrodzenie 09/2026 | no_document (Wynagrodzenia) | any | - |
| `202610093419345457` | mbank_biezacy | 2026-10-09 | -5 980,77 | Karolina Wierzchowska | Wynagrodzenie 09/2026 | no_document (Wynagrodzenia) | any | - |
| `202610095435590927` | mbank_biezacy | 2026-10-09 | -4 105,33 | Tomasz Grabowiecki | Wynagrodzenie 09/2026 | no_document (Wynagrodzenia) | any | - |
| `202610096232801350` | mbank_biezacy | 2026-10-09 | -1 250,00 | Komornik Sądowy przy Sądzie Rejonowym dl | Komornik KM 1187/26 - zajecie wynagrodzenia dluznika Tomasz Grabowiecki za 09/2026 | no_document (Zajęcie komornicze) | queue | - |
| `ML2610095382734` | millennium_biezacy | 2026-10-09 | -7 380,00 | Studio Graficzne Kreska sp. z o.o. | FV 12/09/2026 projekt graficzny Roadshow Nordis | matched | auto | FV 12/09/2026 (7 380,00) |
| `202610120329141687` | mbank_biezacy | 2026-10-12 | -1 845,00 | Biuro Rachunkowe Bilans sp. z o.o. | FV/2026/10/033 uslugi ksiegowe 09/2026 | matched | auto | FV/2026/10/033 (1 845,00) |
| `202610128715345043` | mbank_biezacy | 2026-10-12 | -286,40 | Anna Mazur | Zwrot kosztow - dekoracje kwiatowe Gala ACME | matched | queue | PM R03 (286,40) |
| `202610129033812558` | mbank_biezacy | 2026-10-12 | -520,00 | NATALIA WÓJCICKA | Gala ACME koordynacja hostess 09.10 (+1) | matched | queue | PM R04 (520,00) |
| `202610126851950665` | mbank_biezacy | 2026-10-12 | -450,00 | JULIA SOBIERAJ | Gala ACME hostessa 09.10 (+1) | matched | queue | PM R05 (450,00) |
| `202610124185038206` | mbank_biezacy | 2026-10-12 | -450,00 | OLIWIA KARCZMAREK | Gala ACME hostessa 09.10 (+1) | matched | queue | PM R06 (450,00) |
| `202610121722469427` | mbank_biezacy | 2026-10-12 | -450,00 | ZUZANNA PIETRASIK | Gala ACME hostessa 09.10 (+1) | matched | queue | PM R07 (450,00) |
| `202610136613771164` | mbank_biezacy | 2026-10-13 | 3 075,00 | Orbita Media sp. z o.o. | Zaplata za FV 5/09/2026 | matched | auto | FV 5/09/2026 (3 075,00) |
| `202610131625662595` | mbank_biezacy | 2026-10-13 | -300,00 | DAWID MICHALAK | pomoc przy demontazu (+1) | to_clarify | queue | - |
| `202610143818524718` | mbank_biezacy | 2026-10-14 | 3 075,00 | Orbita Media sp. z o.o. | Zaplata za FV 5/09/2026 | matched | queue | przelew `202610219260141951` (refunded_by); różnica „nadpłata (możliwy duplikat)” 3 075,00 |
| `202610143186203462` | mbank_biezacy | 2026-10-14 | -25 000,00 | Scena Pro sp. z o.o. | /VAT/5750,00/IDC/9516127975/INV/SP/2026/10/087/TXT/technika Gala ACME | matched | auto | SP/2026/10/087 (25 000,00) |
| `202610141002142076` | mbank_vat | 2026-10-14 | -5 750,00 | Scena Pro sp. z o.o. | /VAT/5750,00/IDC/9516127975/INV/SP/2026/10/087/TXT/technika Gala ACME | matched | auto | SP/2026/10/087 (5 750,00) |
| `202610157109429897` | mbank_biezacy | 2026-10-15 | -14 382,47 | Zakład Ubezpieczeń Społecznych | Skladki ZUS za 09/2026 | no_document (ZUS) | any | - |
| `202610166224040945` | mbank_biezacy | 2026-10-16 | -437,12 | GOOGLE *WORKSPACE ODMPAR | GOOGLE *Workspace odmpar G.CO/HELPPAY# IE (+2) | matched | queue | EM-01 5384029117 (437,12); różnica „kurs” 16,25 |
| `202610160785476080` | mbank_biezacy | 2026-10-16 | -1 800,00 | Marek Zawadzki | Rachunek 4/2026 - konferansjerka Gala ACME 09.10 | matched | any | EM-05 4/2026 (1 800,00) |
| `202610191974988946` | mbank_biezacy | 2026-10-19 | -400,00 | AMELIA ZAWILSKA | Nordis Wroclaw hostessa 17.10 (+1) | matched | queue | PM R13 (400,00) |
| `202610197443800288` | mbank_biezacy | 2026-10-19 | -400,00 | LENA KUCZYŃSKA | Nordis Wroclaw hostessa 17.10 (+1) | matched | queue | PM R14 (400,00) |
| `202610199643433857` | mbank_biezacy | 2026-10-19 | -400,00 | WIKTORIA BARTOSZEK | Nordis Wroclaw hostessa 17.10 (+1) | matched | queue | PM R15 (400,00) |
| `202610195324026909` | mbank_biezacy | 2026-10-19 | -900,00 | KINGA RUTKOWSKA | Helios Summit hostessa 15-16.10 (+1) | matched | queue | PM R09 (900,00) |
| `202610191274866862` | mbank_biezacy | 2026-10-19 | -900,00 | PAULA WESOŁOWSKA | Helios Summit hostessa 15-16.10 (+1) | matched | queue | PM R10 (900,00) |
| `202610192377596813` | mbank_biezacy | 2026-10-19 | -900,00 | EWA JABŁONOWSKA | Helios Summit hostessa 15-16.10 (+1) | matched | queue | PM R11 (900,00) |
| `ML2610197665994` | millennium_biezacy | 2026-10-19 | -18 450,00 | Loft Praga sp. z o.o. | LP/118/2026 Helios Partner Summit | matched | auto | LP/118/2026 (18 450,00) |
| `ML2610196097579` | millennium_biezacy | 2026-10-19 | -5,00 | - | Prowizja za przelew Express Elixir | no_document (Prowizje bankowe) | any | - |
| `202610202756598854` | mbank_biezacy | 2026-10-20 | -31 104,00 | Smaki Wisły Catering sp. z o.o. | Faktura 2026/10/0193 catering Gala ACME | matched | auto | 2026/10/0193 (31 104,00) |
| `202610205635891886` | mbank_biezacy | 2026-10-20 | -1 808,00 | Jakub Wrona | Umowa o dzielo 07/2026 - projekt scenografii Helios Summit | matched | queue | PM R12 (1 808,00) |
| `202610208183657650` | mbank_biezacy | 2026-10-20 | -3 214,00 | Urząd Skarbowy - mikrorachunek podatkowy | /TI/N5342650778/OKR/26M09/SFP/PIT4R/TXT/zaliczki PIT 09/2026 | no_document (Podatki (US)) | any | - |
| `202610201635845675` | mbank_biezacy | 2026-10-20 | -6 450,00 | Urząd Skarbowy - mikrorachunek podatkowy | /TI/N5342650778/OKR/26M09/SFP/CIT8/TXT/zaliczka CIT 09/2026 | no_document (Podatki (US)) | any | - |
| `ML2610205610950` | millennium_biezacy | 2026-10-20 | 8 000,00 | Nordis Retail sp. z o.o. | Zaplata FV 2/10/2026 po korekcie | matched | auto | FV 2/10/2026 (10 000,00); KOR 1/10/2026 (-2 000,00) |
| `202610219260141951` | mbank_biezacy | 2026-10-21 | -3 075,00 | Orbita Media sp. z o.o. | Zwrot nadplaty - podwojna wplata z 14.10.2026 za FV 5/09/2026 | matched | queue | przelew `202610143818524718` (refund_of); różnica „zwrot nadpłaty” -3 075,00 |
| `ML2610210025139` | millennium_biezacy | 2026-10-21 | -6 765,00 | Przestrzeń Odra sp. z o.o. | Faktura 2026/10/27 | matched | auto | 2026/10/27 (6 765,00) |
| `202610221759883865` | mbank_biezacy | 2026-10-22 | -281,95 | NOTION LABS INC | NOTION LABS, INC. SAN FRANCISCO US (+2) | matched | queue | EM-03 8F2C41A0-0007 (281,95); różnica „kurs” 6,37 |
| `ML2610224165347` | millennium_biezacy | 2026-10-22 | -14 078,40 | Hotel Wawel Grand sp. z o.o. | FV/2026/10/0412 | matched | auto | FV/2026/10/0412 (14 078,40) |
| `202610234002647079` | mbank_biezacy | 2026-10-23 | 56 000,00 | ACME Polska sp. z o.o. | /VAT/12880,00/IDC/5342650778/INV/FV 3/10/2026/TXT/rozliczenie Gala ACME | matched | auto | FV 3/10/2026 (56 000,00) |
| `202610233423350565` | mbank_biezacy | 2026-10-23 | 14 760,00 | Revolut Bank UAB | KWADRAT DEVELOPMENT SP. Z O.O. - wynajem naglosnienia | matched | queue | FV 4/10/2026 (14 760,00) |
| `202610232074877344` | mbank_biezacy | 2026-10-23 | -464,25 | Orange Polska S.A. | F/40020041/10/26 | matched | auto | F/40020041/10/26 (464,25) |
| `202610238895930439` | mbank_biezacy | 2026-10-23 | -349,41 | Orange Polska S.A. | F/40020215/10/26 | matched | auto | F/40020215/10/26 (349,41) |
| `202610234005550231` | mbank_biezacy | 2026-10-23 | -413,42 | Orange Polska S.A. | F/40020401/10/26 | matched | auto | F/40020401/10/26 (413,42) |
| `202610231207327033` | mbank_biezacy | 2026-10-23 | -404,76 | Orange Polska S.A. | F/40021164/10/26 | matched | auto | F/40021164/10/26 (404,76) |
| `202610234436463076` | mbank_biezacy | 2026-10-23 | -309,91 | Orange Polska S.A. | F/40021770/10/26 | matched | auto | F/40021770/10/26 (309,91) |
| `202610235181437466` | mbank_biezacy | 2026-10-23 | -390,23 | Orange Polska S.A. | F/40021998/10/26 | matched | auto | F/40021998/10/26 (390,23) |
| `202610232954944150` | mbank_biezacy | 2026-10-23 | -392,15 | Orange Polska S.A. | F/40022271/10/26 | matched | auto | F/40022271/10/26 (392,15) |
| `202610232283937056` | mbank_biezacy | 2026-10-23 | -369,62 | Orange Polska S.A. | F/40022282/10/26 | matched | auto | F/40022282/10/26 (369,62) |
| `202610236171165512` | mbank_biezacy | 2026-10-23 | -420,64 | Orange Polska S.A. | F/40022318/10/26 | matched | auto | F/40022318/10/26 (420,64) |
| `202610239443481757` | mbank_biezacy | 2026-10-23 | -152,52 | Orange Polska S.A. | F/40022346/10/26 | matched | auto | F/40022346/10/26 (152,52) |
| `202610236037395553` | mbank_biezacy | 2026-10-23 | -280,78 | Orange Polska S.A. | F/40022509/10/26 | matched | auto | F/40022509/10/26 (280,78) |
| `202610233396451346` | mbank_biezacy | 2026-10-23 | -432,31 | Orange Polska S.A. | F/40022585/10/26 | matched | auto | F/40022585/10/26 (432,31) |
| `202610232774376085` | mbank_vat | 2026-10-23 | 12 880,00 | ACME Polska sp. z o.o. | /VAT/12880,00/IDC/5342650778/INV/FV 3/10/2026/TXT/rozliczenie Gala ACME | matched | auto | FV 3/10/2026 (12 880,00) |
| `202610266785844198` | mbank_biezacy | 2026-10-26 | -3 505,50 | Mobilis Leasing sp. z o.o. | Polecenie zaplaty L/10/2026/0815 rata 10/36 | matched | auto | L/10/2026/0815 (3 505,50) |
| `202610264228948107` | mbank_biezacy | 2026-10-26 | -377,20 | STACJA PETROVA 214 LODZ | STACJA PETROVA 214 LODZ PL (+1) | matched | queue | EM-04 028417 (377,20) |
| `202610266449035591` | mbank_biezacy | 2026-10-26 | -400,00 | ALICJA GRABARCZYK | Nordis Lodz hostessa 24.10 (+1) | matched | queue | PM R16 (400,00) |
| `202610267505245647` | mbank_biezacy | 2026-10-26 | -400,00 | LAURA MIKULSKA | Nordis Lodz hostessa 24.10 (+1) | matched | queue | PM R17 (400,00) |
| `202610268440070535` | mbank_biezacy | 2026-10-26 | -400,00 | NIKOLA SZAFRAŃSKA | Nordis Lodz hostessa 24.10 (+1) | matched | queue | PM R18 (400,00) |
| `202610269739519729` | mbank_biezacy | 2026-10-26 | -400,00 | MARTYNA OLEJNICZAK | Nordis Lodz hostessa 24.10 (+1) | matched | queue | PM R19 (400,00) |
| `202610266071538229` | mbank_vat | 2026-10-26 | -8 877,00 | Urząd Skarbowy - mikrorachunek podatkowy | /TI/N5342650778/OKR/26M09/SFP/VAT7/TXT/VAT za 09/2026 | no_document (Podatki (US)) | any | - |
| `202610275984818978` | mbank_biezacy | 2026-10-27 | 10 000,00 | ACME Polska sp. z o.o. | /VAT/2300,00/IDC/5342650778/INV/FV 7/10/2026/TXT/atrakcje Gala ACME | matched | auto | FV 7/10/2026 (10 000,00) |
| `202610277149922608` | mbank_biezacy | 2026-10-27 | -4 500,00 | Kadr Studio Paweł Wierzbicki | zaplata | to_clarify | queue | remis: FV 14/2026 / FV 15/2026 |
| `202610277768618204` | mbank_vat | 2026-10-27 | 2 300,00 | ACME Polska sp. z o.o. | /VAT/2300,00/IDC/5342650778/INV/FV 7/10/2026/TXT/atrakcje Gala ACME | matched | auto | FV 7/10/2026 (2 300,00) |
| `202610280050568790` | mbank_biezacy | 2026-10-28 | 52 737,50 | Helios Software GmbH | INV FV 5/10/2026 HELIOS PARTNER SUMMIT 2026 | matched | auto | FV 5/10/2026 (52 737,50); różnica „kurs” -347,50 |
| `202610281026795668` | mbank_biezacy | 2026-10-28 | -25,00 | - | Prowizja - przelew zagraniczny przychodzacy SEPA | no_document (Prowizje bankowe) | any | - |
| `202610289276403991` | mbank_biezacy | 2026-10-28 | -16 236,00 | Scena Pro sp. z o.o. | Zaplata za FV SP/2026/10/081, SP/2026/10/093, SP/2026/10/101 | matched | auto | SP/2026/10/081 (4 920,00); SP/2026/10/093 (4 920,00); SP/2026/10/101 (6 396,00) |
| `202610288152441704` | mbank_biezacy | 2026-10-28 | 3 000,00 | ODM Partners sp. z o.o. | Przeksiegowanie z rachunku VAT - postanowienie NUS 1471-SPV.4012.37.2026 z 21.10.2026 | own_transfer | auto | para `202610286656245067` |
| `202610286656245067` | mbank_vat | 2026-10-28 | -3 000,00 | ODM Partners sp. z o.o. | Przeksiegowanie z rachunku VAT - postanowienie NUS 1471-SPV.4012.37.2026 z 21.10.2026 | own_transfer | auto | para `202610288152441704` |
| `ML2610299971452` | millennium_biezacy | 2026-10-29 | -15 000,00 | ODM Partners sp. z o.o. | Przelew wlasny | own_transfer | auto | para `202610309011341667` |
| `202610309011341667` | mbank_biezacy | 2026-10-30 | 15 000,00 | ODM Partners sp. z o.o. | Przelew wlasny | own_transfer | auto | para `ML2610299971452` |
| `202610302699472862` | mbank_biezacy | 2026-10-30 | -89,00 | - | Oplata za prowadzenie rachunku 10/2026 | no_document (Prowizje bankowe) | any | - |
| `ML2610301580065` | millennium_biezacy | 2026-10-30 | 7 380,00 | Nordis Retail sp. z o.o. | FV 8/10/2026 | matched | auto | FV 8/10/2026 (7 380,00) |
| `ML2610305496545` | millennium_biezacy | 2026-10-30 | -39,00 | - | Oplata miesieczna za prowadzenie rachunku | no_document (Prowizje bankowe) | any | - |
| `ML2610309084734` | millennium_biezacy | 2026-10-30 | 3,12 | - | Kapitalizacja odsetek 10/2026 | no_document (Odsetki) | any | - |

Ścieżka: `auto` - automat może zamknąć sam; `queue` - musi trafić do kolejki; `any` - bez wymagań.
