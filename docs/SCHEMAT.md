# Schemat bazy (faza 1)

Migracje: `migrations/0001_core.sql` … `0005_l3_decisions.sql`, stosowane przez `python -m kasa migrate`
(każdy plik w osobnej transakcji, zapis w `schema_migrations` z sumą SHA-256; zmiana zastosowanego pliku = błąd).
Rozszerzenia PostgreSQL: żadne (`gen_random_uuid()` jest wbudowane od wersji 13).

## Wspólne

| Obiekt | Co to jest |
| --- | --- |
| `company` | Firma (najemca systemu): `slug`, nazwa i `extension_slug` - rozszerzenie, które mówi, czym jest jej obiekt kosztowy. |
| `provenance_method` | Metoda zapisu: `code` (parser), `rule` (automat z reguły), `model` (AI), `human` (człowiek). |
| `subject_type` | Rodzaj podmiotu w odwołaniach polimorficznych: `transaction`, `document`, `line`, `internal_record`, `cost_object`, `rule`. |

**Kolumny pochodzenia** - każdy wiersz L1-L3 ma: `company_id`, `source` (skąd: `ksef`, `enablebanking`, `queue`…),
`method`, `confidence` (0-1, skalibrowana pewność, może być NULL), `evidence` (jsonb z dowodem), `schema_version`,
`prompt_version` (obowiązkowe, gdy `method = 'model'`), `created_at`, `created_by`. Tabele L3 mają dodatkowo
`updated_at` (ustawia trigger) i `updated_by`. `company_id` jest też w tabelach podrzędnych - złożone klucze obce
`(id, company_id)` pilnują, że wiersz nigdy nie wskazuje na dane innej firmy.

## L0 - surowe

| Tabela | Opis |
| --- | --- |
| `l0_raw` | Plik dokładnie taki, jak przyszedł (XML KSeF, JSON z banku, PDF, arkusz), z kanałem (`bank`/`ksef`/`file`/`pdf`/`email`), `external_ref` i `sha256`, które baza sama przelicza z `payload`; `UNIQUE (company_id, sha256)` sprawia, że ponowne pobranie nie tworzy duplikatu. |

## L1 - fakty

| Tabela | Opis |
| --- | --- |
| `l1_document` | Faktura lub inny dokument (wersja rekordu): tożsamość (numer KSeF, numer surowy i znormalizowany, rodzaj, skrót pliku, program wystawcy), daty, strony, kwoty w groszach waluty dokumentu z kursem i PLN jako pochodną, powiązania (korekta, zaliczka, zamówienie, umowa), flagi MPP / metoda kasowa / odwrotne obciążenie oraz `superseded_by`. |
| `l1_document_line` | Pozycja dokumentu (nazwa, ilość, jednostka, cena, netto, stawka, VAT, brutto; dla korekty stan przed/po w `before_json`/`after_json`). |
| `l1_document_observation` | Jedna obserwacja dokumentu w jednym surowym pliku (`raw_id` + `locator` w środku pliku) z informacją, po czym ją dopięto (`linked_by`: utworzyła dokument, numer KSeF, skrót pliku, NIP+rodzaj+numer, człowiek) i polami tak, jak je widział ten kanał - to jest deduplikacja: trzy obserwacje, jeden dokument. |
| `l1_bank_account` | Rachunek firmy (IBAN, waluta, bank, rodzaj `current`/`vat`/`savings`, rachunek VAT wskazuje swój bieżący przez `parent_account_id`). |
| `l1_bank_transaction` | Operacja bankowa: `entry_reference` banku i własny `fingerprint`, daty, kwota ze znakiem, waluta i kurs, saldo po, nadawca i tytuł w wersji surowej, `extracted` (to, co kod wyłuskał z tytułu), typ operacji i status `booked`/`pending`; zaksięgowana operacja zastępuje oczekującą przez `superseded_by`. |
| `l1_fx_rate` | Kurs waluty z dnia (PLN za 1 jednostkę); `company_id = NULL` oznacza dane wspólne, np. tabelę A NBP. |
| `l1_title_semantics` | Odczyt tytułu przelewu przez model (intencja, encje), jeden wiersz na transakcję i wersję promptu; tylko `method = 'model'`. |
| `l1_internal_record` | Wiersz pliku wewnętrznego (arkusz PM, lista czynszów) odczytany mapowaniem z rozszerzenia: wszystkie pola w `fields`, a w kolumnach rdzenia osoba (pseudonim), data, kwota, waluta, odwołanie do obiektu kosztowego tak, jak je zapisano, i typ rozliczenia. |

Wersjonowanie: nowa wersja parsera lub promptu dopisuje wiersz i ustawia `superseded_by` w starym. Ograniczenia
wykluczające (`EXCLUDE … DEFERRABLE INITIALLY DEFERRED`, sprawdzane przy commit) pilnują, że wśród wierszy bieżących
(`superseded_by IS NULL`) jest najwyżej jeden dokument o danym numerze KSeF w firmie, jedna operacja o danym
`entry_reference` i jedna o danym `fingerprint` na rachunku, i jeden wiersz pliku wewnętrznego na (plik, arkusz, wiersz).
`fingerprint` musi więc zawierać numer kolejny identycznych operacji z tego samego dnia. Reguły nie piszą faktów
(`method <> 'rule'`).

