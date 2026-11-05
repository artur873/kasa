# Dane testowe (fixtures) - ODM Partners, październik 2026

Wymyślone, ale wewnętrznie spójne dane firmy **ODM Partners sp. z o.o.** (agencja eventowa, NIP 5342650778)
za pełny październik 2026, plus faktury wrześniowe płacone w październiku i październikowe płatne
w listopadzie (należności mają wiek). Z tych plików czytają implementacje `mock` dostawców
(`KASA_BANK_PROVIDER=mock`, `KASA_KSEF_PROVIDER=mock`, `KASA_LLM_PROVIDER=mock`) i testy bramek w `tests/gates/`.

**Jedynym źródłem jest skrypt `fixtures/generate.py`.** Pliki w `fixtures/odm/` są jego wynikiem i są
w repozytorium. Zmiana scenariusza = zmiana skryptu + ponowne wygenerowanie + commit wyniku.
Nie edytuj plików w `fixtures/odm/` ręcznie.

## Uruchomienie

```bash
uv pip install --system reportlab openpyxl   # jeśli brakuje (PDF i XLSX)
python fixtures/generate.py
```

Skrypt:

- kasuje i odtwarza cały katalog `fixtures/odm/`;
- jest deterministyczny: `SEED = 42`, a każda wartość losowa (NIP, IBAN, telefon, `entry_reference`,
  numer KSeF) pochodzi z generatora z kluczem encji (`R("party:acme")`), więc dodanie nowej encji nie
  przesuwa identyfikatorów pozostałych. Dwa uruchomienia dają identyczne bajty (także XLSX i PDF);
- **asertuje spójność** i przerywa z błędem, jeśli scenariusz się rozjedzie: bilans każdego rachunku co do
  grosza (także po ponownym odczycie JSON i camt.053), sumy faktur per stawka, sumy kontrolne NIP/IBAN/KSeF,
  kontrolę MPP (suma bloków `/VAT/` we wpływach = wpływy na rachunek VAT), pokrycie dopasowań (suma
  dopasowań = kwota przelewu, nigdy ponad kwotę faktury), każdy przelew FX ma nazwaną różnicę „kurs”,
  dokładnie jeden wiersz arkusza PM bez banku i jeden BLIK bez wiersza, należności i zobowiązania otwarte,
  marże eventów (przypięte w `EXPECTED_MARGINS`), raport kasowy z różnicą kontrolną 0,00, zgodność JPK z KSeF
  i P_51 z JPK = przelew VAT-7.

PDF-y używają czcionki TTF z polskimi znakami (DejaVu Sans, Liberation Sans albo Arial - pierwsza
znaleziona). Bez żadnej z nich skrypt zadziała, ale PDF-y wyjdą bez ogonków i z innym SHA-256.

## Struktura

```
fixtures/
  generate.py                     # generator (jedyne źródło)
  README.md
  odm/
    SCENARIUSZ.md                 # opis każdej pułapki z entry_reference / numerami faktur (generowany)
    ground_truth.json             # prawda referencyjna do testów bramek (generowana)
    bank/
      enablebanking/
        accounts.json             # 3 rachunki: mBank bieżący, mBank VAT, Millennium bieżący
        <rachunek>/transactions.json          # GET /accounts/{id}/transactions, tylko BOOK
        <rachunek>/transactions_pending.json  # blokady kartowe PDNG (nie wchodzą do salda)
        <rachunek>/balances.json              # GET /accounts/{id}/balances: CLBD, ITAV (+ OPBD)
      camt053/mbank_biezacy_2026-10.xml       # ten sam miesiąc mBanku bieżącego jako camt.053.001.02
    ksef/
      index.json                  # lista z KSeF: numer KSeF, plik, kierunek, data przyjęcia, kwoty, SHA-256
      sales/<numer KSeF>.xml      # faktury sprzedaży FA(3)
      purchase/<numer KSeF>.xml   # faktury zakupu FA(3)
    history/
      JPK_V7M_2026-09.xml         # JPK_V7M za wrzesień (uproszczony)
      biuro_export_2025-10.csv    # eksport z programu biura za 10.2025
    internal/
      arkusz_pm_2026-10.xlsx      # arkusz PM (to samo co CSV)
      arkusz_pm_2026-10.csv
    email_pdfs/
      EM-0x_*.pdf                 # faktury zagraniczne, paragon, rachunek osoby fizycznej, duplikat KSeF
      index.json                  # metadane maili (from, to, date, subject, załącznik + SHA-256)
      expected.json               # oczekiwana ekstrakcja (pozycje sumują się do kwoty)
    reference/
      nbp_rates.json              # FIKCYJNE kursy średnie NBP (EUR, USD), dni robocze 09-10.2026
```

