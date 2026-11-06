# Bank w kasie: EnableBanking, wyciągi zapasowe, ekstrakcja, pseudonimizacja, retencja

Kanał bankowy fazy 2. Kod: `kasa/core/providers/bank.py` (dostawcy), `kasa/core/bank/` (parsery, ekstrakcja,
pozyskanie, kursy), `kasa/core/pseudonym.py`, `kasa/core/retention.py`, kontrole w `kasa/core/checks.py`,
CLI w `kasa/cli/bank.py`, schemat w `migrations/0006_phase2_bank.sql`.

```
EnableBanking (JSON) ─┐                       ┌─ l1_bank_account (rachunki, VAT → bieżący)
camt.053 (XML)  ──────┼─► l0_raw ─► BankEntry ─┼─ l1_bank_balance (salda z banku: OPBD/CLBD/ITAV)
MT940 (tekst)   ──────┘  (bajty,   (model      ├─ l1_bank_transaction (szeroki rekord + extracted + pseudonimy)
                          SHA-256)  pośredni)  ├─ l1_counterparty (kontrahent: NIP / IBAN / nazwa)
                                               └─ split payment: mpp_group_id / mpp_role
                                                    │
                       kontrole prawdy 1 (ciągłość sald) i 5 (MPP) → truth_check_result
```

## 1. Jak podłączyć EnableBanking (kroki dla użytkownika)

EnableBanking (EB) to pośrednik PSD2: kasa czyta historię rachunków mBanku i Millennium bez klikania CSV.
Konto EB zakłada **firma** (ODM) - jest właścicielem zgody; kasa tylko czyta.

1. **Konto EB.** Załóż konto na <https://enablebanking.com> (Control Panel). Wystarczy darmowy tryb dla
   własnych rachunków firmy (ryzyko jego wyłączenia przyjęte 2.11 - patrz rozdz. 3).
2. **Aplikacja.** W Control Panel utwórz aplikację (środowisko produkcyjne), podaj adres powrotu
   (redirect URL, np. `https://localhost/eb-callback` - wystarczy, że skopiujesz z niego parametr `code`).
   EB wygeneruje klucz prywatny RSA: zapisz plik `.pem` poza repozytorium (np. `~/.kasa/eb.pem`,
   uprawnienia `600`). Identyfikator aplikacji (application id) to `kid` w podpisie JWT.
3. **Rachunki i zgoda.** Rozpocznij autoryzację (`POST /auth`: bank `mBank` / `Bank Millennium`, kraj `PL`,
   `psu_type=business`, `access.valid_until` = dziś + 180 dni), zaloguj się w banku i **zaznacz wszystkie
   rachunki, także rachunek VAT** (bez niego nie przejdzie kontrola MPP). Kod z adresu powrotu zamień na
   sesję (`POST /sessions` z `code`); odpowiedź zawiera `session_id` i listę rachunków. Dla każdego banku
   to osobna sesja - kasa obsługuje dziś jedną sesję na proces (`KASA_EB_SESSION_ID`); przy dwóch bankach
   uruchamiaj ingest osobno dla każdej sesji albo zgłoś potrzebę listy sesji.
4. **Zmienne środowiskowe** (np. w `.env` serwera, nigdy w repo):

   | Zmienna | Co to jest |
   | --- | --- |
   | `KASA_BANK_PROVIDER=enablebanking` | wybór prawdziwego dostawcy (domyślnie `mock`) |
   | `KASA_EB_APP_ID` | identyfikator aplikacji EB (`kid` JWT) |
   | `KASA_EB_PRIVATE_KEY_PATH` | ścieżka do klucza prywatnego `.pem` aplikacji |
   | `KASA_EB_SESSION_ID` | sesja po autoryzacji rachunków w banku |
   | `KASA_EB_API_URL` | opcjonalnie inny adres API (domyślnie `https://api.enablebanking.com`) |

   Brak którejkolwiek zmiennej = kasa loguje ostrzeżenie i pracuje na danych testowych (`mock`); nigdy
   nie przerywa startu. Podpis JWT (RS256) wymaga pakietu `cryptography`.
5. **Sprawdzenie:** `python -m kasa bank accounts --company odm --provider enablebanking` (lista rachunków u
   dostawcy i w bazie), potem pierwsze pobranie historii, np.
   `python -m kasa bank ingest --company odm --from 2026-01-01 --to 2026-10-31`.