## L2 - sugestie

| Tabela | Opis |
| --- | --- |
| `l2_suggestion` | Propozycja kodu, reguły lub modelu (`method <> 'human'`): rodzaj (`match`, `allocation`, `category`, `cost_object_candidate`, `rule_candidate`, `counterparty`), podmiot, `proposal`, surowy `score`, `rank` i status `open`/`accepted`/`rejected`/`expired` z decyzją, która ją rozstrzygnęła. |

## L3 - decyzje

| Tabela | Opis |
| --- | --- |
| `l3_rule` | Reguła nauczona z decyzji, wersjonowana (`UNIQUE (company_id, rule_key, version)`), z etapem `candidate`/`hint`/`shadow`/`auto`/`suspended`/`dormant`, wzorcem, akcją, liczbą dowodów i wyjątkami; kod może zaproponować tylko kandydata, każdy dalszy etap ustawia człowiek. |
| `l3_decision_log` | Dziennik wszystkich decyzji (człowiek albo reguła `auto`): rodzaj, podmiot, `payload`, reguła z wersją, kto i kiedy, cofana decyzja (`kind = 'revoke'`), odpowiadana sugestia i opcjonalny powód; tylko dopisywanie. |
| `l3_cost_object` | Obiekt kosztowy firmy; `kind` i schemat `attrs` nadaje rozszerzenie (ODM `event`, Rentease `room_month`), status `candidate`/`approved`/`rejected`/`merged`; każda firma ma systemowy wiersz `UNASSIGNED` (rodzaj `unassigned`), tworzony triggerem przy wstawieniu firmy. |
| `l3_match` | Dopasowanie przelewu do dokumentu albo wiersza pliku wewnętrznego, z kwotą po stronie przelewu i dokumentu, nazwaną różnicą (`fee`/`fx`/`underpayment`/`overpayment`/`tolerance`) i `group_id` spinającym jedno rozliczenie (trzy faktury w jednym przelewie, dwie nogi MPP). |
| `l3_allocation` | Przypisanie przelewu, dokumentu, pozycji albo wiersza wewnętrznego do zatwierdzonego obiektu kosztowego w udziale `share` (0-1]. |
| `l3_category_decision` | Kategoria podmiotu (nazwy kategorii z rozszerzenia); najwyżej jedna aktywna na podmiot. |
| `l3_transaction_status` | Status końcowy przelewu: `matched` / `no_document` (z podtypem) / `own_transfer` / `to_clarify`; najwyżej jeden aktywny na przelew. |

Decyzje `l3_match`, `l3_allocation`, `l3_category_decision`, `l3_transaction_status` mają `decision_id` (wpis w
dzienniku), `status` `active`/`revoked` i `revoked_by_decision`. Cofnięcie = nowy wpis `revoke` w dzienniku plus
`UPDATE … SET status = 'revoked', revoked_by_decision = …, method = 'human'`.

## Zasady triggerów (pilnuje baza, nie aplikacja)

| Kod | Zasada | Gdzie |
| --- | --- | --- |
| `KS001` | Tylko INSERT: UPDATE i DELETE zgłaszają wyjątek. | `l0_raw`, `l3_decision_log` |
| `KS002` | Nic nie kasujemy: DELETE i TRUNCATE zgłaszają wyjątek. | wszystkie tabele L1, L2, L3 (TRUNCATE także L0) |
| `KS003` | Wiersz z `method = 'human'` może zmienić tylko zapis z `method = 'human'` (UPDATE dozwolony, gdy `NEW.method = 'human'` albo `OLD.method <> 'human'`). | wszystkie tabele L3 |
| `KS004` | Zapis z `method = 'rule'` musi wskazać `rule_id` + `rule_version` reguły tej samej firmy w etapie `auto` (wiersz reguły blokowany `FOR SHARE`, więc zawieszenie reguły czeka na zapis automatu). Gdy reguła przestanie być `auto`, automat nie zmieni już nawet swoich wierszy; człowiek może je cofnąć. | wszystkie tabele L3 |
| `KS005` | Podmiot polimorficzny (`subject_type`, `subject_id`) musi istnieć w tej samej firmie. | `l2_suggestion`, `l3_decision_log`, `l3_allocation`, `l3_category_decision` |
| `KS006` | `company_id` istniejącego wiersza nie może się zmienić. | wszystkie tabele L3 |
| `KS007` | Aktywna alokacja może wskazywać tylko obiekt kosztowy `approved`. | `l3_allocation` |

Ograniczenia CHECK uzupełniają triggery: L3 przyjmuje tylko `human` i `rule`, z dwoma wyjątkami - kandydat na obiekt
kosztowy (`code`/`model`, status `candidate`) i kandydat na regułę (`code`, etap `candidate`); obiektu kosztowego nie
zatwierdza, nie łączy ani nie odrzuca automat. Kontrola `python -m kasa check` sprawdza, że wszystkie triggery istnieją
i są włączone oraz że każda tabela L1-L3 ma komplet kolumn pochodzenia.