## Formaty i świadome uproszczenia

**Bank (EnableBanking).** Pola transakcji jak w API EB: `entry_reference`, `booking_date`, `value_date`,
`transaction_date`, `transaction_amount {amount, currency}` (kwota zawsze dodatnia, znak w
`credit_debit_indicator` CRDT/DBIT), `creditor`/`debtor` z `*_account.iban` i `*_agent.bic_fi`,
`remittance_information` (lista linii), `bank_transaction_code {description, code, sub_code}` (rodzina
i podrodzina ISO 20022 + opis banku), `merchant_category_code` dla kart, `exchange_rate` dla walut,
`status` BOOK/PDNG. `balance_after_transaction` jest tylko w mBanku (Millennium go nie podaje - celowo).
`balances.json` zawiera dodatkowo saldo otwarcia `OPBD` (rozszerzenie fixture; w prawdziwym EB saldo
otwarcia liczy się z historii albo bierze z camt.053). BLIK i karty nie mają IBAN-u kontrahenta.
Rachunek VAT jest osobnym rachunkiem w EB.

**camt.053.001.02.** GrpHdr, Stmt, Bal OPBD/CLBD, TxsSummry, Ntry z NtryRef (= `entry_reference` z EB,
klucz deduplikacji między kanałami), Amt, CdtDbtInd, Sts, BookgDt, ValDt, BkTxCd, NtryDtls/TxDtls
(AmtDtls z kursem dla walut, RltdPties, RmtInf/Ustrd). Plik przechodzi walidację oficjalnym XSD
camt.053.001.02 (XSD nie jest w repo).

**KSeF FA(3).** Namespace `http://crd.gov.pl/wzor/2025/06/25/13775/`, `KodFormularza kodSystemowy="FA (3)"`,
`WariantFormularza` 3. Wszystkie pliki przechodzą walidację oficjalnym schematem FA(3) v1-0E
(sprawdzone przy tworzeniu; XSD nie jest w repo). Na co uważać w parserze:

- `P_6` występuje tylko, gdy data sprzedaży różni się od `P_1`; zamiast niej bywa `OkresFa` (`P_6_Od`/`P_6_Do`);
- `TerminPlatnosci` jest opcjonalny (FV 9/10/2026 go nie ma);
- `NrRB` to 26 cyfr bez `PL` (dla faktury w EUR - pełny IBAN + SWIFT);
- korekta (`KOR`): `DaneFaKorygowanej` z numerem KSeF faktury pierwotnej, wiersze przed (`StanPrzed=1`) i po
  korekcie mają ten sam `NrWierszaFa`, kwoty `P_13_x`/`P_14_x`/`P_15` są różnicą (ujemne);
- zaliczkowa (`ZAL`): pozycje są w `Zamowienie/ZamowienieWiersz` (nie w `FaWiersz`), `P_6` = data wpłaty,
  `Platnosc/Zaplacono=1`;
- rozliczeniowa (`ROZ`): `FaWiersz` = całe zamówienie, `P_13/P_14/P_15` = do zapłaty po zaliczce,
  `FakturaZaliczkowa/NrKSeFFaZaliczkowej`;
- faktura w EUR: `P_13_9` (stawka `np II`), `P_18=1`, kurs NBP w `FaWiersz/KursWaluty`.

Numer KSeF: `NIP wystawcy-RRRRMMDD (data przyjęcia)-12 znaków hex-2 znaki hex`, 35 znaków; ostatnia para to
suma kontrolna CRC-8 (wielomian 0x07) liczona w skrypcie. Mock KSeF to lista faktur przyjętych od 14.09.2026
do 31.10.2026 - kompletna w tym oknie.

**JPK_V7M (uproszczony).** Naglowek, Podmiot1, skrócona Deklaracja (P_19, P_20, P_38, P_42, P_43, P_48, P_51),
SprzedazWiersz (K_19/K_20), ZakupWiersz (K_42/K_43), Ctrl. Namespace JPK_V7M(2); plik NIE przejdzie pełnego XSD
(brak części obowiązkowych pozycji deklaracji). Faktury z 14-30.09 są też w KSeF (duplikaty), wcześniejsze
tylko w JPK (rozliczone przed październikiem - patrz `ground_truth.json -> history_only_documents`).