6. **Harmonogram:** przyrost 3× dziennie, np. `bank ingest --from <dziś-5 dni> --to <dziś>` (zakres może
   się nakładać - ponowne pobranie nie tworzy duplikatów).
7. **Zgoda 180 dni.** `python -m kasa bank consent` pokazuje datę końca zgody i liczbę dni; na 14 dni przed
   końcem wypisuje alarm i kończy się kodem 1 (do podpięcia w cron/monitoringu). Odnowienie = ponowna
   autoryzacja w banku (krok 3) i nowy `KASA_EB_SESSION_ID`.

Używane endpointy: `GET /sessions/{id}` (rachunki, ważność zgody), `GET /accounts/{uid}/details`,
`GET /accounts/{uid}/balances`, `GET /accounts/{uid}/transactions?date_from&date_to&transaction_status=BOOK|PDNG`
(stronicowanie `continuation_key`). Testy nie łączą się z siecią - prawdziwy dostawca jest sprawdzany na
podstawionym transporcie.

## 2. Co kasa zapisuje

Każda odpowiedź banku trafia najpierw do `l0_raw` (kanał `bank`, `meta` = endpoint, rachunek, zakres dat,
status), dokładnie bajt w bajt. Potem parser tworzy model pośredni `BankEntry` (pydantic) i z niego wiersze L1.

**`l1_bank_account`** - upsert po IBAN + walucie; rodzaj `current`/`vat`/`savings` (EB: z opisu produktu,
mock: z `accounts.json`), `parent_account_id` rachunku VAT = jedyny bieżący PLN tego samego banku (BIC),
`provider`, `external_id` (uid w EB), `bic`, `owner_name`.

**`l1_bank_balance`** - salda zgłoszone przez bank: `OPBD` (na początek dnia), `CLBD` i `PRCD` (na koniec
dnia), `ITAV`/`CLAV` (dostępne, po blokadach). Nowa wartość tego samego salda zastępuje starą (`superseded_by`).

**`l1_bank_transaction`** - szeroki rekord z Kroku 2 briefingu:

| Grupa | Kolumny |
| --- | --- |
| Tożsamość | `entry_reference` (NtryRef/EB), `fingerprint` = SHA-256(IBAN + status + data + kwota + waluta + tytuł + numer kolejny identycznych operacji tego dnia), `raw_id` + `raw_locator` |
| Daty i kwoty | `booking_date`, `value_date`, `transaction_date`, `amount_minor` ze znakiem (wpływ > 0), `currency`, `original_amount_minor`/`original_currency`/`fx_rate` (kurs banku przy walutowych), `balance_after_minor` + `balance_after_computed` |
| Nadawca | `counterparty_name_raw`, `counterparty_iban_raw`, `counterparty_bic` tak, jak podał bank; `counterparty_id` - hipoteza kodu (rozdz. 5) |
| Tytuł | `title_raw` (linie złączone ` \| `), `extracted` (rozdz. 4), `title_pseudonymized`, `counterparty_pseudonym`, `counterparty_iban_pseudonym`, `sensitive` |
| Typ | `op_type`, `bank_tx_code` (ISO 20022 rodzina/podrodzina, np. `ICDT/DMCT`; MT940: `SWIFT/NTRF`), `bank_tx_description`, `merchant_category_code`, `reference_number` |
| Status | `booked` / `pending` (blokady kartowe: ta sama tabela, poza bilansem; zaksięgowanie wskazuje blokadę przez `superseded_by`) |
| Split payment | `mpp_group_id`, `mpp_role` (`main` / `vat_leg` / `vat_rebooking`) |

**Saldo po operacji.** Gdy bank je podaje (mBank), zapisujemy jego wartość i sprawdzamy ciąg (różnica =
ostrzeżenie w wyniku pobrania). Gdy nie (Millennium, camt.053, MT940), liczymy je od salda otwarcia w
kolejności księgowania, a bez salda otwarcia - wstecz od salda zamknięcia, i oznaczamy
`balance_after_computed = true`.

**Co pokazało pierwsze prawdziwe pobranie (6.11.2026, wersja parsera 4):**