**Eksport biura (`biuro_export_2025-10.csv`).** Kolumny `numer;data;kontrahent;NIP;netto;VAT;brutto;typ`,
kodowanie **Windows-1250**, separator `;`, przecinek dziesiętny, daty `DD.MM.RRRR`, CRLF.

**Arkusz PM.** Kolumny `Osoba, Data, Event, Kwota, Forma, Typ rozliczenia, Uwagi`. Osoba w formacie
„Nazwisko Imię”, event w nazwie roboczej PM („Nordis Gdańsk”), data = dzień pracy (przelew jest później).
CSV: UTF-8 z BOM, separator `;`, przecinek dziesiętny. W tym miesiącu nie ma wierszy z formą „gotówka”.

**PDF-y.** Prawdziwe PDF-y (reportlab) z warstwą tekstową; każdy ma stopkę „DOKUMENT TESTOWY - dane
fikcyjne”. Hotel (EM-06) to ta sama faktura co w KSeF, ale bez numeru KSeF na wydruku.

**Dane osobowe i firmy.** Wszystkie osoby, firmy, adresy, NIP-y, numery VAT UE, IBAN-y (mod-97), telefony
(500-xxx-xxx) i numery KSeF są wymyślone. Nazwy marek (Orange, Google, Anthropic, Notion, mBank,
Millennium, Revolut) występują tylko jako etykiety; ich NIP-y, numery VAT i adresy są fikcyjne. Rachunki
ZUS i mikrorachunek podatkowy mają uproszczoną strukturę (z poprawną sumą kontrolną). Kursy NBP są fikcyjne.

## `ground_truth.json` - jak czytać

| Sekcja | Zawartość |
| --- | --- |
| `accounts` | saldo otwarcia, wpływy, wypływy, saldo zamknięcia, saldo dostępne per rachunek |
| `transactions` | każda transakcja BOOK po `entry_reference`: `expected_status` (matched / no_document / own_transfer / to_clarify), `expected_route`, `matched_documents` z kwotami, `difference` (nazwa i kwota), grupa MPP, para transferu własnego, kandydaci przy remisie, słowa wrażliwe |
| `pending_transactions` | blokady PDNG (bez statusu końcowego) |
| `invoices` | każda faktura KSeF: numer KSeF, kwoty, `expected_event` (id eventu / `UNASSIGNED` / `queue`), sygnał alokacji, sugestia, `final_allocation`, rozliczenie, kwota otwarta, ujęcie przychodu po miesiącach |
| `email_documents`, `pm_sheet_rows` | dokumenty z maili i wiersze arkusza z oczekiwanym dopasowaniem i eventem |
| `events` | 5 eventów (nazwa, klient, daty, miasto) z przychodem, kosztem, marżą memoriałową netto i cash contribution, z listą pozycji |
| `receivables_open`, `payables_open` | otwarte na 31.10.2026, z wiekiem i kubełkiem |
| `metrics_expected` | przychód memoriałowy i kasowy października, sumy należności/zobowiązań, raport kasowy (różnica kontrolna 0,00) |
| `dedup` | KSeF↔JPK, KSeF↔PDF, EB↔camt.053, „ten sam numer, inny NIP”, jeden koszt - wiele dowodów |
| `checks` | bilanse, MPP, kompletność KSeF, JPK P_51, arkusz PM |
| `pii` | nazwiska, telefony (w kilku formatach), IBAN-y osób - do testu bramki pseudonimizacji |

`expected_route`: `auto` - automat może zamknąć dopasowanie sam (twardy dowód); `queue` - musi trafić do
kolejki (zamknięcie automatem to błąd, np. remis, BLIK z arkuszem, Revolut, duplikat, kurs karty);
`any` - bez wymagań. `final_allocation` to stan po decyzjach człowieka - z niego liczona jest marża.

## Jak zmienić scenariusz

W `generate.py`: strony (`company()`, `person()`), faktury (`inv(...)`), wiersze arkusza (`pm(...)`),
transakcje (`tx(...)`, `blik(...)`, `card(...)`), dokumenty z maili (`email_doc(...)`), opisy pułapek
(`SCENARIOS`, `DOC_TRAPS`). Oczekiwane wyniki, które skrypt przypina asercjami (marże, zbiory otwartych
faktur), są w `check_documents()` i `EXPECTED_MARGINS` - po świadomej zmianie scenariusza zaktualizuj je.
Potem `python fixtures/generate.py`, przejrzyj `SCENARIUSZ.md` i zacommituj cały katalog `fixtures/`.