* **mBank podaje saldo po operacji bez znaku.** Rachunek poniżej zera ma w EB `47371.45` zamiast `-47371.45`.
  Kasa przywraca znak z ciągłości sald: z dwóch znaków wybiera ten, przy którym saldo przed operacją
  (po - kwota) jest saldem po innej operacji; gdy pasują oba (albo żaden), ten, który ciągnie łańcuch wstecz
  od salda bieżącego (bez niego - w przód od pierwszego salda). Przywrócony znak: `evidence.extra.balance_after_sign
  = "restored"`; wynik pobrania mówi, w ilu operacjach.
* **Banki dają tylko saldo bieżące bez daty** (`ITBD` zaksięgowane, `ITAV` dostępne, `reference_date = null`).
  Kasa zapisuje je z datą dnia pobrania (to saldo po wszystkich operacjach zaksięgowanych do chwili zapytania).
  Gdy bank nie daje żadnego salda z datą (OPBD/CLBD/PRCD), operacje są pobierane aż do dnia pobrania, a salda
  po operacjach (Millennium) liczone wstecz od `ITBD`.
* **Banki nie trzymają się zakresu dat** (mBank oddał 12 miesięcy przy prośbie o październik, Millennium -
  3 miesiące). Kasa zapisuje wszystko, co przyszło: fakty są idempotentne, a kontrole liczą się per okres.
* **Rachunków VAT nie ma w zgodzie PSD2** (ani mBank, ani Millennium nie dają ich wybrać). Ruchy na rachunkach
  VAT trzeba wczytywać z wyciągów: `bank import-mt940 <plik> --company odm --kind vat` (albo `import-camt`);
  bez nich kontrola MPP jest czerwona.

**Idempotencja i wersje.** Ten sam zakres pobrany ponownie: te same bajty → ten sam `l0_raw`; ta sama
operacja (po `entry_reference`, a bez niego po `fingerprint`) → bez zmian, chyba że bank zmienił jej treść
(status, daty, kwota, tytuł, nadawca, saldo po operacji z banku - porównanie tylko w obrębie tego samego źródła):
wtedy nowa wersja faktu i ostrzeżenie z listą zmienionych pól. Podbicie
`kasa.core.bank.ingest.PARSER_SCHEMA_VERSION` (zmiana parsera lub ekstrakcji; 2 - telefony w
`extracted` tylko jako pseudonimy; 3 - `payer_hint` i `sensitive` z tytułu oraz nazwy kontrahenta listą
bramki semantyki; obecnie 4 - znak salda po operacji i `ITBD` z dnia pobrania) → przy następnym pobraniu każda
operacja dostaje nowy wiersz z nowym `schema_version`, a stary wskazuje go przez `superseded_by`. Te same
operacje z EB i z camt.053 (ten sam `NtryRef`) to jeden fakt.

**Kursy.** `l1_fx_rate` (dane wspólne, `company_id = NULL`, źródło `nbp_a`) zasila `kasa/core/bank/fx.py`:
`FixtureNbpSource` (`fixtures/odm/reference/nbp_rates.json`, fikcyjne kursy) albo `NbpApiSource`
(<https://api.nbp.pl/api/exchangerates/rates/a/{kod}/{data}/>). `rate_before(conn, waluta, dzień)` daje kurs
z ostatniego dnia roboczego **przed** dniem (zasada briefingu). Operacja walutowa ma w `extracted.fx` kurs
banku, kurs NBP z dnia poprzedniego i równowartość PLN po NBP. `bank ingest` dociąga kursy sam
(`--fx fixture|nbp|none`).

## 3. Formaty zapasowe (decyzja D4)

Gdyby EB wyłączyło darmowy tryb, wyciąg pobrany z bankowości internetowej wczytuje się tym samym torem:

* `python -m kasa bank import-camt <plik.xml> --company odm` - ISO 20022 **camt.053.001.02** (także
  nowsze .04/.08): `Stmt/Acct`, `Bal` (OPBD/CLBD/PRCD), `Ntry` z `NtryRef`, `BkTxCd`, `RltdPties`,
  `RmtInf/Ustrd`, `AmtDtls/InstdAmt/CcyXchg`.
* `python -m kasa bank import-mt940 <plik> --company odm` - **MT940**: `:20:`, `:25:`, `:28C:`, `:60F:`,
  `:61:` (referencja banku po `//` = `entry_reference`), `:86:` (strukturalne `NNN~00…~20…~38…` z polskich
  banków: kawałki tytułu ~20-~26/~60-~63 są sklejane, nadawca ~27-~29/~32-~33, IBAN ~38), `:62F:`, `:64:`;
  kodowanie UTF-8 albo Windows-1250. `:60F:` z datą sprzed pierwszej operacji = saldo zamknięcia
  poprzedniego wyciągu (`PRCD`).

Plik trafia do `l0_raw` (kanał `file`), operacje do tych samych tabel co z EB; potem kontrole prawdy za
okres wyciągu. Rachunek nieznany bazie dostaje rodzaj `current` (albo `--kind vat`).

## 4. Co kod wyłuskuje z tytułu (`extracted`, bez modelu)

| Klucz | Przykład z danych testowych |
| --- | --- |
| `invoice_numbers` | `FV 7/10/2026`, `FV/2026/10/0412`, `214/09/2026`, `F/40020041/10/26`, `LP/118/2026`, `L/10/2026/0815`, `FS/2026/10/0007`, `F-2026-10-7`, lista `FV SP/2026/10/081, SP/2026/10/093, …`; każdy z `raw`, `normalized` (`kasa.core.normalize`) i `alt` (z prefiksem `FV` i bez - tytuł nie mówi, czy „FV” jest częścią numeru) |
| `ksef_numbers` | numer KSeF z flagą zgodności sumy CRC-8 |
| `mpp` | `/VAT/12880,00/IDC/5342650778/INV/FV 3/10/2026/TXT/…` → `vat_minor`, `idc_nip`, `inv`, `inv_normalized`, `txt` |
| `tax` | `/TI/N5342650778/OKR/26M09/SFP/PIT4R/TXT/…` → typ i numer identyfikatora, okres, formularz |
| `periods` | `czynsz listopad` (rok z daty księgowania), `pazdziernik 2026`, `za 09/2026`, `26M09` |
| `dates` | `03.10`, `15-16.10`, `14.10.2026` (dni pracy / wpłaty z tytułu) |
| `contract_refs`, `case_refs`, `other_refs` | `umowa 14/2026`, `Umowa o dzielo 07/2026`; `KM 1187/26` (komornik); `postanowienie NUS 1471-SPV.4012.37.2026` - nie są brane za faktury |
| `installment` | `rata 10/36` |
| `card`, `original_amount` | `5375********4417`; `Kwota oryg. 99,00 EUR, kurs 4,4154` |
| `phones` | `+48 500 085 853` → w faktach tylko pseudonim `T-…` (od wersji parsera 2; jawny numer zostaje wyłącznie w `title_raw`) |
| `operator` | Revolut, PayU, Stripe, PayPal, Przelewy24, Tpay, Wise, Adyen (z nadawcy albo tytułu) z referencją, gdy jest |
| `payer_hint` | przy operatorze Revolut / PayU / Stripe: firma z formą prawną z tytułu (`{"name": "KWADRAT DEVELOPMENT SP. Z O.O.", "legal_form": "SP. Z O.O."}`) - płatnik za operatorem; osoba (bez formy prawnej) nigdy tu nie trafia (od wersji parsera 3, `EXTRACTOR_VERSION` 2) |
| `persons`, `sensitive_words`, `fx`, `op_type_reason` | pseudonimy osób z tytułu, znalezione słowa wrażliwe, kursy, uzasadnienie typu |

Okresy („09/2026”), umowy, sygnatury i daty nie trafiają do numerów faktur.

**Typ operacji (`op_type`)** - mechaniczny, z kodu banku, opisu, tytułu i nadawcy, w tej kolejności:
`own` (IBAN nadawcy to nasz rachunek, „przelew własny”, „przeksięgowanie z rachunku VAT”) → `tax` (TAXS,
blok `/TI/`, mikrorachunek podatkowy 1010 0071 222…, urząd skarbowy) → `zus` (rachunek 6000 0002…, nazwa) →
`vat` (noga MPP na rachunku VAT) → `card` (CCRD) → `blik` → `interest` (INTR) → `fee` (MDOP/CHRG) →
`direct_debit` (IDDT) → `instant` (Express Elixir, SDVA) → `foreign_transfer` (XBCT, SEPA) → `transfer`
(RCDT/ICDT) → `other`. Wartości `direct_debit`, `foreign_transfer`, `interest` doszły w 0006, bo tak opisuje
je prawda referencyjna danych testowych.

## 5. Kontrahenci i split payment

**`l1_counterparty`** (fakty, decyzje o kontrahentach to faza 4): `nip` (unikalny w firmie wśród bieżących),
`foreign_tax_id`, `name`, `ibans[]`, `kind` (`company`/`person`/`institution`/`bank`/`own`), pochodzenie.
Jedyną drogą zapisu jest funkcja SQL wspólna dla banku i dokumentów:

```sql
SELECT kasa_upsert_counterparty(company_id, nip, name, iban
       [, p_kind => 'person', p_foreign_tax_id => 'DE…', p_source => 'ksef', p_method => 'code',
          p_confidence => 0.95, p_evidence => '{}'::jsonb, p_created_by => 'kasa.documents']);
```

Kolejność szukania: NIP → zagraniczny identyfikator → IBAN (nasz własny rachunek = kontrahent `own`) → nazwa
(tylko dla stron znanych z samej nazwy). Istniejący wiersz jest tylko uzupełniany (brakujący NIP/nazwa,
nowy IBAN), nigdy nadpisywany. Niepoprawna suma kontrolna NIP = błąd `22023`. `kasa_own_counterparty(company_id,
name, nip)` tworzy/uzupełnia kontrahenta „własnego” (IBAN-y wszystkich rachunków firmy; NIP firmy, gdy znany:
z `company.nip`, z tytułów naszych przelewów podatkowych `/TI/N…` albo z `/IDC/` wpływów MPP). Bank: po IBAN,
po NIP z bloku MPP (tylko w naszych zapłatach - we wpływie `/IDC/` to nasz NIP), a nowy kontrahent z nazwy banku
dostaje `method = 'code'` i niską pewność (0,4 z IBAN-em, 0,3 z samej nazwy). Prowizje i odsetki: kontrahentem jest bank rachunku.

**Split payment jako jedno zdarzenie** (`glue_mpp`): `main` = przelew z blokiem `/VAT/` na rachunku bieżącym,
`vat_leg` = operacja na rachunku VAT tego bieżącego, w tym samym kierunku, na kwotę VAT z bloku, data ±1 dzień
(ten sam blok, inaczej ten sam kontrahent), `vat_rebooking` = przelew własny VAT ↔ bieżący na tę kwotę w tym
samym oknie (obie nogi). `mpp_group_id` jest deterministyczny (UUIDv5 z odcisku głównej nogi), więc nowa
wersja parsera dostaje te same grupy. Przeksięgowanie z postanowienia urzędu (3 000,00 w danych testowych)
nie należy do żadnej grupy - to transfer własny.

## 6. Pseudonimizacja (`kasa/core/pseudonym.py`)

Zanim jakikolwiek tekst trafi do modelu, przechodzi przez bramkę: `pseudonymize_text(conn, company_id, tekst)`
zwraca tekst z kodami i listę trafień (`PseudonymHit`: rodzaj, pozycja, wartość, pseudonim, metoda).

* **Co wykrywa kod:** IBAN-y (także z odstępami i gołe 26 cyfr NRB, suma mod-97), telefony PL (`500 085 853`,
  `500-085-853`, `+48500085853`, stacjonarne), e-maile, PESEL (suma kontrolna i data), adresy (`ul. Długa
  12/4, 00-238 Warszawa`) oraz osoby: (1) osoby znane - każdy wiersz `person` w `pseudonym_key` (odbiorcy
  BLIK i wynagrodzeń z banku, osoby z arkuszy PM) w dowolnej kolejności słów, także samo nazwisko w odmianie
  („Kuczyńskiej”); (2) polskie imię (ok. 330 imion z dopełniaczem: „Tomasza”, „Anny”, „Pawła”) obok słowa z
  wielkiej litery, które nie jest słowem bezpiecznym ani słowem nazwy firmy z rejestru kontrahentów;
  (3) kontekst: „Pan/Pani/P.” przed nazwą, „BLIK” przed nią albo „tel.”/„PESEL” zaraz po niej. Od fazy 3
  dwa słowa z wielkiej litery **bez takiego dowodu nie są osobą** („Google Workspace”, „Rogerson's Quay”,
  „Scena Pro” zostają jawne). Osoba znana = osoba zarejestrowana (arkusz, kontrahent `person`) albo klucz z
  dowodem (`pseudonym_key.basis`); stare klucze bez dowodu zostają w tabeli (nic nie kasujemy), ale nie są
  używane. Słownik bezpieczny: miesiące, dni, miasta (z odmianą), słownictwo tytułów i nazw firm. Nazwa spółki kapitałowej
  (sp. z o.o., S.A., GmbH…) jest brana w całości; w nazwie jednoosobowej działalności („Kadr Studio Paweł
  Wierzbicki”) osoba jest wycinana.
* **Stabilność:** ta sama wartość → ten sam kod (`P-7f3a` osoba, `I-…` IBAN, `T-…` telefon, `E-…` e-mail,
  `N-…` PESEL, `A-…` adres). Klucz `pseudonym_key.value_hash` = HMAC-SHA256 wartości znormalizowanej
  (telefon do 9 cyfr, osoba = posortowane słowa bez ogonków, więc „Lewandowicz Maja” = „MAJA LEWANDOWICZ”).
* **Klucz zostaje u nas:** wartość jest tylko zaszyfrowana (`pgp_sym_encrypt`, pgcrypto) kluczem
  wyprowadzonym z `KASA_PSEUDONYM_SECRET`; sekret nie trafia do bazy. Bez zmiennej działa klucz deweloperski
  z ostrzeżeniem w logu - **na produkcji ustaw własny sekret i nie zmieniaj go** (inny sekret = inne kody i
  brak odczytu starych wartości; bramka wtedy zgłasza `PseudonymizationError` - wywołania modelu nie ma).
* **Odwracanie:** `depseudonymize(conn, company_id, tekst, role=…)` tylko dla ról `owner`, `operator`,
  `accountant`; agent i model dostają `PermissionError`.
* **Dla dokumentów i arkuszy:** `register_person(conn, company_id, "Nazwisko Imię")` zwraca pseudonim do
  `l1_internal_record.person_pseudonym` i od razu czyni osobę „znaną” dla tytułów przelewów.
* **Słowa wrażliwe:** lista rdzenia (`zdrowie`, `związki`, `komornik`, `alimenty`) + lista rozszerzenia firmy
  + uzupełnienie rdzenia (`zajęcie wynagrodzenia`, `egzekucja`, `dłużnik`, `choroba`, `leczenie`…), od fazy 3
  w tabeli `sensitive_term` i w `kasa.core.sensitive` (stemmer z odmianą polską, `docs/SEMANTYKA.md`),
  dopasowanie po rdzeniu słowa, nie po podciągu („komornicze”, „alimentów”, „związkowej”). Trafienie w tytule
  **albo w nazwie kontrahenta** (ta sama lista co bramka semantyki: rdzeń + rozszerzenie + słowa z bazy, także
  dodane przez człowieka; od wersji parsera 3) ustawia `sensitive = true` i zapisuje słowa w
  `extracted.sensitive_words`: taki przelew omija model (semantyka nie czyta nawet jego tytułu) i idzie do
  kolejki. Blokada bramki (słowo dodane później) jest trwałą flagą: karta „kategorię nadaje człowiek” nie jest
  ponawiana przy kolejnych przebiegach, a dopasowanie traktuje taki przelew jak wrażliwy (bez twardego dowodu),
  dopóki człowiek nie powie „to nie jest wrażliwe”.

## 7. Retencja (`kasa/core/retention.py`, Krok 9, D6)

* `l0_raw` nadal przyjmuje tylko INSERT (KS001). Jedyny wyjątek: `SELECT kasa_purge_raw(raw_id, 'powód'
  [, 'kto'])` (SECURITY DEFINER) - czyści `payload` do pustego, zostawia wiersz, `sha256` i `meta`, ustawia
  `purged_at` i dopisuje wpis do `l0_purge_log` (kto, kiedy, ile bajtów, powód). Strażnik przepuszcza
  zmianę tylko wtedy, gdy flaga transakcji `kasa.purge` jest włączona przez funkcję **i** w tej samej
  transakcji jest wpis w dzienniku **i** zmiana dotyczy wyłącznie treści. Ponowne pobranie tych samych bajtów
  nie zapisze ich drugi raz (skrót zostaje).
* `purge_company_raw` - pliki źródłowe po końcu umowy; `shred_pseudonyms` - anonimizacja osób po umowie + 1 rok:
  zaszyfrowana wartość → `NULL`, pseudonim zostaje (agregaty i fakty bez osób nadal się liczą). Klucza nie da
  się usunąć ani zmienić inaczej (KS002/KS008).
* Pozostałe terminy (prompty i odpowiedzi modelu 90 dni, log dostępu 3 lata, zgoda bankowa 180 dni, alert 14 dni)
  są w `retention.POLICY`.

## 8. Kontrole prawdy 1 i 5

* **1. Ciągłość sald** (`check_balance_continuity`) - per rachunek i dzień, co do grosza: saldo otwarcia
  (OPBD dnia, CLBD/PRCD dnia poprzedniego, ostatnie saldo zamknięcia przed okresem + operacje po nim, albo
  wyprowadzone z salda po operacji z banku) + operacje zaksięgowane = saldo zamknięcia. Każdy dzień jest
  sprawdzany wobec sald po operacji (kolejność w dniu odtwarzana z samych sald) i wobec każdego zgłoszonego
  OPBD/CLBD; bez żadnego niezależnego salda z banku kontrola jest czerwona. W szczegółach także saldo
  dostępne (zamknięcie + blokady).
* **5. MPP** (`check_mpp`) - suma bloków `/VAT/` we wpływach na rachunki bieżące = wpływy na rachunki VAT
  (bez przelewów własnych); w szczegółach nogi bez grupy i strona wychodząca.

Wynik każdego przebiegu trafia do `truth_check_result` (tylko dopisywanie). Uruchomienie:
`python -m kasa bank balance-check --company odm --month 2026-10`, automatycznie po `bank ingest` / imporcie
wyciągu oraz w `python -m kasa check` (wszystkie firmy i miesiące z operacjami).

## 9. Polecenia

| Polecenie | Co robi |
| --- | --- |
| `bank accounts [--company odm] [--provider mock\|enablebanking]` | rachunki u dostawcy i w bazie |
| `bank ingest --company odm --from 2026-10-01 --to 2026-10-31 [--provider …] [--account IBAN] [--fx …]` | kursy NBP, pobranie (L0 + L1), sklejenie MPP, kontrole prawdy; kod 1, gdy kontrola czerwona |
| `bank import-camt <plik>` / `bank import-mt940 <plik>` `[--company odm] [--kind vat]` | wyciąg z pliku |
| `bank consent [--provider …]` | ważność zgody; kod 1 na 14 dni przed końcem |
| `bank balance-check --company odm --month 2026-10` | kontrole 1 i 5 za miesiąc |

## 10. Ograniczenia (świadome)

* Jedna sesja EB na proces (`KASA_EB_SESSION_ID`); dwa banki = dwie sesje → dwa uruchomienia.
* Kontrola ciągłości odtwarza kolejność operacji w dniu z samych sald (z nawrotami - para przelew i zwrot +X/-X
  wraca do tego samego salda), ale nie przenosi operacji między dniami. W danych mBanku za 11.2025-11.2026 są
  dwa takie miejsca po stronie banku: operacja z 5.01.2026 wliczona w saldo przed operacją z 3.01.2026 oraz
  saldo 0,00 zamiast -193,95 przy przejściu pod zero 27.07.2026 - kontrola pokazuje je jako przerwy w łańcuchu,
  salda miesięcy się zgadzają.
* Kontrola dla banku bez sald po operacjach i bez sald z datą (Millennium) opiera się na jednym saldzie bieżącym
  (`ITBD`): potwierdza rachunek, nie niezależnie każdy dzień. Mocniejsza byłaby z wyciągiem MT940 za miesiąc.
* Zaksięgowanie zastępuje blokadę tylko przy tej samej kwocie (± 3 dni, ta sama nazwa); blokada zwolniona bez
  zaksięgowania zostaje `pending`.
* Heurystyka nazwisk woli nadmiar niż brak: rzadkie nazwy własne z wielkiej litery mogą dostać kod osoby.
